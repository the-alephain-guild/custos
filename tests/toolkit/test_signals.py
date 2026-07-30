"""Tests for shared.signals.types module."""

from decimal import Decimal

import pytest
from custos_toolkit.signals import Signal, SignalDirection


class TestSignalDirection:
    """Tests for SignalDirection enum."""

    def test_is_entry_long(self):
        """Test LONG is an entry signal."""
        assert SignalDirection.ENTER_LONG.is_entry() is True

    def test_is_entry_short(self):
        """Test SHORT is an entry signal."""
        assert SignalDirection.ENTER_SHORT.is_entry() is True

    def test_is_entry_neutral(self):
        """Test NEUTRAL is not an entry signal."""
        assert SignalDirection.NEUTRAL.is_entry() is False

    def test_is_entry_exit_long(self):
        """Test EXIT_LONG is not an entry signal."""
        assert SignalDirection.EXIT_LONG.is_entry() is False

    def test_is_entry_exit_short(self):
        """Test EXIT_SHORT is not an entry signal."""
        assert SignalDirection.EXIT_SHORT.is_entry() is False

    def test_is_exit_long(self):
        """Test EXIT_LONG is an exit signal."""
        assert SignalDirection.EXIT_LONG.is_exit() is True

    def test_is_exit_short(self):
        """Test EXIT_SHORT is an exit signal."""
        assert SignalDirection.EXIT_SHORT.is_exit() is True

    def test_is_exit_long_entry(self):
        """Test LONG is not an exit signal."""
        assert SignalDirection.ENTER_LONG.is_exit() is False

    def test_is_exit_neutral(self):
        """Test NEUTRAL is not an exit signal."""
        assert SignalDirection.NEUTRAL.is_exit() is False

    def test_opposite_long(self):
        """Test opposite of LONG is SHORT."""
        assert SignalDirection.ENTER_LONG.opposite() == SignalDirection.ENTER_SHORT

    def test_opposite_short(self):
        """Test opposite of SHORT is LONG."""
        assert SignalDirection.ENTER_SHORT.opposite() == SignalDirection.ENTER_LONG

    def test_opposite_exit_long(self):
        """Test opposite of EXIT_LONG is EXIT_SHORT."""
        assert SignalDirection.EXIT_LONG.opposite() == SignalDirection.EXIT_SHORT

    def test_opposite_exit_short(self):
        """Test opposite of EXIT_SHORT is EXIT_LONG."""
        assert SignalDirection.EXIT_SHORT.opposite() == SignalDirection.EXIT_LONG

    def test_opposite_neutral(self):
        """Test opposite of NEUTRAL is NEUTRAL."""
        assert SignalDirection.NEUTRAL.opposite() == SignalDirection.NEUTRAL


class TestSignal:
    """Tests for Signal dataclass."""

    def test_neutral_signal(self):
        """Test creating a neutral signal."""
        signal = Signal.neutral(100.5)
        assert signal.direction == SignalDirection.NEUTRAL
        assert signal.price == Decimal("100.5")
        assert signal.is_actionable() is False

    def test_neutral_signal_with_pair(self):
        """Test neutral signal with trading pair."""
        signal = Signal.neutral(50000, pair="BTC-USDT")
        assert signal.pair == "BTC-USDT"
        assert signal.direction == SignalDirection.NEUTRAL

    def test_neutral_signal_with_metadata(self):
        """Test neutral signal with metadata kwargs."""
        signal = Signal.neutral(50000, supertrend=49500.0, trend=1, prev_trend=1)
        assert signal.direction == SignalDirection.NEUTRAL
        assert signal.metadata["supertrend"] == 49500.0
        assert signal.metadata["trend"] == 1
        assert signal.metadata["prev_trend"] == 1
        assert signal.is_actionable() is False

    def test_neutral_signal_with_pair_and_metadata(self):
        """Test neutral signal with both pair and metadata."""
        signal = Signal.neutral(50000, pair="BTC-USDT", trend=1, action="hold")
        assert signal.pair == "BTC-USDT"
        assert signal.metadata["trend"] == 1
        assert signal.metadata["action"] == "hold"

    def test_neutral_signal_empty_metadata(self):
        """Test neutral signal without metadata has empty dict."""
        signal = Signal.neutral(50000)
        assert signal.metadata == {}

    def test_long_signal(self):
        """Test creating a long signal."""
        signal = Signal.enter_long(50000, strength=0.8, atr=100.5)
        assert signal.is_enter_long() is True
        assert signal.is_enter_short() is False
        assert signal.is_actionable() is True
        assert signal.metadata["atr"] == 100.5
        assert signal.strength == 0.8

    def test_long_signal_default_strength(self):
        """Test long signal has default strength of 1.0."""
        signal = Signal.enter_long(50000)
        assert signal.strength == 1.0

    def test_short_signal(self):
        """Test creating a short signal."""
        signal = Signal.enter_short(50000, strength=0.7)
        assert signal.is_enter_short() is True
        assert signal.is_enter_long() is False
        assert signal.is_actionable() is True
        assert signal.strength == 0.7

    def test_exit_long_signal(self):
        """Test creating an exit long signal."""
        executors = ["exec1", "exec2"]
        signal = Signal.exit_long(50000, executors=executors)
        assert signal.is_exit() is True
        assert signal.direction == SignalDirection.EXIT_LONG
        assert signal.metadata["executors"] == executors

    def test_exit_short_signal(self):
        """Test creating an exit short signal."""
        signal = Signal.exit_short(50000, reason="trend_reversal")
        assert signal.is_exit() is True
        assert signal.direction == SignalDirection.EXIT_SHORT
        assert signal.metadata["reason"] == "trend_reversal"

    def test_strength_validation_too_high(self):
        """Test that strength > 1.0 raises ValueError."""
        with pytest.raises(ValueError, match="Signal strength must be between 0 and 1"):
            Signal(SignalDirection.ENTER_LONG, Decimal("100"), strength=1.5)

    def test_strength_validation_negative(self):
        """Test that negative strength raises ValueError."""
        with pytest.raises(ValueError, match="Signal strength must be between 0 and 1"):
            Signal(SignalDirection.ENTER_LONG, Decimal("100"), strength=-0.1)

    def test_strength_boundary_zero(self):
        """Test strength at 0 boundary."""
        signal = Signal(SignalDirection.ENTER_LONG, Decimal("100"), strength=0.0)
        assert signal.strength == 0.0

    def test_strength_boundary_one(self):
        """Test strength at 1 boundary."""
        signal = Signal(SignalDirection.ENTER_LONG, Decimal("100"), strength=1.0)
        assert signal.strength == 1.0

    def test_price_conversion_float(self):
        """Test that float price is converted to Decimal."""
        signal = Signal.enter_long(100.5)
        assert isinstance(signal.price, Decimal)
        assert signal.price == Decimal("100.5")

    def test_price_conversion_int(self):
        """Test that int price is converted to Decimal."""
        signal = Signal.enter_long(50000)
        assert isinstance(signal.price, Decimal)
        assert signal.price == Decimal("50000")

    def test_price_decimal_preserved(self):
        """Test that Decimal price is preserved."""
        price = Decimal("50000.12345")
        signal = Signal(SignalDirection.ENTER_LONG, price)
        assert signal.price == price

    def test_is_actionable_long(self):
        """Test long signal is actionable."""
        signal = Signal.enter_long(50000)
        assert signal.is_actionable() is True

    def test_is_actionable_short(self):
        """Test short signal is actionable."""
        signal = Signal.enter_short(50000)
        assert signal.is_actionable() is True

    def test_is_actionable_exit(self):
        """Test exit signal is actionable."""
        signal = Signal.exit_long(50000)
        assert signal.is_actionable() is True

    def test_is_actionable_neutral(self):
        """Test neutral signal is not actionable."""
        signal = Signal.neutral(50000)
        assert signal.is_actionable() is False

    def test_metadata_multiple_kwargs(self):
        """Test signal with multiple metadata kwargs."""
        signal = Signal.enter_long(50000, atr=100, supertrend=49500, adx=30)
        assert signal.metadata["atr"] == 100
        assert signal.metadata["supertrend"] == 49500
        assert signal.metadata["adx"] == 30

    def test_timestamp_default_none(self):
        """Test timestamp defaults to None."""
        signal = Signal.enter_long(50000)
        assert signal.timestamp is None

    def test_timestamp_set(self):
        """Test setting timestamp."""
        signal = Signal(SignalDirection.ENTER_LONG, Decimal("50000"), timestamp=1704067200)
        assert signal.timestamp == 1704067200
