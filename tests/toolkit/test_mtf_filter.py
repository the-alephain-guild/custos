# tests/test_mtf_filter.py
"""Tests for MTFFilter (Multi-TimeFrame Filter)."""

from dataclasses import dataclass

import pytest
from custos_toolkit.filters import create_filter
from custos_toolkit.filters.mtf import MTFFilter
from custos_toolkit.signals.types import Signal


@dataclass
class MockBarType:
    """Mock bar type for testing HTF detection."""

    timeframe: str = "1h"

    def __eq__(self, other):
        if isinstance(other, MockBarType):
            return self.timeframe == other.timeframe
        return False

    def __hash__(self):
        return hash(self.timeframe)


@dataclass
class MockBar:
    """Mock bar for testing."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0
    bar_type: MockBarType = None


class TestMTFFilter:
    """Tests for MTFFilter."""

    def test_filter_not_ready_without_direction(self):
        """Should not be ready until direction is set."""
        config = {"enabled": True}
        f = MTFFilter(config)

        assert f.is_ready() is False
        assert f.get_direction() is None

    def test_filter_becomes_ready_after_set_direction(self):
        """Should become ready after direction is set."""
        config = {"enabled": True}
        f = MTFFilter(config)

        f.set_direction(1)
        assert f.is_ready() is True
        assert f.get_direction() == 1

    def test_set_direction_validates_values(self):
        """Should only accept valid direction values."""
        config = {"enabled": True}
        f = MTFFilter(config)

        # Valid values
        f.set_direction(1)
        assert f.get_direction() == 1

        f.set_direction(-1)
        assert f.get_direction() == -1

        f.set_direction(0)
        assert f.get_direction() == 0

        # Invalid values
        with pytest.raises(ValueError) as exc_info:
            f.set_direction(2)
        assert "must be 1, -1, or 0" in str(exc_info.value)

        with pytest.raises(ValueError):
            f.set_direction(-2)

    def test_filter_allows_when_disabled(self):
        """Should allow all trades when disabled."""
        config = {"enabled": False}
        f = MTFFilter(config)

        # Don't set direction
        result = f.check(MockBar())
        assert result.passed is True

    def test_filter_blocks_when_not_ready(self):
        """Should block trades when direction not set."""
        config = {"enabled": True}
        f = MTFFilter(config)

        result = f.check(MockBar())
        assert result.passed is False
        assert "not set" in result.reason

    # Tests for same_direction mode (default)
    def test_same_direction_long_with_bullish_htf(self):
        """In same_direction mode, long signal should pass with bullish HTF."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(1)  # Bullish

        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is True
        assert result.size_factor == 1.0

    def test_same_direction_long_with_bearish_htf(self):
        """In same_direction mode, long signal should fail with bearish HTF."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(-1)  # Bearish

        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is False
        assert "bearish" in result.reason.lower()

    def test_same_direction_long_with_neutral_htf(self):
        """In same_direction mode, long signal should fail with neutral HTF."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(0)  # Neutral

        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is False
        assert "neutral" in result.reason.lower()

    def test_same_direction_short_with_bearish_htf(self):
        """In same_direction mode, short signal should pass with bearish HTF."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(-1)  # Bearish

        signal = Signal.enter_short(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is True
        assert result.size_factor == 1.0

    def test_same_direction_short_with_bullish_htf(self):
        """In same_direction mode, short signal should fail with bullish HTF."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(1)  # Bullish

        signal = Signal.enter_short(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is False
        assert "bullish" in result.reason.lower()

    def test_same_direction_short_with_neutral_htf(self):
        """In same_direction mode, short signal should fail with neutral HTF."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(0)  # Neutral

        signal = Signal.enter_short(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is False
        assert "neutral" in result.reason.lower()

    # Tests for not_against mode
    def test_not_against_long_with_bullish_htf(self):
        """In not_against mode, long signal should pass with bullish HTF."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(1)  # Bullish

        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is True
        assert result.size_factor == 1.0

    def test_not_against_long_with_neutral_htf(self):
        """In not_against mode, long signal should pass with neutral HTF (reduced size)."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(0)  # Neutral

        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is True
        assert result.size_factor == 0.5  # Reduced size for neutral

    def test_not_against_long_with_bearish_htf(self):
        """In not_against mode, long signal should fail with bearish HTF."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(-1)  # Bearish

        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is False
        assert "bearish" in result.reason.lower()

    def test_not_against_short_with_bearish_htf(self):
        """In not_against mode, short signal should pass with bearish HTF."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(-1)  # Bearish

        signal = Signal.enter_short(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is True
        assert result.size_factor == 1.0

    def test_not_against_short_with_neutral_htf(self):
        """In not_against mode, short signal should pass with neutral HTF (reduced size)."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(0)  # Neutral

        signal = Signal.enter_short(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is True
        assert result.size_factor == 0.5  # Reduced size for neutral

    def test_not_against_short_with_bullish_htf(self):
        """In not_against mode, short signal should fail with bullish HTF."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(1)  # Bullish

        signal = Signal.enter_short(100.0)
        result = f.check(MockBar(), signal)

        assert result.passed is False
        assert "bullish" in result.reason.lower()

    # Tests for exit signals
    def test_exit_signals_always_allowed(self):
        """Exit signals should always be allowed regardless of HTF direction."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(-1)  # Bearish

        # Exit long should be allowed even with bearish HTF
        signal = Signal.exit_long(100.0)
        result = f.check(MockBar(), signal)
        assert result.passed is True

        # Exit short should be allowed even with bearish HTF
        signal = Signal.exit_short(100.0)
        result = f.check(MockBar(), signal)
        assert result.passed is True

    def test_neutral_signals_always_allowed(self):
        """Neutral signals should always be allowed."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(-1)  # Bearish

        signal = Signal.neutral(100.0)
        result = f.check(MockBar(), signal)
        assert result.passed is True

    # Tests for check without signal (backward compatibility)
    def test_check_without_signal_nonzero_direction(self):
        """Check without signal should pass if direction is non-zero."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(1)  # Bullish

        result = f.check(MockBar())
        assert result.passed is True

    def test_check_without_signal_neutral_same_direction(self):
        """Check without signal in same_direction mode should fail if direction is neutral."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)
        f.set_direction(0)  # Neutral

        result = f.check(MockBar())
        assert result.passed is False
        assert "neutral" in result.reason.lower()

    def test_check_without_signal_neutral_not_against(self):
        """Check without signal in not_against mode should pass with reduced size if neutral."""
        config = {"enabled": True, "alignment_mode": "not_against"}
        f = MTFFilter(config)
        f.set_direction(0)  # Neutral

        result = f.check(MockBar())
        assert result.passed is True
        assert result.size_factor == 0.5

    # Tests for configuration
    def test_invalid_alignment_mode_raises_error(self):
        """Should raise error for invalid alignment_mode."""
        config = {"enabled": True, "alignment_mode": "invalid_mode"}
        with pytest.raises(ValueError) as exc_info:
            MTFFilter(config)
        assert "invalid_mode" in str(exc_info.value)

    def test_default_config_values(self):
        """Should use default values when not specified."""
        f = MTFFilter({})

        assert f.enabled is True
        assert f.alignment_mode == "same_direction"
        assert f.higher_timeframe == "1h"

    def test_custom_higher_timeframe(self):
        """Should accept custom higher_timeframe config."""
        config = {"higher_timeframe": "4h"}
        f = MTFFilter(config)

        assert f.higher_timeframe == "4h"

    def test_create_via_registry(self):
        """Should be creatable via registry."""
        from custos_toolkit.filters.registry import is_filter_registered, register_filter

        # Re-register if cleared by other tests
        if not is_filter_registered("mtf"):
            register_filter("mtf")(MTFFilter)

        f = create_filter("mtf", {"enabled": True, "alignment_mode": "not_against"})
        assert isinstance(f, MTFFilter)
        assert f.alignment_mode == "not_against"

    def test_reset_clears_state(self):
        """Reset should clear direction state."""
        config = {"enabled": True}
        f = MTFFilter(config)

        f.set_direction(1)
        assert f.is_ready() is True
        assert f.get_direction() == 1

        f.reset()
        assert f.is_ready() is False
        assert f.get_direction() is None

    def test_update_is_noop(self):
        """Update method should be a no-op (direction set via set_direction)."""
        config = {"enabled": True}
        f = MTFFilter(config)

        # Update doesn't change state
        f.update(MockBar())
        assert f.is_ready() is False
        assert f.get_direction() is None

    def test_direction_can_be_updated(self):
        """Direction should be updatable as market conditions change."""
        config = {"enabled": True, "alignment_mode": "same_direction"}
        f = MTFFilter(config)

        # Start bullish
        f.set_direction(1)
        signal = Signal.enter_long(100.0)
        result = f.check(MockBar(), signal)
        assert result.passed is True

        # Market turns bearish
        f.set_direction(-1)
        result = f.check(MockBar(), signal)
        assert result.passed is False

        # Market turns neutral
        f.set_direction(0)
        result = f.check(MockBar(), signal)
        assert result.passed is False

        # Market turns bullish again
        f.set_direction(1)
        result = f.check(MockBar(), signal)
        assert result.passed is True

    def test_filter_name(self):
        """Filter name should be 'mtf'."""
        f = MTFFilter({})
        assert f.name == "mtf"


class TestMTFFilterHTFBarDetection:
    """Tests for HTF bar detection capability."""

    def test_set_htf_bar_type(self):
        """set_htf_bar_type should store the bar type for later detection."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")

        f.set_htf_bar_type(htf_bar_type)

        assert f._htf_bar_type == htf_bar_type

    def test_is_htf_bar_returns_true_for_matching_type(self):
        """is_htf_bar should return True when bar's bar_type matches HTF bar type."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        bar = MockBar(bar_type=MockBarType("4h"))

        assert f.is_htf_bar(bar) is True

    def test_is_htf_bar_returns_false_for_non_matching_type(self):
        """is_htf_bar should return False when bar's bar_type doesn't match HTF bar type."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        bar = MockBar(bar_type=MockBarType("1m"))  # Different timeframe

        assert f.is_htf_bar(bar) is False

    def test_is_htf_bar_returns_false_when_not_set(self):
        """is_htf_bar should return False when HTF bar type is not set."""
        config = {"enabled": True}
        f = MTFFilter(config)

        bar = MockBar(bar_type=MockBarType("4h"))

        assert f.is_htf_bar(bar) is False

    def test_update_with_htf_bar_sets_direction_bullish(self):
        """update() with HTF bar where close > open should set direction to 1 (bullish)."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        # Bullish bar: close > open
        bar = MockBar(open=100.0, close=105.0, bar_type=MockBarType("4h"))

        f.update(bar)

        assert f.get_direction() == 1
        assert f.is_ready() is True

    def test_update_with_htf_bar_sets_direction_bearish(self):
        """update() with HTF bar where close < open should set direction to -1 (bearish)."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        # Bearish bar: close < open
        bar = MockBar(open=105.0, close=100.0, bar_type=MockBarType("4h"))

        f.update(bar)

        assert f.get_direction() == -1
        assert f.is_ready() is True

    def test_update_with_htf_bar_sets_direction_neutral(self):
        """update() with HTF bar where close == open should set direction to 0 (neutral)."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        # Neutral bar: close == open
        bar = MockBar(open=100.0, close=100.0, bar_type=MockBarType("4h"))

        f.update(bar)

        assert f.get_direction() == 0
        assert f.is_ready() is True

    def test_update_with_non_htf_bar_ignored(self):
        """update() with non-HTF bar should not change filter state."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        # Bar with different bar_type (not HTF)
        bar = MockBar(open=100.0, close=105.0, bar_type=MockBarType("1m"))

        f.update(bar)

        # State should remain unchanged
        assert f.get_direction() is None
        assert f.is_ready() is False

    def test_htf_bar_type_init_default_none(self):
        """_htf_bar_type should be None by default after init."""
        config = {"enabled": True}
        f = MTFFilter(config)

        assert f._htf_bar_type is None

    def test_is_htf_bar_handles_bar_without_bar_type_attribute(self):
        """is_htf_bar should handle bars that don't have bar_type attribute."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        # Create a simple object without bar_type attribute
        class SimpleBar:
            def __init__(self):
                self.open = 100.0
                self.close = 105.0

        bar = SimpleBar()

        assert f.is_htf_bar(bar) is False

    def test_reset_clears_htf_bar_type(self):
        """reset() should clear the HTF bar type."""
        config = {"enabled": True}
        f = MTFFilter(config)
        htf_bar_type = MockBarType("4h")
        f.set_htf_bar_type(htf_bar_type)

        f.reset()

        assert f._htf_bar_type is None
