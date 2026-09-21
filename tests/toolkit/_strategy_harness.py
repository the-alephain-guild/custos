"""Shared in-memory strategy harness for the coordinator regression suite.

Built from the review probes in
``.forge/reviews/2026-09-20-custos-strategy-deep-repro.py`` so the regressions
exercise the real coordinators, trackers, price calculators and allocator. The
strategy environment, cache and order dispatch/report are controlled doubles; no
venue is contacted and no matching engine is imitated.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from custos_toolkit.position.tracker import PositionTracker
from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit_nautilus.adapter.coordinators import ExecutionCoordinator
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


def scaled_monitor(
    levels: int = 2, exit_pcts: tuple[str, ...] = (".5", ".5")
) -> TickMonitorManager:
    """A scaled take-profit monitor with ascending targets."""
    targets = [".02", ".04", ".06"]
    return TickMonitorManager(
        mode="tick",
        tp_method="scaled",
        tp_levels=[
            {"target_pct": Decimal(targets[i]), "exit_pct": Decimal(exit_pcts[i])}
            for i in range(levels)
        ],
    )


class Harness:
    """A strategy stand-in wired to the real coordinators under test."""

    def __init__(
        self,
        mode: SLTPMode = SLTPMode.TICK,
        monitor: TickMonitorManager | None = None,
    ) -> None:
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
            quantity=Decimal("1"),
            avg_px_open=Decimal("100"),
            is_long=True,
            is_short=False,
            is_closed=False,
            side="LONG",
            instrument_id=self.instrument.id,
        )
        self.positions = [self.position]
        self.orders: dict[str, NS] = {}
        self.sent: list[NS] = []
        self.cancelled: list[str] = []
        self.closed: list[tuple] = []
        self.now_ns = 100_000_000_000
        self.log = MagicMock()
        self.bars: list[NS] = [NS(close=Decimal("105"))]
        self.cache = NS(
            instrument=lambda _: self.instrument,
            positions_open=lambda **kw: self.positions,
            orders_open=lambda **kw: [o for o in self.orders.values() if o.is_open],
            order=lambda oid: self.orders.get(oid),
            # Newest first, the way the real cache stores them: NautilusTrader's
            # add_bar does push_front, and its own docs say "Index 0 is the most
            # recent". A single-bar double cannot catch a caller that reads the
            # wrong end, which is how LB-3 survived.
            bars=lambda _: list(self.bars),
            bar=lambda _: self.bars[0] if self.bars else None,
        )
        self.order_factory = NS(
            market=lambda **kw: self.order(OrderType.MARKET, **kw),
            stop_market=lambda **kw: self.order(OrderType.STOP_MARKET, **kw),
            limit=lambda **kw: self.order(OrderType.LIMIT, **kw),
        )
        self.clock = NS(timestamp_ns=lambda: self.now_ns)
        self._mode = mode
        self._order_signal_map: dict[str, str] = {}
        self._capital_allocator = None
        self._risk_controller = MagicMock()
        self._risk_manager = MagicMock()
        self.config = NS(
            position=NS(limits=NS(max_total_positions=0), capital_mode="fixed_capital"),
            trading=NS(order_type="market"),
            risk=NS(trade=NS(take_profit=NS(method="none", scaled=None))),
        )
        tracker = PositionTracker()
        tracker.record_entry(Decimal("100"), Decimal("1"))
        self.ctx = NS(
            pair="BTC-USDT",
            instrument_id=self.instrument.id,
            bar_type="fixture",
            order_tracker=OrderTracker(),
            position_tracker=tracker,
            tick_monitor=monitor,
            indicators={"atr": NS(initialized=True, value=Decimal("2"))},
            allocated_capital=Decimal("0"),
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
        self.ctx.tp_submitter = TakeProfitSubmitter(
            self.order_factory, self.cache, self.log, self._order_calculator
        )
        from custos_toolkit_nautilus.adapter.coordinators import SLTPCoordinator

        self._sltp_coordinator = SLTPCoordinator(self)
        self.pause = MagicMock()
        self.on_trade_closed = MagicMock()

    def order(self, kind, **kw) -> NS:
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

    def entry_order(self, **kw) -> NS:
        return self.order(
            OrderType.LIMIT,
            instrument_id=kw["instrument_id"],
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(kw["size"] / Decimal(str(kw["bar"].close))),
            price=kw["bar"].close,
            reduce_only=False,
        )

    def submit_order(self, order) -> None:
        self.sent.append(order)

    def cancel_order(self, order) -> None:
        self.cancelled.append(order.client_order_id)

    def cancel_all_orders(self, *args) -> None:
        self.cancelled.extend(self.orders)

    def close_position(self, *args, **kw) -> None:
        self.closed.append((args, kw))

    def tick(self, price: str) -> None:
        ExecutionCoordinator(self).handle_trade_tick(
            NS(instrument_id=self.instrument.id, price=Decimal(str(price)))
        )

    def flat(self) -> None:
        """Drop to a no-position, no-entry-history state."""
        self.positions = []
        self.ctx.position_tracker.reset()
