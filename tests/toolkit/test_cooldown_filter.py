# tests/test_cooldown_filter.py
"""Tests for CooldownFilter."""

from dataclasses import dataclass

from custos_toolkit.filters import create_filter
from custos_toolkit.filters.cooldown import CooldownFilter


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0


class TestCooldownFilter:
    """Tests for CooldownFilter."""

    def test_allows_without_prior_exit(self):
        """Should allow trading with no prior exit."""
        f = CooldownFilter({"enabled": True, "after_exit": 60})
        bar = MockBar(timestamp=1000 * 1_000_000_000)
        assert f.check(bar).passed is True

    def test_blocks_during_cooldown(self):
        """Should block during cooldown period."""
        f = CooldownFilter({"enabled": True, "after_exit": 60})

        # Record exit at t=1000
        f.record_exit(1000 * 1_000_000_000, "exit")

        # Check at t=1030 (30s later) - should block
        bar = MockBar(timestamp=1030 * 1_000_000_000)
        result = f.check(bar)
        assert result.passed is False
        assert "30s remaining" in result.reason

    def test_allows_after_cooldown(self):
        """Should allow after cooldown period."""
        f = CooldownFilter({"enabled": True, "after_exit": 60})

        # Record exit at t=1000
        f.record_exit(1000 * 1_000_000_000, "exit")

        # Check at t=1070 (70s later) - should allow
        bar = MockBar(timestamp=1070 * 1_000_000_000)
        assert f.check(bar).passed is True

    def test_stop_loss_cooldown(self):
        """Should use longer cooldown after stop loss."""
        f = CooldownFilter(
            {
                "enabled": True,
                "after_exit": 60,
                "after_stop_loss": 300,
            }
        )

        # Record stop loss at t=1000
        f.record_exit(1000 * 1_000_000_000, "stop_loss")

        # Check at t=1100 (100s later) - should block (need 300s)
        bar = MockBar(timestamp=1100 * 1_000_000_000)
        result = f.check(bar)
        assert result.passed is False
        assert "stop_loss" in result.reason

        # Check at t=1310 (310s later) - should allow
        bar2 = MockBar(timestamp=1310 * 1_000_000_000)
        assert f.check(bar2).passed is True

    def test_take_profit_no_cooldown(self):
        """Should allow immediately after take profit if cooldown is 0."""
        f = CooldownFilter(
            {
                "enabled": True,
                "after_take_profit": 0,
            }
        )

        f.record_exit(1000 * 1_000_000_000, "take_profit")

        # Should allow immediately
        bar = MockBar(timestamp=1001 * 1_000_000_000)
        assert f.check(bar).passed is True

    def test_allows_when_disabled(self):
        """Should allow all when disabled."""
        f = CooldownFilter({"enabled": False})
        f.record_exit(1000 * 1_000_000_000, "stop_loss")

        bar = MockBar(timestamp=1001 * 1_000_000_000)
        assert f.check(bar).passed is True

    def test_reset_clears_state(self):
        """Reset should clear cooldown state."""
        f = CooldownFilter({"enabled": True, "after_exit": 60})
        f.record_exit(1000 * 1_000_000_000, "exit")
        f.reset()

        bar = MockBar(timestamp=1001 * 1_000_000_000)
        assert f.check(bar).passed is True

    def test_create_via_registry(self):
        """Should be creatable via registry."""
        # Ensure CooldownFilter is registered (re-register if cleared by other tests)
        from custos_toolkit.filters.registry import is_filter_registered, register_filter

        if not is_filter_registered("cooldown"):
            register_filter("cooldown")(CooldownFilter)

        f = create_filter("cooldown", {"after_stop_loss": 600})
        assert isinstance(f, CooldownFilter)
        assert f.after_stop_loss == 600
