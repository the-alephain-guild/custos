from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig  # noqa: E402
from custos.core.order_reservation_boundary import RunnerReservationBoundary  # noqa: E402
from custos.core.runner_fact import RunnerStateAuthorityError  # noqa: E402
from custos.engines.nautilus.runner_safety import (  # noqa: E402
    NautilusCachedOrderSemantics,
    OrderRefusal,
    RunnerSafetyOrderGate,
    install_order_gate,
)
from custos.engines.nautilus.strategy_hooks import StrategyHookUnsupported  # noqa: E402
from custos.engines.nautilus.venue_binance import (  # noqa: E402
    BINANCE_CLIENT_ORDER_ID_LEN_LIMIT,
)

DEPLOYMENT_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
POLICY_ID = UUID("22222222-2222-4222-8222-222222222222")


class _Store:
    def __init__(self, log: list[tuple]) -> None:
        self.log = log
        self.reject_reservation = False
        self.reservations: dict[str, SimpleNamespace] = {}

    def reserve_order_notional(self, **kwargs):
        self.log.append(("reserve", kwargs))
        if self.reject_reservation:
            raise RunnerStateAuthorityError("runner cap exceeded")
        snapshot = SimpleNamespace(
            client_order_id=kwargs["client_order_id"],
            reserved_notional=kwargs["requested_notional"],
        )
        self.reservations[kwargs["client_order_id"]] = snapshot
        return snapshot

    def load_order_reservation(self, deployment_instance_id, client_order_id):
        del deployment_instance_id
        return self.reservations[client_order_id]

    def has_order_reservation(self, deployment_instance_id, client_order_id):
        del deployment_instance_id
        return client_order_id in self.reservations

    def replace_order_reservation(self, **kwargs):
        self.log.append(("replace", kwargs))
        snapshot = SimpleNamespace(
            client_order_id=kwargs["client_order_id"],
            reserved_notional=kwargs["new_reserved_notional"],
        )
        self.reservations[kwargs["client_order_id"]] = snapshot
        return snapshot

    def release_order_reservation(self, **kwargs):
        self.log.append(("release", kwargs))
        return self.reservations.get(kwargs["client_order_id"])

    def record_order_fill(self, **kwargs):
        self.log.append(("fill", kwargs))
        return self.reservations.get(kwargs["client_order_id"])

    def record_position_reduction(self, **kwargs):
        self.log.append(("reduce", kwargs))
        return self.reservations.get(kwargs["client_order_id"])

    def record_position_reduction_fifo(self, **kwargs):
        self.log.append(("reduce_fifo", kwargs))
        return next(iter(self.reservations.values()), None)

    reserve_order_notional_sync = reserve_order_notional
    load_order_reservation_sync = load_order_reservation
    has_order_reservation_sync = has_order_reservation
    replace_order_reservation_sync = replace_order_reservation
    release_order_reservation_sync = release_order_reservation
    record_order_fill_sync = record_order_fill
    record_position_reduction_sync = record_position_reduction
    record_position_reduction_fifo_sync = record_position_reduction_fifo


class _Semantics:
    def order_notional(self, order) -> Decimal:
        return Decimal(str(order.notional))

    def modified_order_notional(self, intent) -> Decimal:
        # The real one reads the cached order and the requested quantity; this double
        # only needs the requested quantity, which is what the gate passes through.
        return Decimal(str(intent.quantity))

    def fill_notional(self, event) -> Decimal:
        return Decimal(str(event.notional))

    def fill_quantity(self, event) -> Decimal:
        return Decimal(str(event.quantity))

    def order_instrument_id(self, order) -> str:
        return str(getattr(order, "instrument_id", "INSTRUMENT"))

    def order_quantity(self, order) -> Decimal:
        return Decimal(str(getattr(order, "quantity", 0)))

    def order_is_risk_reducing(self, order, already_reducing: Decimal = Decimal(0)) -> bool:
        # This double declares reduce-only directly, so there is no position to
        # measure the claimed room against; the real semantics does that.
        del already_reducing
        return bool(order.reduce_only)

    def event_is_risk_reducing(self, event) -> bool:
        return bool(event.reduce_only)

    def event_exposure_source_order_id(self, event) -> str | None:
        return getattr(event, "exposure_source_order_id", None)

    def event_position_id(self, event) -> str | None:
        return getattr(event, "position_id", None)

    def event_instrument_id(self, event) -> str | None:
        return getattr(event, "instrument_id", None)

    def event_side(self, event) -> str | None:
        return getattr(event, "order_side", None)


class _Downstream:
    """The nautilus methods the gate wraps: reached only if the gate allows it.

    These stand where the execution client used to. In 2.0 the gate sits on the
    strategy, so what it lets through is a call to the strategy's own submit, and
    what it refuses simply never happens -- there is no rejection event to record
    because the order was never submitted (see the module docstring in
    ``runner_safety``).
    """

    def __init__(self, log: list[tuple]) -> None:
        self.log = log

    def submit_order(self, order, *_args, **_kwargs) -> None:
        self.log.append(("submit", order.client_order_id))

    def submit_order_list(self, order_list, *_args, **_kwargs) -> None:
        self.log.append(("submit_list", tuple(o.client_order_id for o in order_list.orders)))

    def modify_order(self, order, *_args, **_kwargs) -> None:
        self.log.append(("modify_upstream", order.client_order_id))


def _gate(
    boundary,
    refusals: list[OrderRefusal] | None = None,
    *,
    client_order_id_len_limit: int | None = BINANCE_CLIENT_ORDER_ID_LEN_LIMIT,
) -> RunnerSafetyOrderGate:
    # The Binance cap by default because that is the venue these cases are about; the
    # parameter is here so a case can ask what a venue with no measured cap does.
    return RunnerSafetyOrderGate(
        boundary=boundary,
        client_order_id_len_limit=client_order_id_len_limit,
        on_refusal=(refusals if refusals is None else refusals.append),
    )


class _GatedStrategy:
    """A strategy shaped enough for the gate to be installed on it."""

    config = SimpleNamespace(
        manage_contingent_orders=False,
        manage_gtd_expiry=False,
        manage_stop=False,
    )

    def __init__(self) -> None:
        self.submitted: list = []

    def submit_order(self, order, *_args, **_kwargs) -> None:
        self.submitted.append(order)

    def submit_order_list(self, order_list, *_args, **_kwargs) -> None:
        self.submitted.extend(order_list.orders)

    def modify_order(self, order, *_args, **_kwargs) -> None:
        self.submitted.append(order)

    def market_exit(self, *_args, **_kwargs) -> None:
        self.submitted.append("market_exit")

    def cancel_order(self, order, *_args, **_kwargs) -> None: ...

    def cancel_all_orders(self, instrument_id, *_args, **_kwargs) -> None: ...

    def close_position(self, position, *_args, **_kwargs) -> None: ...

    def close_all_positions(self, *_args, **_kwargs) -> None: ...


def _order(
    client_order_id: str,
    *,
    notional: str = "25",
    reduce_only: bool = False,
    emulation_trigger=None,
    exec_algorithm_id=None,
):
    return SimpleNamespace(
        client_order_id=client_order_id,
        strategy_id="STRATEGY-001",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="OrderSide.BUY",
        notional=notional,
        reduce_only=reduce_only,
        emulation_trigger=emulation_trigger,
        exec_algorithm_id=exec_algorithm_id,
    )


def _breaker() -> FallbackBreaker:
    return FallbackBreaker(
        FallbackBreakerConfig(
            max_notional=Decimal("1000"),
            max_drawdown_pct=Decimal("10"),
        )
    )


def test_market_order_uses_the_subscribed_mark_when_mid_price_is_unavailable() -> None:
    class Instrument:
        @staticmethod
        def notional_value(quantity, price):
            return Decimal(str(quantity)) * Decimal(str(price))

    class Cache:
        @staticmethod
        def instrument(_instrument_id):
            return Instrument()

        @staticmethod
        def mark_price(_instrument_id):
            return SimpleNamespace(value=Decimal("63706.50"))

        @staticmethod
        def price(_instrument_id, _price_type):
            return None

    semantics = NautilusCachedOrderSemantics(Cache())
    order = SimpleNamespace(
        instrument_id="BTCUSDT-PERP.BINANCE",
        quantity="0.0070",
        price=None,
        trigger_price=None,
        is_quote_quantity=False,
    )

    assert semantics.order_notional(order) == Decimal("445.945500")


def test_plain_opposite_order_matching_one_open_position_is_risk_reducing() -> None:
    position = SimpleNamespace(
        is_closed=False,
        is_long=False,
        is_short=True,
        quantity=Decimal("1"),
        strategy_id="STRATEGY-001",
    )

    class Cache:
        @staticmethod
        def positions_open(*, instrument_id):
            assert instrument_id == "BTCUSDT-PERP.BINANCE"
            return [position]

    semantics = NautilusCachedOrderSemantics(Cache())
    close = SimpleNamespace(
        is_reduce_only=False,
        is_quote_quantity=False,
        instrument_id="BTCUSDT-PERP.BINANCE",
        strategy_id="STRATEGY-001",
        side="OrderSide.BUY",
        quantity=Decimal("1"),
    )

    assert semantics.order_is_risk_reducing(close) is True
    close.quantity = Decimal("1.001")
    assert semantics.order_is_risk_reducing(close) is False
    close.quantity = Decimal("1")
    close.side = "OrderSide.SELL"
    assert semantics.order_is_risk_reducing(close) is False


def test_verified_plain_close_skips_new_risk_reservation_and_reports_dispatch() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reject_reservation = True
    position = SimpleNamespace(
        is_closed=False,
        is_long=False,
        is_short=True,
        quantity=Decimal("1"),
        strategy_id="STRATEGY-001",
    )
    cache = SimpleNamespace(positions_open=lambda **_kwargs: [position])
    boundary = RunnerReservationBoundary(
        store=store,
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        policy_id=POLICY_ID,
        fallback_breaker=_breaker(),
        semantics=NautilusCachedOrderSemantics(cache),
    )
    gate = _gate(boundary)
    order = SimpleNamespace(
        client_order_id="plain-close",
        strategy_id="STRATEGY-001",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="OrderSide.BUY",
        quantity=Decimal("1"),
        is_quote_quantity=False,
        is_reduce_only=False,
        emulation_trigger=None,
        exec_algorithm_id=None,
    )

    dispatched = gate.submit_order(_Downstream(log).submit_order, order)

    assert dispatched is True
    assert [entry[0] for entry in log] == ["submit"]


def _boundary(
    store: _Store,
    *,
    fallback_breaker: FallbackBreaker | None = None,
) -> RunnerReservationBoundary:
    return RunnerReservationBoundary(
        store=store,
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        policy_id=POLICY_ID,
        fallback_breaker=fallback_breaker or _breaker(),
        semantics=_Semantics(),
    )


def test_direct_submit_reserves_before_the_order_leaves() -> None:
    log: list[tuple] = []
    downstream = _Downstream(log)
    gate = _gate(_boundary(_Store(log)))

    gate.submit_order(downstream.submit_order, _order("order-1"))

    assert [entry[0] for entry in log] == ["reserve", "submit"]
    assert log[0][1]["deployment_instance_id"] == DEPLOYMENT_INSTANCE_ID
    assert log[0][1]["policy_id"] == POLICY_ID
    assert log[0][1]["requested_notional"] == Decimal("25")


def test_a_capped_order_is_not_submitted_and_the_refusal_is_reported() -> None:
    """Nautilus produces no event for this, so the report is the only record."""
    log: list[tuple] = []
    store = _Store(log)
    store.reject_reservation = True
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(store), refusals)

    dispatched = gate.submit_order(_Downstream(log).submit_order, _order("order-denied"))

    assert dispatched is False
    assert [entry[0] for entry in log] == ["reserve"], "the order reached the venue"
    assert refusals == [
        OrderRefusal(
            client_order_id="order-denied",
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="buy",
            reason_code="custos_runner_notional_policy_rejected",
        )
    ]


def test_unexpected_safety_failure_is_not_mislabeled_as_a_notional_rejection() -> None:
    class BrokenBoundary:
        @staticmethod
        def before_submit_order(_command):
            raise AttributeError("simulated boundary ABI failure")

    refusals: list[OrderRefusal] = []
    gate = _gate(BrokenBoundary(), refusals)

    gate.submit_order(_Downstream([]).submit_order, _order("safety-unavailable"))

    assert refusals[0].reason_code == "custos_runner_safety_boundary_unavailable"


def test_a_risk_reducing_order_is_never_blocked() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reject_reservation = True
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(store), refusals)

    gate.submit_order(
        _Downstream(log).submit_order,
        _order("reduce-1", notional="500", reduce_only=True),
    )

    assert [entry[0] for entry in log] == ["submit"]
    assert refusals == []


def test_cancelling_is_not_something_the_gate_can_refuse() -> None:
    """Cancels reduce exposure, so the gate does not sit on them at all.

    1.x passed them straight through a client it had wrapped. Here they are simply
    not wrapped, which says the same thing in a way that cannot be got wrong: there
    is no code path on which a cancel could be refused.

    ``close_position`` used to be on this list. It is wrapped now: it is compiled,
    so the order it builds reaches the venue without passing the submit_order
    hook, and a plain close through it could open the opposite side with nothing
    judging it. Being wrapped does not make it refusable as a cancel is not -- a
    reduce-only close still passes untouched; only the plain form is sent back to
    submit_order to be measured.
    """
    strategy = _GatedStrategy()
    install_order_gate(strategy, _gate(_boundary(_Store([]))))

    for untouched in ("cancel_order", "cancel_all_orders"):
        assert untouched not in vars(strategy), (
            f"{untouched} was wrapped, so a cancel could be refused"
        )


def test_frozen_breaker_refuses_risk_increasing_but_not_reduce_only() -> None:
    log: list[tuple] = []
    breaker = _breaker()
    breaker.fail_closed("portfolio_snapshot_unreliable")
    refusals: list[OrderRefusal] = []
    downstream = _Downstream(log)
    gate = _gate(_boundary(_Store(log), fallback_breaker=breaker), refusals)

    gate.submit_order(downstream.submit_order, _order("risk-increasing"))
    gate.submit_order(downstream.submit_order, _order("reduce-only", reduce_only=True))

    assert [entry[0] for entry in log] == ["submit"]
    assert refusals[0].client_order_id == "risk-increasing"
    assert refusals[0].reason_code == "custos_runner_fallback_breaker_frozen"


def test_frozen_breaker_allows_reduce_only_modification_without_reservation() -> None:
    log: list[tuple] = []
    breaker = _breaker()
    breaker.fail_closed("portfolio_snapshot_unreliable")
    refusals: list[OrderRefusal] = []
    downstream = _Downstream(log)
    gate = _gate(_boundary(_Store(log), fallback_breaker=breaker), refusals)
    order = _order("protective", reduce_only=True)

    gate.submit_order(downstream.submit_order, order)
    gate.modify_order(downstream.modify_order, order, trigger_price=Decimal("99"))

    assert [entry[0] for entry in log] == ["submit", "modify_upstream"]
    assert refusals == []


def test_modify_reserves_new_notional_before_upstream() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-2",
        policy_id=POLICY_ID,
        requested_notional=Decimal("10"),
    )
    log.clear()
    gate = _gate(_boundary(store))

    gate.modify_order(
        _Downstream(log).modify_order,
        SimpleNamespace(
            client_order_id="order-2",
            strategy_id="STRATEGY-001",
            instrument_id="BTCUSDT-PERP.BINANCE",
            venue_order_id="venue-2",
            notional="40",
            reduce_only=False,
        ),
        "40",
    )

    assert [entry[0] for entry in log] == ["replace", "modify_upstream"]
    assert log[0][1]["new_reserved_notional"] == Decimal("40")


class OrderFilled:
    def __init__(
        self,
        *,
        reduce_only: bool = False,
        exposure_source_order_id: str | None = None,
    ) -> None:
        self.event_id = "fill-event-1"
        self.client_order_id = "order-3"
        self.notional = "7"
        self.quantity = "0.007"
        self.reduce_only = reduce_only
        self.exposure_source_order_id = exposure_source_order_id

    @classmethod
    def to_dict(cls, event):
        return {
            "event_id": event.event_id,
            "client_order_id": event.client_order_id,
        }


class OrderCanceled:
    event_id = "cancel-event-1"
    client_order_id = "order-3"

    @classmethod
    def to_dict(cls, event):
        return {
            "event_id": event.event_id,
            "client_order_id": event.client_order_id,
        }


class _Forwarder:
    """Stands in for the host's event forwarder, which replaced the message bus."""

    def __init__(self) -> None:
        self.order_sinks: list = []
        self.position_sinks: list = []

    def add_order_sink(self, _name: str, sink) -> None:
        self.order_sinks.append(sink)

    def add_position_sink(self, _name: str, sink) -> None:
        self.position_sinks.append(sink)


def test_order_events_advance_fill_and_cancel_reservations() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-3",
        policy_id=POLICY_ID,
        requested_notional=Decimal("25"),
    )
    log.clear()
    boundary = _boundary(store)
    forwarder = _Forwarder()
    boundary.bootstrap(forwarder)

    (handler,) = forwarder.order_sinks
    handler(OrderFilled())
    handler(OrderCanceled())

    assert [entry[0] for entry in log] == ["fill", "release"]
    assert log[0][1]["fill_notional"] == Decimal("7")
    assert log[0][1]["fill_quantity"] == Decimal("0.007")
    assert log[1][1]["reason"] == "canceled"


def test_terminal_event_without_a_reservation_is_a_safe_noop() -> None:
    log: list[tuple] = []
    boundary = _boundary(_Store(log))

    boundary.on_order_event(OrderCanceled())

    assert log == []


def test_reduce_only_fill_releases_open_exposure_when_reservation_exists() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-3",
        policy_id=POLICY_ID,
        requested_notional=Decimal("0"),
    )
    log.clear()
    boundary = _boundary(store)

    boundary.on_order_event(OrderFilled(reduce_only=True, exposure_source_order_id="order-3"))

    assert [entry[0] for entry in log] == ["reduce"]
    assert log[0][1]["reduction_notional"] == Decimal("7")
    assert log[0][1]["reduction_quantity"] == Decimal("0.007")


def test_unattributed_reduce_only_fill_freezes_new_risk_without_raising() -> None:
    log: list[tuple] = []
    breaker = _breaker()
    boundary = _boundary(_Store(log), fallback_breaker=breaker)
    boundary.before_submit_order(
        SimpleNamespace(order=_order("order-3", reduce_only=True), id="reduce-submit")
    )

    boundary.on_order_event(OrderFilled(reduce_only=True))

    assert breaker.frozen is True
    assert log == []


def test_fill_accounting_failure_freezes_new_risk_without_killing_event_loop() -> None:
    class BrokenFillStore(_Store):
        def record_order_fill_sync(self, **kwargs):
            raise RunnerStateAuthorityError("simulated post-trade accounting mismatch")

    log: list[tuple] = []
    store = BrokenFillStore(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-3",
        policy_id=POLICY_ID,
        requested_notional=Decimal("25"),
    )
    breaker = _breaker()
    boundary = _boundary(store, fallback_breaker=breaker)

    boundary.on_order_event(OrderFilled())

    assert breaker.frozen is True


def test_foreign_risk_increasing_fill_is_ignored_without_freezing_this_instance() -> None:
    log: list[tuple] = []
    breaker = _breaker()
    boundary = _boundary(_Store(log), fallback_breaker=breaker)

    boundary.on_order_event(OrderFilled())

    assert breaker.frozen is False
    assert log == []


def test_foreign_reduce_only_fill_is_ignored_without_freezing_this_instance() -> None:
    log: list[tuple] = []
    breaker = _breaker()
    boundary = _boundary(_Store(log), fallback_breaker=breaker)

    boundary.on_order_event(OrderFilled(reduce_only=True, exposure_source_order_id="foreign-entry"))

    assert breaker.frozen is False
    assert log == []


def test_an_id_the_venue_would_refuse_is_rejected_here_instead() -> None:
    """The venue's id-length limit is enforced at the boundary, not just in the builder.

    The id's shape is decided where the strategy config is built, and that is the only
    place it can be decided — the flags are read-only on the strategy. Which makes it a
    convention: a signed artifact whose adapter builds its own config, or a strategy
    passing an explicit client_order_id, reaches the venue without consulting that
    builder and reproduces the rejection of every order.

    Enforced here, that becomes an invariant regardless of who built the config, and the
    failure is local and named rather than a venue answering -4015 to everything.
    """

    log: list[tuple] = []
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(_Store(log)), refusals)

    # The exact id the venue refused: 44 characters, the shape before the fix.
    refused = "O-20260730-044937-dcb00e520b45569e83b0-000-2"
    gate.submit_order(_Downstream(log).submit_order, _order(refused))

    assert log == [], "the order reached the reservation or the venue despite an unusable id"
    assert [refusal.reason_code for refusal in refusals] == [
        "custos_runner_client_order_id_too_long_for_venue"
    ], "refused for the wrong reason, so the cause would not be diagnosable from the record"


def test_the_boundary_lets_the_shape_the_builder_produces_through() -> None:
    """The guard must not reject what the fix produces, or it would block every order."""

    log: list[tuple] = []
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(_Store(log)), refusals)

    # A hyphen-free UUID, which is what build_nautilus_base_config now asks for.
    gate.submit_order(_Downstream(log).submit_order, _order("33e0789d38f9419aa8a0e00e98d07878"))

    assert [entry[0] for entry in log] == ["reserve", "submit"]
    assert refusals == []


def test_one_unusable_id_fails_the_whole_order_list() -> None:
    """A partly-submitted bracket is worse than none: the venue would refuse that leg."""

    log: list[tuple] = []
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(_Store(log)), refusals)
    orders = (
        _order("33e0789d38f9419aa8a0e00e98d07878"),
        _order("O-20260730-044937-dcb00e520b45569e83b0-000-2"),
    )

    gate.submit_order_list(
        _Downstream(log).submit_order_list,
        SimpleNamespace(orders=orders),
    )

    assert log == [], "part of the list reached the venue"
    assert len(refusals) == 2, "both legs must be refused, not only the unusable one"
    assert {refusal.reason_code for refusal in refusals} == {
        "custos_runner_client_order_id_too_long_for_venue"
    }


# ---------------------------------------------------------------------------
# What holds the gate's coverage together
#
# 1.x wrapped the execution client, so every command crossed the guard whatever
# sent it. 2.0 has no seat there for python, and the strategy edge does not have
# that property for free: nautilus can submit on the strategy's behalf. Each way
# it can is closed below, and these are the tests that say so.
# ---------------------------------------------------------------------------


def test_the_gate_sits_on_every_outbound_method() -> None:
    strategy = _GatedStrategy()

    install_order_gate(strategy, _gate(_boundary(_Store([]))))

    # Installed hooks live on the instance; anything still resolving to the class is
    # a path the gate never took over.
    for wrapped in ("submit_order", "submit_order_list", "modify_order", "market_exit"):
        assert wrapped in vars(strategy), (
            f"{wrapped} still reaches nautilus without passing the gate"
        )


def test_a_strategy_that_lets_nautilus_submit_for_it_is_refused() -> None:
    """The order manager submits contingent and emulated orders from inside the
    engine, where the gate is not. The switch defaults off; this is what makes
    'defaults off' into 'is off'."""
    strategy = _GatedStrategy()
    strategy.config = SimpleNamespace(
        manage_contingent_orders=True,
        manage_gtd_expiry=False,
        manage_stop=False,
    )

    with pytest.raises(StrategyHookUnsupported, match="manage_contingent_orders"):
        install_order_gate(strategy, _gate(_boundary(_Store([]))))


@pytest.mark.parametrize(
    "switch",
    ["manage_contingent_orders", "manage_gtd_expiry", "manage_stop"],
)
def test_each_bypass_switch_is_refused_on_its_own(switch: str) -> None:
    strategy = _GatedStrategy()
    switches = dict.fromkeys(
        ("manage_contingent_orders", "manage_gtd_expiry", "manage_stop"), False
    )
    switches[switch] = True
    strategy.config = SimpleNamespace(**switches)

    with pytest.raises(StrategyHookUnsupported, match=switch):
        install_order_gate(strategy, _gate(_boundary(_Store([]))))


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("emulation_trigger", "LAST_PRICE"), ("exec_algorithm_id", "TWAP-001")],
)
def test_an_order_nautilus_would_resubmit_itself_is_refused(attribute: str, value: str) -> None:
    """Both are routed away from the ordinary path and resubmitted from inside the
    engine, so the reservation taken here would stop describing what is working."""
    log: list[tuple] = []
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(_Store(log)), refusals)

    gate.submit_order(
        _Downstream(log).submit_order,
        _order("routed-away", **{attribute: value}),
    )

    assert log == [], "the order was reserved or submitted despite being routed away"
    assert [refusal.reason_code for refusal in refusals] == [
        "custos_runner_order_would_bypass_the_gate"
    ]


def test_market_exit_is_refused_because_nautilus_performs_it_itself() -> None:
    log: list[tuple] = []
    refusals: list[OrderRefusal] = []
    strategy = _GatedStrategy()
    install_order_gate(strategy, _gate(_boundary(_Store(log)), refusals))

    strategy.market_exit()

    assert strategy.submitted == [], "the exit ran, and everything it submits skips the gate"
    assert [refusal.reason_code for refusal in refusals] == [
        "custos_runner_market_exit_bypasses_the_gate"
    ]


def test_an_installed_gate_reserves_before_the_strategys_own_submit() -> None:
    """End to end through the installed hook rather than through the gate directly:
    what the strategy calls is what a real strategy calls."""
    log: list[tuple] = []
    strategy = _GatedStrategy()
    install_order_gate(strategy, _gate(_boundary(_Store(log))))

    order = _order("through-the-hook")
    strategy.submit_order(order)

    assert [entry[0] for entry in log] == ["reserve"]
    assert strategy.submitted == [order]


def test_a_refusal_that_cannot_be_reported_does_not_let_the_order_out() -> None:
    """Reporting is how a refusal becomes visible, but it is not what enforces it.

    If the fact sink throws, the exposure still has to stay contained -- losing the
    record is bad, losing the containment because the record failed would be worse.
    """
    log: list[tuple] = []
    store = _Store(log)
    store.reject_reservation = True

    def _sink_that_fails(_refusal: OrderRefusal) -> None:
        raise RuntimeError("fact stream unavailable")

    gate = RunnerSafetyOrderGate(
        boundary=_boundary(store),
        client_order_id_len_limit=BINANCE_CLIENT_ORDER_ID_LEN_LIMIT,
        on_refusal=_sink_that_fails,
    )

    gate.submit_order(_Downstream(log).submit_order, _order("unreportable"))

    assert [entry[0] for entry in log] == ["reserve"], "the order was submitted anyway"


# ---------------------------------------------------------------------------
# Money path: a price of zero is a broken price, not a cheap order
# ---------------------------------------------------------------------------


class _CacheWithPrice:
    def __init__(self, price) -> None:
        self._price = price

    def instrument(self, _instrument_id):
        return SimpleNamespace(notional_value=lambda quantity, price: Decimal(str(price)) * 2)

    def price(self, _instrument_id, _price_type):
        return self._price

    def order(self, _client_order_id):
        return None


def _priced_order(price):
    return SimpleNamespace(
        client_order_id="O-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        quantity="2",
        price=price,
        is_quote_quantity=False,
        is_reduce_only=False,
    )


def test_a_zero_price_is_refused_rather_than_valued_at_nothing() -> None:
    """A zero notional reserves nothing and passes every cap there is.

    The old chain used ``or``, which treats zero as absent and falls through to the
    next source -- valuing the order off a price the venue is not using, and doing
    it silently.
    """
    semantics = NautilusCachedOrderSemantics(_CacheWithPrice(price="0"))

    with pytest.raises(RuntimeError, match="must be a positive decimal"):
        semantics.order_notional(_priced_order("0"))


def test_a_real_price_is_valued_normally() -> None:
    semantics = NautilusCachedOrderSemantics(_CacheWithPrice(price="100"))

    assert semantics.order_notional(_priced_order("50")) == Decimal("100")


def test_a_market_order_falls_through_to_the_mid_price() -> None:
    """A market order genuinely has neither price nor trigger_price in 2.0, so the
    fall-through is the real path rather than defensive padding."""
    semantics = NautilusCachedOrderSemantics(_CacheWithPrice(price="30"))
    market = SimpleNamespace(
        client_order_id="O-2",
        instrument_id="BTCUSDT-PERP.BINANCE",
        quantity="1",
        is_quote_quantity=False,
        is_reduce_only=False,
    )

    assert semantics.order_notional(market) == Decimal("60")


def test_a_zero_mid_price_is_refused_too() -> None:
    semantics = NautilusCachedOrderSemantics(_CacheWithPrice(price="0"))
    market = SimpleNamespace(
        client_order_id="O-3",
        instrument_id="BTCUSDT-PERP.BINANCE",
        quantity="1",
        is_quote_quantity=False,
        is_reduce_only=False,
    )

    with pytest.raises(RuntimeError, match="must be a positive decimal"):
        semantics.order_notional(market)


def test_a_venue_with_no_measured_id_cap_refuses_nothing_on_length() -> None:
    """A cap carried over from another exchange would be a claim nobody measured.

    SoDEX declares none, so the guard stands down there rather than applying Binance's
    36. What is lost is the local refusal: an over-long id would be refused by that
    venue instead, one round trip later. What is not risked is this runner vouching
    for a number it has never put to the venue.
    """
    log: list[tuple] = []
    refusals: list[OrderRefusal] = []
    gate = _gate(_boundary(_Store(log)), refusals, client_order_id_len_limit=None)
    over_long = "O-20260730-044937-dcb00e520b45569e83b0-000-2"

    gate.submit_order(_Downstream(log).submit_order, _order(over_long))

    assert [refusal.reason_code for refusal in refusals] == []
    assert ("submit", over_long) in log
