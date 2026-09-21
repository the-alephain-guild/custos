"""An amendment must be judged by the order it would leave, not the one it found.

``before_modify_order`` read the risk direction off the cached order -- the one as
it is *now* -- and returned immediately when that read as risk-reducing. The
requested quantity never entered the judgement. So a legitimate plain close of a
long 1 could be amended to sell 2 while the breaker was frozen: no freeze check,
no reservation, and the native matching engine opens a short 1.

The two books the boundary keeps have to move together. ``_unsettled_reductions``
records how much of the open position accepted closes already claim (fix 16), and
``order_reservation`` records the notional a risk-increasing order holds. An
amendment can move an order from one book to the other in either direction.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.testkit.providers import TestInstrumentProvider  # noqa: E402

from custos.core.fallback_breaker import (  # noqa: E402
    FallbackBreaker,
    FallbackBreakerConfig,
)
from custos.core.order_reservation_boundary import RunnerReservationBoundary  # noqa: E402
from custos.engines.nautilus.runner_safety import (  # noqa: E402
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)

DEPLOYMENT_INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
POLICY_ID = UUID("20000000-0000-4000-8000-000000000003")
INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()


class _Store:
    """Records what the durable reservation book was asked to do, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reserved: dict[str, Decimal] = {}

    def reserve_order_notional_sync(self, *, client_order_id, requested_notional, **_kwargs):
        self.calls.append(("reserve", client_order_id, Decimal(str(requested_notional))))
        self.reserved[client_order_id] = Decimal(str(requested_notional))

    def replace_order_reservation_sync(self, *, client_order_id, new_reserved_notional, **_kwargs):
        if client_order_id not in self.reserved:
            raise RuntimeError("order reservation is absent")
        self.calls.append(("replace", client_order_id, Decimal(str(new_reserved_notional))))
        self.reserved[client_order_id] = Decimal(str(new_reserved_notional))

    def release_order_reservation_sync(self, *, client_order_id, reason, **_kwargs):
        self.calls.append(("release", client_order_id, reason))
        self.reserved.pop(client_order_id, None)

    def load_order_reservation_sync(self, _instance, client_order_id):
        return SimpleNamespace(reserved_notional=str(self.reserved[client_order_id]))

    def has_order_reservation_sync(self, _instance, client_order_id) -> bool:
        return client_order_id in self.reserved

    def record_order_fill_sync(self, **_kwargs):
        self.calls.append(("fill",))

    def record_position_reduction_sync(self, **_kwargs):
        self.calls.append(("reduce",))

    def record_position_reduction_fifo_sync(self, **_kwargs):
        self.calls.append(("reduce_fifo",))


class OrderModifyRejected:
    """The boundary dispatches on ``type(event).__name__``."""

    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id
        self.event_id = f"evt-reject-{client_order_id}"


def _long_position(quantity: str = "1"):
    return SimpleNamespace(
        is_closed=False,
        is_long=True,
        is_short=False,
        quantity=Decimal(quantity),
        strategy_id="STRATEGY-001",
    )


def _sell(client_order_id: str, quantity: str, price: str = "100", *, reduce_only: bool = False):
    return SimpleNamespace(
        client_order_id=client_order_id,
        strategy_id="STRATEGY-001",
        instrument_id=INSTRUMENT.id,
        side="OrderSide.SELL",
        quantity=Decimal(quantity),
        price=Decimal(price),
        is_quote_quantity=False,
        is_reduce_only=reduce_only,
        emulation_trigger=None,
        exec_algorithm_id=None,
    )


class _Cache:
    """The canonical view: what is resting, and what position is open."""

    def __init__(self, position) -> None:
        self.orders: dict[str, SimpleNamespace] = {}
        self.position = position

    def order(self, client_order_id):
        return self.orders.get(str(client_order_id))

    def positions_open(self, **_kwargs):
        return [] if self.position is None else [self.position]

    def instrument(self, _instrument_id):
        return INSTRUMENT


def _wired(position=None, *, frozen: bool = True):
    cache = _Cache(_long_position() if position is None else position)
    store = _Store()
    breaker = FallbackBreaker(
        FallbackBreakerConfig(max_notional=Decimal("10000"), max_drawdown_pct=Decimal("10"))
    )
    if frozen:
        breaker.fail_closed("review_freeze")
    boundary = RunnerReservationBoundary(
        store=store,
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        policy_id=POLICY_ID,
        fallback_breaker=breaker,
        semantics=NautilusCachedOrderSemantics(cache),
        trading_mode="testnet",
    )
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    return cache, store, boundary, gate


def _submit(gate, cache, order) -> bool:
    def submit(sent, *_args, **_kwargs):
        cache.orders[str(sent.client_order_id)] = sent

    return gate.submit_order(submit, order)


def _modify(gate, cache, client_order_id, *, quantity=None, price=None):
    applied: list[tuple] = []

    def modify(order_id, new_quantity, new_price, _trigger, *_args, **_kwargs):
        applied.append((str(order_id), new_quantity, new_price))
        resting = cache.orders[str(order_id)]
        if new_quantity is not None:
            resting.quantity = new_quantity
        if new_price is not None:
            resting.price = new_price

    gate.modify_order(modify, client_order_id, quantity, price, None)
    return applied


class TestAnAmendmentCannotTurnACloseIntoAReversal:
    """FR-1: the defect, and the amount of position it would have opened."""

    def test_enlarging_a_plain_close_past_the_position_is_refused(self):
        cache, store, _boundary, gate = _wired()
        assert _submit(gate, cache, _sell("close-1", "1")) is True

        applied = _modify(gate, cache, "close-1", quantity=Decimal("2"), price=Decimal("100"))

        assert applied == [], "selling 2 against a long 1 opens a short; it is not a close"
        assert cache.orders["close-1"].quantity == Decimal("1")
        assert store.calls == [], "a refused amendment must leave both books untouched"

    def test_enlarging_within_the_position_is_still_a_close(self):
        """The control: growing a half close to a full one closes and nothing more."""
        cache, store, _boundary, gate = _wired(_long_position("2"))
        assert _submit(gate, cache, _sell("close-1", "1")) is True

        applied = _modify(gate, cache, "close-1", quantity=Decimal("2"))

        assert len(applied) == 1
        assert cache.orders["close-1"].quantity == Decimal("2")
        assert store.calls == [], "a close holds no reservation before or after"

    def test_a_reduce_only_amendment_is_never_refused(self):
        """A reduce-only order cannot open anything, whatever it is amended to."""
        cache, _store, _boundary, gate = _wired()
        assert _submit(gate, cache, _sell("protect-1", "1", reduce_only=True)) is True

        applied = _modify(gate, cache, "protect-1", quantity=Decimal("5"))

        assert len(applied) == 1


class TestTheTwoBooksMoveTogether:
    """FR-1 acceptance: the claimed closable quantity and the notional reservation."""

    def test_shrinking_a_close_hands_back_the_room_it_held(self):
        cache, _store, _boundary, gate = _wired()
        assert _submit(gate, cache, _sell("close-1", "1")) is True
        assert _submit(gate, cache, _sell("close-2", "1")) is False, "nothing left to close"

        _modify(gate, cache, "close-1", quantity=Decimal("0.4"))

        assert _submit(gate, cache, _sell("close-2", "0.6")) is True, (
            "the amendment gave back 0.6 of the position's closable quantity"
        )

    def test_another_exit_order_narrows_what_an_amendment_may_claim(self):
        cache, _store, _boundary, gate = _wired()
        assert _submit(gate, cache, _sell("close-1", "0.5")) is True
        assert _submit(gate, cache, _sell("close-2", "0.5")) is True

        applied = _modify(gate, cache, "close-1", quantity=Decimal("0.6"))

        assert applied == [], "0.5 is already claimed by the other exit; only 0.5 is left"

    def test_an_amendment_into_risk_reserves_what_it_would_add(self):
        """Unfrozen, the same amendment is allowed -- and must be paid for."""
        cache, store, _boundary, gate = _wired(frozen=False)
        assert _submit(gate, cache, _sell("close-1", "1")) is True
        assert store.calls == []

        applied = _modify(gate, cache, "close-1", quantity=Decimal("2"), price=Decimal("100"))

        assert len(applied) == 1
        assert [call[0] for call in store.calls] == ["reserve"]
        assert store.reserved["close-1"] == Decimal("200")

    def test_an_order_that_stops_adding_risk_releases_its_reservation(self):
        cache, store, _boundary, gate = _wired(frozen=False)
        assert _submit(gate, cache, _sell("entry-1", "3", "100")) is True
        assert store.reserved["entry-1"] == Decimal("300")

        _modify(gate, cache, "entry-1", quantity=Decimal("1"))

        assert [call[0] for call in store.calls] == ["reserve", "release"]
        assert "entry-1" not in store.reserved
        # The amended order now claims the whole position's closable quantity, so
        # the next sell of 1 is not a close any more. Unfrozen it is still allowed
        # -- as a reserved risk-increasing order, which is what proves the claim
        # was recorded: a plain close pays nothing.
        assert _submit(gate, cache, _sell("close-2", "1")) is True
        assert "close-2" in store.reserved


class TestARejectedAmendmentPutsBothBooksBack:
    """FR-1 acceptance: rollback on refusal or venue rejection."""

    def test_a_rejected_enlargement_releases_the_reservation_it_took(self):
        cache, store, boundary, gate = _wired(frozen=False)
        assert _submit(gate, cache, _sell("close-1", "1")) is True
        _modify(gate, cache, "close-1", quantity=Decimal("2"), price=Decimal("100"))
        cache.orders["close-1"].quantity = Decimal("1")

        boundary.on_order_event(OrderModifyRejected("close-1"))

        assert [call[0] for call in store.calls] == ["reserve", "release"]
        # The order is a plain close again and claims the position back, so the next
        # sell of 1 has nothing left to close and has to pay for itself.
        assert _submit(gate, cache, _sell("close-2", "1")) is True
        assert "close-2" in store.reserved

    def test_a_rejected_shrink_restores_the_reservation_it_released(self):
        cache, store, boundary, gate = _wired(frozen=False)
        assert _submit(gate, cache, _sell("entry-1", "3", "100")) is True
        _modify(gate, cache, "entry-1", quantity=Decimal("1"))
        cache.orders["entry-1"].quantity = Decimal("3")

        boundary.on_order_event(OrderModifyRejected("entry-1"))

        assert [call[0] for call in store.calls] == ["reserve", "release", "reserve"]
        assert store.reserved["entry-1"] == Decimal("300")

    def test_a_rejected_shrink_of_a_close_restores_the_room_it_gave_back(self):
        cache, _store, boundary, gate = _wired()
        assert _submit(gate, cache, _sell("close-1", "1")) is True
        _modify(gate, cache, "close-1", quantity=Decimal("0.4"))
        cache.orders["close-1"].quantity = Decimal("1")

        boundary.on_order_event(OrderModifyRejected("close-1"))

        assert _submit(gate, cache, _sell("close-2", "0.6")) is False, (
            "the rejection means the close still claims the whole position"
        )


class TestAnUnknownOrderIsNotJudged:
    def test_amending_an_order_the_cache_does_not_hold_is_refused(self):
        cache, store, _boundary, gate = _wired()

        applied = _modify(gate, cache, "never-submitted", quantity=Decimal("1"))

        assert applied == []
        assert store.calls == []
