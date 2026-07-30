# tests/test_risk_controller.py
"""Tests for RiskController."""

from datetime import UTC, datetime
from decimal import Decimal

from custos_toolkit.risk import RiskController


def _ns(dt: datetime) -> int:
    """UTC datetime -> epoch nanoseconds (matches bar.ts_event units)."""
    return int(dt.timestamp() * 1_000_000_000)


class TestRiskController:
    """Tests for RiskController."""

    def test_allows_within_limits(self):
        """Should allow trading when within limits."""
        controller = RiskController(
            {"max_daily_trades": 10, "max_drawdown": 0.1},
            initial_capital=10000,
        )
        allowed, reason = controller.check_limits(10000, 0)
        assert allowed is True
        assert reason == ""

    def test_blocks_max_trades(self):
        """Should block when max trades reached."""
        controller = RiskController(
            {"max_daily_trades": 2},
            initial_capital=10000,
        )
        controller.record_trade(100)
        controller.record_trade(-50)

        allowed, reason = controller.check_limits(10050, 0)
        assert allowed is False
        assert "Max daily trades" in reason

    def test_blocks_max_drawdown_compound(self):
        """Should block when drawdown exceeds limit (compound mode)."""
        controller = RiskController(
            {"max_drawdown": 0.1},
            initial_capital=10000,
            capital_mode="compound",
        )
        # Peak is 10000, current is 8900 = 11% drawdown
        allowed, reason = controller.check_limits(8900, 0)
        assert allowed is False
        assert "drawdown" in reason

    def test_blocks_max_drawdown_fixed(self):
        """Should block when drawdown exceeds limit (fixed capital mode)."""
        controller = RiskController(
            {"max_drawdown": 0.1},
            initial_capital=10000,
            capital_mode="fixed_capital",
        )
        # Initial is 10000, current is 8900 = 11% drawdown
        allowed, reason = controller.check_limits(8900, 0)
        assert allowed is False
        assert "drawdown" in reason

    def test_blocks_daily_loss(self):
        """Should block when daily loss limit reached."""
        controller = RiskController(
            {"max_daily_loss": 0.05},
            initial_capital=10000,
        )
        controller.record_trade(-600)  # 6% loss

        allowed, reason = controller.check_limits(9400, 0)
        assert allowed is False
        assert "Daily loss" in reason

    def test_blocks_consecutive_losses(self):
        """Should block after consecutive losses."""
        controller = RiskController(
            {"consecutive_loss_pause": 3},
            initial_capital=10000,
        )
        controller.record_trade(-100)
        controller.record_trade(-100)
        controller.record_trade(-100)

        allowed, reason = controller.check_limits(9700, 0)
        assert allowed is False
        assert "consecutive losses" in reason

    def test_consecutive_losses_reset_on_win(self):
        """Consecutive losses should reset on win."""
        controller = RiskController(
            {"consecutive_loss_pause": 3},
            initial_capital=10000,
        )
        controller.record_trade(-100)
        controller.record_trade(-100)
        controller.record_trade(50)  # Win resets counter
        controller.record_trade(-100)

        assert controller.consecutive_losses == 1
        allowed, _ = controller.check_limits(9850, 0)
        assert allowed is True

    def test_update_peak_equity_compound(self):
        """Should update peak in compound mode."""
        controller = RiskController({}, 10000, "compound")
        controller.update_peak_equity(11000)

        # 10% drawdown from new peak = 9900
        allowed, _ = controller.check_limits(9900, 0)
        assert allowed is True  # No drawdown limit set

        # Check state
        assert controller._state.peak_equity == Decimal("11000")

    def test_no_peak_update_fixed_capital(self):
        """Should not update peak in fixed capital mode."""
        controller = RiskController({}, 10000, "fixed_capital")
        controller.update_peak_equity(11000)

        # Peak should remain at initial
        assert controller._state.peak_equity == Decimal("10000")

    def test_pause_blocks_trading(self):
        """Should block trading during pause."""
        controller = RiskController(
            {"pause_duration": 3600},
            initial_capital=10000,
        )
        current_ts = 1000 * 1_000_000_000  # 1000 seconds in ns
        controller.apply_pause(current_ts)

        # Check during pause
        check_ts = 1500 * 1_000_000_000  # 500s later
        allowed, reason = controller.check_limits(10000, check_ts)
        assert allowed is False
        assert "paused" in reason

        # Check after pause
        check_ts = 5000 * 1_000_000_000  # 4000s later
        allowed, _ = controller.check_limits(10000, check_ts)
        assert allowed is True

    def test_reset_session(self):
        """Reset should clear session counters."""
        controller = RiskController(
            {"max_daily_trades": 2},
            initial_capital=10000,
        )
        controller.record_trade(100)
        controller.record_trade(-50)
        controller.reset_session()

        assert controller._state.session_trade_count == 0
        assert controller._state.session_pnl == Decimal("0")
        # Consecutive losses preserved
        assert controller.consecutive_losses == 1

    def test_daily_loss_resets_after_day_boundary(self):
        """Daily-loss limit must reset at the UTC reset_time boundary.

        Regression: the daily session counters were never reset (reset_session had
        no caller and reset_time was orphaned), so 'daily' loss was really
        'since-startup cumulative' and a tripped limit never recovered.
        """
        controller = RiskController(
            {"max_daily_loss": 0.05, "reset_time": "00:00"},
            initial_capital=10000,
        )
        controller.record_trade(-600)  # 6% loss

        day1_noon = _ns(datetime(2026, 6, 22, 12, 0, tzinfo=UTC))
        allowed, reason = controller.check_limits(9400, day1_noon)
        assert allowed is False
        assert "Daily loss" in reason

        # Crossing the next UTC midnight resets the daily session -> allowed again
        day2_morning = _ns(datetime(2026, 6, 23, 1, 0, tzinfo=UTC))
        allowed, _ = controller.check_limits(9400, day2_morning)
        assert allowed is True
        assert controller.session_pnl == Decimal("0")

    def test_pause_duration_zero_pauses_until_next_day(self):
        """pause_duration=0 must pause until the next reset boundary, not 0 seconds.

        Regression: apply_pause read 0 literally -> paused_until == current_ts, so
        the pause expired on the very next bar and consecutive-loss protection was
        effectively a single-bar skip.
        """
        controller = RiskController(
            {"consecutive_loss_pause": 3, "pause_duration": 0, "reset_time": "00:00"},
            initial_capital=10000,
        )
        controller.record_trade(-100)
        controller.record_trade(-100)
        controller.record_trade(-100)

        day1_noon = _ns(datetime(2026, 6, 22, 12, 0, tzinfo=UTC))
        allowed, reason = controller.check_limits(9700, day1_noon)
        assert allowed is False
        assert "consecutive losses" in reason

        # Same day, hours later -> still paused (not just one bar)
        day1_evening = _ns(datetime(2026, 6, 22, 18, 0, tzinfo=UTC))
        allowed, reason = controller.check_limits(9700, day1_evening)
        assert allowed is False
        assert "paused" in reason

        # Next day after the boundary -> pause expired and session reset
        day2_morning = _ns(datetime(2026, 6, 23, 1, 0, tzinfo=UTC))
        allowed, _ = controller.check_limits(9700, day2_morning)
        assert allowed is True
