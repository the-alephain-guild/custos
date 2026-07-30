# tests/test_momentum_filter.py
"""Tests for MomentumFilter."""

from dataclasses import dataclass

import pytest
from custos_toolkit.filters import create_filter
from custos_toolkit.filters.momentum import MomentumFilter


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0


class TestMomentumFilterRSI:
    """Tests for MomentumFilter with RSI indicator."""

    def test_filter_not_ready_without_data(self):
        """Should not be ready until enough data is collected."""
        config = {"enabled": True, "indicator": "rsi", "rsi_period": 14}
        f = MomentumFilter(config)

        assert f.is_ready() is False
        assert f.get_rsi() is None

    def test_filter_becomes_ready_after_warmup(self):
        """Should become ready after receiving enough bars."""
        config = {"enabled": True, "indicator": "rsi", "rsi_period": 5}
        f = MomentumFilter(config)

        # Feed 5 bars - not ready yet (need period + 1)
        for i in range(5):
            f.update(MockBar(close=100.0 + i))
        assert f.is_ready() is False

        # Feed 6th bar - should be ready
        f.update(MockBar(close=105.0))
        assert f.is_ready() is True

    def test_rsi_calculation_uptrend(self):
        """RSI should be high in consistent uptrend."""
        config = {"enabled": True, "indicator": "rsi", "rsi_period": 5}
        f = MomentumFilter(config)

        # Simulate consistent uptrend: 100 -> 101 -> 102 -> 103 -> 104 -> 105
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        for price in prices:
            f.update(MockBar(close=price))

        rsi = f.get_rsi()
        assert rsi is not None
        assert rsi == 100.0  # All gains, no losses = RSI 100

    def test_rsi_calculation_downtrend(self):
        """RSI should be low in consistent downtrend."""
        config = {"enabled": True, "indicator": "rsi", "rsi_period": 5}
        f = MomentumFilter(config)

        # Simulate consistent downtrend
        prices = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
        for price in prices:
            f.update(MockBar(close=price))

        rsi = f.get_rsi()
        assert rsi is not None
        assert rsi == 0.0  # All losses, no gains = RSI 0

    def test_rsi_calculation_mixed(self):
        """RSI should be around 50 in mixed market."""
        config = {"enabled": True, "indicator": "rsi", "rsi_period": 4}
        f = MomentumFilter(config)

        # Simulate mixed: +1, -1, +1, -1, +1 (3 gains, 2 losses)
        prices = [100.0, 101.0, 100.0, 101.0, 100.0]
        for price in prices:
            f.update(MockBar(close=price))

        rsi = f.get_rsi()
        assert rsi is not None
        # avg_gain = (1+0+1+0) / 4 = 0.5
        # avg_loss = (0+1+0+1) / 4 = 0.5
        # RS = 0.5 / 0.5 = 1, RSI = 100 - (100 / 2) = 50
        assert rsi == pytest.approx(50.0)

    def test_rsi_allows_within_range(self):
        """Should allow when RSI is within configured range."""
        config = {
            "enabled": True,
            "indicator": "rsi",
            "rsi_period": 4,
            "rsi_long_min": 30.0,
            "rsi_long_max": 70.0,
        }
        f = MomentumFilter(config)

        # Create RSI around 50
        prices = [100.0, 101.0, 100.0, 101.0, 100.0]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is True

    def test_rsi_blocks_when_too_low(self):
        """Should block when RSI is below minimum."""
        config = {
            "enabled": True,
            "indicator": "rsi",
            "rsi_period": 5,
            "rsi_long_min": 30.0,
            "rsi_long_max": 70.0,
        }
        f = MomentumFilter(config)

        # Create low RSI (downtrend)
        prices = [105.0, 104.0, 103.0, 102.0, 101.0, 100.0]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is False
        assert "below minimum" in result.reason

    def test_rsi_blocks_when_too_high(self):
        """Should block when RSI is above maximum."""
        config = {
            "enabled": True,
            "indicator": "rsi",
            "rsi_period": 5,
            "rsi_long_min": 30.0,
            "rsi_long_max": 70.0,
        }
        f = MomentumFilter(config)

        # Create high RSI (uptrend)
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is False
        assert "above maximum" in result.reason


class TestMomentumFilterMACD:
    """Tests for MomentumFilter with MACD indicator."""

    def test_macd_not_ready_without_data(self):
        """Should not be ready until enough data for slow EMA + signal."""
        config = {
            "enabled": True,
            "indicator": "macd",
            "macd_fast": 3,
            "macd_slow": 5,
            "macd_signal": 2,
        }
        f = MomentumFilter(config)

        assert f.is_ready() is False
        assert f.get_macd() is None

    def test_macd_becomes_ready_after_warmup(self):
        """Should become ready after slow + signal periods."""
        config = {
            "enabled": True,
            "indicator": "macd",
            "macd_fast": 3,
            "macd_slow": 5,
            "macd_signal": 2,
        }
        f = MomentumFilter(config)

        # Need slow + signal = 7 bars
        for i in range(6):
            f.update(MockBar(close=100.0 + i))
        assert f.is_ready() is False

        f.update(MockBar(close=106.0))
        assert f.is_ready() is True

    def test_macd_positive_histogram_in_uptrend(self):
        """MACD histogram should be positive in strong uptrend."""
        config = {
            "enabled": True,
            "indicator": "macd",
            "macd_fast": 3,
            "macd_slow": 5,
            "macd_signal": 2,
            "macd_histogram_positive": True,
        }
        f = MomentumFilter(config)

        # Strong uptrend
        prices = [100, 102, 104, 106, 108, 110, 112]
        for price in prices:
            f.update(MockBar(close=float(price)))

        macd = f.get_macd()
        assert macd is not None
        assert "histogram" in macd
        assert macd["histogram"] > 0

        result = f.check(MockBar())
        assert result.passed is True

    def test_macd_negative_histogram_in_downtrend(self):
        """MACD histogram should be negative in downtrend."""
        config = {
            "enabled": True,
            "indicator": "macd",
            "macd_fast": 3,
            "macd_slow": 5,
            "macd_signal": 2,
            "macd_histogram_positive": True,
        }
        f = MomentumFilter(config)

        # Strong downtrend
        prices = [112, 110, 108, 106, 104, 102, 100]
        for price in prices:
            f.update(MockBar(close=float(price)))

        macd = f.get_macd()
        assert macd is not None
        assert "histogram" in macd
        assert macd["histogram"] < 0

        result = f.check(MockBar())
        assert result.passed is False
        assert "not positive" in result.reason

    def test_macd_allows_when_histogram_positive_not_required(self):
        """Should allow when histogram_positive is False."""
        config = {
            "enabled": True,
            "indicator": "macd",
            "macd_fast": 3,
            "macd_slow": 5,
            "macd_signal": 2,
            "macd_histogram_positive": False,
        }
        f = MomentumFilter(config)

        # Downtrend - histogram will be negative
        prices = [112, 110, 108, 106, 104, 102, 100]
        for price in prices:
            f.update(MockBar(close=float(price)))

        result = f.check(MockBar())
        assert result.passed is True


class TestMomentumFilterROC:
    """Tests for MomentumFilter with ROC indicator."""

    def test_roc_not_ready_without_data(self):
        """Should not be ready until enough data is collected."""
        config = {"enabled": True, "indicator": "roc", "roc_period": 5}
        f = MomentumFilter(config)

        assert f.is_ready() is False
        assert f.get_roc() is None

    def test_roc_becomes_ready_after_warmup(self):
        """Should become ready after period + 1 bars."""
        config = {"enabled": True, "indicator": "roc", "roc_period": 5}
        f = MomentumFilter(config)

        # Need period + 1 = 6 bars
        for i in range(5):
            f.update(MockBar(close=100.0 + i))
        assert f.is_ready() is False

        f.update(MockBar(close=105.0))
        assert f.is_ready() is True

    def test_roc_calculation_positive(self):
        """ROC should be positive when price increased."""
        config = {"enabled": True, "indicator": "roc", "roc_period": 5}
        f = MomentumFilter(config)

        # 100 -> 110 = 10% ROC
        prices = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
        for price in prices:
            f.update(MockBar(close=price))

        roc = f.get_roc()
        assert roc is not None
        assert roc == pytest.approx(10.0)

    def test_roc_calculation_negative(self):
        """ROC should be negative when price decreased."""
        config = {"enabled": True, "indicator": "roc", "roc_period": 5}
        f = MomentumFilter(config)

        # 100 -> 90 = -10% ROC
        prices = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0]
        for price in prices:
            f.update(MockBar(close=price))

        roc = f.get_roc()
        assert roc is not None
        assert roc == pytest.approx(-10.0)

    def test_roc_allows_above_threshold(self):
        """Should allow when ROC is above threshold."""
        config = {
            "enabled": True,
            "indicator": "roc",
            "roc_period": 5,
            "roc_long_threshold": 5.0,
        }
        f = MomentumFilter(config)

        # 100 -> 110 = 10% ROC > 5% threshold
        prices = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is True

    def test_roc_blocks_below_threshold(self):
        """Should block when ROC is below threshold."""
        config = {
            "enabled": True,
            "indicator": "roc",
            "roc_period": 5,
            "roc_long_threshold": 5.0,
        }
        f = MomentumFilter(config)

        # 100 -> 102 = 2% ROC < 5% threshold
        prices = [100.0, 100.4, 100.8, 101.2, 101.6, 102.0]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is False
        assert "below threshold" in result.reason

    def test_roc_default_threshold_zero(self):
        """Default threshold should be 0 (any positive ROC allowed)."""
        config = {"enabled": True, "indicator": "roc", "roc_period": 5}
        f = MomentumFilter(config)

        # 100 -> 100.1 = 0.1% ROC > 0% threshold
        prices = [100.0, 100.0, 100.0, 100.0, 100.0, 100.1]
        for price in prices:
            f.update(MockBar(close=price))

        result = f.check(MockBar())
        assert result.passed is True


class TestMomentumFilterGeneral:
    """General tests for MomentumFilter."""

    def test_filter_allows_when_disabled(self):
        """Should allow all trades when disabled."""
        config = {"enabled": False, "indicator": "rsi"}
        f = MomentumFilter(config)

        # Don't feed any data
        result = f.check(MockBar())
        assert result.passed is True

    def test_filter_blocks_when_not_ready(self):
        """Should block trades when not ready (insufficient data)."""
        config = {"enabled": True, "indicator": "rsi", "rsi_period": 14}
        f = MomentumFilter(config)

        # Feed only 5 bars when 15 required
        for i in range(5):
            f.update(MockBar(close=100.0 + i))

        result = f.check(MockBar())
        assert result.passed is False
        assert "Insufficient data" in result.reason

    def test_invalid_indicator_raises_error(self):
        """Should raise error for invalid indicator."""
        config = {"enabled": True, "indicator": "invalid"}
        with pytest.raises(ValueError) as exc_info:
            MomentumFilter(config)
        assert "invalid" in str(exc_info.value)

    def test_default_config_values(self):
        """Should use default values when not specified."""
        f = MomentumFilter({})

        assert f.enabled is True
        assert f.indicator == "rsi"
        assert f.rsi_config.period == 14
        assert f.rsi_config.long_min == 30.0
        assert f.rsi_config.long_max == 70.0
        assert f.macd_config.fast == 12
        assert f.macd_config.slow == 26
        assert f.macd_config.signal == 9
        assert f.macd_config.histogram_positive is True
        assert f.roc_config.period == 12
        assert f.roc_config.long_threshold == 0.0

    def test_create_via_registry(self):
        """Should be creatable via registry."""
        from custos_toolkit.filters.registry import is_filter_registered, register_filter

        # Re-register if cleared by other tests
        if not is_filter_registered("momentum"):
            register_filter("momentum")(MomentumFilter)

        f = create_filter("momentum", {"enabled": True, "indicator": "roc"})
        assert isinstance(f, MomentumFilter)
        assert f.indicator == "roc"

    def test_name_property(self):
        """Should return correct filter name."""
        f = MomentumFilter({})
        assert f.name == "momentum"

    def test_sliding_window_behavior(self):
        """Should maintain sliding window of prices."""
        config = {"enabled": True, "indicator": "roc", "roc_period": 5}
        f = MomentumFilter(config)

        # Initial uptrend
        for price in [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]:
            f.update(MockBar(close=price))
        roc1 = f.get_roc()
        assert roc1 is not None
        assert roc1 > 0  # Positive ROC

        # Add downtrend data - old data should slide out
        for price in [108.0, 106.0, 104.0, 102.0, 100.0, 98.0]:
            f.update(MockBar(close=price))

        roc2 = f.get_roc()
        assert roc2 is not None
        assert roc2 < 0  # Negative ROC now


class TestMomentumFilterAccessors:
    """Tests for accessor methods."""

    def test_get_rsi_returns_none_for_wrong_indicator(self):
        """get_rsi should return None when indicator is not RSI."""
        config = {"enabled": True, "indicator": "macd"}
        f = MomentumFilter(config)

        # Even if ready, should return None for wrong indicator
        for i in range(50):
            f.update(MockBar(close=100.0 + i * 0.1))

        assert f.get_rsi() is None

    def test_get_macd_returns_none_for_wrong_indicator(self):
        """get_macd should return None when indicator is not MACD."""
        config = {"enabled": True, "indicator": "rsi"}
        f = MomentumFilter(config)

        for i in range(50):
            f.update(MockBar(close=100.0 + i * 0.1))

        assert f.get_macd() is None

    def test_get_roc_returns_none_for_wrong_indicator(self):
        """get_roc should return None when indicator is not ROC."""
        config = {"enabled": True, "indicator": "rsi"}
        f = MomentumFilter(config)

        for i in range(50):
            f.update(MockBar(close=100.0 + i * 0.1))

        assert f.get_roc() is None

    def test_get_macd_contains_all_fields(self):
        """get_macd should return dict with all expected fields."""
        config = {
            "enabled": True,
            "indicator": "macd",
            "macd_fast": 3,
            "macd_slow": 5,
            "macd_signal": 2,
        }
        f = MomentumFilter(config)

        for i in range(10):
            f.update(MockBar(close=100.0 + i))

        macd = f.get_macd()
        assert macd is not None
        assert "macd_line" in macd
        assert "signal_line" in macd
        assert "histogram" in macd
