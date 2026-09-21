"""A plain close may only close. Two of them may not open the other side.

`order_is_risk_reducing` compares one order against the cached position, so two
plain closes for the full size both read as reducing: each is "within" the
position that is still there when it is judged. Both skip the freeze check and
the reservation, and the second one opens a position in the opposite direction.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.fallback_breaker import (  # noqa: E402
    FallbackBreaker,
    FallbackBreakerConfig,
)
from custos.core.order_reservation_boundary import RunnerReservationBoundary  # noqa: E402
from custos.engines.nautilus.runner_safety import (  # noqa: E402
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)

DEPLOYMENT_INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000001")
POLICY_ID = "policy-1"
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


class _Store:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reserve_order_notional_sync(self, **_kwargs):
        self.calls.append("reserve")

    def release_order_reservation_sync(self, **_kwargs):
        self.calls.append("release")

    def load_order_reservation_sync(self, *_args, **_kwargs):
        return SimpleNamespace(reserved_notional="0")

    def replace_order_reservation_sync(self, **_kwargs):
        self.calls.append("replace")

    def record_order_fill_sync(self, **_kwargs):
        self.calls.append("fill")

    def record_position_reduction_sync(self, **_kwargs):
        self.calls.append("reduce")

    def record_position_reduction_fifo_sync(self, **_kwargs):
        self.calls.append("reduce_fifo")


class OrderCanceled:
    """The boundary dispatches on ``type(event).__name__``, so the class name is
    the contract -- a leading underscore here would simply not be recognised."""

    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id
        self.event_id = f"evt-{client_order_id}"


def _frozen_breaker() -> FallbackBreaker:
    breaker = FallbackBreaker(
        FallbackBreakerConfig(max_notional=Decimal("10000"), max_drawdown_pct=Decimal("10"))
    )
    breaker.fail_closed("review_freeze")
    assert breaker.frozen
    return breaker


def _long_position(quantity: str = "1"):
    return SimpleNamespace(
        is_closed=False,
        is_long=True,
        is_short=False,
        quantity=Decimal(quantity),
        strategy_id="STRATEGY-001",
    )


def _plain_close(client_order_id: str, quantity: str = "1"):
    return SimpleNamespace(
        client_order_id=client_order_id,
        strategy_id="STRATEGY-001",
        instrument_id=INSTRUMENT,
        side="OrderSide.SELL",
        quantity=Decimal(quantity),
        is_quote_quantity=False,
        is_reduce_only=False,
        emulation_trigger=None,
        exec_algorithm_id=None,
    )


def _wired(position):
    store = _Store()
    cache = SimpleNamespace(positions_open=lambda **_kwargs: [position])
    boundary = RunnerReservationBoundary(
        store=store,
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        policy_id=POLICY_ID,
        fallback_breaker=_frozen_breaker(),
        trading_mode="testnet",
        semantics=NautilusCachedOrderSemantics(cache),
    )
    gate = RunnerSafetyOrderGate(
        boundary=boundary,
        client_order_id_len_limit=None,
        on_refusal=None,
    )
    return store, gate


class TestASecondPlainCloseIsNotStillReducing:
    def test_the_second_full_close_is_refused_while_frozen(self):
        """The position can only be closed once; the second one opens a short."""
        store, gate = _wired(_long_position("1"))
        sent: list[str] = []

        first = gate.submit_order(
            lambda order, *a, **k: sent.append(order.client_order_id), _plain_close("close-1")
        )
        second = gate.submit_order(
            lambda order, *a, **k: sent.append(order.client_order_id), _plain_close("close-2")
        )

        assert first is True, "closing the open long is legitimate"
        assert second is False, "there is nothing left to close; this would open a short"
        assert sent == ["close-1"]

    def test_two_half_closes_both_fit(self):
        """Reducing in parts is fine as long as the parts fit the position."""
        store, gate = _wired(_long_position("1"))
        sent: list[str] = []
        submit = lambda order, *a, **k: sent.append(order.client_order_id)  # noqa: E731

        first = gate.submit_order(submit, _plain_close("half-1", "0.5"))
        second = gate.submit_order(submit, _plain_close("half-2", "0.5"))

        assert (first, second) == (True, True)
        assert sent == ["half-1", "half-2"]

    def test_a_third_half_does_not_fit(self):
        store, gate = _wired(_long_position("1"))
        sent: list[str] = []
        submit = lambda order, *a, **k: sent.append(order.client_order_id)  # noqa: E731
        gate.submit_order(submit, _plain_close("half-1", "0.5"))
        gate.submit_order(submit, _plain_close("half-2", "0.5"))

        third = gate.submit_order(submit, _plain_close("half-3", "0.5"))

        assert third is False
        assert sent == ["half-1", "half-2"]

    def test_a_terminated_close_gives_its_room_back(self):
        """A close that will never fill must not block the next attempt."""
        store, gate = _wired(_long_position("1"))
        sent: list[str] = []
        submit = lambda order, *a, **k: sent.append(order.client_order_id)  # noqa: E731
        gate.submit_order(submit, _plain_close("close-1"))
        boundary = gate._boundary

        boundary.on_order_event(OrderCanceled("close-1"))
        retry = gate.submit_order(submit, _plain_close("close-2"))

        assert retry is True, "the cancelled close released the room it held"
        assert sent == ["close-1", "close-2"]


class TestTheNativeCloseIsBehindTheGate:
    """close_position is compiled: its internal submit does not reach the hook.

    A reduce-only close cannot open anything, so it passes. A plain one has to
    go through submit_order, where the room left on the position is checked.
    """

    @staticmethod
    def _gate_only():
        store = _Store()
        cache = SimpleNamespace(positions_open=lambda **_kwargs: [_long_position("1")])
        boundary = RunnerReservationBoundary(
            store=store,
            deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
            policy_id=POLICY_ID,
            fallback_breaker=_frozen_breaker(),
            trading_mode="testnet",
            semantics=NautilusCachedOrderSemantics(cache),
        )
        return RunnerSafetyOrderGate(
            boundary=boundary, client_order_id_len_limit=None, on_refusal=None
        )

    def test_a_reduce_only_native_close_passes(self):
        gate = self._gate_only()
        calls: list[str] = []

        gate.close_position(lambda *a, **k: calls.append("closed"), object(), reduce_only=True)

        assert calls == ["closed"]

    def test_a_plain_native_close_is_refused(self):
        gate = self._gate_only()
        calls: list[str] = []

        result = gate.close_position(
            lambda *a, **k: calls.append("closed"), object(), reduce_only=False
        )

        assert result is None
        assert calls == [], "a plain close must go through submit_order to be judged"

    def test_the_default_is_reduce_only(self):
        """nautilus defaults reduce_only to True; omitting it must not be refused."""
        gate = self._gate_only()
        calls: list[str] = []

        gate.close_position(lambda *a, **k: calls.append("closed"), object())

        assert calls == ["closed"]

    def test_the_install_list_names_both_native_closes(self):
        import inspect

        from custos.engines.nautilus.runner_safety import (
            CLOSE_ALL_POSITIONS,
            CLOSE_POSITION,
            install_order_gate,
        )

        source = inspect.getsource(install_order_gate)
        assert CLOSE_POSITION in source and CLOSE_ALL_POSITIONS in source
