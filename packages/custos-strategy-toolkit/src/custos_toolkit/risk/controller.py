# shared/risk/controller.py
"""
Risk limit controller.

Enforces trading limits: drawdown, daily loss, trade counts, consecutive losses.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..config._values import config_value


@dataclass
class RiskState:
    """
    Risk control state.

    Tracks session metrics for risk limit enforcement.
    """

    session_trade_count: int = 0
    session_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    consecutive_losses: int = 0
    win_streak: int = 0
    loss_streak: int = 0
    peak_equity: Decimal = field(default_factory=lambda: Decimal("0"))
    paused_until: int = 0  # Timestamp in nanoseconds
    # Epoch-ns timestamp of the next daily reset boundary. 0 = not yet observed
    # (initialized lazily on the first timestamped check, without resetting).
    next_reset_ns: int = 0


class RiskController:
    """
    Platform-agnostic risk limit enforcement.

    Checks trading limits and tracks session metrics.
    """

    def __init__(
        self,
        config: dict[str, object],
        initial_capital: Decimal,
        capital_mode: str = "compound",
    ):
        """
        Initialize risk controller.

        Args:
            config: Risk config dict with keys:
                - max_daily_trades: Max trades per session
                - max_daily_loss: Max loss as fraction (e.g., 0.05 = 5%)
                - max_daily_profit: Profit target as fraction
                - max_drawdown: Max drawdown as fraction
                - consecutive_loss_pause: Pause after N consecutive losses
                - pause_duration: Pause duration in seconds
            initial_capital: Starting capital (Decimal)
            capital_mode: "compound" or "fixed_capital"
        """
        self.config = config
        self.initial_capital = initial_capital
        self.capital_mode = capital_mode
        self._state = RiskState()

        # Initialize peak equity
        self._state.peak_equity = self.initial_capital

        # Daily reset boundary (UTC). Format already validated upstream by
        # GlobalRiskConfig.__post_init__; parse defensively for raw dict callers.
        self._reset_hour, self._reset_minute = self._parse_reset_time(
            str(config.get("reset_time", "00:00"))
        )

    @staticmethod
    def _parse_reset_time(value: str) -> tuple[int, int]:
        """Parse 'HH:MM' into (hour, minute); fall back to midnight UTC."""
        parts = value.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        return 0, 0

    def _compute_next_reset_ns(self, current_ts: int) -> int:
        """Epoch-ns of the next reset_time boundary strictly after current_ts."""
        now = datetime.fromtimestamp(current_ts / 1_000_000_000, tz=UTC)
        boundary = now.replace(
            hour=self._reset_hour, minute=self._reset_minute, second=0, microsecond=0
        )
        if boundary <= now:
            boundary += timedelta(days=1)
        return int(boundary.timestamp() * 1_000_000_000)

    def _maybe_daily_reset(self, current_ts: int) -> None:
        """Reset the daily session when current_ts crosses the reset boundary.

        First timestamped observation only initializes the boundary (no reset), so
        a fresh controller does not wipe state on its very first bar.
        """
        if self._state.next_reset_ns == 0:
            self._state.next_reset_ns = self._compute_next_reset_ns(current_ts)
            return
        if current_ts >= self._state.next_reset_ns:
            self.reset_session()
            self._state.next_reset_ns = self._compute_next_reset_ns(current_ts)

    def check_limits(
        self,
        current_equity: Decimal,
        current_ts: int = 0,
    ) -> tuple[bool, str]:
        """
        Check all risk limits.

        Args:
            current_equity: Current account equity (Decimal)
            current_ts: Current timestamp in nanoseconds

        Returns:
            (allowed, reason) - True if trading allowed, reason if blocked
        """
        equity = current_equity

        # Roll over the daily session at the UTC reset boundary (timestamped checks
        # only; current_ts==0 means "time unknown" and must not trigger a reset).
        if current_ts > 0:
            self._maybe_daily_reset(current_ts)

        # Check pause
        if self._state.paused_until > 0 and current_ts < self._state.paused_until:
            return False, "Trading paused"

        # Check max trades
        max_trades = config_value(self.config, "max_daily_trades", 0)
        if max_trades > 0 and self._state.session_trade_count >= max_trades:
            return False, f"Max daily trades ({max_trades}) reached"

        # Check daily loss
        max_loss = config_value(self.config, "max_daily_loss", 0.0)
        if max_loss > 0 and equity > 0:
            loss_pct = -self._state.session_pnl / equity
            if loss_pct >= Decimal(str(max_loss)):
                return False, f"Daily loss limit ({max_loss:.1%}) reached"

        # Check daily profit
        max_profit = config_value(self.config, "max_daily_profit", 0.0)
        if max_profit > 0 and equity > 0:
            profit_pct = self._state.session_pnl / equity
            if profit_pct >= Decimal(str(max_profit)):
                return False, f"Daily profit target ({max_profit:.1%}) reached"

        # Check drawdown
        max_dd = config_value(self.config, "max_drawdown", 0.0)
        if max_dd > 0:
            dd_pct = self._calculate_drawdown(equity)
            if dd_pct >= Decimal(str(max_dd)):
                return False, f"Max drawdown ({max_dd:.1%}) reached"

        # Check consecutive losses
        max_consec = config_value(self.config, "consecutive_loss_pause", 0)
        if max_consec > 0 and self._state.consecutive_losses >= max_consec:
            # Apply time-based pause and reset counter to allow recovery
            if self._state.paused_until == 0 or current_ts >= self._state.paused_until:
                self.apply_pause(current_ts)
                self._state.consecutive_losses = 0
            return False, "Trading paused (consecutive losses)"

        return True, ""

    def _calculate_drawdown(self, current_equity: Decimal) -> Decimal:
        """Calculate drawdown based on capital mode."""
        if self.capital_mode == "fixed_capital":
            if self.initial_capital > 0:
                return (self.initial_capital - current_equity) / self.initial_capital
        else:
            if self._state.peak_equity > 0:
                return (self._state.peak_equity - current_equity) / self._state.peak_equity
        return Decimal("0")

    def record_trade(self, pnl: Decimal, current_ts: int = 0) -> None:
        """
        Record completed trade result.

        Args:
            pnl: Realized P&L (Decimal)
            current_ts: Execution time in nanoseconds. A fill belongs to the day it
                happened on, so the day boundary advances here, before the result is
                booked -- otherwise a fill just after midnight lands on yesterday's
                tally and is cleared with it, handing back the whole daily budget.
                ``0`` means the time is unknown and must not be guessed at with the
                wall clock: the result is booked against the current session.
        """
        if current_ts > 0:
            self._maybe_daily_reset(current_ts)
        self._state.session_pnl += pnl
        self._state.session_trade_count += 1

        if pnl < Decimal("0"):
            self._state.consecutive_losses += 1
            self._state.loss_streak += 1
            self._state.win_streak = 0
        elif pnl > Decimal("0"):
            self._state.consecutive_losses = 0
            self._state.win_streak += 1
            self._state.loss_streak = 0
        else:
            # PnL == 0: break-even trade, reset consecutive losses but don't count as win
            self._state.consecutive_losses = 0
            self._state.loss_streak = 0

    def update_peak_equity(self, current_equity: Decimal) -> None:
        """
        Update peak equity (compound mode only).

        Args:
            current_equity: Current account equity (Decimal)
        """
        if self.capital_mode == "compound":
            if current_equity > self._state.peak_equity:
                self._state.peak_equity = current_equity

    def apply_pause(self, current_ts: int) -> None:
        """
        Apply trading pause.

        ``pause_duration == 0`` means "pause until the next daily reset boundary"
        (so consecutive-loss protection survives until the session rolls over),
        not a zero-second pause. A positive value pauses for that many seconds.

        Args:
            current_ts: Current timestamp in nanoseconds
        """
        duration = config_value(self.config, "pause_duration", 3600)  # Default 1 hour
        if duration == 0:
            if self._state.next_reset_ns == 0:
                self._state.next_reset_ns = self._compute_next_reset_ns(current_ts)
            self._state.paused_until = self._state.next_reset_ns
        else:
            self._state.paused_until = current_ts + (duration * 1_000_000_000)

    def reset_session(self) -> None:
        """Reset daily session counters."""
        self._state.session_trade_count = 0
        self._state.session_pnl = Decimal("0")
        # Don't reset consecutive_losses, streaks, or peak_equity

    @property
    def session_pnl(self) -> Decimal:
        """Get session P&L."""
        return self._state.session_pnl

    @property
    def consecutive_losses(self) -> int:
        """Get consecutive loss count."""
        return self._state.consecutive_losses

    @property
    def win_streak(self) -> int:
        """Get current win streak."""
        return self._state.win_streak

    @property
    def loss_streak(self) -> int:
        """Get current loss streak."""
        return self._state.loss_streak

    @property
    def peak_equity(self) -> Decimal:
        """Get peak equity value."""
        return self._state.peak_equity
