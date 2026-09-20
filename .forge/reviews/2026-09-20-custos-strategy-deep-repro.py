"""Strategy-coordinator review probes; no real venue calls or application edits."""

from __future__ import annotations

import sys

# ruff: noqa: E402 -- allow a standalone script to import checkout packages.
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custos_toolkit.position.tracker import PositionTracker
from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit.signals.types import Signal, SignalDirection
from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
from custos_toolkit_nautilus.adapter.config.risk import (
    ScaledTakeProfitConfig,
    ScaledTakeProfitLevelConfig,
)
from custos_toolkit_nautilus.adapter.coordinators import (
    ExecutionCoordinator,
    OrderReconciler,
    SignalExecutionCoordinator,
    SLTPCoordinator,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.orders import (
    OrderTracker,
    StopLossSubmitter,
    TakeProfitSubmitter,
)
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager
from nautilus_trader.model import (
    Currency,
    CurrencyPair,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    Symbol,
)


class Harness:
    def __init__(self, mode=SLTPMode.TICK, monitor=None):
        self.instrument = CurrencyPair(
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
        self.position = NS(
            quantity=D("1"),
            avg_px_open=D("100"),
            is_long=True,
            is_short=False,
            is_closed=False,
            side="LONG",
            instrument_id=self.instrument.id,
        )
        self.positions = [self.position]
        self.orders = {}
        self.sent = []
        self.cancelled = []
        self.closed = []
        self.now_ns = 100_000_000_000
        self.log = MagicMock()
        self.cache = NS(
            instrument=lambda _: self.instrument,
            positions_open=lambda **kw: self.positions,
            orders_open=lambda **kw: [o for o in self.orders.values() if o.is_open],
            order=lambda oid: self.orders.get(oid),
            bars=lambda _: [NS(close=D("105"))],
        )
        self.order_factory = NS(
            market=lambda **kw: self.order(OrderType.MARKET, **kw),
            stop_market=lambda **kw: self.order(OrderType.STOP_MARKET, **kw),
            limit=lambda **kw: self.order(OrderType.LIMIT, **kw),
        )
        self.clock = NS(timestamp_ns=lambda: self.now_ns)
        self._mode = mode
        self._order_signal_map = {}
        self._capital_allocator = None
        self._risk_controller = MagicMock()
        self.config = NS(
            position=NS(limits=NS(max_total_positions=0), capital_mode="fixed_capital"),
            trading=NS(order_type="market"),
            risk=NS(trade=NS(take_profit=NS(method="none", scaled=None))),
        )
        tracker = PositionTracker()
        tracker.record_entry(D("100"), D("1"))
        self.ctx = NS(
            pair="BTC-USDT",
            instrument_id=self.instrument.id,
            bar_type="fixture",
            order_tracker=OrderTracker(),
            position_tracker=tracker,
            tick_monitor=monitor,
            indicators={"atr": NS(initialized=True, value=D("2"))},
            allocated_capital=D("0"),
            active_signal_id=None,
            pending_entry_is_reversal=False,
            sl_tp_submitted_for_reversal=False,
            break_even_applied=False,
            stale_cancel_attempts={},
            exchange_sl_rebuild_deadline_ns=0,
            native_trailing_rebuild_deadline_ns=0,
        )
        self.ctx.execution_manager = NS(create_entry_order=self.entry_order)
        self._contexts = {self.instrument.id: self.ctx}
        self._get_context_from_instrument = lambda _: self.ctx
        self._order_calculator = OrderPriceCalculator(
            {"stop_loss": {"method": "atr", "atr": {"multiplier": 2}}}
        )
        self.ctx.sl_submitter = StopLossSubmitter(
            self.order_factory, self.cache, self.log, self._order_calculator
        )
        self._sltp_coordinator = SLTPCoordinator(self)
        self.pause = MagicMock()
        self.on_trade_closed = MagicMock()

    def order(self, kind, **kw):
        oid = f"O-{len(self.orders) + 1}"
        order = NS(
            client_order_id=oid,
            order_type=kind,
            quantity=kw["quantity"],
            side=kw["order_side"],
            is_reduce_only=kw.get("reduce_only", False),
            is_open=True,
            is_closed=False,
            ts_init=0,
            trigger_price=kw.get("trigger_price"),
            price=kw.get("price"),
            tags=kw.get("tags"),
        )
        self.orders[oid] = order
        return order

    def entry_order(self, **kw):
        return self.order(
            OrderType.LIMIT,
            instrument_id=kw["instrument_id"],
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(kw["size"] / D(str(kw["bar"].close))),
            price=kw["bar"].close,
            reduce_only=False,
        )

    def submit_order(self, order):
        self.sent.append(order)

    def cancel_order(self, order):
        self.cancelled.append(order.client_order_id)

    def cancel_all_orders(self, *args):
        self.cancelled.extend(self.orders)

    def close_position(self, *args, **kw):
        self.closed.append((args, kw))

    def tick(self, price):
        ExecutionCoordinator(self).handle_trade_tick(
            NS(instrument_id=self.instrument.id, price=D(str(price)))
        )


def scaled_monitor(levels=2):
    return TickMonitorManager(
        mode="tick",
        tp_method="scaled",
        tp_levels=[
            {"target_pct": D(".02"), "exit_pct": D(".5")},
            {"target_pct": D(".04"), "exit_pct": D(".5")},
        ][:levels],
    )


def scaled_quantities_leave_residual():
    monitor = scaled_monitor()
    monitor.init_position(D("100"), True)
    h = Harness(monitor=monitor)

    def fill(order):
        h.sent.append(order)
        h.position.quantity -= D(str(order.quantity))

    h.submit_order = fill
    h.tick("103")
    h.tick("105")
    h.tick("110")
    assert [D(str(o.quantity)) for o in h.sent] == [D(".5"), D(".25")]
    assert h.position.quantity == D(".25") and monitor.check(D("110")) is None
    config = ScaledTakeProfitConfig(
        levels=2,
        level_1=ScaledTakeProfitLevelConfig(target_pct=0.02, exit_pct=0.5),
        level_2=ScaledTakeProfitLevelConfig(target_pct=0.04, exit_pct=0.5),
    )
    h.position.quantity = D("1")
    submitter = TakeProfitSubmitter(h.order_factory, h.cache, h.log, h._order_calculator)
    orders = submitter.create_scaled_orders(
        h.instrument.id, Signal.enter_long(price=100.0), D("100"), h.position, config
    )
    assert [D(str(o.quantity)) for o in orders] == [D(".5"), D(".5")]
    print(
        "ST-1: identical 50%+50% config exits .5+.25 in tick mode (residual .25), versus .5+.5 in exchange mode"
    )


def scaled_levels_spent_without_dispatch():
    monitor = scaled_monitor(1)
    monitor.init_position(D("100"), True)
    h = Harness(monitor=monitor)
    attempts = []

    def reject(order):
        attempts.append(order)
        return False  # Current gate protocol: refused before dispatch.

    h.submit_order = reject
    try:
        h.tick("103")
    except RuntimeError:
        pass
    h.tick("103")
    assert len(attempts) == 1 and h.position.quantity == 1 and monitor._tp_levels_hit == [True]
    recovered = Harness(monitor=scaled_monitor(1))
    OrderReconciler(recovered).recover_from_existing_positions()
    recovered.tick("105")
    assert recovered.sent == [] and recovered.ctx.tick_monitor._tp_levels_hit == [True]
    hybrid_monitor = scaled_monitor(1)
    hybrid_monitor.init_position(D("100"), True)
    hybrid = Harness(mode=SLTPMode.HYBRID, monitor=hybrid_monitor)
    protection = hybrid.order(
        OrderType.STOP_MARKET,
        quantity=hybrid.instrument.make_qty(D("1")),
        order_side=OrderSide.SELL,
        reduce_only=True,
        trigger_price=Price.from_str("90.00"),
    )
    hybrid.ctx.order_tracker.add_exchange_sl_order(protection.client_order_id, D("1"))
    hybrid.tick("103")
    partial = hybrid.sent[-1]
    assert partial.client_order_id not in hybrid.ctx.order_tracker.tp_order_ids
    OrderReconciler(hybrid).handle_order_rejected(
        NS(
            instrument_id=hybrid.instrument.id,
            client_order_id=partial.client_order_id,
            reason="-2022 ReduceOnly Order is rejected.",
        )
    )
    assert protection.client_order_id in hybrid.cancelled
    assert hybrid.ctx.order_tracker.exchange_sl_order_ids == []
    print(
        "ST-2: scaled level is spent before successful execution; recovery also spends it without dispatch; rejected untracked partial TP cancels existing hybrid stop"
    )


def atr_repair_loses_available_atr():
    h = Harness(mode=SLTPMode.EXCHANGE)
    signal = Signal.enter_long(price=100.0)
    h.ctx.position_tracker.set_pending_signal(signal, D("2"))
    assert (
        h._order_calculator.calculate_stop_loss(D("100"), SignalDirection.ENTER_LONG, D("2")) == 96
    )
    reconciler = OrderReconciler(h)
    for _ in range(3):
        reconciler.ensure_exchange_sl_protection(h.ctx)
        h.now_ns += 61_000_000_000
    assert h.sent == [] and h.ctx.position_tracker.pending_entry_atr is None
    assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == 0
    print(
        "ST-3: ATR=2 exists and implies stop96, but repair overwrites ATR with None and cannot submit any stop across repeated bars"
    )


def cancel_rejection_forgets_live_entry():
    h = Harness(mode=SLTPMode.EXCHANGE)
    h.positions = []
    h.ctx.position_tracker.reset()
    coordinator = SignalExecutionCoordinator(h)
    coordinator.execute_entry_for_pair(
        h.ctx, Signal.enter_long(price=100.0), size=D("100"), bar=NS(close=D("100"))
    )
    entry = h.sent[0]
    OrderReconciler(h).handle_order_cancel_rejected(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=entry.client_order_id,
            reason="temporary cancellation failure",
        )
    )
    assert entry.is_open and h.ctx.order_tracker.entry_order_id is None
    h.positions = [h.position]
    entry.is_open = False
    entry.is_closed = True
    before = len(h.sent)
    TradeEventHandler(h).handle_order_filled(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=entry.client_order_id,
            order_side=OrderSide.BUY,
            last_qty=Quantity.from_str("1.000"),
            last_px=Price.from_str("100.00"),
        )
    )
    assert len(h.sent) == before and h.ctx.position_tracker.pending_signal is not None
    assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == 0
    # A pending cancellation has the same ownership loss when the next entry replaces the sole tracked ID.
    second = Harness(mode=SLTPMode.EXCHANGE)
    second.positions = []
    second.ctx.position_tracker.reset()
    c = SignalExecutionCoordinator(second)
    c.execute_entry_for_pair(
        second.ctx, Signal.enter_long(price=100.0), size=D("100"), bar=NS(close=D("100"))
    )
    old = second.sent[0]
    c.execute_entry_for_pair(
        second.ctx, Signal.enter_long(price=100.0), size=D("100"), bar=NS(close=D("100"))
    )
    assert old.is_open and old.client_order_id in second.cancelled
    assert second.ctx.order_tracker.entry_order_id != old.client_order_id and len(second.sent) == 2
    print(
        "ST-4: cancel rejection clears a still-open entry; its later fill receives no protection. Replacement also overwrites ownership before cancel confirmation"
    )


def allocation_refusal_does_not_block_order():
    h = Harness()
    h.positions = []
    h.ctx.position_tracker.reset()
    h._capital_allocator = CapitalAllocator(
        AllocationConfig(tiers={"BTC-USDT": 0.1}), D("1000"), h.cache
    )
    h._capital_allocator.register_pair(h.ctx.pair, h.instrument.id)
    SignalExecutionCoordinator(h).execute_entry_for_pair(
        h.ctx, Signal.enter_long(price=100.0), size=D("200"), bar=NS(close=D("100"))
    )
    assert h._capital_allocator.get_tier_limit(h.ctx.pair) == 100
    assert h._capital_allocator.available_cash == 1000 and len(h.sent) == 1
    assert h.ctx.allocated_capital == 200
    print(
        "ST-5: allocation limit100 rejects reservation200, but coordinator still submits order200 and records context allocation200"
    )


def unfilled_terminal_order_leaks_capital():
    for outcome in ("cancel", "reject"):
        h = Harness()
        h.positions = []
        h.ctx.position_tracker.reset()
        h._capital_allocator = CapitalAllocator(
            AllocationConfig(tiers={"BTC-USDT": 1}), D("1000"), h.cache
        )
        h._capital_allocator.register_pair(h.ctx.pair, h.instrument.id)
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=D("400"), bar=NS(close=D("100"))
        )
        entry = h.sent[0]
        entry.is_open = False
        entry.is_closed = True
        event = NS(
            instrument_id=h.instrument.id,
            client_order_id=entry.client_order_id,
            reason="rejected before fill",
        )
        if outcome == "cancel":
            TradeEventHandler(h).handle_order_canceled(event)
        else:
            OrderReconciler(h).handle_order_rejected(event)
        assert h.positions == [] and h.ctx.order_tracker.entry_order_id is None
        assert h._capital_allocator.available_cash == 600 and h.ctx.allocated_capital == 400
        assert h.ctx.position_tracker.entry_count == 1 and h.ctx.position_tracker.has_position
    print(
        "ST-6: canceled/rejected unfilled entry leaves 400 capital reserved and one phantom entry while the venue position is empty"
    )


def tick_break_even_blocks_take_profit():
    monitor = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=D(".04"))
    monitor.init_position(D("100"), True)
    h = Harness(monitor=monitor)
    assert h._mode.allows_break_even
    h._sltp_coordinator.move_stop_to_break_even(h.ctx, h.position, D("100"))
    assert len(h.sent) == 1 and h.ctx.break_even_applied
    assert (
        h.ctx.order_tracker.sl_order_ids == [] and h.ctx.order_tracker.exchange_sl_order_ids == []
    )
    h.tick("105")
    h.now_ns += 200_000_000_000
    OrderReconciler(h).sweep_stale_orders_for_pair(h.ctx)
    h.tick("106")
    assert h.cancelled == [] and h.closed == []
    assert h.cache.orders_open(instrument_id=h.instrument.id)
    print(
        "ST-7: tick-mode break-even submits an untracked stop; subsequent full TP waits forever for that stop to be canceled, but cancellation finds no tracked order"
    )


def equal_allocation_depends_on_registration_order():
    allocator = CapitalAllocator(AllocationConfig(mode="equal", tiers={}), D("200"), MagicMock())
    allocator.register_pair("BTC-USDT", InstrumentId.from_str("BTCUSDT.BINANCE"))
    allocator.register_pair("ETH-USDT", InstrumentId.from_str("ETHUSDT.BINANCE"))
    assert allocator.get_tier_limit("BTC-USDT") == 200
    assert allocator.get_tier_limit("ETH-USDT") == 100
    assert allocator.allocate("BTC-USDT", D("200"))
    assert allocator.get_available_capital("ETH-USDT") == 0
    print(
        "ST-8: equal allocation of 200 across two pairs grants limits200/100; first pair can take all capital"
    )


if __name__ == "__main__":
    scaled_quantities_leave_residual()
    scaled_levels_spent_without_dispatch()
    atr_repair_loses_available_atr()
    cancel_rejection_forgets_live_entry()
    allocation_refusal_does_not_block_order()
    unfilled_terminal_order_leaks_capital()
    tick_break_even_blocks_take_profit()
    equal_allocation_depends_on_registration_order()
