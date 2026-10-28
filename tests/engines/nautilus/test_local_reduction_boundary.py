"""The local outbound gate consumes durable claims rather than a net position."""

from __future__ import annotations

import hashlib
import importlib
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("nautilus_trader")
from nautilus_trader.core import UUID4
from nautilus_trader.model import (
    AccountId,
    ClientOrderId,
    Currency,
    InstrumentId,
    LiquiditySide,
    OrderCanceled,
    OrderFilled,
    OrderRejected,
    OrderSide,
    OrderType,
    PositionId,
    Price,
    Quantity,
    StrategyId,
    TradeId,
    TraderId,
    VenueOrderId,
)

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runtime_authority import RuntimeAuthorityError
from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)
from tests.test_local_reduction_claims import bind_actual_routing as bind_actual_routing
from tests.test_local_reservations import local_risk as local_risk
from tests.test_local_reservations import reserve, snapshot

INSTRUMENT = InstrumentId.from_str("BTC-USD-PERP.PAPERTRADE")
ACCOUNT = "PAPERTRADE-001"


def close_order(identifier="close-7", *, reduce_only=True):
    return SimpleNamespace(
        client_order_id=ClientOrderId(identifier),
        strategy_id=StrategyId("LOCAL-001"),
        instrument_id=INSTRUMENT,
        side=OrderSide.SELL,
        quantity=Quantity.from_str("0.005"),
        is_quote_quantity=False,
        is_reduce_only=reduce_only,
        emulation_trigger=None,
        exec_algorithm_id=None,
    )


def native_fill(*, account=ACCOUNT, trade="close-trade-7"):
    return OrderFilled(
        trader_id=TraderId("LOCAL-001"),
        strategy_id=StrategyId("LOCAL-001"),
        instrument_id=INSTRUMENT,
        client_order_id=ClientOrderId("close-7"),
        venue_order_id=VenueOrderId("venue-7"),
        account_id=AccountId(account),
        trade_id=TradeId(trade),
        order_side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        last_qty=Quantity.from_str("0.005"),
        last_px=Price.from_str("96000"),
        currency=Currency.from_str("USD"),
        liquidity_side=LiquiditySide.TAKER,
        event_id=UUID4(),
        ts_event=1,
        ts_init=1,
        reconciliation=False,
        position_id=PositionId("PT-7"),
    )


@pytest.fixture
def gated(local_risk):
    risk = local_risk
    reserve(risk)
    digest = hashlib.sha256(b"opening-trade").hexdigest()
    risk.store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{digest}:entry",
        runtime_id=risk.manifest.run_id,
        client_order_id="entry",
        fill_notional=Decimal("500"),
        fill_quantity=Decimal("0.005"),
        leaves_quantity=Decimal(0),
        position_id="PT-7",
        instrument_id=str(INSTRUMENT),
        side="BUY",
    )
    positions = [
        SimpleNamespace(
            id=PositionId("PT-7"),
            account_id=AccountId(ACCOUNT),
            instrument_id=INSTRUMENT,
            quantity=Quantity.from_str("0.005"),
            is_closed=False,
            is_long=True,
            is_short=False,
        )
    ]
    instrument = SimpleNamespace(
        make_qty=lambda x: x,
        make_price=lambda x: x,
        notional_value=lambda quantity, price: quantity.as_decimal() * price.as_decimal(),
    )
    cache = SimpleNamespace(
        positions_open=lambda **kwargs: positions,
        instrument=lambda _: instrument,
        order=lambda _: None,
    )
    api = importlib.import_module("custos.engines.nautilus.local_reductions")
    port = api.LocalPapertradeReductions(
        store=risk.store,
        account_id=ACCOUNT,
        client_id="PAPERTRADE",
    )
    breaker = FallbackBreaker(FallbackBreakerConfig(Decimal("2000"), Decimal("10")))
    boundary = RunnerReservationBoundary(
        store=risk.store,
        runtime_id=risk.manifest.run_id,
        policy_id=risk.permit.permit_id,
        fallback_breaker=breaker,
        trading_mode="live",
        semantics=NautilusCachedOrderSemantics(cache),
        control_guard=risk.lease,
        reduction_claims=port,
    )
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    return SimpleNamespace(
        risk=risk, gate=gate, boundary=boundary, positions=positions, port=port, cache=cache
    )


def test_a_local_control_lease_cannot_select_platform_memory_reductions(local_risk):
    with pytest.raises(RuntimeAuthorityError):
        RunnerReservationBoundary(
            store=local_risk.store,
            runtime_id=local_risk.manifest.run_id,
            policy_id=local_risk.permit.permit_id,
            fallback_breaker=FallbackBreaker(FallbackBreakerConfig(Decimal("2000"), Decimal("10"))),
            trading_mode="live",
            control_guard=local_risk.lease,
            reduction_claims="platform_memory",
        )


def test_native_dispatch_observes_committed_marker_and_duplicate_never_sends(gated):
    seen = []

    def native(order, **kwargs):
        claim = gated.risk.store.reductions.load(str(order.client_order_id))
        seen.append((claim.state, kwargs["position_id"]))

    for expected in (True, False):
        assert (
            gated.gate.submit_order(native, close_order(), position_id=PositionId("PT-7"))
            is expected
        )
    assert seen == [("dispatching", PositionId("PT-7"))]
    assert snapshot(gated.risk).filled_exposure == Decimal("500")


def test_native_exception_retains_unknown_close_and_entry_cost(gated):
    def native(*args, **kwargs):
        raise RuntimeError("injected transport uncertainty")

    with pytest.raises(RuntimeError):
        gated.gate.submit_order(native, close_order(), position_id=PositionId("PT-7"))
    assert gated.risk.store.reductions.load("close-7").state == "unknown"
    assert snapshot(gated.risk).filled_exposure == Decimal("500")
    assert gated.risk.blocker.is_frozen()


def test_synchronous_fill_wins_over_the_following_native_exception(gated):
    def native(*args, **kwargs):
        gated.boundary.on_order_event(native_fill())
        raise RuntimeError("after authoritative fill")

    with pytest.raises(RuntimeError):
        gated.gate.submit_order(native, close_order(), position_id=PositionId("PT-7"))
    assert gated.risk.store.reductions.load("close-7").state == "filled"
    assert snapshot(gated.risk).filled_exposure == 0


def test_late_and_duplicate_native_fill_does_not_require_position_or_order_cache(gated):
    assert gated.gate.submit_order(
        lambda *args, **kwargs: None, close_order(), position_id=PositionId("PT-7")
    )
    gated.positions.clear()
    gated.boundary.on_order_event(native_fill())
    gated.boundary.on_order_event(native_fill())
    assert snapshot(gated.risk).filled_exposure == 0
    assert gated.risk.store.reductions.load("close-7").state == "filled"


def test_native_fill_from_another_account_cannot_release_this_claim(gated):
    assert gated.gate.submit_order(
        lambda *args, **kwargs: None, close_order(), position_id=PositionId("PT-7")
    )
    gated.boundary.on_order_event(native_fill(account="PAPERTRADE-OTHER"))
    assert snapshot(gated.risk).filled_exposure == Decimal("500")
    assert gated.risk.store.reductions.load("close-7").state != "filled"
    assert gated.risk.blocker.is_frozen()


@pytest.mark.parametrize(
    "name,args,kwargs",
    [
        ("close_position", (object(),), {}),
        ("close_all_positions", (INSTRUMENT,), {}),
        ("submit_order_list", (SimpleNamespace(orders=[close_order()]),), {}),
        ("modify_order", (ClientOrderId("close-7"),), {}),
        ("modify_orders", ([],), {}),
    ],
)
def test_every_unadapted_native_outbound_form_is_refused(gated, name, args, kwargs):
    sent = []
    getattr(gated.gate, name)(lambda *a, **kw: sent.append(True), *args, **kwargs)
    assert not sent
    assert not gated.risk.store.reductions.pending()


def test_plain_base_quantity_order_cannot_be_misclassified_as_a_local_close(gated):
    sent = []
    assert (
        gated.gate.submit_order(lambda *a, **kw: sent.append(True), close_order(reduce_only=False))
        is False
    )
    assert not sent and not gated.risk.store.reductions.pending()


@pytest.mark.parametrize("kind", ["rejected", "canceled"])
def test_native_synthetic_terminal_does_not_release_a_close_and_late_fill_still_applies(
    gated, kind
):
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    fields = dict(
        trader_id=TraderId("LOCAL-001"),
        strategy_id=StrategyId("LOCAL-001"),
        instrument_id=INSTRUMENT,
        client_order_id=ClientOrderId("close-7"),
        event_id=UUID4(),
        ts_event=1,
        ts_init=1,
        reconciliation=True,
        account_id=AccountId(ACCOUNT),
    )
    event = (
        OrderRejected(**fields, reason="INFLIGHT_TIMEOUT")
        if kind == "rejected"
        else OrderCanceled(**fields)
    )
    gated.boundary.on_order_event(event)
    assert gated.risk.store.reductions.load("close-7").state == "unknown"
    assert snapshot(gated.risk).filled_exposure == Decimal("500")
    assert gated.risk.blocker.is_frozen()
    gated.positions.clear()
    gated.boundary.on_order_event(native_fill())
    assert gated.risk.store.reductions.load("close-7").state == "filled"
    assert snapshot(gated.risk).filled_exposure == 0


def test_unavailable_fill_valuation_retains_pending_venue_identity(gated):
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    gated.cache.instrument = lambda _: None
    gated.boundary.on_order_event(native_fill())
    assert snapshot(gated.risk).filled_exposure == Decimal("500")
    assert gated.risk.blocker.is_frozen()
    gated.cache.instrument = lambda _: SimpleNamespace(
        make_qty=lambda x: x,
        make_price=lambda x: x,
        notional_value=lambda q, p: q.as_decimal() * p.as_decimal(),
    )
    gated.boundary.on_order_event(native_fill())
    assert snapshot(gated.risk).filled_exposure == Decimal("500")


def test_durable_reductions_cannot_be_rebound_to_another_cache(gated):
    with pytest.raises(RuntimeAuthorityError):
        gated.boundary.bind_runtime(semantics=NautilusCachedOrderSemantics(SimpleNamespace()))


def test_opening_exception_keeps_reserved_notional_and_a_durable_latch(gated):
    opening = close_order("new-entry", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True

    def native(*args, **kwargs):
        raise RuntimeError("unconfirmed opening submission")

    with pytest.raises(RuntimeError):
        gated.gate.submit_order(native, opening)
    assert snapshot(gated.risk, "new-entry").reserved_notional == Decimal("500")
    assert gated.risk.blocker.is_frozen()


def test_close_classification_comes_from_the_persistent_claim(gated):
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    assert gated.boundary.judged_risk_reducing("close-7") is True
    assert gated.boundary.judged_risk_reducing("entry") is False


def test_platform_policy_renewal_cannot_replace_a_fixed_local_permit(gated):
    with pytest.raises(RuntimeAuthorityError):
        gated.boundary.adopt_policy(
            uuid4(),
            FallbackBreakerConfig(Decimal("3000"), Decimal("10")),
            trading_mode="live",
        )
    assert gated.boundary.policy_id == gated.risk.permit.permit_id


@pytest.mark.parametrize("mismatch", ["policy", "guard", "store"])
def test_durable_port_requires_its_actual_store_policy_and_lease(gated, mismatch):
    fields = dict(
        store=gated.risk.store,
        runtime_id=gated.risk.manifest.run_id,
        policy_id=gated.risk.permit.permit_id,
        fallback_breaker=FallbackBreaker(FallbackBreakerConfig(Decimal("2000"), Decimal("10"))),
        trading_mode="live",
        control_guard=gated.risk.lease,
        reduction_claims=gated.port,
    )
    if mismatch == "policy":
        fields["policy_id"] = uuid4()
    elif mismatch == "guard":
        fields["control_guard"] = None
    else:
        fields["store"] = object()
    with pytest.raises(RuntimeAuthorityError):
        RunnerReservationBoundary(**fields)


def test_reopened_boundary_accounts_for_late_fill_without_old_memory_sets(gated):
    risk = gated.risk
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    gated.positions.clear()
    api = importlib.import_module("custos.engines.nautilus.local_reductions")
    port = api.LocalPapertradeReductions(
        store=recovered.store, account_id=ACCOUNT, client_id="PAPERTRADE"
    )
    boundary = RunnerReservationBoundary(
        store=recovered.store,
        runtime_id=recovered.manifest.run_id,
        policy_id=recovered.permit.permit_id,
        fallback_breaker=FallbackBreaker(FallbackBreakerConfig(Decimal("2000"), Decimal("10"))),
        trading_mode="live",
        control_guard=recovered.lease,
        reduction_claims=port,
        semantics=NautilusCachedOrderSemantics(gated.cache),
    )
    boundary.on_order_event(native_fill())
    assert snapshot(recovered).filled_exposure == 0
    assert recovered.store.reductions.load("close-7").state == "filled"
    assert recovered.blocker.is_frozen()


@pytest.mark.parametrize("instrument", ["ETH-USD-PERP.PAPERTRADE", "BTC-USDT-PERP.BINANCE"])
def test_opening_outside_the_signed_instrument_scope_never_reserves_or_sends(gated, instrument):
    opening = close_order("out-of-scope", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True
    opening.instrument_id = InstrumentId.from_str(instrument)
    sent = []
    assert gated.gate.submit_order(lambda *a, **kw: sent.append(True), opening) is False
    assert not sent
    assert not gated.risk.store.has_order_reservation_sync(
        gated.risk.manifest.run_id, "out-of-scope"
    )
