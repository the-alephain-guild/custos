"""Offline review probes. Assertions describe bugs at the audited revision."""

from __future__ import annotations

# ruff: noqa: E402 -- standalone probe adds the checkout before importing fixtures.
import ast
import asyncio
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custos.core.engine_lifecycle import EngineLifecycleDurableState, EngineLifecycleQuarantined
from custos.core.runner_fact_producer import RunnerFactEventBridge, RunnerFactProductionLoop
from custos.engines.nautilus.binance_ledger import BinanceVenueLedgerError, BinanceVenueLedgerSource
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.portfolio_snapshot import NautilusPortfolioSnapshotProvider
from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics
from tests import test_engine_lifecycle as lifecycle
from tests import test_order_reservation as reservations
from tests import test_portfolio_snapshot as portfolio
from tests import test_strategy_signal_bridge as bridge_fixtures
from tests.engines.nautilus import test_runner_safety_execution_boundary as gate_fixtures


class Capture:
    def __init__(self):
        self.facts = []

    async def emit(self, authority, facts):
        self.facts.extend(facts)

    def emit_sync(self, authority, facts):
        self.facts.extend(facts)


def binance_source(spot=False):
    return BinanceVenueLedgerSource(
        spec={
            "trading_mode": "testnet",
            "connector": "binance" if spot else "binance_perpetual",
            "pairs": ["BTC-USDT"],
        },
        credential={"api_key": "fixture", "api_secret": "fixture"},
    )


async def signed_generation():
    class AttachedHost(NtTradingNodeHost):
        def __init__(self):
            super().__init__(tenant_id="fixture", runner_id="fixture")
            self._active_nodes[str(lifecycle.INSTANCE)] = object()
            self.deploy_calls = 0
            self.stop_calls = 0

        async def deploy(self, *args):
            self.deploy_calls += 1
            return await super().deploy(*args)

        async def stop(self, instance):
            self.stop_calls += 1
            self._active_nodes.pop(instance, None)

    host = AttachedHost()
    store = lifecycle._Store(
        state=EngineLifecycleDurableState(
            desired_status="pending",
            applied_generation=1,
            applied_command_fingerprint="b" * 64,
            engine_handle="old-node",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    verified = lifecycle._verified(generation=2)
    spec = {
        "deployment_instance_id": str(lifecycle.INSTANCE),
        "deployment_spec_id": str(lifecycle.SPEC),
        "deployment_spec_digest": lifecycle.DIGEST,
        "generation": 2,
        "trading_mode": "sandbox",
        "connector": "binance",
        "pairs": ["BTC-USDT"],
    }
    try:
        await lifecycle._supervisor(store, host).apply(
            delivery_id="new-generation",
            verified=verified,
            runtime_spec=spec,
            credential={},
            artifact=lifecycle._Artifact(),
        )
    except EngineLifecycleQuarantined:
        pass
    else:
        raise AssertionError("expected the observed quarantine bug")
    assert host.deploy_calls == 3 and host.stop_calls == 0
    assert str(lifecycle.INSTANCE) in host._active_nodes
    assert store.state.desired_status == "quarantined"
    print("DR-1: signed generation 2 quarantined after 3 deploys; stop calls=0; old node retained")


async def unobserved_breaker_and_false_health():
    from custos.cli._daemon import _build_runner_safety_boundary_factory

    class Resolver:
        async def resolve(self, mode):
            return SimpleNamespace(
                owner_policy=True,
                policy_id=gate_fixtures.POLICY_ID,
                breaker=gate_fixtures._breaker().config,
            )

    log = []
    store = gate_fixtures._Store(log)
    factory = _build_runner_safety_boundary_factory(
        state_store=store, safety_policy_resolver=Resolver()
    )
    boundary = await factory(
        {
            "trading_mode": "sandbox",
            "deployment_instance_id": str(gate_fixtures.DEPLOYMENT_INSTANCE_ID),
        }
    )
    boundary.bind_runtime(semantics=gate_fixtures._Semantics())
    runtime = portfolio._Runtime(mark_price=portfolio._DecimalValue("100"))
    host = NtTradingNodeHost(
        tenant_id="fixture",
        runner_id="fixture",
        portfolio_snapshot_provider=NautilusPortfolioSnapshotProvider(price_type_mid="MID"),
    )
    instance = str(gate_fixtures.DEPLOYMENT_INSTANCE_ID)
    portfolio._register(host, instance, cache=runtime.cache, portfolio=runtime.portfolio)
    host._settlement_currencies[instance] = "USDT"
    host._runner_fact_contexts[instance] = object()
    host._runner_safety_boundaries[instance] = boundary
    await host.get_engine_status(instance)
    runtime.portfolio._equities = {"USDT": portfolio._DecimalValue("500")}
    status = await host.get_engine_status(instance)
    assert status.drawdown_pct == Decimal("50")
    emitter = Capture()
    deployment = SimpleNamespace(
        authority=SimpleNamespace(stream_key="review", deployment_spec_id=uuid4()),
        deployment_instance_id=instance,
        currency="USDT",
    )
    loop = RunnerFactProductionLoop(
        host=host, emitter=emitter, snapshot_interval_secs=1, period_secs=60, period_retry_secs=1
    )
    await loop._emit_observability(deployment)
    gate = gate_fixtures._gate(boundary)
    gate.submit_order(
        gate_fixtures._Downstream(log).submit_order, gate_fixtures._order("after-drawdown")
    )
    assert any(row[0] == "submit" for row in log)
    assert not boundary._fallback_breaker.frozen
    constructors = []
    for path in (ROOT / "src/custos").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "EngineSafetySupervisor"
            ):
                constructors.append(str(path.relative_to(ROOT)))
    assert constructors == ["src/custos/offline/safety.py"]
    print(
        "DR-2: signed boundary admits new order at observed 50% drawdown; breaker never evaluated; only supervisor composition is offline"
    )
    host._record_forwarding_failure(instance, "runner_facts", "runner_facts:OSError")
    assert not (await host.get_engine_status(instance)).reliable
    emitter.facts.clear()
    await loop._emit_observability(deployment)
    heartbeat = next(f for f in emitter.facts if f["kind"] == "heartbeat")
    assert heartbeat["status"] == "online"
    print(
        "DR-3: host status is unreliable/degraded after audit loss, signed heartbeat still online"
    )


def reduce_only_modify():
    from custos.core.order_reservation_boundary import RunnerReservationBoundary

    with tempfile.TemporaryDirectory() as directory:
        log, refusals = [], []
        store = reservations._store(Path(directory) / "modify.db")
        boundary = RunnerReservationBoundary(
            store=store,
            deployment_instance_id=reservations.INSTANCE_A,
            policy_id=reservations.POLICY_ID,
            fallback_breaker=gate_fixtures._breaker(),
            semantics=gate_fixtures._Semantics(),
        )
        gate = gate_fixtures._gate(boundary, refusals)
        downstream = gate_fixtures._Downstream(log)
        order = gate_fixtures._order("protective", reduce_only=True)
        gate.submit_order(downstream.submit_order, order)
        assert not store.has_order_reservation_sync(reservations.INSTANCE_A, "protective")
        gate.modify_order(downstream.modify_order, order, trigger_price=Decimal("99"))
        assert [row[0] for row in log] == ["submit"]
        assert refusals[0].reason_code == "custos_runner_notional_policy_rejected"
        print(
            "DR-4: reduce-only order submitted, then modify refused because no reservation was created"
        )


def base_asset_commission():
    source = binance_source(spot=True)
    try:
        source._trade_rows(
            [
                {
                    "symbol": "BTCUSDT",
                    "id": 1,
                    "orderId": 1,
                    "time": 1_000,
                    "isBuyer": True,
                    "qty": "1",
                    "price": "100",
                    "commission": "0.001",
                    "commissionAsset": "BTC",
                }
            ]
        )
    except BinanceVenueLedgerError as exc:
        assert "cannot be represented" in str(exc)
    else:
        raise AssertionError("expected base-asset fee rejection")
    print("DR-5: a valid BTC-denominated spot commission prevents Binance ledger collection")


def native_position():
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import (
        AccountId,
        ClientOrderId,
        CryptoPerpetual,
        Currency,
        InstrumentId,
        LiquiditySide,
        Money,
        OrderFilled,
        OrderSide,
        OrderType,
        Position,
        PositionId,
        Price,
        Quantity,
        StrategyId,
        Symbol,
        TradeId,
        TraderId,
        VenueOrderId,
    )

    instrument = CryptoPerpetual(
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
        Symbol("BTCUSDT"),
        Currency.from_str("BTC"),
        Currency.from_str("USDT"),
        Currency.from_str("USDT"),
        False,
        2,
        3,
        Price.from_str("0.01"),
        Quantity.from_str("0.001"),
        0,
        0,
    )

    def fill(side, price, qty, fee, n):
        return OrderFilled(
            TraderId("TRADER-001"),
            StrategyId("TEST-001"),
            instrument.id,
            ClientOrderId("order-" + str(n)),
            VenueOrderId(str(n)),
            AccountId("BINANCE-001"),
            TradeId(str(n)),
            side,
            OrderType.MARKET,
            Quantity.from_str(qty),
            Price.from_str(price),
            Currency.from_str("USDT"),
            LiquiditySide.TAKER,
            UUID4(),
            n * 1_000_000_000,
            n * 1_000_000_000,
            False,
            position_id=PositionId("P-001"),
            commission=Money.from_str(fee + " USDT"),
        )

    opening = fill(OrderSide.BUY, "100.00", "1.000", "1", 1)
    position = Position(instrument, opening)
    partial = fill(OrderSide.SELL, "110.00", "0.500", "1", 2)
    position.apply(partial)
    return instrument, position, fill


def pnl_basis():
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import OrderSide, PositionChanged, PositionClosed

    _, position, fill = native_position()
    emitter = Capture()
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=bridge_fixtures._deployment())
    partial = fill(OrderSide.SELL, "110.00", "0.500", "1", 2)
    bridge._on_position_event(PositionChanged.create(position, partial, UUID4(), 2_000_000_000))
    assert emitter.facts == []
    closing = fill(OrderSide.SELL, "110.00", "0.500", "1", 3)
    position.apply(closing)
    closed = PositionClosed.create(position, closing, UUID4(), 3_000_000_000)
    try:
        bridge._on_position_event(closed)
    except AttributeError as exc:
        assert "to_dict" in str(exc)
    else:
        raise AssertionError("expected native PositionClosed ABI mismatch")
    assert emitter.facts == []
    print(
        "DR-6: native PositionClosed has no to_dict; bridge raises AttributeError and emits no close fact"
    )
    # Inspect the actual native value without inventing a serializer or claiming
    # it already made it through the broken bridge.
    internal = closed.realized_pnl.as_decimal()
    incomes = binance_source()._income_fee_rows(
        [
            {
                "incomeType": "REALIZED_PNL",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "5",
                "time": 2000,
                "tranId": 2,
            },
            {
                "incomeType": "REALIZED_PNL",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "5",
                "time": 3000,
                "tranId": 3,
            },
        ]
    )
    external = sum(Decimal(row["amount"]) for row in incomes)
    assert internal == Decimal("7") and external == Decimal("10")
    print(
        "DR-7: native closed PnL=7 (after 3 commissions); Binance income PnL=10; partial close emits no internal PnL fact"
    )


def net_position_reduction():
    from nautilus_trader.model import OrderSide, Position

    from custos.core.order_reservation_boundary import RunnerReservationBoundary

    instrument, _, fill = native_position()
    first = fill(OrderSide.BUY, "50.00", "1.000", "0", 10)
    second = fill(OrderSide.BUY, "50.00", "1.000", "0", 11)
    position = Position(instrument, first)
    position.apply(second)
    assert position.quantity.as_decimal() == 2
    assert str(position.opening_order_id) == "order-10"
    closed = fill(OrderSide.SELL, "50.00", "2.000", "0", 12)
    position.apply(closed)
    assert position.is_closed
    with tempfile.TemporaryDirectory() as directory:
        store = reservations._store(Path(directory) / "state.db")
        for order_id in ("order-10", "order-11"):
            store.reserve_order_notional_sync(
                event_id="reserve-" + order_id,
                deployment_instance_id=reservations.INSTANCE_A,
                client_order_id=order_id,
                policy_id=reservations.POLICY_ID,
                requested_notional=Decimal("50"),
            )
            store.record_order_fill_sync(
                event_id="fill-" + order_id,
                deployment_instance_id=reservations.INSTANCE_A,
                client_order_id=order_id,
                fill_notional=Decimal("50"),
                fill_quantity=Decimal("1"),
            )
        closing_order = SimpleNamespace(is_reduce_only=True, client_order_id="order-12")
        cache = SimpleNamespace(
            order=lambda _: closing_order,
            position=lambda _: position,
            instrument=lambda _: instrument,
        )
        breaker = gate_fixtures._breaker()
        boundary = RunnerReservationBoundary(
            store=store,
            deployment_instance_id=reservations.INSTANCE_A,
            policy_id=reservations.POLICY_ID,
            fallback_breaker=breaker,
            semantics=NautilusCachedOrderSemantics(cache),
        )
        boundary.before_submit_order(SimpleNamespace(order=closing_order, id="exit-command"))
        boundary.on_order_event(closed)
        assert breaker.frozen
        amounts = [
            store.load_order_reservation_sync(reservations.INSTANCE_A, key).filled_exposure
            for key in ("order-10", "order-11")
        ]
        assert sum(amounts) == Decimal("100")
        print(
            "DR-8: native position is flat after closing 2 units; only first 1-unit entry is debited; breaker frozen and stale exposure=100"
        )


async def policy_rollover_exposure():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from custos.contracts.crucible_runner_safety_policy import RunnerAggregateCapPolicyRefV1
    from tests import test_runner_policy_runtime as policy_fixtures

    with tempfile.TemporaryDirectory() as directory:
        store = reservations._store(Path(directory) / "policy.db")
        with store._outbox._connect() as connection:
            connection.execute("DELETE FROM runner_cap_policy_head")
            connection.execute("DELETE FROM runner_cap_policy")
        signing_key = Ed25519PrivateKey.generate()
        first = policy_fixtures._verified_policy(
            signing_key, max_order="100", max_total="150", expires_at="2099-01-01T00:00:00Z"
        )
        await store.record_verified_runner_safety_policy(first)
        store.reserve_order_notional_sync(
            event_id="old-reserve",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="old-entry",
            policy_id=first.policy.policy_id,
            requested_notional=Decimal("100"),
        )
        store.record_order_fill_sync(
            event_id="old-fill",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="old-entry",
            fill_notional=Decimal("100"),
            fill_quantity=Decimal("1"),
        )
        second = policy_fixtures._verified_policy(
            signing_key,
            revision=2,
            previous=RunnerAggregateCapPolicyRefV1(
                policy_id=first.policy.policy_id,
                revision=1,
                policy_digest=first.policy.policy_digest,
            ),
            max_order="100",
            max_total="150",
            expires_at="2099-01-01T00:00:00Z",
        )
        await store.record_verified_runner_safety_policy(second)
        before = await store.load_runner_exposure(second.policy.policy_id)
        assert before.total_exposure == 0
        store.reserve_order_notional_sync(
            event_id="new-reserve",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="new-entry",
            policy_id=second.policy.policy_id,
            requested_notional=Decimal("100"),
        )
        old = store.load_order_reservation_sync(reservations.INSTANCE_A, "old-entry")
        current = await store.load_runner_exposure(second.policy.policy_id)
        assert old.filled_exposure + current.total_exposure == Decimal("200")
        assert current.max_total_notional == Decimal("150")
        print(
            "DR-9: verified policy rollover resets counted exposure to 0; permits total exposure 200 under cap 150"
        )


async def main():
    await signed_generation()
    await unobserved_breaker_and_false_health()
    reduce_only_modify()
    base_asset_commission()
    pnl_basis()
    net_position_reduction()
    await policy_rollover_exposure()


if __name__ == "__main__":
    asyncio.run(main())
