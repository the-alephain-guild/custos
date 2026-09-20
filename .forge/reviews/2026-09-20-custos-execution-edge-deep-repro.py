"""Offline execution-edge audit. Assertions describe defects, not desired behavior.

Transport and policy fixtures use generated test identities only. Native engine
probes use simulated matching. No real accounts, secrets, or venue requests.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/toolkit")]

# ruff: noqa: E402 -- standalone review entry point imports repository test fixtures.
from _strategy_harness import Harness, scaled_monitor
from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
from custos_toolkit_nautilus.adapter.coordinators import (
    SignalExecutionCoordinator,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from nautilus_trader.model import OrderSide, OrderType, Price, Quantity

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import RunnerFactOutbox, RunnerStateAuthorityError, heartbeat
from custos.core.runner_fact_producer import RunnerFactEventBridge
from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
    install_order_gate,
)
from tests.test_order_reservation import INSTANCE_A, POLICY_ID, _store
from tests.test_runner_fact_outbox import _authority, _identity, _publisher
from tests.test_strategy_signal_bridge import (
    OrderFilled,
    OrderInitialized,
    _deployment,
    _Emitter,
)


def duplicate_plain_closes_open_reverse_while_frozen():
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import (
        AccountType,
        AggressorSide,
        Currency,
        Money,
        OmsType,
        QuoteTick,
        StrategyId,
        TradeId,
        TradeTick,
        Venue,
    )
    from nautilus_trader.testkit.providers import TestInstrumentProvider
    from nautilus_trader.trading import Strategy, StrategyConfig

    instrument = TestInstrumentProvider.btcusdt_perp_binance()

    def run(reduce_only, native_close=False):
        class Probe(Strategy):
            def __init__(self, config):
                super().__init__(config)
                self.count = 0
                self.decisions = []
                self.store = MagicMock()

            def on_start(self):
                self.subscribe_trades(instrument.id)

            def on_trade(self, tick):
                self.count += 1
                if self.count == 1:
                    # Establish the pre-existing exposure before the containment test.
                    self.submit_order(
                        self.order_factory.market(
                            instrument_id=instrument.id,
                            order_side=OrderSide.BUY,
                            quantity=instrument.make_qty(1),
                        )
                    )
                elif self.count == 2:
                    breaker = FallbackBreaker(FallbackBreakerConfig(D("1"), D("10")))
                    breaker.fail_closed("review_freeze")
                    boundary = RunnerReservationBoundary(
                        store=self.store,
                        deployment_instance_id=uuid4(),
                        policy_id=uuid4(),
                        fallback_breaker=breaker,
                        semantics=NautilusCachedOrderSemantics(self.cache),
                    )
                    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
                    if native_close:
                        install_order_gate(self, gate)
                    for _ in range(2):
                        if native_close:
                            self.close_position(
                                self.cache.positions_open()[0], reduce_only=reduce_only
                            )
                            continue
                        order = self.order_factory.market(
                            instrument_id=instrument.id,
                            order_side=OrderSide.SELL,
                            quantity=instrument.make_qty(1),
                            reduce_only=reduce_only,
                        )
                        self.decisions.append(gate.submit_order(self.submit_order, order))

        engine = BacktestEngine(
            BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True), run_analysis=False)
        )
        try:
            engine.add_venue(
                Venue("BINANCE"),
                OmsType.NETTING,
                AccountType.MARGIN,
                [Money.from_str("100000 USDT")],
                base_currency=Currency.from_str("USDT"),
            )
            engine.add_instrument(instrument)
            strategy = Probe(StrategyConfig(strategy_id=StrategyId("EdgeReview-001")))
            engine.add_strategy(strategy)
            data = [
                QuoteTick(
                    instrument.id,
                    instrument.make_price(100),
                    instrument.make_price(100),
                    instrument.make_qty(20),
                    instrument.make_qty(20),
                    10**9,
                    10**9,
                )
            ]
            for n in range(2):
                ts = (n + 2) * 10**9
                data.append(
                    TradeTick(
                        instrument.id,
                        instrument.make_price(100),
                        instrument.make_qty(20),
                        AggressorSide.BUY,
                        TradeId(str(n)),
                        ts,
                        ts,
                    )
                )
            engine.add_data(data)
            engine.run()
            positions = [(str(p.side), D(str(p.quantity))) for p in engine.cache.positions_open()]
            assert strategy.decisions == ([] if native_close else [True, True])
            assert not strategy.store.mock_calls
            return positions
        finally:
            engine.dispose()

    assert run(False) == [("SHORT", D("1"))]
    assert run(True) == []
    assert run(False, native_close=True) == [("SHORT", D("1"))]
    print(
        "EE-1: frozen real gate allows two plain closes of long1; native matching opens short1; native close_position also opens short1. Reduce-only control stays flat"
    )


def refused_reversal_cancels_old_protection():
    h = Harness(mode=SLTPMode.HYBRID)
    h._capital_allocator = CapitalAllocator(
        AllocationConfig(mode="equal", tiers={}), D("100"), h.cache
    )
    h._capital_allocator.register_pair(h.ctx.pair, h.instrument.id)
    assert h._capital_allocator.allocate(h.ctx.pair, D("100"))
    h.ctx.allocated_capital = D("100")
    stop = h.order(
        OrderType.STOP_MARKET,
        quantity=Quantity.from_str("1.000"),
        order_side=OrderSide.SELL,
        reduce_only=True,
        trigger_price=Price.from_str("95.00"),
    )
    h.ctx.order_tracker.add_exchange_sl_order(stop.client_order_id, D("1"))
    SignalExecutionCoordinator(h).execute_entry_for_pair(
        h.ctx, Signal.enter_short(price=100), D("100"), NS(close=D("100"))
    )
    assert not h.sent and stop.client_order_id in h.cancelled
    assert h.positions == [h.position] and h.position.is_long
    assert h.ctx.order_tracker.protected_quantity(exchange_managed=True) == 0
    # Confirm the already-issued cancellation, as a venue would later report it.
    stop.is_open, stop.is_closed = False, True
    assert not [o for o in h.orders.values() if o.is_open and o.is_reduce_only]
    print(
        "EE-2: capital-refused reversal dispatches no entry, but cancels the old long's only SL and clears coverage"
    )


def new_bridge_drops_existing_order_fill():
    deployment = _deployment()
    original_emitter = _Emitter()
    original = RunnerFactEventBridge(emitter=original_emitter, deployment=deployment)
    original._on_order_event(OrderInitialized())
    original._on_order_event(OrderFilled("supertrend-entry-1", last_qty="0.003"))
    assert len(original_emitter.fact_batches) == 1
    replacement_emitter = _Emitter()
    replacement = RunnerFactEventBridge(emitter=replacement_emitter, deployment=deployment)
    forwarder = NS(add_order_sink=MagicMock(), add_position_sink=MagicMock())
    replacement.bootstrap(forwarder)
    late_fill = OrderFilled(
        "supertrend-entry-1", trade_id="trade-2", event_id=str(uuid4()), last_qty="0.004"
    )
    replacement._on_order_event(late_fill)
    assert not replacement_emitter.fact_batches
    original._on_order_event(late_fill)
    assert len(original_emitter.fact_batches) == 2
    print(
        "EE-3: replacement bridge drops a previously initialized order's remaining fill; original bridge emits the same event"
    )


def blocked_stream_starves_other_streams():
    async def exercise(directory):
        outbox = RunnerFactOutbox(directory / "queue.sqlite3")
        identity = _identity()
        earlier, later = _authority(), replace(_authority(), trading_mode="testnet")
        assert earlier.stream_key < later.stream_key
        for _ in range(64):
            await outbox.enqueue(
                earlier,
                identity,
                [heartbeat(event_id=uuid4(), status="online", observed_at="2026-09-20T00:00:00Z")],
            )
        healthy_id = await outbox.enqueue(
            later,
            identity,
            [heartbeat(event_id=uuid4(), status="online", observed_at="2026-09-20T00:00:00Z")],
        )

        class Broker:
            def __init__(self):
                self.generations = []

            async def publish(self, subject, payload, **kwargs):
                generation = json.loads(payload)["trading_mode"]
                self.generations.append(generation)
                if generation == earlier.trading_mode:
                    raise RuntimeError("old stream temporarily unavailable")
                return NS(stream="RUNNER_FACTS", seq=41, duplicate=False, domain=None)

        broker = Broker()

        def publisher_for(queue):
            publisher = _publisher(queue, broker)
            publisher._connection_profiles["testnet"] = publisher._connection_profiles["sandbox"]
            publisher._nats["testnet"] = publisher._nats["sandbox"]
            publisher._jetstreams["testnet"] = broker
            return publisher

        publisher = publisher_for(outbox)
        for _ in range(3):
            assert await publisher.drain_once() == 0
        assert broker.generations == [earlier.trading_mode] * 3
        pending = await outbox.pending(limit=100)
        assert len(pending) == 65
        assert next(b for b in pending if b.batch_id == healthy_id).attempts == 0
        assert await outbox.publication_receipt(healthy_id) is None
        # Control: the exact same later stream works when not hidden by the first page.
        control = RunnerFactOutbox(directory / "control.sqlite3")
        await control.enqueue(
            later,
            identity,
            [heartbeat(event_id=uuid4(), status="online", observed_at="2026-09-20T00:00:00Z")],
        )
        assert await publisher_for(control).drain_once() == 1

    with TemporaryDirectory(prefix="custos-edge-queue-") as tmp:
        asyncio.run(exercise(Path(tmp)))
    print(
        "EE-4: 64 queued rows in failing stream1 hide healthy stream2 forever; three drains never attempt stream2"
    )


def first_partial_fill_fixes_tp_base_too_small():
    h = Harness(monitor=scaled_monitor())
    h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100), D("2"))
    entry = h.order(OrderType.LIMIT, quantity=Quantity.from_str("1.000"), order_side=OrderSide.BUY)
    h.ctx.order_tracker.set_entry_order(entry.client_order_id, 1, order_quantity=D("1"))
    handler = TradeEventHandler(h)
    for quantity, terminal in ((D(".5"), False), (D("1"), True)):
        h.position.quantity = quantity
        entry.is_open, entry.is_closed = not terminal, terminal
        handler.handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=D(".5"),
                last_px=Price.from_str("100.00"),
            )
        )
    assert h.ctx.tick_monitor.initial_quantity == D(".5") and h.position.quantity == 1
    amounts = []
    for price in ("103", "105"):
        h.tick(price)
        take = h.sent[-1]
        amount = D(str(take.quantity))
        amounts.append(amount)
        h.position.quantity -= amount
        take.is_open, take.is_closed = False, True
        handler.handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=take.client_order_id,
                order_side=OrderSide.SELL,
                last_qty=amount,
                last_px=Price.from_str(price),
            )
        )
    assert amounts == [D(".25"), D(".25")]
    assert h.position.quantity == D(".5") and h.ctx.tick_monitor.check(D("110")) is None
    print(
        "EE-5: two entry fills .5+.5 seed TP base .5; two 50% tiers sell only .25+.25, leaving half the position"
    )


def improved_fill_price_leaks_runner_reservations():
    with TemporaryDirectory(prefix="custos-edge-reservation-") as tmp:
        store = _store(Path(tmp) / "state.sqlite3")
        for n in range(6):
            order_id = f"entry-{n}"
            store.reserve_order_notional_sync(
                event_id=f"reserve-{n}",
                deployment_instance_id=INSTANCE_A,
                client_order_id=order_id,
                policy_id=POLICY_ID,
                requested_notional=D("100"),
            )
            filled = store.record_order_fill_sync(
                event_id=f"fill-{n}",
                deployment_instance_id=INSTANCE_A,
                client_order_id=order_id,
                fill_notional=D("90"),
                fill_quantity=D("1"),
                position_id=f"position-{n}",
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="buy",
            )
            assert filled.reserved_notional == 10
            closed = store.record_position_reduction_fifo_sync(
                event_id=f"close-{n}",
                deployment_instance_id=INSTANCE_A,
                position_id=f"position-{n}",
                reduction_notional=D("90"),
                reduction_quantity=D("1"),
            )
            assert closed.filled_quantity == 0 and closed.reserved_notional == 10
        exposure = asyncio.run(store.load_runner_exposure(POLICY_ID))
        assert exposure.open_exposure == 0 and exposure.reserved_notional == 60
        try:
            store.reserve_order_notional_sync(
                event_id="next-reserve",
                deployment_instance_id=INSTANCE_A,
                client_order_id="next",
                policy_id=POLICY_ID,
                requested_notional=D("100"),
            )
        except RunnerStateAuthorityError as exc:
            assert "aggregate cap" in str(exc)
        else:
            raise AssertionError("expected the leaked reservation to block the next order")
    print(
        "EE-6: six fully filled/closed orders leave60 reserved from price improvement; flat account cannot submit100 under150 cap"
    )


if __name__ == "__main__":
    for probe in (
        duplicate_plain_closes_open_reverse_while_frozen,
        refused_reversal_cancels_old_protection,
        new_bridge_drops_existing_order_fill,
        blocked_stream_starves_other_streams,
        first_partial_fill_fixes_tp_base_too_small,
        improved_fill_price_leaks_runner_reservations,
    ):
        probe()
