"""
Tick-level position monitoring for trailing stops and scaled exits.

Provides classes for managing tick-by-tick monitoring of positions,
including trailing stop logic with activation thresholds and
scaled take profit handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .config.risk import TradeRiskConfig


class _TakeProfitLevel(TypedDict):
    target_pct: Decimal
    exit_pct: Decimal


class TakeProfitLevelState(str, Enum):  # noqa: UP042 - match the str(Enum) style of SLTPMode
    """Where one scaled take-profit level stands.

    Reaching the target price is an intent, not an outcome. A level is only spent
    once the venue reports the fill that took the quantity, so a dispatch that was
    refused, rejected or never sent returns the level to the market.
    """

    ARMED = "armed"
    PENDING = "pending"
    COMPLETED = "completed"


@dataclass
class ExitAction:
    """
    Action returned by tick monitoring components when an exit is triggered.

    Represents the result of a tick-level check that determined an exit
    should be executed. Contains all information needed for the strategy
    to execute the exit.

    Attributes:
        exit_type: Type of exit ("trailing_stop", "take_profit", "partial_tp")
        price: Current price that triggered the exit
        reason: Human-readable explanation for logging
        partial_pct: Percentage of position to exit (for scaled TP, e.g., 0.33)
        level: Take profit level number (1, 2, 3 for scaled exits)
    """

    exit_type: str
    price: Decimal
    reason: str
    partial_pct: Decimal | None = None
    level: int | None = None


class TrailingStopManager:
    """
    Manages trailing stop state and activation.

    Tracks the peak price (most favorable price reached) and determines
    when the trailing stop should activate and when it should trigger
    an exit based on drawdown from the peak.

    The trailing stop has two phases:
    1. Inactive: Waiting for profit to reach activation_pct threshold
    2. Active: Tracking peak and triggering exit on trailing_pct drawdown

    For long positions:
    - Peak is the highest price reached
    - Activation occurs when profit >= activation_pct
    - Exit triggers when drawdown from peak >= trailing_pct

    For short positions:
    - Peak is the lowest price reached
    - Activation occurs when profit >= activation_pct
    - Exit triggers when price rises above peak by trailing_pct

    Example:
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # Activate at 2% profit
            trailing_pct=Decimal("0.01"),    # Exit at 1% drawdown from peak
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # On each tick:
        manager.update_peak(current_price, is_long=True)
        action = manager.check(current_price, entry_price, is_long=True)
        if action:
            # Execute exit
            pass
    """

    def __init__(
        self,
        activation_pct: Decimal | float,
        trailing_pct: Decimal | float,
    ) -> None:
        """
        Initialize trailing stop manager.

        Args:
            activation_pct: Profit percentage to activate trailing stop
                           (e.g., 0.02 = 2%)
            trailing_pct: Drawdown percentage from peak to trigger exit
                         (e.g., 0.01 = 1%)
        """
        self._activation_pct = self._to_decimal(activation_pct)
        self._trailing_pct = self._to_decimal(trailing_pct)

        # State
        self._activated: bool = False
        self._peak_price: Decimal | None = None

    @property
    def activated(self) -> bool:
        """Whether trailing stop is currently activated."""
        return self._activated

    @property
    def peak_price(self) -> Decimal | None:
        """Current peak price (most favorable price reached)."""
        return self._peak_price

    def init_position(self, entry_price: Decimal | float, is_long: bool) -> None:
        """
        Initialize for a new position.

        Sets the peak price to the entry price and resets the
        activated state. Should be called when entering a new position.

        Args:
            entry_price: Entry price of the position
            is_long: True for long position, False for short
        """
        self._peak_price = self._to_decimal(entry_price)
        self._activated = False

    def reset(self) -> None:
        """
        Reset all state.

        Clears the peak price and activated state. Should be called
        when a position is closed or the strategy is reset.
        """
        self._activated = False
        self._peak_price = None

    def update_peak(self, current_price: Decimal | float, is_long: bool) -> None:
        """
        Update peak price if current price is more favorable.

        For long positions, updates peak if price is higher.
        For short positions, updates peak if price is lower.

        Args:
            current_price: Current market price
            is_long: True for long position, False for short
        """
        if self._peak_price is None:
            return

        current = self._to_decimal(current_price)

        if is_long:
            # For long: higher is better
            if current > self._peak_price:
                self._peak_price = current
        else:
            # For short: lower is better
            if current < self._peak_price:
                self._peak_price = current

    def check(
        self,
        current_price: Decimal | float,
        entry_price: Decimal | float,
        is_long: bool,
    ) -> ExitAction | None:
        """
        Check if trailing stop should trigger an exit.

        First checks if the trailing stop should activate (if not already),
        then checks if the drawdown from peak exceeds the trailing threshold.

        Args:
            current_price: Current market price
            entry_price: Position entry price
            is_long: True for long position, False for short

        Returns:
            ExitAction if exit should be triggered, None otherwise
        """
        if self._peak_price is None:
            return None

        current = self._to_decimal(current_price)
        entry = self._to_decimal(entry_price)

        # Check activation
        if not self._activated:
            profit_pct = self._calculate_profit_pct(current, entry, is_long)
            if profit_pct >= self._activation_pct:
                self._activated = True

        # If not activated, no exit check needed
        if not self._activated:
            return None

        # Check drawdown from peak
        drawdown = self._calculate_drawdown(current, is_long)
        if drawdown >= self._trailing_pct:
            return ExitAction(
                exit_type="trailing_stop",
                price=current,
                reason=f"Trailing stop triggered: {drawdown:.2%} drawdown from peak {self._peak_price}",
            )

        return None

    def _calculate_profit_pct(
        self,
        current: Decimal,
        entry: Decimal,
        is_long: bool,
    ) -> Decimal:
        """Calculate profit percentage from entry."""
        if entry == 0:
            return Decimal("0")

        if is_long:
            return (current - entry) / entry
        else:
            return (entry - current) / entry

    def _calculate_drawdown(self, current: Decimal, is_long: bool) -> Decimal:
        """Calculate drawdown from peak price."""
        if self._peak_price is None or self._peak_price == 0:
            return Decimal("0")

        if is_long:
            # For long: drawdown is (peak - current) / peak
            return (self._peak_price - current) / self._peak_price
        else:
            # For short: drawdown is (current - peak) / peak
            return (current - self._peak_price) / self._peak_price

    @staticmethod
    def _to_decimal(value: Decimal | float | int) -> Decimal:
        """Convert value to Decimal."""
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class TickMonitorManager:
    """
    Manages tick-level SL/TP monitoring for tick/hybrid modes.

    Provides unified handling of different take profit methods (fixed, scaled,
    trailing) with tick-by-tick price monitoring. The manager is only active
    for "tick" or "hybrid" modes; "exchange" mode relies on exchange-level
    orders and this manager returns inactive.

    Take Profit Methods:
    - "fixed": Exit 100% when pnl_pct >= tp_fixed_pct
    - "scaled": Partial exits at multiple target levels
    - "trailing": Use TrailingStopManager for dynamic trailing take profit

    Example:
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # On each tick:
        action = manager.check(current_price=Decimal("105.00"))
        if action:
            # Execute exit based on action.exit_type
            pass
    """

    def __init__(
        self,
        mode: str = "tick",
        tp_method: str = "fixed",
        tp_fixed_pct: Decimal | None = None,
        tp_levels: list[_TakeProfitLevel] | None = None,
        trailing_activation_pct: Decimal | None = None,
        trailing_pct: Decimal | None = None,
    ) -> None:
        """
        Initialize TickMonitorManager.

        Args:
            mode: Monitoring mode ("exchange", "tick", or "hybrid")
            tp_method: Take profit method ("fixed", "scaled", or "trailing")
            tp_fixed_pct: Target profit percentage for fixed method
            tp_levels: List of scaled TP level dicts with target_pct and exit_pct
            trailing_activation_pct: Activation threshold for trailing method
            trailing_pct: Trailing stop percentage for trailing method
        """
        self._mode = mode
        self._tp_method = tp_method
        self._tp_fixed_pct = tp_fixed_pct
        self._tp_levels = tp_levels or []
        self._tp_level_states: list[TakeProfitLevelState] = [
            TakeProfitLevelState.ARMED
        ] * len(self._tp_levels)
        # order id -> zero-based level index, for the level a dispatched order owns.
        self._level_orders: dict[str, int] = {}
        # The position size the exit percentages are shares of. Exchange mode prices
        # every level off the size at submission; tick mode must use one base too, or
        # the same config exits a different total depending on fill timing.
        self._initial_quantity: Decimal | None = None
        # zero-based level index -> quantity actually dispatched for it.
        self._level_dispatched: dict[int, Decimal] = {}
        # zero-based level index -> quantity a venue has confirmed filled for it.
        self._level_filled: dict[int, Decimal] = {}

        # Trailing stop manager (only created for trailing method)
        self._trailing_manager: TrailingStopManager | None = None
        if (
            tp_method == "trailing"
            and trailing_activation_pct is not None
            and trailing_pct is not None
        ):
            self._trailing_manager = TrailingStopManager(
                activation_pct=trailing_activation_pct,
                trailing_pct=trailing_pct,
            )

        # Position state
        self._entry_price: Decimal | None = None
        self._is_long: bool | None = None
        self._entry_atr: Decimal | None = None

    @property
    def is_active(self) -> bool:
        """Whether tick monitoring is active (True for tick/hybrid modes)."""
        return self._mode in ("tick", "hybrid")

    @property
    def peak_price(self) -> Decimal | None:
        """Current peak price from trailing manager, or None if not in trailing mode."""
        if self._trailing_manager is not None:
            return self._trailing_manager.peak_price
        return None

    @property
    def entry_atr(self) -> Decimal | None:
        """Get the ATR value at entry."""
        return self._entry_atr

    def init_position(
        self,
        entry_price: Decimal | float,
        is_long: bool,
        entry_atr: Decimal | float | None = None,
        quantity: Decimal | float | None = None,
    ) -> None:
        """
        Initialize for a new position.

        Sets the entry price and direction, resets scaled TP level tracking,
        and initializes the trailing manager if in trailing mode.

        Args:
            entry_price: Entry price of the position
            is_long: True for long position, False for short
            entry_atr: ATR value at entry (for SL/TP calculations)
            quantity: Position size the scaled exit percentages are shares of
        """
        self._entry_price = self._to_decimal(entry_price)
        self._is_long = is_long
        self._entry_atr = self._to_decimal(entry_atr) if entry_atr is not None else None
        self._initial_quantity = self._to_decimal(quantity) if quantity is not None else None

        # Reset scaled TP levels
        self._reset_levels()

        # Initialize trailing manager if present
        if self._trailing_manager is not None:
            self._trailing_manager.init_position(entry_price, is_long)

    def reset(self) -> None:
        """
        Reset all state.

        Clears position state and resets trailing manager.
        Should be called when a position is closed.
        """
        self._entry_price = None
        self._is_long = None
        self._entry_atr = None
        self._initial_quantity = None
        self._reset_levels()

        if self._trailing_manager is not None:
            self._trailing_manager.reset()

    def check(self, current_price: Decimal | float) -> ExitAction | None:
        """
        Check if take profit should trigger.

        Evaluates the current price against the configured take profit
        method and returns an ExitAction if an exit should be executed.

        Args:
            current_price: Current market price

        Returns:
            ExitAction if exit should be triggered, None otherwise
        """
        # Not active in exchange mode
        if not self.is_active:
            return None

        # No position initialized
        if self._entry_price is None or self._is_long is None:
            return None

        current = self._to_decimal(current_price)
        pnl_pct = self._calculate_pnl_pct(current)

        # Route to appropriate TP method
        if self._tp_method == "fixed":
            return self._check_fixed_tp(current, pnl_pct)
        elif self._tp_method == "scaled":
            return self._check_scaled_tp(current, pnl_pct)
        elif self._tp_method == "trailing":
            return self._check_trailing_tp(current)

        return None

    def observe(self, current_price: Decimal | float) -> None:
        """Take in the current price without producing or consuming an exit.

        Recovery needs the monitor to see where the market is, but reading it must
        not advance a scaled level: a level advanced here would be paid for with a
        quantity nothing was ever sent for.
        """
        if not self.is_active or self._entry_price is None or self._is_long is None:
            return
        if self._trailing_manager is not None:
            self._trailing_manager.update_peak(self._to_decimal(current_price), self._is_long)

    def bind_level_order(self, level: int, order_id: object) -> None:
        """Record which order carries a level's quantity (``level`` is 1-based)."""
        index = level - 1
        if 0 <= index < len(self._tp_level_states):
            self._level_orders[str(order_id)] = index

    def confirm_level_order(self, order_id: object, filled_quantity: Decimal | float) -> bool:
        """Credit a fill to the level its order carries. Returns True if one matched.

        An IOC lot can come back part filled, and the part that did not fill was not
        sold: the level closes only once nothing is outstanding on it.
        """
        key = str(order_id)
        index = self._level_orders.get(key)
        if index is None:
            return False
        self._level_filled[index] = self._level_filled.get(index, Decimal("0")) + self._to_decimal(
            filled_quantity
        )
        if self._level_outstanding(index) <= 0:
            self._tp_level_states[index] = TakeProfitLevelState.COMPLETED
            self._level_orders.pop(key, None)
        return True

    def release_level_order(self, order_id: object) -> bool:
        """The order is done at the venue: settle its level by what it still owes."""
        index = self._level_orders.pop(str(order_id), None)
        if index is None:
            return False
        if self._tp_level_states[index] is TakeProfitLevelState.PENDING:
            if self._level_outstanding(index) > 0:
                self._tp_level_states[index] = TakeProfitLevelState.ARMED
            else:
                self._tp_level_states[index] = TakeProfitLevelState.COMPLETED
            self._level_dispatched.pop(index, None)
        return True

    def release_level(self, level: int) -> None:
        """Return a level that was triggered but never dispatched (``level`` is 1-based)."""
        index = level - 1
        if 0 <= index < len(self._tp_level_states):
            if self._tp_level_states[index] is TakeProfitLevelState.PENDING:
                self._tp_level_states[index] = TakeProfitLevelState.ARMED
                self._level_dispatched.pop(index, None)

    @property
    def initial_quantity(self) -> Decimal | None:
        """The position size the scaled exit percentages are shares of."""
        return self._initial_quantity

    def is_final_level(self, level: int) -> bool:
        """Whether this 1-based level is the last one configured."""
        return level == len(self._tp_levels)

    def planned_exit_quantity(self, level: int, remaining: Decimal) -> Decimal:
        """The quantity this 1-based level should take now.

        Levels are shares of the base recorded at init_position, so the total taken
        does not depend on how much earlier levels already sold, and a retry after a
        part fill asks only for the part still owed.
        """
        index = level - 1
        if not 0 <= index < len(self._tp_levels):
            return Decimal("0")
        if self._initial_quantity is None or self._initial_quantity <= 0:
            outstanding = self._level_outstanding(index)
            if outstanding > 0:
                return outstanding
            return remaining * self._tp_levels[index].get("exit_pct", Decimal("0"))
        return self._level_outstanding(index)

    def _level_outstanding(self, index: int) -> Decimal:
        """How much of this level's target has still not been sold.

        The final level is owed whatever is left of the planned total, so rounding
        the earlier levels lost is recovered there rather than stranded.
        """
        filled = self._level_filled.get(index, Decimal("0"))
        base = self._initial_quantity
        if base is None or base <= 0:
            # No base: the only target on record is what was actually dispatched.
            return max(self._level_dispatched.get(index, Decimal("0")) - filled, Decimal("0"))
        if index == len(self._tp_levels) - 1:
            planned_total = base * sum(
                (level_config.get("exit_pct", Decimal("0")) for level_config in self._tp_levels),
                Decimal("0"),
            )
            return max(
                planned_total - sum(self._level_filled.values(), Decimal("0")), Decimal("0")
            )
        exit_pct = self._tp_levels[index].get("exit_pct", Decimal("0"))
        return max(base * exit_pct - filled, Decimal("0"))

    def record_dispatch(self, level: int, quantity: Decimal) -> None:
        """Record the quantity actually sent for a 1-based level."""
        index = level - 1
        if 0 <= index < len(self._tp_levels):
            self._level_dispatched[index] = Decimal(str(quantity))

    def _reset_levels(self) -> None:
        self._tp_level_states = [TakeProfitLevelState.ARMED] * len(self._tp_levels)
        self._level_orders = {}
        self._level_dispatched = {}
        self._level_filled = {}

    def _check_fixed_tp(self, current_price: Decimal, pnl_pct: Decimal) -> ExitAction | None:
        """Check fixed take profit trigger."""
        if self._tp_fixed_pct is None:
            return None

        if pnl_pct >= self._tp_fixed_pct:
            return ExitAction(
                exit_type="take_profit",
                price=current_price,
                reason=f"Fixed take profit reached: {pnl_pct:.2%} >= {self._tp_fixed_pct:.2%}",
            )
        return None

    def _check_scaled_tp(self, current_price: Decimal, pnl_pct: Decimal) -> ExitAction | None:
        """Check scaled take profit levels."""
        for i, level in enumerate(self._tp_levels):
            # Skip levels already taken, and those awaiting an execution report.
            if self._tp_level_states[i] is not TakeProfitLevelState.ARMED:
                continue

            target_pct = level.get("target_pct", Decimal("0"))
            exit_pct = level.get("exit_pct", Decimal("0"))

            if pnl_pct >= target_pct:
                # Awaiting dispatch and its report -- not yet spent.
                self._tp_level_states[i] = TakeProfitLevelState.PENDING
                return ExitAction(
                    exit_type="partial_tp",
                    price=current_price,
                    reason=f"Scaled take profit level {i + 1} reached: {pnl_pct:.2%} >= {target_pct:.2%}",
                    partial_pct=exit_pct,
                    level=i + 1,
                )
        return None

    def _check_trailing_tp(self, current_price: Decimal) -> ExitAction | None:
        """Check trailing take profit trigger."""
        if self._trailing_manager is None or self._entry_price is None or self._is_long is None:
            return None

        # Update peak price
        self._trailing_manager.update_peak(current_price, self._is_long)

        # Check for trailing stop trigger
        return self._trailing_manager.check(current_price, self._entry_price, self._is_long)

    def _calculate_pnl_pct(self, current_price: Decimal) -> Decimal:
        """Calculate profit/loss percentage from entry."""
        if self._entry_price is None or self._entry_price == 0:
            return Decimal("0")

        if self._is_long:
            return (current_price - self._entry_price) / self._entry_price
        else:
            return (self._entry_price - current_price) / self._entry_price

    @staticmethod
    def _to_decimal(value: Decimal | float | int) -> Decimal:
        """Convert value to Decimal."""
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @classmethod
    def from_config(cls, config: TradeRiskConfig, mode: str) -> TickMonitorManager:
        """
        Create TickMonitorManager from TradeRiskConfig.

        Factory method that extracts take profit configuration from
        a TradeRiskConfig struct and creates the appropriate manager.

        Args:
            config: TradeRiskConfig struct with take_profit configuration
            mode: Monitoring mode ("exchange", "tick", or "hybrid")

        Returns:
            Configured TickMonitorManager instance
        """
        # For exchange mode, return inactive manager
        if mode == "exchange":
            return cls(mode="exchange")

        tp_config = config.take_profit
        tp_method = tp_config.method

        # Fixed take profit
        if tp_method == "fixed":
            return cls(
                mode=mode,
                tp_method="fixed",
                tp_fixed_pct=Decimal(str(tp_config.fixed.value)),
            )

        # Scaled take profit
        if tp_method == "scaled":
            scaled_config = tp_config.scaled
            levels: list[_TakeProfitLevel] = []
            for i in range(1, scaled_config.levels + 1):
                level_config = getattr(scaled_config, f"level_{i}", None)
                if level_config:
                    levels.append(
                        {
                            "target_pct": Decimal(str(level_config.target_pct)),
                            "exit_pct": Decimal(str(level_config.exit_pct)),
                        }
                    )
            return cls(
                mode=mode,
                tp_method="scaled",
                tp_levels=levels,
            )

        # Trailing take profit
        if tp_method == "trailing":
            trailing_config = tp_config.trailing
            return cls(
                mode=mode,
                tp_method="trailing",
                trailing_activation_pct=Decimal(str(trailing_config.activation_pct)),
                trailing_pct=Decimal(str(trailing_config.callback_pct)),
            )

        # Default to fixed with sensible default
        return cls(
            mode=mode,
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),
        )
