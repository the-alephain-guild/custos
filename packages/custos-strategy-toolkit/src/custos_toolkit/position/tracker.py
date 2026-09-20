# shared/position/tracker.py
"""
Position entry tracking.

Tracks entry count, average entry price, and position quantity
for scaling and P&L calculations.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from ..config._values import config_value

if TYPE_CHECKING:
    from custos_toolkit.signals.types import Signal


@dataclass
class PositionState:
    """
    Position state for scaling tracking.

    Attributes:
        entry_count: Number of entries made
        total_quantity: Total position quantity
        avg_entry_price: Weighted average entry price
        last_entry_price: Most recent entry price
        first_entry_price: Initial entry price (for SL/TP calculations)
    """

    entry_count: int = 0
    total_quantity: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_entry_price: Decimal = field(default_factory=lambda: Decimal("0"))
    last_entry_price: Decimal = field(default_factory=lambda: Decimal("0"))
    first_entry_price: Decimal = field(default_factory=lambda: Decimal("0"))


class PositionTracker:
    """
    Track position entries for scaling and average price calculation.

    Platform-agnostic tracking of entry count and weighted average price.
    """

    def __init__(self) -> None:
        """Initialize tracker with empty state."""
        self._state = PositionState()
        self._pending_signal: Signal | None = None
        self._pending_entry_atr: Decimal | None = None

    @property
    def entry_count(self) -> int:
        """Get number of entries."""
        return self._state.entry_count

    @property
    def total_quantity(self) -> Decimal:
        """Get total position quantity."""
        return self._state.total_quantity

    @property
    def avg_entry_price(self) -> Decimal:
        """Get weighted average entry price."""
        return self._state.avg_entry_price

    @property
    def last_entry_price(self) -> Decimal:
        """Get most recent entry price."""
        return self._state.last_entry_price

    @property
    def first_entry_price(self) -> Decimal:
        """Get first entry price (for SL/TP calculations)."""
        return self._state.first_entry_price

    @property
    def has_position(self) -> bool:
        """Check if there is an open position."""
        return self._state.entry_count > 0

    @property
    def pending_signal(self) -> "Signal | None":
        """Get the pending entry signal."""
        return self._pending_signal

    @property
    def pending_entry_atr(self) -> Decimal | None:
        """Get the ATR value at pending entry."""
        return self._pending_entry_atr

    def set_pending_signal(
        self,
        signal: "Signal",
        entry_atr: Decimal | None = None,
    ) -> None:
        """
        Store the signal that triggered the pending entry.

        Args:
            signal: The entry signal awaiting order fill
            entry_atr: ATR value at entry time (for SL/TP calculations)
        """
        self._pending_signal = signal
        self._pending_entry_atr = entry_atr

    def correct_entry_price(self, actual_price: Decimal) -> None:
        """Replace the reference entry price with what the position actually cost.

        An entry is recorded from the signal bar's close, because that is all
        that is known before the order goes out. Once the venue reports a fill
        the real cost is known, and every protective order is priced off it: a
        limit offset, price improvement or slippage otherwise leaves the stop
        measured from a price the position was never opened at.
        """
        if actual_price <= 0:
            return
        self._state.first_entry_price = actual_price
        self._state.avg_entry_price = actual_price

    def clear_pending_signal(self) -> None:
        """Clear the pending signal and entry ATR after fill or rejection."""
        self._pending_signal = None
        self._pending_entry_atr = None

    def record_entry(self, price: Decimal, quantity: Decimal) -> None:
        """
        Record a new entry.

        Updates weighted average entry price and total quantity.

        Args:
            price: Entry price (Decimal)
            quantity: Entry quantity (Decimal)
        """
        price_d = price
        qty_d = quantity

        if self._state.entry_count == 0:
            self._state.first_entry_price = price_d  # Capture first entry
            self._state.avg_entry_price = price_d
            self._state.total_quantity = qty_d
        else:
            total_qty = self._state.total_quantity + qty_d
            if total_qty > 0:
                self._state.avg_entry_price = (
                    self._state.avg_entry_price * self._state.total_quantity + price_d * qty_d
                ) / total_qty
            self._state.total_quantity = total_qty

        self._state.last_entry_price = price_d
        self._state.entry_count += 1

    def record_partial_exit(self, quantity: Decimal) -> None:
        """
        Record a partial exit (reduce quantity).

        Args:
            quantity: Quantity exited (Decimal)
        """
        self._state.total_quantity = max(Decimal("0"), self._state.total_quantity - quantity)

    def reset(self) -> None:
        """Reset state when position fully closes."""
        self._state = PositionState()
        self._pending_signal = None
        self._pending_entry_atr = None

    def get_unrealized_pnl(self, current_price: Decimal, is_long: bool = True) -> Decimal:
        """
        Calculate unrealized P&L.

        Args:
            current_price: Current market price (Decimal)
            is_long: True for long position, False for short

        Returns:
            Unrealized P&L in quote currency
        """
        if self._state.entry_count == 0:
            return Decimal("0")

        if is_long:
            return (current_price - self._state.avg_entry_price) * self._state.total_quantity
        else:
            return (self._state.avg_entry_price - current_price) * self._state.total_quantity

    def should_scale_in(
        self,
        current_price: Decimal,
        is_long: bool,
        scaling_config: dict[str, object],
    ) -> bool:
        """
        Check if conditions are met for scaled entry.

        Args:
            current_price: Current market price (Decimal)
            is_long: True for long position
            scaling_config: Scaling configuration dict with keys:
                - enabled: bool
                - max_entries: int
                - entry_interval_pct: float

        Returns:
            True if scaling entry is allowed
        """
        if not scaling_config or not scaling_config.get("enabled"):
            return False

        max_entries = config_value(scaling_config, "max_entries", 3)
        if self._state.entry_count >= max_entries:
            return False

        if self._state.entry_count == 0:
            return True  # First entry always allowed

        last_price = self._state.last_entry_price
        if last_price <= 0:
            return True

        price_change_pct = abs(current_price - last_price) / last_price
        entry_interval = Decimal(str(scaling_config.get("entry_interval_pct", 0.02)))

        # For scaling IN, price should move against us (averaging down/up)
        if is_long:
            # Price should drop for long scaling (buy the dip)
            if current_price >= last_price:
                return False
        else:
            # Price should rise for short scaling
            if current_price <= last_price:
                return False

        return price_change_pct >= entry_interval
