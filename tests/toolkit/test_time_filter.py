# tests/test_time_filter.py
"""Tests for TimeFilter — the single time-of-day + weekday trading filter."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from custos_toolkit.filters import TimeFilter, create_filter
from custos_toolkit.filters.time_filter import parse_trading_hours


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0  # nanoseconds


def _bar_at(dt: datetime) -> MockBar:
    return MockBar(timestamp=int(dt.timestamp() * 1e9))


class TestTimeFilterHourWindow:
    def test_minute_granularity_within_and_before_window(self):
        """09:30-17:00 must gate at minute precision: 09:15 blocked, 09:30 allowed."""
        f = TimeFilter({"enabled": True, "trading_hours": "09:30-17:00"})
        assert f.check(_bar_at(datetime(2026, 6, 15, 9, 30, tzinfo=UTC))).passed is True
        assert f.check(_bar_at(datetime(2026, 6, 15, 9, 45, tzinfo=UTC))).passed is True
        # 09:15 is inside hour 9 but before 09:30 — an hour-level filter would wrongly allow it.
        assert f.check(_bar_at(datetime(2026, 6, 15, 9, 15, tzinfo=UTC))).passed is False

    def test_blocks_after_window(self):
        f = TimeFilter({"enabled": True, "trading_hours": "09:30-17:00"})
        assert f.check(_bar_at(datetime(2026, 6, 15, 18, 0, tzinfo=UTC))).passed is False

    def test_overnight_window(self):
        f = TimeFilter({"enabled": True, "trading_hours": "22:00-06:00"})
        assert f.check(_bar_at(datetime(2026, 6, 15, 23, 0, tzinfo=UTC))).passed is True
        assert f.check(_bar_at(datetime(2026, 6, 16, 3, 0, tzinfo=UTC))).passed is True
        assert f.check(_bar_at(datetime(2026, 6, 15, 12, 0, tzinfo=UTC))).passed is False

    def test_full_day_sentinel_allows_all_hours(self):
        """ "00:00-23:59" is the full-day sentinel — no hour gating, including 23:59."""
        f = TimeFilter({"enabled": True, "trading_hours": "00:00-23:59"})
        for hour in (0, 12, 23):
            assert f.check(_bar_at(datetime(2026, 6, 15, hour, 59, tzinfo=UTC))).passed is True


class TestTimeFilterWeekday:
    def test_excluded_days_block_listed_weekdays(self):
        """excluded_days uses 0=Mon..6=Sun; listed days blocked, others allowed."""
        f = TimeFilter({"enabled": True, "excluded_days": [5, 6]})
        base = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        saturday = base + timedelta(days=(5 - base.weekday()) % 7)
        sunday = base + timedelta(days=(6 - base.weekday()) % 7)
        monday = base + timedelta(days=(0 - base.weekday()) % 7)
        assert saturday.weekday() == 5 and sunday.weekday() == 6 and monday.weekday() == 0
        assert f.check(_bar_at(saturday)).passed is False
        assert f.check(_bar_at(sunday)).passed is False
        assert f.check(_bar_at(monday)).passed is True


class TestTimeFilterMisc:
    def test_disabled_allows_all(self):
        f = TimeFilter({"enabled": False})
        assert f.check(_bar_at(datetime(2026, 6, 20, 3, 0, tzinfo=UTC))).passed is True

    def test_excluded_dates_block(self):
        f = TimeFilter({"enabled": True, "excluded_dates": ["2026-06-17"]})
        assert f.check(_bar_at(datetime(2026, 6, 17, 12, 0, tzinfo=UTC))).passed is False
        assert f.check(_bar_at(datetime(2026, 6, 18, 12, 0, tzinfo=UTC))).passed is True

    def test_ready_immediately(self):
        assert TimeFilter({"enabled": True}).is_ready() is True

    def test_malformed_trading_hours_fails_fast(self):
        """A malformed window must raise at construction, not silently allow all trades."""
        with pytest.raises(ValueError):
            TimeFilter({"enabled": True, "trading_hours": "garbage"})

    def test_create_via_registry(self):
        from custos_toolkit.filters.registry import is_filter_registered, register_filter

        if not is_filter_registered("time"):
            register_filter("time")(TimeFilter)
        f = create_filter("time", {"enabled": True, "trading_hours": "09:30-17:00"})
        assert isinstance(f, TimeFilter)
        assert f.trading_hours == "09:30-17:00"


class TestParseTradingHoursStrict:
    """parse_trading_hours must reject out-of-range clock values, not silently
    accept them (an invalid window would otherwise block all real minutes)."""

    @pytest.mark.parametrize(
        "bad",
        ["24:00-25:00", "09:60-10:00", "9-17", "09:00", "", "noon-evening"],
    )
    def test_invalid_clock_values_raise(self, bad):
        with pytest.raises(ValueError):
            parse_trading_hours(bad)

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_trading_hours(None)  # type: ignore[arg-type]

    def test_timefilter_rejects_out_of_range_window(self):
        with pytest.raises(ValueError):
            TimeFilter({"enabled": True, "trading_hours": "24:00-25:00"})

    def test_valid_boundary_values_accepted(self):
        assert parse_trading_hours("00:00-23:59") == (0, 1439)
