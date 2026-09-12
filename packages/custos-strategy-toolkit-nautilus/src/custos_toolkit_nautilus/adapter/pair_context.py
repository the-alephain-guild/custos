"""
PairContext for multi-pair strategy support.

Encapsulates all state for a single trading pair within a multi-pair strategy.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from custos_toolkit.position import PositionTracker
from nautilus_trader.model import BarType, InstrumentId

from custos_toolkit_nautilus.adapter.orders import OrderTracker
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.execution import ExecutionManager
    from custos_toolkit_nautilus.adapter.filter_manager import FilterManager
    from custos_toolkit_nautilus.adapter.orders import (
        NativeTrailingStopSubmitter,
        StopLossSubmitter,
        TakeProfitSubmitter,
    )


@dataclass
class PairContext:
    """
    Encapsulates all state for a single trading pair.

    Each trading pair in a multi-pair strategy gets its own PairContext,
    which holds the instrument identifiers, state trackers, indicators,
    and execution components.

    Attributes:
        pair: Trading pair identifier (e.g., "BTC-USDT")
        instrument_id: NautilusTrader instrument ID
        bar_type: Bar type for this pair
        position_tracker: Tracks position state
        order_tracker: Tracks order IDs for SL/TP
        tick_monitor: Optional tick-level monitoring
        execution_manager: Manages order creation and execution
        sl_submitter: Creates stop loss orders
        tp_submitter: Creates take profit orders
        indicators: Strategy-managed indicators for this pair
        warmed_up: Whether indicators are warmed up
    """

    pair: str
    instrument_id: InstrumentId
    bar_type: BarType

    # State managers (created with defaults)
    position_tracker: PositionTracker = field(default_factory=PositionTracker)
    order_tracker: OrderTracker = field(default_factory=OrderTracker)
    tick_monitor: TickMonitorManager | None = None

    # Execution components (initialized by strategy)
    execution_manager: "ExecutionManager | None" = None
    sl_submitter: "StopLossSubmitter | None" = None
    tp_submitter: "TakeProfitSubmitter | None" = None
    # Exchange-managed trailing stop submitter (native_trailing mode only)
    native_trailing_submitter: "NativeTrailingStopSubmitter | None" = None

    # Filter manager for per-pair filters (initialized by strategy)
    filter_manager: "FilterManager | None" = None

    # Indicator references (managed by concrete strategy)
    indicators: dict[str, object] = field(default_factory=dict)

    # Position size factor (dynamic, set by per-pair filters, reset after each use)
    size_reduction_factor: float = 1.0

    # Entry delay window (ns); set when filters fail with on_filter_fail="delay",
    # keeps blocking entries until bar.ts_event passes it (0 = no delay)
    filter_delay_until: int = 0

    # Capital allocated for current position (for correct release on close)
    allocated_capital: Decimal = field(default_factory=lambda: Decimal("0"))

    # State flags
    warmed_up: bool = False
    break_even_applied: bool = False
    sl_tp_submitted_for_reversal: bool = False
    # Set by SignalExecutionCoordinator.execute_entry_for_pair when the entry is a
    # reversal; consumed by on_order_filled so sl_tp_submitted_for_reversal is
    # only raised for reversal entries (normal closes must cancel SL/TP).
    pending_entry_is_reversal: bool = False

    # Entry signal id of the current position (set on entry), used to link subsequent
    # SL/TP orders back to that signal -- both as an order tag and in
    # strategy._order_signal_map. See adapter/signal_correlation.py.
    active_signal_id: str | None = None

    # Stale-order sweep rate guard: client_order_id -> last cancel attempt (ns)
    stale_cancel_attempts: dict[object, int] = field(default_factory=dict)

    # native_trailing protection rebuild cooldown (ns).
    # Guards the per-bar self-heal against a reject->rebuild->reject flood when the
    # venue keeps rejecting the trailing stop. 0 = may rebuild now.
    native_trailing_rebuild_deadline_ns: int = 0
    # Same rate guard for standard/hybrid exchange stop coverage repair.
    exchange_sl_rebuild_deadline_ns: int = 0

    def reset(self) -> None:
        """Reset context state to initial values."""
        self.position_tracker = PositionTracker()
        self.order_tracker = OrderTracker()
        self.indicators.clear()
        self.warmed_up = False
        self.break_even_applied = False
        self.sl_tp_submitted_for_reversal = False
        self.pending_entry_is_reversal = False
        self.active_signal_id = None
        self.allocated_capital = Decimal("0")
        self.stale_cancel_attempts.clear()
        self.native_trailing_rebuild_deadline_ns = 0
        self.exchange_sl_rebuild_deadline_ns = 0
        if self.tick_monitor:
            self.tick_monitor.reset()
