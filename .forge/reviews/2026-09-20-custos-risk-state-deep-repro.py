"""Offline review probes for risk state, contract units, and protective ownership.

Uses real Custos coordinators and native Nautilus value objects. Only the final
reversal probe uses the native backtest matching engine; other probes control
cache and transport outcomes explicitly. No venue or account network calls.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.config.risk import GlobalRiskConfig
from custos_toolkit_nautilus.adapter.coordinators import (
    OrderReconciler,
    RiskControlCoordinator,
    SignalExecutionCoordinator,
    SnapshotCoordinator,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.execution import ExecutionManager
from custos_toolkit_nautilus.adapter.orders import StopLossSubmitter, TakeProfitSubmitter
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy
from nautilus_trader.model import (
    CryptoPerpetual,
    Currency,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    Symbol,
)

from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
from custos.engines.nautilus.venue_okx import client_order_id_is_valid

_fixture_path = Path(__file__).with_name("2026-09-20-custos-strategy-deep-repro.py")
_spec = importlib.util.spec_from_file_location("strategy_review_fixture", _fixture_path)
assert _spec and _spec.loader
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
Harness = _fixture.Harness


def contract_reversal_oversizes():
    h = Harness()
    h.instrument = CryptoPerpetual(
        InstrumentId.from_str("BTC-USDT-SWAP.OKX"),
        Symbol("BTC-USDT-SWAP"),
        Currency.from_str("BTC"),
        Currency.from_str("USDT"),
        Currency.from_str("USDT"),
        False,
        2,
        0,
        Price.from_str("0.01"),
        Quantity.from_str("1"),
        0,
        0,
        multiplier=Quantity.from_str("0.01"),
    )
    h.ctx.instrument_id = h.instrument.id
    h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
    h.position.quantity = D("10")
    h.position.is_long, h.position.is_short = False, True
    SignalExecutionCoordinator(h).execute_entry_for_pair(
        h.ctx, Signal.enter_long(price=100), D("10"), NS(close=D("100"))
    )
    # Existing short: ten contracts * .01 BTC * $100 = $10.
    # Desired long: $10 = ten contracts. Correct reversal: buy twenty contracts.
    sent = D(str(h.sent[0].quantity))
    assert sent == 1010 and sent != 20
    assert (sent - 10) * D(".01") * 100 == 1000
    print("RS-1: reverse short10 to long10 contracts submits buy1010; target $10 becomes $1000")


def refused_protection_stays_counted():
    h = Harness(mode=SLTPMode.HYBRID)
    h.config.risk.trade.max_loss_pct = D(".05")
    # Factory creates an order object only. Native submit owns cache registration;
    # a gate refusal never reaches it and produces no OrderRejected callback.
    original_factory = h.order_factory.stop_market

    def create_uncached(**kw):
        order = original_factory(**kw)
        h.orders.pop(order.client_order_id)
        order.instrument_id = h.instrument.id
        return order

    h.order_factory.stop_market = create_uncached
    refused = []
    gate = RunnerSafetyOrderGate(
        boundary=MagicMock(),
        client_order_id_len_limit=None,
        client_order_id_validator=client_order_id_is_valid,
        on_refusal=refused.append,
    )
    h.submit_order = lambda order: gate.submit_order(h.sent.append, order)
    h._sltp_coordinator.submit_safety_stop_loss(h.ctx, Signal.enter_long(price=100))
    assert len(refused) == 1 and not h.sent and not h.orders
    reconciler = OrderReconciler(h)
    for _ in range(3):
        h.now_ns += 70_000_000_000
        reconciler.ensure_exchange_sl_protection(h.ctx)
    assert h.ctx.order_tracker.protected_quantity(exchange_managed=True) == 1
    assert len(refused) == 1 and not h.sent and not h.pause.called
    print(
        "RS-2: locally refused SL is counted as coverage1 forever; three repair passes do nothing"
    )


def limit_fill_stop_uses_signal_price():
    h = Harness(mode=SLTPMode.EXCHANGE)
    h.positions.clear()
    h.ctx.position_tracker.reset()
    h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
    h._order_calculator = OrderPriceCalculator(
        {"stop_loss": {"method": "fixed", "fixed": {"value": 0.02}}}
    )
    h.ctx.sl_submitter = StopLossSubmitter(h.order_factory, h.cache, h.log, h._order_calculator)
    h.ctx.tp_submitter = TakeProfitSubmitter(h.order_factory, h.cache, h.log, h._order_calculator)
    signal = Signal.enter_long(price=100)
    signal.order_type, signal.order_price_offset = "limit", D("10")
    SignalExecutionCoordinator(h).execute_entry_for_pair(
        h.ctx, signal, D("100"), NS(close=D("100"))
    )
    entry = h.sent[0]
    assert D(str(entry.price)) == 90
    entry.is_closed, entry.is_open = True, False
    h.position.avg_px_open = D("90")
    h.positions.append(h.position)
    TradeEventHandler(h).handle_order_filled(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=entry.client_order_id,
            order_side=OrderSide.BUY,
            last_qty=D("1"),
            last_px=Price.from_str("90.00"),
        )
    )
    stop = h.sent[1]
    assert D(str(stop.trigger_price)) == 98
    assert D(str(stop.trigger_price)) > h.position.avg_px_open
    print("RS-3: buy limit fills90; configured 2% stop is98 (already crossed), not88.2")


def risk_harness(**risk):
    h = NS(log=MagicMock(), _last_risk_reason="", equity=D("1000"))
    h.config = NS(
        position=NS(capital_mode="compound"), risk=NS(global_risk=GlobalRiskConfig(**risk))
    )
    h._get_effective_capital = lambda: h.equity
    h._get_risk_equity = lambda: h.equity
    coordinator = RiskControlCoordinator(h)
    coordinator.init_risk_controls()
    return h, coordinator


def drawdown_peak_never_samples_marks():
    h, coordinator = risk_harness(max_drawdown=0.05)
    h.equity = D("1100")
    assert coordinator.check_risk_limits(1_000_000_000)
    h.equity = D("1030")
    assert coordinator.check_risk_limits(2_000_000_000)
    assert h._risk_controller.peak_equity == 1000
    print("RS-4: observed equity1000->1100->1030 still permits entries at6.36% drawdown with5% cap")


def daily_rollover_erases_new_day_loss():
    h, coordinator = risk_harness(max_daily_loss=0.05, max_drawdown=0, consecutive_loss_pause=0)

    def ts(value):
        return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1e9)

    coordinator.check_risk_limits(ts("2026-09-19T23:59:00"))
    # A stop fills at 00:01, before the next entry signal causes check_limits.
    h.equity = D("940")
    h._risk_controller.record_trade(D("-60"))
    assert h._risk_controller.session_pnl == -60
    assert coordinator.check_risk_limits(ts("2026-09-20T00:02:00"))
    assert h._risk_controller.session_pnl == 0
    print("RS-5: new-day loss60 (6%) is erased on first entry check; daily5% cap permits entry")


def restart_resets_risk_budget():
    h, coordinator = risk_harness(max_daily_loss=0.05, max_drawdown=0, consecutive_loss_pause=0)
    h._risk_controller.record_trade(D("-100"))
    assert not coordinator.check_risk_limits(1_000_000_000)
    h._contexts = {}
    h.clock = NS(timestamp_ns=lambda: 2_000_000_000)
    h.get_snapshot_state = lambda: NautilusTradingStrategy.get_snapshot_state(h)
    state = SnapshotCoordinator(h).save_state()
    loaded, restarted = risk_harness(max_daily_loss=0.05, max_drawdown=0, consecutive_loss_pause=0)
    loaded._contexts = {}
    loaded._get_warmup_config = lambda: NS(mode="snapshot")
    loaded.get_snapshot_indicators = lambda: {}
    loaded.restore_from_snapshot = lambda snapshot: NautilusTradingStrategy.restore_from_snapshot(
        loaded, snapshot
    )
    SnapshotCoordinator(loaded).load_state(state)
    SnapshotCoordinator(loaded).apply_loaded_snapshot()
    assert restarted.check_risk_limits(3_000_000_000)
    assert loaded._risk_controller.session_pnl == 0
    print(
        "RS-6: save/load on same day turns daily-loss-blocked controller into an allowed controller"
    )


def native_reversal_resets_new_tick_protection():
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import (
        AccountType,
        AggressorSide,
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

    class Probe(Strategy):
        def __init__(self, config):
            super().__init__(config)
            self.n = 0
            self.h = Harness(
                monitor=TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=D(".04"))
            )
            self.events = []

        def on_start(self):
            self.h.cache = self.cache
            self.h.ctx.instrument_id = instrument.id
            self.subscribe_trades(instrument.id)

        def on_trade(self, tick):
            self.n += 1
            if self.n not in (1, 2):
                return
            side = OrderSide.BUY if self.n == 1 else OrderSide.SELL
            quantity = instrument.make_qty(1 if self.n == 1 else 2)
            order = self.order_factory.market(
                instrument_id=instrument.id, order_side=side, quantity=quantity
            )
            signal = Signal.enter_long(price=100) if self.n == 1 else Signal.enter_short(price=100)
            ctx = self.h.ctx
            ctx.pending_entry_is_reversal = self.n == 2
            ctx.position_tracker.set_pending_signal(signal, D("2"))
            ctx.order_tracker.set_entry_order(
                order.client_order_id,
                1 if self.n == 1 else -1,
                exposure_offset_quantity=D("0") if self.n == 1 else D("1"),
            )
            self.submit_order(order)

        def on_order_filled(self, event):
            TradeEventHandler(self.h).handle_order_filled(event)
            self.events.append(("fill", self.h.ctx.tick_monitor._entry_price))

        def on_position_closed(self, event):
            TradeEventHandler(self.h).handle_position_closed(event)
            self.events.append(("close", self.h.ctx.tick_monitor._entry_price))

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
        strategy = Probe(StrategyConfig(strategy_id=StrategyId("Review-001")))
        engine.add_strategy(strategy)
        ticks = [
            QuoteTick(
                instrument.id,
                instrument.make_price(100),
                instrument.make_price(100),
                instrument.make_qty(20),
                instrument.make_qty(20),
                1_000_000_000,
                1_000_000_000,
            )
        ]
        for n in range(3):
            ts = (2 + n) * 1_000_000_000
            ticks.append(
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
        engine.add_data(ticks)
        engine.run()
        assert strategy.events == [("fill", D("100")), ("fill", D("100")), ("close", None)]
        position = engine.cache.positions_open()[0]
        assert position.is_short and D(str(position.quantity)) == 1
        assert strategy.h.ctx.tick_monitor.check(D("90")) is None
        print(
            "RS-7: native reversal opens short1; old PositionClosed resets newly initialized tick protection"
        )
    finally:
        engine.dispose()


def signed_breaker_restart_forgets_trip():
    import asyncio
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from custos.cli._daemon import _build_runner_safety_boundary_factory
    from custos.core.fallback_breaker import FallbackBreakerConfig

    async def exercise():
        policy_id = uuid4()
        config = FallbackBreakerConfig(D("10000"), D("10"), policy_id=policy_id, owner_policy=True)
        resolver = NS(
            resolve=AsyncMock(
                return_value=NS(owner_policy=True, policy_id=policy_id, breaker=config)
            )
        )
        store = MagicMock()
        spec = {"trading_mode": "live", "deployment_instance_id": str(uuid4())}
        build = _build_runner_safety_boundary_factory(
            state_store=store, safety_policy_resolver=resolver, boundaries={}
        )
        before = await build(spec)
        before.fallback_breaker.evaluate(open_notional=D("0"), current_equity=D("1000"))
        before.fallback_breaker.evaluate(open_notional=D("0"), current_equity=D("800"))
        assert before.fallback_breaker.frozen
        # Same process replacement retains the trip.
        assert (await build(spec)).fallback_breaker.frozen
        # A daemon restart creates a new registry but reuses the durable store.
        rebuild = _build_runner_safety_boundary_factory(
            state_store=store, safety_policy_resolver=resolver, boundaries={}
        )
        after = await rebuild(spec)
        verdict = after.fallback_breaker.evaluate(open_notional=D("0"), current_equity=D("800"))
        assert after.fallback_breaker.allows_new_orders() and not verdict.tripped
        assert not store.mock_calls

    asyncio.run(exercise())
    print(
        "RS-6b: signed boundary factory retains breaker on replacement, but daemon restart clears20% drawdown trip"
    )


def partial_entry_survives_close_without_ownership():
    h = Harness(mode=SLTPMode.HYBRID)
    h.config.risk.trade.max_loss_pct = D(".05")
    h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100), D("2"))
    entry = h.order(OrderType.LIMIT, quantity=Quantity.from_str("1.000"), order_side=OrderSide.BUY)
    h.ctx.order_tracker.set_entry_order(entry.client_order_id, 1)
    h.ctx.order_tracker.record_entry_fill(D(".5"))
    h.positions.clear()
    TradeEventHandler(h).handle_position_closed(
        NS(instrument_id=h.instrument.id, realized_pnl=NS(as_decimal=lambda: D("-2")))
    )
    assert entry.is_open and entry.client_order_id not in h.cancelled
    assert h.ctx.order_tracker.entry_order_id is None
    h.position.quantity = D(".5")
    h.positions.append(h.position)
    entry.is_open, entry.is_closed = False, True
    TradeEventHandler(h).handle_order_filled(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=entry.client_order_id,
            order_side=OrderSide.BUY,
            last_qty=D(".5"),
            last_px=Price.from_str("100.00"),
        )
    )
    assert not h.sent
    print(
        "RS-7b: SL closes partial position; remaining entry stays open but is untracked, then fills without protection"
    )


def rejected_stop_pauses_protective_ticks():
    from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore

    monitor = TickMonitorManager(
        mode="hybrid", tp_method="trailing", trailing_activation_pct=D(".02"), trailing_pct=D(".01")
    )
    monitor.init_position(D("100"), True)
    monitor.check(D("120"))
    h = Harness(mode=SLTPMode.HYBRID, monitor=monitor)
    h.config.risk.trade.max_loss_pct = D(".05")
    h._paused = False
    h.pause = lambda: NautilusStrategyCore.pause(h)
    h.on_core_trade_tick = MagicMock()
    h._log_error = MagicMock()
    stop = h.order(
        OrderType.STOP_MARKET,
        quantity=D("1"),
        order_side=OrderSide.SELL,
        reduce_only=True,
        trigger_price=Price.from_str("95.00"),
    )
    h.ctx.order_tracker.add_exchange_sl_order(stop.client_order_id, D("1"))
    stop.is_open, stop.is_closed = False, True
    reconciler = OrderReconciler(h)
    reconciler.handle_order_rejected(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=stop.client_order_id,
            reason="temporary venue rejection",
        )
    )
    assert h._paused
    reconciler.ensure_exchange_sl_protection(h.ctx)
    assert len(h.sent) == 1 and h._paused
    NautilusStrategyCore.on_trade(
        h, NS(instrument_id=h.instrument.id, price=Price.from_str("115.00"))
    )
    assert not h.on_core_trade_tick.called
    assert monitor.check(D("115")) is not None
    print(
        "RS-8: a stop rejection soft-pauses all ticks; repaired safety SL does not restore trailing exits"
    )


def rolling_supertrend_invents_reversal():
    import pandas as pd
    from custos_toolkit_nautilus.adapter.indicators._pandas_ta import ta
    from custos_toolkit_nautilus.adapter.indicators.supertrend import SuperTrend

    closes = [200.0] * 15 + [200.0 - 5 * n for n in range(1, 21)] + [100.0] * 100
    highs, lows = [c + 1 for c in closes], [c - 1 for c in closes]
    indicator = SuperTrend(length=10, multiplier=3.0)
    states = []
    for high, low, close in zip(highs, lows, closes, strict=True):
        indicator.update_raw(high, low, close)
        states.append(indicator.trend)
    reference = ta.supertrend(
        pd.Series(highs), pd.Series(lows), pd.Series(closes), length=10, multiplier=3.0
    )
    assert states[79] == -1 and states[80] == 1
    assert closes[34:] == [100.0] * len(closes[34:])
    assert reference["SUPERTd_10_3.0"].iloc[80] == -1
    assert indicator.trend == 1 and reference["SUPERTd_10_3.0"].iloc[-1] == -1
    print(
        "RS-9: flat price100 falsely flips SuperTrend short->long at index80; same full-history algorithm stays short"
    )


if __name__ == "__main__":
    for probe in (
        contract_reversal_oversizes,
        refused_protection_stays_counted,
        limit_fill_stop_uses_signal_price,
        drawdown_peak_never_samples_marks,
        daily_rollover_erases_new_day_loss,
        restart_resets_risk_budget,
        native_reversal_resets_new_tick_protection,
        signed_breaker_restart_forgets_trip,
        partial_entry_survives_close_without_ownership,
        rejected_stop_pauses_protective_ticks,
        rolling_supertrend_invents_reversal,
    ):
        probe()
