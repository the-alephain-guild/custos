"""Independent post-fix review probes; no real venue calls or application edits."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/toolkit")]
# ruff: noqa: E402 -- standalone review imports checkout fixtures.
from _strategy_harness import Harness
from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.coordinators import TradeEventHandler
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager
from nautilus_trader.model import OrderSide, OrderType, Price, Quantity

from custos.cli._daemon import _build_policy_renewal_notifier, _SupervisionStartups
from custos.core.engine_safety import EngineSafetySupervisor
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)


def plain_close_amendment_opens_reverse(amend=True):
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

    class Probe(Strategy):
        def __init__(self, config):
            super().__init__(config)
            self.n = 0
            self.store = MagicMock()
            self.breaker = FallbackBreaker(FallbackBreakerConfig(D("10"), D("10")))
            self.boundary = None
            self.gate = None
            self.close_order = None
            self.modified = False

        def on_start(self):
            self.subscribe_trades(instrument.id)

        def on_trade(self, tick):
            self.n += 1
            if self.n == 1:
                self.submit_order(
                    self.order_factory.market(
                        instrument_id=instrument.id,
                        order_side=OrderSide.BUY,
                        quantity=instrument.make_qty(1),
                    )
                )
            elif self.n == 2:
                self.breaker.fail_closed("review_frozen")
                self.boundary = RunnerReservationBoundary(
                    store=self.store,
                    deployment_instance_id=uuid4(),
                    policy_id=uuid4(),
                    fallback_breaker=self.breaker,
                    semantics=NautilusCachedOrderSemantics(self.cache),
                )
                self.gate = RunnerSafetyOrderGate(
                    boundary=self.boundary, client_order_id_len_limit=None
                )
                self.close_order = self.order_factory.limit(
                    instrument_id=instrument.id,
                    order_side=OrderSide.SELL,
                    quantity=instrument.make_qty(1),
                    price=instrument.make_price(150 if amend else 100),
                    reduce_only=False,
                )
                assert self.gate.submit_order(self.submit_order, self.close_order)
                if not amend:
                    second = self.order_factory.market(
                        instrument_id=instrument.id,
                        order_side=OrderSide.SELL,
                        quantity=instrument.make_qty(1),
                        reduce_only=False,
                    )
                    assert self.gate.submit_order(self.submit_order, second) is False
            elif self.n == 3 and amend:
                self.gate.modify_order(
                    self.modify_order,
                    self.close_order.client_order_id,
                    quantity=instrument.make_qty(2),
                    price=instrument.make_price(100),
                )
                self.modified = True

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
        strategy = Probe(StrategyConfig(strategy_id=StrategyId("Recheck-001")))
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
        for n in range(3):
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
        positions = engine.cache.positions_open()
        assert strategy.breaker.frozen and strategy.modified == amend
        if amend:
            assert (
                len(positions) == 1 and positions[0].is_short and D(str(positions[0].quantity)) == 1
            )
            assert D(str(engine.cache.order(strategy.close_order.client_order_id).quantity)) == 2
        else:
            assert not positions
        assert not strategy.store.mock_calls
    finally:
        engine.dispose()
    print(
        "FR-1: frozen gate accepts amendment of plain SELL1 close to SELL2; native matching opens SHORT1 without reservation"
        if amend
        else "CONTROL: prior EE-1 double-close defect is fixed; second plain close rejected and final position flat"
    )


def partial_entry_refill_has_no_tick_protection():
    monitor = TickMonitorManager(
        mode="hybrid", tp_method="trailing", trailing_activation_pct=D(".02"), trailing_pct=D(".01")
    )
    h = Harness(mode=SLTPMode.HYBRID, monitor=monitor)
    h.config.risk.trade.max_loss_pct = D(".05")
    h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100), D("2"))
    entry = h.order(OrderType.LIMIT, quantity=Quantity.from_str("1.000"), order_side=OrderSide.BUY)
    h.ctx.order_tracker.set_entry_order(entry.client_order_id, 1, order_quantity=D("1"))
    handler = TradeEventHandler(h)

    def fill():
        handler.handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=D(".5"),
                last_px=Price.from_str("100.00"),
            )
        )

    h.position.quantity = D(".5")
    fill()
    assert monitor._entry_price == 100
    h.positions.clear()
    handler.handle_position_closed(
        NS(
            instrument_id=h.instrument.id,
            realized_pnl=NS(as_decimal=lambda: D("-2")),
            ts_event=10**9,
        )
    )
    assert h.ctx.order_tracker.entry_order_id == entry.client_order_id
    h.positions.append(h.position)
    entry.is_open = False
    entry.is_closed = True
    fill()
    assert h.position.quantity == D(".5")
    assert monitor._entry_price is None
    assert monitor.check(D("120")) is None and monitor.check(D("115")) is None
    assert len(h.sent) == 2, "the repaired ownership still creates both safety SL lots"
    print(
        "FR-2: partial entry is stopped flat then fills again; safety SL exists but new position has no initialized tick/trailing monitor"
    )


def flat_containment_leaves_working_entry():
    async def exercise():
        host = NtTradingNodeHost()
        instance = "review-flat"
        order = NS(
            client_order_id="entry", is_reduce_only=False, instrument_id="BTCUSDT-PERP.BINANCE"
        )
        strategy = NS(cancel_order=MagicMock(), close_all_positions=MagicMock())
        host._active_nodes[instance] = NS(
            cache=NS(positions_open=lambda: [], orders_open=lambda: [order]), strategies=(strategy,)
        )
        await host.flatten_positions(instance, "risk_freeze")
        assert not strategy.cancel_order.called
        host._containment_confirmed.add(instance)
        await host.flatten_positions(instance, "risk_freeze")
        assert not strategy.cancel_order.called

    asyncio.run(exercise())
    print(
        "FR-3: containment with flat position and working entry never cancels; prior confirmed flag also bypasses the order check"
    )


def restart_skips_readiness_wait():
    async def exercise():
        startups = _SupervisionStartups(90)
        host = NS(
            deployment_ready=AsyncMock(side_effect=[True, False]),
            get_engine_status=AsyncMock(
                return_value=NS(reliable=False, unreliable_reason="new-node-account-not-ready")
            ),
            flatten_positions=AsyncMock(),
        )
        assert await startups.may_evaluate(host, "same-instance")
        # A fresh node now holds the same instance id. Its readiness would be False.
        assert await startups.may_evaluate(host, "same-instance")
        assert host.deployment_ready.await_count == 1
        breaker = FallbackBreaker(FallbackBreakerConfig(D("1000"), D("10")))
        await EngineSafetySupervisor(engine=host, breaker=breaker).evaluate_once("same-instance")
        assert breaker.frozen and host.flatten_positions.called

    asyncio.run(exercise())
    print(
        "FR-4: same-instance restart inherits evaluated flag, never checks new readiness and freezes on startup data gap"
    )


def policy_renewal_changes_other_mode():
    async def exercise():
        active_policy, other_policy = uuid4(), uuid4()
        active = RunnerReservationBoundary(
            store=MagicMock(),
            deployment_instance_id=uuid4(),
            policy_id=active_policy,
            fallback_breaker=FallbackBreaker(
                FallbackBreakerConfig(
                    D("10000"), D("10"), policy_id=active_policy, owner_policy=True
                )
            ),
        )
        cfg = FallbackBreakerConfig(D("100"), D("10"), policy_id=other_policy, owner_policy=True)
        resolver = NS(
            resolve=AsyncMock(
                return_value=NS(owner_policy=True, policy_id=other_policy, breaker=cfg)
            )
        )
        notify = _build_policy_renewal_notifier(
            boundaries={"testnet-instance": active}, safety_policy_resolver=resolver
        )
        await notify("sandbox")
        assert (
            active.policy_id == other_policy and active.fallback_breaker.config.max_notional == 100
        )
        assert active.fallback_breaker.evaluate(
            open_notional=D("500"), current_equity=D("1000")
        ).tripped

    asyncio.run(exercise())
    print(
        "FR-5: a sandbox policy renewal replaces the testnet boundary policy and lowers its cap10000->100; legitimate exposure500 trips"
    )


def transient_write_loses_freeze_permanently():
    durable = {}
    failed = False
    calls = 0

    def write(peak, frozen, reason):
        nonlocal failed, calls
        calls += 1
        if frozen and not failed:
            failed = True
            raise OSError("controlled transient write failure")
        durable.update(peak=peak, frozen=frozen)

    config = FallbackBreakerConfig(D("1000"), D("10"))
    breaker = FallbackBreaker(config, on_state_change=write)
    breaker.evaluate(open_notional=D("0"), current_equity=D("1000"))
    breaker.fail_closed("execution_accounting_error")
    for _ in range(3):
        breaker.fail_closed("execution_accounting_error")
        breaker.evaluate(open_notional=D("0"), current_equity=D("1000"))
    assert calls == 2 and durable["frozen"] is False and breaker.frozen
    restarted = FallbackBreaker(config)
    restarted.restore(peak_equity=durable["peak"], frozen=durable["frozen"])
    assert restarted.allows_new_orders()
    print(
        "FR-6: first freeze persistence fails once; later ticks never retry, and restored breaker permits new orders"
    )


def neutral_bars_do_not_sample_drawdown_peak():
    from custos_toolkit.risk.controller import RiskController
    from custos_toolkit.signals.types import SignalDirection
    from custos_toolkit_nautilus.adapter.coordinators import RiskControlCoordinator
    from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    h = Harness()
    h.equity = D("1100")
    h.signal = Signal(SignalDirection.NEUTRAL, price=D("110"))
    h._risk_controller = RiskController(
        {"max_drawdown": 0.05, "max_daily_loss": 0}, D("1000"), "compound"
    )
    h._get_risk_equity = lambda: h.equity
    h._last_risk_reason = ""
    h._risk_control_coordinator = RiskControlCoordinator(h)
    h._warmup_coordinator = NS(handle_warmup_gate=lambda *_: False)
    h._filter_coordinator = NS(
        handle_mtf_bar=lambda *_: False,
        update_global=MagicMock(),
        update_pair=MagicMock(),
        check_global=lambda *_: True,
        check_pair=lambda *_: True,
    )
    h._equity_provider = NS(is_risk_equity_reliable=lambda: True)
    h._signal_execution_coordinator = NS(
        execute_entry_for_pair=MagicMock(), manage_positions_for_pair=MagicMock()
    )
    h.calculate_signal = lambda *_: h.signal
    h.calculate_position_size = lambda *_: D("10")
    h._is_direction_allowed = lambda *_: True
    h._entry_gates_pass = lambda ctx, bar, direction: NautilusTradingStrategy._entry_gates_pass(
        h, ctx, bar, direction
    )
    h.on_pre_bar = MagicMock()
    h.on_post_bar = MagicMock()
    h._shutdown_position_policy = None
    h._paused = False
    h._reconciler = MagicMock()
    h._on_bar_risk_hygiene = lambda bar: NautilusTradingStrategy._on_bar_risk_hygiene(h, bar)
    h.on_core_bar = lambda bar: NautilusTradingStrategy._process_bar(h, bar)
    h._log_error = MagicMock()
    bar = NS(bar_type=NS(instrument_id=h.instrument.id), close=D("110"), ts_event=10**9)
    NautilusStrategyCore.on_bar(h, bar)
    assert h._risk_controller.peak_equity == 1000
    h.equity = D("1030")
    h.signal = Signal.enter_long(price=103)
    bar.close, bar.ts_event = D("103"), 2 * 10**9
    NautilusStrategyCore.on_bar(h, bar)
    assert not h._log_error.called
    assert h._signal_execution_coordinator.execute_entry_for_pair.call_count == 1
    assert h._risk_controller.peak_equity == 1030
    h._risk_controller.update_peak_equity(D("1100"))
    assert not h._risk_control_coordinator.check_risk_limits(3 * 10**9)
    print(
        "FR-7: real bar pipeline ignores equity1100 during NEUTRAL, then permits entry at1030 despite5% drawdown cap"
    )


if __name__ == "__main__":
    plain_close_amendment_opens_reverse(False)
    for probe in (
        plain_close_amendment_opens_reverse,
        partial_entry_refill_has_no_tick_protection,
        flat_containment_leaves_working_entry,
        restart_skips_readiness_wait,
        policy_renewal_changes_other_mode,
        transient_write_loses_freeze_permanently,
        neutral_bars_do_not_sample_drawdown_peak,
    ):
        probe()
