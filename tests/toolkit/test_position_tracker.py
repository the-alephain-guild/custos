# tests/test_position_tracker.py
"""Tests for PositionTracker."""

from decimal import Decimal

from custos_toolkit.position import PositionTracker


class TestPositionTracker:
    """Tests for PositionTracker."""

    def test_initial_state(self):
        """Should start with no position."""
        tracker = PositionTracker()
        assert tracker.entry_count == 0
        assert tracker.total_quantity == Decimal("0")
        assert tracker.has_position is False

    def test_record_first_entry(self):
        """Should record first entry correctly."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)

        assert tracker.entry_count == 1
        assert tracker.total_quantity == Decimal("10")
        assert tracker.avg_entry_price == Decimal("100")
        assert tracker.last_entry_price == Decimal("100")
        assert tracker.has_position is True

    def test_record_multiple_entries(self):
        """Should calculate weighted average for multiple entries."""
        tracker = PositionTracker()

        # First entry: 10 @ 100 = 1000
        tracker.record_entry(100.0, 10.0)

        # Second entry: 10 @ 110 = 1100
        tracker.record_entry(110.0, 10.0)

        # Total: 20 units, avg = (1000 + 1100) / 20 = 105
        assert tracker.entry_count == 2
        assert tracker.total_quantity == Decimal("20")
        assert tracker.avg_entry_price == Decimal("105")
        assert tracker.last_entry_price == Decimal("110")

    def test_weighted_average_unequal_quantities(self):
        """Should weight average by quantity."""
        tracker = PositionTracker()

        # 10 @ 100 = 1000
        tracker.record_entry(100.0, 10.0)

        # 30 @ 120 = 3600
        tracker.record_entry(120.0, 30.0)

        # Total: 40 units, avg = 4600 / 40 = 115
        assert tracker.avg_entry_price == Decimal("115")

    def test_record_partial_exit(self):
        """Should reduce quantity on partial exit."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 20.0)
        tracker.record_partial_exit(5.0)

        assert tracker.total_quantity == Decimal("15")
        assert tracker.entry_count == 1  # Entry count unchanged

    def test_reset(self):
        """Reset should clear all state."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)
        tracker.reset()

        assert tracker.entry_count == 0
        assert tracker.total_quantity == Decimal("0")
        assert tracker.avg_entry_price == Decimal("0")
        assert tracker.has_position is False

    def test_unrealized_pnl_long(self):
        """Should calculate unrealized P&L for long position."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)

        # Price up to 110: PnL = (110-100) * 10 = 100
        pnl = tracker.get_unrealized_pnl(110.0, is_long=True)
        assert pnl == Decimal("100")

        # Price down to 90: PnL = (90-100) * 10 = -100
        pnl = tracker.get_unrealized_pnl(90.0, is_long=True)
        assert pnl == Decimal("-100")

    def test_unrealized_pnl_short(self):
        """Should calculate unrealized P&L for short position."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)

        # Price down to 90: PnL = (100-90) * 10 = 100
        pnl = tracker.get_unrealized_pnl(90.0, is_long=False)
        assert pnl == Decimal("100")

        # Price up to 110: PnL = (100-110) * 10 = -100
        pnl = tracker.get_unrealized_pnl(110.0, is_long=False)
        assert pnl == Decimal("-100")

    def test_unrealized_pnl_no_position(self):
        """Should return 0 with no position."""
        tracker = PositionTracker()
        assert tracker.get_unrealized_pnl(100.0) == Decimal("0")

    def test_should_scale_in_first_entry_always_allowed(self):
        tracker = PositionTracker()
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        assert tracker.should_scale_in(100.0, True, config) is True

    def test_should_scale_in_disabled_returns_false(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        config = {"enabled": False}
        assert tracker.should_scale_in(95.0, True, config) is False

    def test_should_scale_in_max_entries_reached(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        tracker.record_entry(98.0, 1.0)
        tracker.record_entry(96.0, 1.0)
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        assert tracker.should_scale_in(94.0, True, config) is False

    def test_should_scale_in_long_price_dropped_enough(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        # Price dropped 3% (> 2% threshold)
        assert tracker.should_scale_in(97.0, True, config) is True

    def test_should_scale_in_long_price_not_dropped_enough(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        # Price dropped 1% (< 2% threshold)
        assert tracker.should_scale_in(99.0, True, config) is False

    def test_should_scale_in_long_price_went_up(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        # Price went up - no scaling for long
        assert tracker.should_scale_in(102.0, True, config) is False

    def test_should_scale_in_short_price_rose_enough(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        # Price rose 3% (> 2% threshold) - good for short scaling
        assert tracker.should_scale_in(103.0, False, config) is True

    def test_should_scale_in_short_price_dropped(self):
        tracker = PositionTracker()
        tracker.record_entry(100.0, 1.0)
        config = {"enabled": True, "max_entries": 3, "entry_interval_pct": 0.02}
        # Price dropped - no scaling for short
        assert tracker.should_scale_in(97.0, False, config) is False

    def test_first_entry_price_set_on_first_entry(self):
        """First entry price should be captured on first entry only."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)

        assert tracker.first_entry_price == Decimal("100")

    def test_first_entry_price_unchanged_on_subsequent_entries(self):
        """First entry price should not change on subsequent entries."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)
        tracker.record_entry(110.0, 10.0)

        assert tracker.first_entry_price == Decimal("100")
        assert tracker.last_entry_price == Decimal("110")

    def test_first_entry_price_reset_on_reset(self):
        """First entry price should be cleared on reset."""
        tracker = PositionTracker()
        tracker.record_entry(100.0, 10.0)
        tracker.reset()

        assert tracker.first_entry_price == Decimal("0")
