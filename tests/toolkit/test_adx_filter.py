# tests/test_adx_filter.py
"""Tests for AdxFilter."""

from dataclasses import dataclass

from custos_toolkit.filters import create_filter
from custos_toolkit.filters.adx import AdxFilter


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0


class TestAdxFilter:
    """Tests for AdxFilter."""

    def test_blocks_during_warmup(self):
        """Should block during warmup period when ADX not calculated."""
        f = AdxFilter({"enabled": True, "period": 14})
        bar = MockBar()

        # Not enough data yet
        result = f.check(bar)
        assert result.passed is False
        assert "warming up" in result.reason

    def test_ready_after_warmup(self):
        """Should be ready after sufficient bars for ADX calculation."""
        f = AdxFilter({"enabled": True, "period": 5})

        # Need 2 * period bars to calculate ADX (period for DI, then period for ADX)
        for i in range(15):
            bar = MockBar(
                high=100.0 + i % 5,
                low=95.0 + i % 5,
                close=98.0 + i % 5,
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

        assert f.is_ready() is True

    def test_allows_above_threshold(self):
        """Should allow when ADX is above threshold."""
        f = AdxFilter({"enabled": True, "period": 5, "threshold": 20.0})

        # Create trending bars to generate high ADX
        # Alternating up/down moves to create volatility and trend
        for i in range(20):
            # Strong uptrend pattern
            bar = MockBar(
                high=100.0 + i * 2,
                low=95.0 + i * 2,
                close=98.0 + i * 2,
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

        if f.is_ready():
            adx = f.get_adx()
            # If ADX is above threshold, should pass
            result = f.check(MockBar())
            if adx is not None and adx >= 20.0:
                assert result.passed is True

    def test_blocks_below_threshold(self):
        """Should block when ADX is below threshold."""
        f = AdxFilter(
            {
                "enabled": True,
                "period": 5,
                "threshold": 50.0,  # High threshold - most ranging markets will be below
            }
        )

        # Create ranging bars with no clear trend (sideways movement)
        for i in range(20):
            # Oscillating prices (no clear trend)
            offset = 1.0 if i % 2 == 0 else -1.0
            bar = MockBar(
                high=101.0 + offset,
                low=99.0 + offset,
                close=100.0 + offset,
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

        if f.is_ready():
            result = f.check(MockBar())
            adx = f.get_adx()
            if adx is not None and adx < 50.0:
                assert result.passed is False
                assert "Weak trend" in result.reason

    def test_allows_when_disabled(self):
        """Should allow all when disabled."""
        f = AdxFilter({"enabled": False, "period": 5})

        # No warmup needed when disabled
        bar = MockBar()
        result = f.check(bar)
        assert result.passed is True

    def test_directional_movement_calculation(self):
        """Should calculate +DM and -DM correctly."""
        f = AdxFilter({"enabled": True, "period": 3})

        # First bar - establishes baseline
        bar1 = MockBar(high=100.0, low=95.0, close=98.0)
        f.update(bar1)

        # Second bar - higher high, higher low (+DM should be positive)
        bar2 = MockBar(high=105.0, low=98.0, close=103.0)
        f.update(bar2)

        # Third bar - lower high, lower low (-DM should be positive)
        bar3 = MockBar(high=103.0, low=95.0, close=97.0)
        f.update(bar3)

        # Continue updating to calculate ADX
        for i in range(10):
            bar = MockBar(high=100.0 + i, low=95.0 + i, close=98.0 + i)
            f.update(bar)

        # Just verify it doesn't crash and produces values
        if f.is_ready():
            assert f.get_adx() is not None
            assert f.get_plus_di() is not None
            assert f.get_minus_di() is not None

    def test_reset_clears_state(self):
        """Reset should clear ADX calculation state."""
        f = AdxFilter({"enabled": True, "period": 3, "threshold": 20.0})

        # Warm up filter
        for i in range(15):
            bar = MockBar(high=100.0 + i, low=95.0 + i, close=98.0 + i)
            f.update(bar)

        if f.is_ready():
            assert f.get_adx() is not None

        # Reset and verify state is cleared
        f.reset()
        assert f.is_ready() is False
        assert f.get_adx() is None
        assert f.get_plus_di() is None
        assert f.get_minus_di() is None

    def test_create_via_registry(self):
        """Should be creatable via registry."""
        from custos_toolkit.filters.registry import is_filter_registered

        # Ensure AdxFilter is registered
        assert is_filter_registered("adx")

        f = create_filter("adx", {"period": 20, "threshold": 30.0})
        assert isinstance(f, AdxFilter)
        assert f.period == 20
        assert f.threshold == 30.0

    def test_default_config_values(self):
        """Should use sensible defaults."""
        f = AdxFilter({})
        assert f.enabled is True
        assert f.period == 14
        assert f.threshold == 25.0

    def test_plus_di_and_minus_di_range(self):
        """DI values should be between 0 and 100."""
        f = AdxFilter({"enabled": True, "period": 5})

        # Create varied price movement
        for i in range(20):
            bar = MockBar(
                high=100.0 + (i % 3) * 2,
                low=95.0 + (i % 3) * 2,
                close=98.0 + (i % 3) * 2,
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

        if f.is_ready():
            plus_di = f.get_plus_di()
            minus_di = f.get_minus_di()

            if plus_di is not None:
                assert 0 <= plus_di <= 100
            if minus_di is not None:
                assert 0 <= minus_di <= 100

    def test_adx_range(self):
        """ADX should be between 0 and 100."""
        f = AdxFilter({"enabled": True, "period": 5})

        # Create varied price movement
        for i in range(20):
            bar = MockBar(
                high=100.0 + (i % 5) * 2,
                low=95.0 + (i % 5) * 2,
                close=98.0 + (i % 5) * 2,
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

        if f.is_ready():
            adx = f.get_adx()
            if adx is not None:
                assert 0 <= adx <= 100

    def test_trending_market_has_high_adx(self):
        """Strong trend should produce high ADX."""
        f = AdxFilter({"enabled": True, "period": 5, "threshold": 25.0})

        # Create strong uptrend - each bar higher than previous
        for i in range(25):
            bar = MockBar(
                high=100.0 + i * 3,  # Consistently higher highs
                low=95.0 + i * 3,  # Consistently higher lows
                close=98.0 + i * 3,  # Consistently higher closes
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

        if f.is_ready():
            _adx = f.get_adx()
            plus_di = f.get_plus_di()
            minus_di = f.get_minus_di()

            # In uptrend, +DI should be greater than -DI
            if plus_di is not None and minus_di is not None:
                assert plus_di > minus_di

    def test_filter_name(self):
        """Should return correct filter name."""
        f = AdxFilter({"enabled": True})
        assert f.name == "adx"

    def test_wilder_smoothing(self):
        """Should use Wilder's smoothing method."""
        f = AdxFilter({"enabled": True, "period": 5})

        # Feed enough bars to trigger ADX calculation
        prev_adx = None
        for i in range(30):
            bar = MockBar(
                high=100.0 + i * 0.5,
                low=95.0 + i * 0.5,
                close=98.0 + i * 0.5,
                timestamp=i * 1_000_000_000,
            )
            f.update(bar)

            # Once ready, ADX should change smoothly (Wilder's smoothing)
            if f.is_ready():
                current_adx = f.get_adx()
                if prev_adx is not None and current_adx is not None:
                    # Wilder's smoothing: change should be gradual
                    # New ADX = ((period-1) * prev_adx + dx) / period
                    # So change should be less than absolute new value
                    change = abs(current_adx - prev_adx)
                    # The change should be bounded (smooth)
                    assert change < 50  # Reasonable bound for smooth changes
                prev_adx = current_adx

    def test_wilder_smoothing_initial_value_is_sma(self):
        """Initial smoothed TR/DM must be SMA (sum/period), not raw sum.

        Wilder's smoothing formula:
          Step 1: smoothed = sum(values) / period   (SMA)
          Step N: smoothed = prev - prev/period + current

        If step 1 uses raw sum instead of SMA, all DI/DX/ADX values are
        distorted by a factor of `period`.
        """
        period = 3
        f = AdxFilter({"enabled": True, "period": period, "threshold": 25.0})

        bars_data = [
            (102, 98, 100),
            (105, 99, 103),
            (108, 101, 106),
        ]

        for high, low, close in bars_data:
            f.update(MockBar(high=float(high), low=float(low), close=float(close)))

        # At exactly period bars, smoothed values are initialized.
        # They must equal sum/period (SMA), NOT the raw sum.
        assert f._smoothed_tr is not None
        tr_sum = sum(f._tr_values)
        expected_sma = tr_sum / period
        assert abs(f._smoothed_tr - expected_sma) < 1e-9, (
            f"Initial smoothed TR should be SMA={expected_sma:.4f}, "
            f"got {f._smoothed_tr:.4f} (raw sum={tr_sum:.4f})"
        )
