"""The local outbound gate consumes durable claims rather than a net position."""

from __future__ import annotations

import hashlib
import importlib
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("nautilus_trader")
from nautilus_trader.core import UUID4
from nautilus_trader.model import (
    AccountId,
    ClientId,
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
from tests.test_runtime_authority import account

INSTRUMENT = InstrumentId.from_str("BTC-USD-PERP.PAPERTRADE")
ACCOUNT = "PAPERTRADE-001"


class FixtureNativeFactory:
    @staticmethod
    def submission_params_keccak256(params=None):
        # A deterministic test token; actual Keccak conversion needs the new Native wheel.
        raw = json.dumps(params or None, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "0x" + hashlib.sha256(raw.encode()).hexdigest()


class FixtureHeldJournal:
    def __init__(self, risk):
        self.risk = risk
        self.revision = 10
        self.closed = False
        self.orders = {}
        self._save_revision()

    def _save_revision(self):
        self.risk.bindings.journal.path.write_text(
            f"{self.risk.bindings.journal.store_id}:{self.revision}"
        )

    def identity(self):
        identity = account()
        return {
            "schema_version": 1,
            "journal_id": str(self.risk.bindings.journal.store_id),
            "revision": self.revision,
            "owner": f"{identity.chain_id}:{identity.exchange}:{identity.wallet_address}:{ACCOUNT}",
            "canonical_path": str(self.risk.bindings.journal.path.resolve()),
        }

    def snapshot_json(self):
        if self.closed:
            raise RuntimeError("fixture Native owner released")
        return json.dumps(self.identity())

    def order_evidence_json(self, identifier):
        if self.closed:
            raise RuntimeError("fixture Native owner released")
        return json.dumps(
            {
                "schema_version": 1,
                "journal": self.identity(),
                "client_id": "PAPERTRADE",
                "account_id": ACCOUNT,
                "client_order_id": identifier,
                **self.orders.get(identifier, {"kind": "missing"}),
            }
        )

    def prove_no_fill(self, identifier):
        binding = self.risk.store._native_dispatch.load(identifier).binding
        self.revision += 1
        self._save_revision()
        self.orders[identifier] = {
            "kind": "proven_no_fill",
            "binding": {
                "intent_id": "0x" + "11" * 32,
                "command_keccak256": "0x" + "22" * 32,
                "submission_params_keccak256": binding.submission_params_keccak256,
                "action": "closePositions" if binding.reduce_only else "openPosition",
                "target": binding.target,
                "instrument_id": binding.instrument_id,
                "side": binding.side,
                "quantity": str(binding.quantity),
                "quote_quantity": binding.quote_quantity,
                "reduce_only": binding.reduce_only,
                "deadline": 100,
            },
            "proof": {
                "schema_version": 1,
                "journal_id": str(self.risk.bindings.journal.store_id),
                "command_keccak256": "0x" + "22" * 32,
                "sealed_at_revision": self.revision,
                "source": {"kind": "never_submitted"},
            },
        }


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


def build_gated(risk):
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
    native_journal = FixtureHeldJournal(risk)
    storage = importlib.import_module(
        "custos.engines.nautilus.runtime_storage"
    ).PapertradeRuntimeStorageProbe(
        state=risk.events,
        journal_path=risk.bindings.journal.path,
        account=account(),
        adapter_account_id=ACCOUNT,
        adapter_client_id="PAPERTRADE",
        native_probe=native_journal,
    )
    port = api.LocalPapertradeReductions(
        store=risk.store,
        account_id=ACCOUNT,
        client_id="PAPERTRADE",
        storage_probe=storage,
        native_factory=FixtureNativeFactory,
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
        risk=risk,
        gate=gate,
        boundary=boundary,
        positions=positions,
        port=port,
        cache=cache,
        native_journal=native_journal,
        storage=storage,
    )


@pytest.fixture
def gated(local_risk):
    return build_gated(local_risk)


def test_temporary_cash_refusal_compensates_only_the_unsent_prepared_opening(
    local_risk, monkeypatch
):
    from dataclasses import replace

    from tests.test_local_loss_budget import observation

    risk = local_risk
    budget = risk.store.bind_loss_budget()
    initial = observation(risk)
    budget.initialize(initial)
    runtime = build_gated(risk)
    original = runtime.port.begin_dispatch

    def become_pending(identifier):
        budget.observe(
            replace(
                initial,
                checkpoint_sequence=2,
                source_digest="0x" + "22" * 32,
                position_ids=("PT-7",),
                cohort_ready=False,
            )
        )
        return original(identifier)

    monkeypatch.setattr(runtime.port, "begin_dispatch", become_pending)
    opening = close_order("pending-opening", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True
    sent = []
    assert runtime.gate.submit_order(lambda *a, **kw: sent.append(True), opening) is False
    assert sent == []
    assert runtime.port._dispatch.load("pending-opening").state == "not_sent"
    assert snapshot(risk, "pending-opening").reserved_notional == 0
    assert snapshot(risk).filled_exposure == 500
    assert not risk.blocker.is_frozen()


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


@pytest.mark.parametrize("mismatch", ["account", "client"])
def test_port_route_mismatch_is_refused_before_native_risk_binding(local_risk, mismatch):
    import sqlite3

    risk = local_risk
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute(
            "UPDATE local_risk_meta SET native_account_id=NULL,client_id=NULL,native_journal_id=NULL"
        )
    native_journal = FixtureHeldJournal(risk)
    storage = importlib.import_module(
        "custos.engines.nautilus.runtime_storage"
    ).PapertradeRuntimeStorageProbe(
        state=risk.events,
        journal_path=risk.bindings.journal.path,
        account=account(),
        adapter_account_id=ACCOUNT,
        adapter_client_id="PAPERTRADE",
        native_probe=native_journal,
    )
    calls = []
    factory = SimpleNamespace(
        submission_params_keccak256=lambda params: calls.append(params) or "0x" + "11" * 32
    )
    with pytest.raises(RuntimeAuthorityError):
        importlib.import_module(
            "custos.engines.nautilus.local_reductions"
        ).LocalPapertradeReductions(
            store=risk.store,
            account_id="OTHER-001" if mismatch == "account" else ACCOUNT,
            client_id="OTHER" if mismatch == "client" else "PAPERTRADE",
            storage_probe=storage,
            native_factory=factory,
        )
    assert calls == []
    with sqlite3.connect(risk.events.path) as connection:
        assert connection.execute(
            "SELECT native_account_id,client_id,native_journal_id FROM local_risk_meta"
        ).fetchone() == (None, None, None)
        assert connection.execute("SELECT COUNT(*) FROM local_native_dispatch").fetchone()[0] == 0


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


def test_pending_exception_after_native_call_cannot_use_unsent_compensation(gated):
    from custos.core.runtime_authority import RuntimeRiskPending

    opening = close_order("post-native-pending", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True

    def native(*args, **kwargs):
        raise RuntimeRiskPending("injected exception after entering Native")

    with pytest.raises(RuntimeRiskPending):
        gated.gate.submit_order(native, opening)
    assert snapshot(gated.risk, "post-native-pending").reserved_notional == 500
    assert gated.port._dispatch.load("post-native-pending").state == "dispatched"
    assert gated.risk.blocker.is_frozen()


def test_close_no_fill_evidence_is_read_from_the_same_bound_native_handle(gated):
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    gated.native_journal.prove_no_fill("close-7")
    assert gated.port.reconcile_no_fill("close-7") is True
    assert gated.risk.store.reductions.load("close-7").state == "proven_no_fill"
    assert snapshot(gated.risk).filled_exposure == Decimal("500")


def test_missing_native_order_does_not_release_an_unknown_opening(gated):
    opening = close_order("new-entry", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True
    assert gated.gate.submit_order(lambda *a, **kw: None, opening)
    assert gated.port.reconcile_no_fill("new-entry") is False
    assert snapshot(gated.risk, "new-entry").reserved_notional == Decimal("500")


def test_native_opening_call_observes_the_durable_dispatch_marker(gated):
    opening = close_order("new-entry", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True
    seen = []

    def native(order, **kwargs):
        seen.append(gated.risk.store._native_dispatch.load(str(order.client_order_id)).state)

    assert gated.gate.submit_order(native, opening)
    assert seen == ["dispatched"]


def test_opening_no_fill_proof_releases_only_that_order_reservation(gated):
    opening = close_order("new-entry", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True
    assert gated.gate.submit_order(lambda *a, **kw: None, opening)
    gated.native_journal.prove_no_fill("new-entry")
    assert gated.port.reconcile_no_fill("new-entry")
    assert snapshot(gated.risk, "new-entry").reserved_notional == 0
    assert snapshot(gated.risk).filled_exposure == Decimal("500")


def test_an_existing_native_identifier_cannot_be_reused_before_dispatch(gated):
    gated.native_journal.orders["close-7"] = {
        "kind": "unproven",
        "binding": {
            "intent_id": "0x" + "11" * 32,
            "command_keccak256": "0x" + "22" * 32,
            "submission_params_keccak256": FixtureNativeFactory.submission_params_keccak256(),
            "action": "closePositions",
            "target": "7",
            "instrument_id": str(INSTRUMENT),
            "side": "SELL",
            "quantity": "0.005",
            "quote_quantity": False,
            "reduce_only": True,
            "deadline": 100,
        },
    }
    seen = []
    assert (
        gated.gate.submit_order(
            lambda *a, **kw: seen.append(True), close_order(), position_id=PositionId("PT-7")
        )
        is False
    )
    assert seen == []
    assert gated.risk.store.reductions.load("close-7").state == "prepared"
    assert gated.risk.blocker.is_frozen()


def test_corrupt_params_proof_keeps_the_claim_and_position_cost(gated):
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    gated.native_journal.prove_no_fill("close-7")
    gated.native_journal.orders["close-7"]["binding"]["submission_params_keccak256"] = (
        "0x" + "ff" * 32
    )
    with pytest.raises(RuntimeAuthorityError):
        gated.port.reconcile_no_fill("close-7")
    assert gated.risk.store.reductions.load("close-7").state == "dispatching"
    assert snapshot(gated.risk).filled_exposure == Decimal("500")
    assert gated.risk.blocker.is_frozen()


def test_lost_native_owner_cannot_be_replaced_by_an_old_no_fill_observation(gated):
    assert gated.gate.submit_order(
        lambda *a, **kw: None, close_order(), position_id=PositionId("PT-7")
    )
    gated.native_journal.prove_no_fill("close-7")
    gated.native_journal.closed = True
    with pytest.raises(RuntimeAuthorityError):
        gated.port.reconcile_no_fill("close-7")
    assert snapshot(gated.risk).filled_exposure == Decimal("500")
    assert gated.risk.store.reductions.load("close-7").state == "dispatching"


def test_local_pre_dispatch_rollback_uses_the_specific_withdrawal_path(gated):
    from custos.engines.nautilus.runner_safety import _SubmitIntent

    opening = close_order("new-entry", reduce_only=False)
    opening.quantity = Quantity.from_str("500")
    opening.is_quote_quantity = True
    command = _SubmitIntent(opening, (), {})
    reservations = gated.boundary.before_submit_order(command)
    gated.boundary.rollback_submit(reservations, command_id="not-entered-native")
    assert gated.risk.store._native_dispatch.load("new-entry").state == "not_sent"
    assert snapshot(gated.risk, "new-entry").reserved_notional == 0


@pytest.mark.parametrize("style", ["omitted", "keyword_none", "positional_none", "explicit"])
def test_local_submit_pins_the_observed_client_in_the_actual_forwarded_arguments(gated, style):
    seen = []
    params = {"papertrade_position_id": "7", "extra": {"tags": ["original"]}}

    def native(order, *args, **kwargs):
        client = args[1] if len(args) >= 2 else kwargs.get("client_id")
        forwarded = args[2] if len(args) >= 3 else kwargs.get("params")
        seen.append((client, forwarded))

    if style == "positional_none":
        result = gated.gate.submit_order(native, close_order(), PositionId("PT-7"), None, params)
    else:
        fields = {"position_id": PositionId("PT-7"), "params": params}
        if style != "omitted":
            fields["client_id"] = None if style == "keyword_none" else ClientId("PAPERTRADE")
        result = gated.gate.submit_order(native, close_order(), **fields)
    assert result is True
    assert seen == [(ClientId("PAPERTRADE"), params)]


@pytest.mark.parametrize("client", ["PAPERTRADE", ClientId("OTHER")])
def test_local_submit_refuses_an_explicit_unverified_execution_route_before_claiming(gated, client):
    seen = []
    assert (
        gated.gate.submit_order(
            lambda *a, **kw: seen.append(True),
            close_order(),
            position_id=PositionId("PT-7"),
            client_id=client,
        )
        is False
    )
    assert seen == []
    assert gated.risk.store.reductions.load("close-7") is None


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
    native_journal = FixtureHeldJournal(recovered)
    storage = importlib.import_module(
        "custos.engines.nautilus.runtime_storage"
    ).PapertradeRuntimeStorageProbe(
        state=recovered.events,
        journal_path=recovered.bindings.journal.path,
        account=account(),
        adapter_account_id=ACCOUNT,
        adapter_client_id="PAPERTRADE",
        native_probe=native_journal,
    )
    port = api.LocalPapertradeReductions(
        store=recovered.store,
        account_id=ACCOUNT,
        client_id="PAPERTRADE",
        storage_probe=storage,
        native_factory=FixtureNativeFactory,
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
