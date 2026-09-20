"""Read-only local probes for fix 03 re-review; assertions preserve observed bugs."""

from __future__ import annotations

# ruff: noqa: E402 -- standalone script imports checkout-local test fixtures.
import asyncio
import gc
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nautilus_trader.core import UUID4
from nautilus_trader.model import (
    ClientOrderId,
    Currency,
    CurrencyPair,
    InstrumentId,
    LimitOrder,
    OrderSide,
    Price,
    Quantity,
    StrategyId,
    Symbol,
    TimeInForce,
    TraderId,
)
from nautilus_trader.trading import Strategy

from custos.cli._daemon import _build_runner_safety_boundary_factory, _run_signed_safety_supervision
from custos.core.engine_safety import EngineSafetySupervisor
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import RunnerStateAuthorityError
from custos.core.runner_safety_policy import DurableRunnerSafetyPolicyResolver
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)
from tests import test_independent_venue_ledgers as ledgers
from tests import test_order_reservation as store_fixtures
from tests import test_portfolio_snapshot as portfolio
from tests import test_runner_policy_runtime as policies
from tests.cli import test_runner_safety_daemon_composition as daemon_fixtures
from tests.engines.nautilus import test_runner_safety_execution_boundary as gate_fixtures


def native_order():
    instrument = CurrencyPair(
        InstrumentId.from_str("BTC-USDT.OKX"),
        Symbol("BTC-USDT"),
        Currency.from_str("BTC"),
        Currency.from_str("USDT"),
        2,
        3,
        Price.from_str("0.01"),
        Quantity.from_str("0.001"),
        0,
        0,
    )
    order = LimitOrder(
        TraderId("TRADER-001"),
        StrategyId("REVIEW-001"),
        instrument.id,
        ClientOrderId("entry"),
        OrderSide.BUY,
        Quantity.from_str("1.000"),
        Price.from_str("100.00"),
        TimeInForce.GTC,
        False,
        False,
        False,
        UUID4(),
        0,
    )
    return instrument, order


def native_price_type():
    instrument, order = native_order()
    semantics = NautilusCachedOrderSemantics(SimpleNamespace(instrument=lambda _: instrument))
    assert instrument.notional_value(order.quantity, order.price).as_decimal() == 100
    try:
        semantics.order_notional(order)
    except TypeError as exc:
        assert "Price" in str(exc) and "Decimal" in str(exc)
    else:
        raise AssertionError("expected Decimal passed to native Price API")
    print(
        "RR-1: native order notional is 100; Custos semantics raises Decimal-is-not-Price TypeError"
    )


async def native_order_ids():
    _, order = native_order()
    gate = gate_fixtures._gate(gate_fixtures._boundary(gate_fixtures._Store([])))
    calls = []
    try:
        gate.modify_order(
            lambda *args: calls.append(args),
            order.client_order_id,
            quantity=Quantity.from_str("2.000"),
        )
    except AttributeError as exc:
        assert "client_order_id" in str(exc)
    else:
        raise AssertionError("expected native ClientOrderId rejected by wrapper")
    assert calls == []
    runtime = SimpleNamespace(
        cache=SimpleNamespace(positions_open=lambda: [], orders_open=lambda: [order])
    )
    try:
        await NtTradingNodeHost()._preserve_and_confirm_shutdown(
            "review", runtime, (Strategy(),), timeout_secs=1
        )
    except TypeError as exc:
        assert "ClientOrderId" in str(exc)
    else:
        raise AssertionError("expected native cancel_order to reject an Order object")
    print(
        "RR-2: native ClientOrderId cannot pass modify wrapper; preserve shutdown passes Order to native cancel_order and raises"
    )


async def startup_freezes_breaker():
    stop = asyncio.Event()

    class Host(NtTradingNodeHost):
        async def get_engine_status(self, instance):
            result = await super().get_engine_status(instance)
            stop.set()
            return result

    host = Host()
    runtime = portfolio._Runtime(mark_price=None, portfolio=portfolio._Portfolio(equities={}))
    runtime.cache.positions_open = lambda: []
    instance = str(daemon_fixtures.DEPLOYMENT_INSTANCE_ID)
    portfolio._register(host, instance, cache=runtime.cache, portfolio=runtime.portfolio)
    host._settlement_currencies[instance] = "USDT"
    host._runner_fact_contexts[instance] = (SimpleNamespace(deployment_instance_id=instance), None)
    boundary = await _build_runner_safety_boundary_factory(
        state_store=object(), safety_policy_resolver=daemon_fixtures._Resolver()
    )({"deployment_instance_id": instance, "trading_mode": "sandbox"})
    assert not host._active_nodes[instance].is_running
    await _run_signed_safety_supervision(
        stop, host=host, boundaries={instance: boundary}, interval_secs=0.01
    )
    assert boundary.fallback_breaker.frozen
    runtime.portfolio._equities = {"USDT": portfolio._DecimalValue("1000")}
    assert (await host.get_engine_status(instance)).reliable
    assert boundary.fallback_breaker.frozen
    print(
        "RR-4: pre-ready empty account freezes breaker; later reliable balance does not clear latch"
    )


async def policy_update_leaves_stale_boundary():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from custos.contracts.crucible_runner_safety_policy import RunnerAggregateCapPolicyRefV1

    with tempfile.TemporaryDirectory() as directory:
        store = store_fixtures._store(Path(directory) / "state.db")
        with store._outbox._connect() as db:
            db.execute("DELETE FROM runner_cap_policy_head")
            db.execute("DELETE FROM runner_cap_policy")
        key = Ed25519PrivateKey.generate()
        first = policies._verified_policy(key, max_total="150", expires_at="2099-01-01T00:00:00Z")
        await store.record_verified_runner_safety_policy(first)
        boundary = await _build_runner_safety_boundary_factory(
            state_store=store, safety_policy_resolver=DurableRunnerSafetyPolicyResolver(store)
        )({"deployment_instance_id": str(store_fixtures.INSTANCE_A), "trading_mode": "sandbox"})
        boundary.bind_runtime(semantics=gate_fixtures._Semantics())
        second = policies._verified_policy(
            key,
            revision=2,
            max_total="150",
            expires_at="2099-01-01T00:00:00Z",
            previous=RunnerAggregateCapPolicyRefV1(
                policy_id=first.policy.policy_id,
                revision=1,
                policy_digest=first.policy.policy_digest,
            ),
        )
        await store.record_verified_runner_safety_policy(second)
        try:
            boundary.before_submit_order(
                SimpleNamespace(
                    id="valid-new-order", order=gate_fixtures._order("new", notional="25")
                )
            )
        except RunnerStateAuthorityError as exc:
            assert "current effective" in str(exc)
        else:
            raise AssertionError("expected running boundary to retain old policy ID")
        store.reserve_order_notional_sync(
            event_id="current",
            deployment_instance_id=store_fixtures.INSTANCE_A,
            client_order_id="with-new-policy",
            policy_id=second.policy.policy_id,
            requested_notional=Decimal("25"),
        )
        print(
            "RR-5: valid new policy is stored; active boundary rejects affordable order with stale policy ID"
        )


def okx_double_commission():
    ledger = ledgers.okx()
    ledger._get = lambda *args, **kwargs: [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "7",
            "uTime": "150",
            "cTime": "110",
            "type": "2",
            "pnl": "12",
            "fee": "-2",
            "liqPenalty": "0",
            "ccy": "USDT",
        }
    ]
    rows = ledger._closed_position_pnl("BTC-USDT-SWAP", 100, 200)
    net = Decimal(rows[0]["amount"])
    assert net == 10
    consumer = (
        ROOT.parent / "crucible-rust/crates/store/src/runner_fact_reconciliation_projector.rs"
    ).read_text()
    assert "gross_realized_pnl - commission" in consumer
    assert net - Decimal("2") == 8
    print(
        "RR-6: OKX producer already returns net 10 for gross 12/fee 2; current consumer subtracts fee again to 8 (source arithmetic, no PG run)"
    )


def legacy_positions_have_no_lots():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "old.db"
        store = store_fixtures._store(path)
        store.reserve_order_notional_sync(
            event_id="reserve",
            deployment_instance_id=store_fixtures.INSTANCE_A,
            client_order_id="entry",
            policy_id=store_fixtures.POLICY_ID,
            requested_notional=Decimal("50"),
        )
        # Previous source stored quantity and cost but had no position-lot table.
        store.record_order_fill_sync(
            event_id="fill",
            deployment_instance_id=store_fixtures.INSTANCE_A,
            client_order_id="entry",
            fill_notional=Decimal("50"),
            fill_quantity=Decimal("1"),
        )
        with store._outbox._connect() as db:
            db.execute("DROP TABLE runner_position_exposure_lot")
        reopened = store_fixtures._store(path)
        try:
            reopened.record_position_reduction_fifo_sync(
                event_id="close",
                deployment_instance_id=store_fixtures.INSTANCE_A,
                position_id="P-from-runtime",
                reduction_notional=Decimal("50"),
                reduction_quantity=Decimal("1"),
            )
        except RunnerStateAuthorityError as exc:
            assert "position lot quantity" in str(exc)
        else:
            raise AssertionError("expected missing historical lot attribution")
        assert (
            reopened.load_order_reservation_sync(store_fixtures.INSTANCE_A, "entry").filled_exposure
            == 50
        )
        print(
            "RR-7: old database with filled position reopens successfully but cannot account its close; exposure remains 50"
        )


async def breaker_leaves_entry_orders():
    runtime = portfolio._Runtime(mark_price=portfolio._DecimalValue("100"))
    entries = [SimpleNamespace(client_order_id="resting-entry", is_reduce_only=False)]
    runtime.cache.orders_open = lambda: entries

    class StrategyProbe:
        def __init__(self):
            self.closed = []
            self.cancelled = []

        def close_all_positions(self, instrument):
            self.closed.append(instrument)

        def cancel_order(self, order):
            self.cancelled.append(order)

        def cancel_all_orders(self, instrument):
            self.cancelled.append(instrument)

    strategy = StrategyProbe()
    host = NtTradingNodeHost()
    portfolio._register(
        host, "instance", cache=runtime.cache, portfolio=runtime.portfolio, strategies=(strategy,)
    )
    host._settlement_currencies["instance"] = "USDT"
    await host.get_engine_status("instance")
    runtime.portfolio._equities = {"USDT": portfolio._DecimalValue("500")}
    breaker = gate_fixtures._breaker()
    tick = await EngineSafetySupervisor(engine=host, breaker=breaker).evaluate_once("instance")
    assert tick.verdict.tripped and strategy.closed and not strategy.cancelled
    assert entries and "instance" in host._active_nodes
    print(
        "RR-8: breaker trips and requests position closes, but leaves risk-increasing venue orders and strategy attached"
    )


def native_batch_modify_bypasses_frozen_boundary():
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.model import AccountType, Money, OmsType, QuoteTick, Venue

    from custos.engines.nautilus.runner_safety import install_order_gate

    instrument, _ = native_order()

    class Boundary(RunnerReservationBoundary):
        calls = 0

        def before_modify_order(self, command):
            self.calls += 1
            return super().before_modify_order(command)

    with tempfile.TemporaryDirectory() as directory:
        store = store_fixtures._store(Path(directory) / "batch.db")
        store.reserve_order_notional_sync(
            event_id="seed",
            deployment_instance_id=store_fixtures.INSTANCE_A,
            client_order_id="entry",
            policy_id=store_fixtures.POLICY_ID,
            requested_notional=Decimal("1"),
        )
        boundary = Boundary(
            store=store,
            deployment_instance_id=store_fixtures.INSTANCE_A,
            policy_id=store_fixtures.POLICY_ID,
            fallback_breaker=gate_fixtures._breaker(),
        )

        class Probe(Strategy):
            def __init__(self):
                super().__init__()
                self.changed = False

            def on_start(self):
                self.subscribe_quotes(instrument.id)
                self.order = LimitOrder(
                    TraderId("TRADER-001"),
                    StrategyId("Probe-000"),
                    instrument.id,
                    ClientOrderId("entry"),
                    OrderSide.BUY,
                    Quantity.from_str("1.000"),
                    Price.from_str("1.00"),
                    TimeInForce.GTC,
                    False,
                    False,
                    False,
                    UUID4(),
                    0,
                )
                # Seed a resting order, then freeze and install the actual runner gate.
                self.submit_order(self.order)
                boundary.fallback_breaker.fail_closed("review_trip")
                install_order_gate(
                    self, RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
                )

            def on_quote(self, event):
                if not self.changed and event.ts_event > 1_000_000_000:
                    self.changed = True
                    self.modify_orders(
                        [(self.order.client_order_id, Quantity.from_str("200.000"), None, None)]
                    )

        engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
        try:
            engine.add_venue(
                Venue("OKX"), OmsType.NETTING, AccountType.CASH, [Money.from_str("10000 USDT")]
            )
            engine.add_instrument(instrument)
            boundary.bind_runtime(semantics=NautilusCachedOrderSemantics(engine.cache))
            strategy = Probe()
            engine.add_strategy(strategy)
            engine.add_data(
                [
                    QuoteTick(
                        instrument.id,
                        Price.from_str("100.00"),
                        Price.from_str("101.00"),
                        Quantity.from_str("10.000"),
                        Quantity.from_str("10.000"),
                        n * 1_000_000_000,
                        n * 1_000_000_000,
                    )
                    for n in (1, 2, 3)
                ]
            )
            engine.run()
            order = engine.cache.order(ClientOrderId("entry"))
            assert order.quantity.as_decimal() == 200
            assert boundary.calls == 0 and boundary.fallback_breaker.frozen
            assert (
                store.load_order_reservation_sync(
                    store_fixtures.INSTANCE_A, "entry"
                ).reserved_notional
                == 1
            )
            print(
                "RR-3: native batch modify increases accepted order 1 -> 200 despite frozen breaker; boundary calls=0; durable reservation=1"
            )
        finally:
            engine.dispose()


def spot_fill_omits_fee_currency():
    from tests.engines.nautilus.test_binance_ledger_economic_rows import _spot_source

    fills, fees = _spot_source()._trade_rows(
        [
            {
                "symbol": "BTCUSDT",
                "id": 1,
                "orderId": 2,
                "isBuyer": True,
                "qty": "1",
                "price": "100",
                "commission": "0.001",
                "commissionAsset": "BTC",
                "time": 1786662060971,
            }
        ]
    )
    assert fills[0]["currency"] == "USDT" and fills[0]["fee"] == "0.001"
    assert "fee_currency" not in fills[0] and fees[0]["currency"] == "BTC"
    print(
        "RR-9: Binance fee row is BTC but its fill carries fee=0.001, currency=USDT and no fee_currency"
    )


async def main():
    native_price_type()
    await native_order_ids()
    gc.collect()
    await startup_freezes_breaker()
    await policy_update_leaves_stale_boundary()
    okx_double_commission()
    legacy_positions_have_no_lots()
    await breaker_leaves_entry_orders()
    spot_fill_omits_fee_currency()


if __name__ == "__main__":
    native_batch_modify_bypasses_frozen_boundary()
    gc.collect()
    asyncio.run(main())
