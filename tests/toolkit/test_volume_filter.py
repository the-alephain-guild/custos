# tests/test_volume_filter.py
"""Tests for VolumeFilter."""

from dataclasses import dataclass

import pytest
from custos_toolkit.filters import create_filter
from custos_toolkit.filters.volume import VolumeFilter


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0


class TestVolumeFilter:
    """Tests for VolumeFilter."""

    def test_not_ready_before_warmup(self):
        """Should not be ready until enough bars are processed."""
        f = VolumeFilter({"enabled": True, "ma_period": 5})
        assert f.is_ready() is False

        # Feed 4 bars - still not ready
        for _ in range(4):
            f.update(MockBar(volume=1000.0))
        assert f.is_ready() is False

        # Feed 5th bar - now ready
        f.update(MockBar(volume=1000.0))
        assert f.is_ready() is True

    def test_blocks_during_warmup(self):
        """Should block trades during warmup period."""
        f = VolumeFilter({"enabled": True, "ma_period": 5})
        bar = MockBar(volume=1000.0)

        result = f.check(bar)
        assert result.passed is False
        assert "warming up" in result.reason

    def test_allows_high_volume_ema(self):
        """Should allow when volume exceeds threshold (EMA)."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 5,
                "threshold": 1.0,
                "ma_type": "ema",
            }
        )

        # Warm up with consistent volume
        for _ in range(5):
            f.update(MockBar(volume=1000.0))

        # Check with high volume - should allow
        high_vol_bar = MockBar(volume=1500.0)
        result = f.check(high_vol_bar)
        assert result.passed is True
        assert result.size_factor > 1.0  # Size factor boost

    def test_blocks_low_volume_ema(self):
        """Should block when volume is below threshold (EMA)."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 5,
                "threshold": 1.5,  # Require 1.5x average
                "ma_type": "ema",
            }
        )

        # Warm up with consistent volume
        for _ in range(5):
            f.update(MockBar(volume=1000.0))

        # Check with normal volume - should block (need 1.5x)
        bar = MockBar(volume=1000.0)
        result = f.check(bar)
        assert result.passed is False
        assert "below threshold" in result.reason

    def test_allows_high_volume_sma(self):
        """Should allow when volume exceeds threshold (SMA)."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 5,
                "threshold": 1.0,
                "ma_type": "sma",
            }
        )

        # Warm up with consistent volume
        for _ in range(5):
            f.update(MockBar(volume=1000.0))

        # Check with high volume - should allow
        high_vol_bar = MockBar(volume=1200.0)
        result = f.check(high_vol_bar)
        assert result.passed is True

    def test_blocks_low_volume_sma(self):
        """Should block when volume is below threshold (SMA)."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 5,
                "threshold": 1.5,
                "ma_type": "sma",
            }
        )

        # Warm up
        for _ in range(5):
            f.update(MockBar(volume=1000.0))

        # Check with normal volume - should block
        bar = MockBar(volume=1000.0)
        result = f.check(bar)
        assert result.passed is False

    def test_sma_calculation(self):
        """Should correctly calculate SMA."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 3,
                "ma_type": "sma",
            }
        )

        # Feed bars: 100, 200, 300 -> avg = 200
        f.update(MockBar(volume=100.0))
        f.update(MockBar(volume=200.0))
        f.update(MockBar(volume=300.0))

        assert f.is_ready() is True
        assert f.current_ma == 200.0

        # Add another bar: 200, 300, 400 -> avg = 300
        f.update(MockBar(volume=400.0))
        assert f.current_ma == 300.0

    def test_ema_calculation(self):
        """Should correctly calculate EMA."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 3,
                "ma_type": "ema",
            }
        )

        # Feed first bar
        f.update(MockBar(volume=100.0))
        assert f.current_ma == 100.0

        # Feed second bar: EMA = 0.5 * 200 + 0.5 * 100 = 150
        # alpha = 2 / (3 + 1) = 0.5
        f.update(MockBar(volume=200.0))
        assert f.current_ma == 150.0

        # Feed third bar: EMA = 0.5 * 300 + 0.5 * 150 = 225
        f.update(MockBar(volume=300.0))
        assert f.current_ma == 225.0

    def test_size_factor_boost(self):
        """Should boost size factor for high volume."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 5,
                "threshold": 1.0,
                "ma_type": "ema",
            }
        )

        # Warm up with 1000 volume
        for _ in range(5):
            f.update(MockBar(volume=1000.0))

        # Check with 1.5x volume - should get ~1.5 size factor
        bar = MockBar(volume=1500.0)
        result = f.check(bar)
        assert result.passed is True
        assert 1.4 <= result.size_factor <= 1.6

    def test_size_factor_capped(self):
        """Size factor should be capped at 2.0."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 5,
                "threshold": 1.0,
                "ma_type": "ema",
            }
        )

        # Warm up with low volume
        for _ in range(5):
            f.update(MockBar(volume=100.0))

        # Check with 5x volume - should cap at 2.0
        bar = MockBar(volume=500.0)
        result = f.check(bar)
        assert result.passed is True
        assert result.size_factor == 2.0

    def test_allows_when_disabled(self):
        """Should allow all when disabled."""
        f = VolumeFilter({"enabled": False})

        # No warmup, but should still allow
        bar = MockBar(volume=1.0)
        result = f.check(bar)
        assert result.passed is True

    def test_reset_clears_state(self):
        """Reset should clear all state."""
        f = VolumeFilter({"enabled": True, "ma_period": 5})

        # Warm up
        for _ in range(5):
            f.update(MockBar(volume=1000.0))
        assert f.is_ready() is True
        assert f.bar_count == 5

        # Reset
        f.reset()
        assert f.is_ready() is False
        assert f.bar_count == 0
        assert f.current_ma is None

    def test_invalid_ma_type_raises(self):
        """Should raise error for invalid ma_type."""
        with pytest.raises(ValueError, match="Invalid ma_type"):
            VolumeFilter({"ma_type": "invalid"})

    def test_default_config(self):
        """Should have sensible defaults."""
        f = VolumeFilter({})
        assert f.enabled is True
        assert f._ma_period == 20
        assert f._threshold == 1.0
        assert f._ma_type == "ema"

    def test_create_via_registry(self):
        """Should be creatable via registry."""
        # Ensure VolumeFilter is registered (re-register if cleared by other tests)
        from custos_toolkit.filters.registry import is_filter_registered, register_filter

        if not is_filter_registered("volume"):
            register_filter("volume")(VolumeFilter)

        f = create_filter("volume", {"ma_period": 10, "threshold": 1.2})
        assert isinstance(f, VolumeFilter)
        assert f._ma_period == 10
        assert f._threshold == 1.2

    def test_update_without_check(self):
        """Update should work independently of check."""
        f = VolumeFilter({"enabled": True, "ma_period": 3})

        # Just update, don't check
        for _ in range(10):
            f.update(MockBar(volume=1000.0))

        assert f.is_ready() is True
        assert f.bar_count == 10

    def test_threshold_exact_match(self):
        """Should allow when volume exactly matches threshold."""
        f = VolumeFilter(
            {
                "enabled": True,
                "ma_period": 3,
                "threshold": 1.0,
                "ma_type": "sma",
            }
        )

        # Warm up with 1000 volume
        for _ in range(3):
            f.update(MockBar(volume=1000.0))

        # Check with exactly 1000 (1.0x average)
        bar = MockBar(volume=1000.0)
        result = f.check(bar)
        assert result.passed is True
        assert result.size_factor == 1.0

    def test_filter_name(self):
        """Should return correct filter name."""
        f = VolumeFilter({})
        assert f.name == "volume"
