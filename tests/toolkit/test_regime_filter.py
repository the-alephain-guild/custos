# tests/test_regime_filter.py
"""Tests for RegimeFilter."""

from dataclasses import dataclass

import pytest
from custos_toolkit.filters import create_filter
from custos_toolkit.filters.regime import RegimeFilter


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0


class TestRegimeFilter:
    """Tests for RegimeFilter."""

    def test_filter_not_ready_without_data(self):
        """Should not be ready until enough data is collected."""
        config = {"enabled": True, "lookback": 20}
        f = RegimeFilter(config)

        assert f.is_ready() is False
        assert f.get_current_regime() == "unknown"

    def test_filter_becomes_ready_after_warmup(self):
        """Should become ready after receiving enough bars."""
        config = {"enabled": True, "lookback": 5}
        f = RegimeFilter(config)

        # Feed 4 bars - not ready yet
        for i in range(4):
            f.update(MockBar(close=100.0 + i))
        assert f.is_ready() is False

        # Feed 5th bar - should be ready
        f.update(MockBar(close=104.0))
        assert f.is_ready() is True

    def test_efficiency_ratio_detects_trending(self):
        """Efficiency ratio should detect trending market (strong directional move)."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "efficiency_ratio",
            "trending_threshold": 0.5,
            "allow_regime": "trending",
        }
        f = RegimeFilter(config)

        # Simulate strong uptrend: 100 -> 101 -> 102 -> 103 -> 104
        # Net change = 4, total change = 4, efficiency = 1.0
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        for price in prices:
            f.update(MockBar(close=price))

        assert f.get_current_regime() == "trending"
        result = f.check(MockBar())
        assert result.passed is True

    def test_efficiency_ratio_detects_ranging(self):
        """Efficiency ratio should detect ranging market (choppy movement)."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "efficiency_ratio",
            "trending_threshold": 0.5,
            "allow_regime": "trending",
        }
        f = RegimeFilter(config)

        # Simulate ranging: 100 -> 102 -> 100 -> 102 -> 100
        # Net change = 0, total change = 8, efficiency = 0.0
        prices = [100.0, 102.0, 100.0, 102.0, 100.0]
        for price in prices:
            f.update(MockBar(close=price))

        assert f.get_current_regime() == "ranging"
        result = f.check(MockBar())
        assert result.passed is False
        assert "ranging" in result.reason.lower()

    def test_atr_percentile_detects_trending(self):
        """ATR percentile should detect trending market (wide price range)."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "atr_percentile",
            "trending_threshold": 0.05,  # 5% range threshold
            "allow_regime": "trending",
        }
        f = RegimeFilter(config)

        # Simulate uptrend with wide range
        # Range = 110 - 100 = 10, avg = 105, range_pct = 0.095 > 0.05
        prices = [100.0, 102.0, 105.0, 108.0, 110.0]
        for price in prices:
            f.update(MockBar(close=price))

        assert f.get_current_regime() == "trending"

    def test_atr_percentile_detects_ranging(self):
        """ATR percentile should detect ranging market (narrow price range)."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "atr_percentile",
            "trending_threshold": 0.05,  # 5% range threshold
            "allow_regime": "trending",
        }
        f = RegimeFilter(config)

        # Simulate ranging with narrow range
        # Range = 101 - 99 = 2, avg = 100, range_pct = 0.02 < 0.05
        prices = [100.0, 100.5, 99.5, 100.0, 101.0]
        for price in prices:
            f.update(MockBar(close=price))

        assert f.get_current_regime() == "ranging"

    def test_allow_ranging_regime(self):
        """Should allow trades when allow_regime is 'ranging' and market is ranging."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "efficiency_ratio",
            "trending_threshold": 0.5,
            "allow_regime": "ranging",
        }
        f = RegimeFilter(config)

        # Simulate ranging market
        prices = [100.0, 102.0, 100.0, 102.0, 100.0]
        for price in prices:
            f.update(MockBar(close=price))

        assert f.get_current_regime() == "ranging"
        result = f.check(MockBar())
        assert result.passed is True

    def test_allow_both_regimes(self):
        """Should allow trades when allow_regime is 'both'."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "efficiency_ratio",
            "trending_threshold": 0.5,
            "allow_regime": "both",
        }
        f = RegimeFilter(config)

        # Simulate trending market
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is True

        # Now simulate ranging market
        f2 = RegimeFilter(config)
        prices = [100.0, 102.0, 100.0, 102.0, 100.0]
        for price in prices:
            f2.update(MockBar(close=price))

        result = f2.check(MockBar())
        assert result.passed is True

    def test_filter_allows_when_disabled(self):
        """Should allow all trades when disabled."""
        config = {"enabled": False, "lookback": 5}
        f = RegimeFilter(config)

        # Don't feed any data
        result = f.check(MockBar())
        assert result.passed is True

    def test_filter_blocks_when_not_ready(self):
        """Should block trades when not ready (insufficient data)."""
        config = {"enabled": True, "lookback": 10}
        f = RegimeFilter(config)

        # Feed only 5 bars when 10 required
        for i in range(5):
            f.update(MockBar(close=100.0 + i))

        result = f.check(MockBar())
        assert result.passed is False
        assert "Insufficient data" in result.reason

    def test_get_efficiency_ratio(self):
        """Should return correct efficiency ratio."""
        config = {"enabled": True, "lookback": 5, "method": "efficiency_ratio"}
        f = RegimeFilter(config)

        # Not ready - should return None
        assert f.get_efficiency_ratio() is None

        # Feed trending data
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        for price in prices:
            f.update(MockBar(close=price))

        ratio = f.get_efficiency_ratio()
        assert ratio is not None
        assert ratio == pytest.approx(1.0)  # Perfect trend

    def test_invalid_method_raises_error(self):
        """Should raise error for invalid method."""
        config = {"enabled": True, "method": "invalid_method"}
        with pytest.raises(ValueError) as exc_info:
            RegimeFilter(config)
        assert "invalid_method" in str(exc_info.value)

    def test_invalid_allow_regime_raises_error(self):
        """Should raise error for invalid allow_regime."""
        config = {"enabled": True, "allow_regime": "invalid_regime"}
        with pytest.raises(ValueError) as exc_info:
            RegimeFilter(config)
        assert "invalid_regime" in str(exc_info.value)

    def test_default_config_values(self):
        """Should use default values when not specified."""
        f = RegimeFilter({})

        assert f.enabled is True
        assert f.lookback == 20
        assert f.method == "efficiency_ratio"
        assert f.trending_threshold == 0.5
        assert f.allow_regime == "trending"

    def test_create_via_registry(self):
        """Should be creatable via registry."""
        from custos_toolkit.filters.registry import is_filter_registered, register_filter

        # Re-register if cleared by other tests
        if not is_filter_registered("regime"):
            register_filter("regime")(RegimeFilter)

        f = create_filter("regime", {"enabled": True, "lookback": 10})
        assert isinstance(f, RegimeFilter)
        assert f.lookback == 10

    def test_sliding_window_behavior(self):
        """Should maintain sliding window of prices."""
        config = {"enabled": True, "lookback": 5, "method": "efficiency_ratio"}
        f = RegimeFilter(config)

        # Initial trending data
        for price in [100.0, 101.0, 102.0, 103.0, 104.0]:
            f.update(MockBar(close=price))
        assert f.get_current_regime() == "trending"

        # Add ranging data - old trending data should slide out
        for price in [104.0, 102.0, 104.0, 102.0, 104.0]:
            f.update(MockBar(close=price))

        # Now the window contains [102, 104, 102, 104, 104] approximately
        # which should be ranging
        assert f.get_current_regime() == "ranging"

    def test_regime_transition(self):
        """Should correctly transition between regimes as market changes."""
        config = {
            "enabled": True,
            "lookback": 5,
            "method": "efficiency_ratio",
            "trending_threshold": 0.5,
        }
        f = RegimeFilter(config)

        # Start with trending
        for price in [100.0, 102.0, 104.0, 106.0, 108.0]:
            f.update(MockBar(close=price))
        assert f.get_current_regime() == "trending"

        # Transition to ranging
        for price in [108.0, 106.0, 108.0, 106.0, 108.0]:
            f.update(MockBar(close=price))
        assert f.get_current_regime() == "ranging"

        # Back to trending
        for price in [108.0, 110.0, 112.0, 114.0, 116.0]:
            f.update(MockBar(close=price))
        assert f.get_current_regime() == "trending"
