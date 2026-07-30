"""
Tests for deep dict conversion of msgspec structs.

Tests the deep_asdict utility function from shared.nautilus.utils.

Verifies the fix for:
AttributeError('StopLossConfig' object has no attribute 'get')

The issue was that msgspec.structs.asdict() only converts the top level
to a dictionary, leaving nested structs unchanged. This caused OrderPriceCalculator
to fail when calling .get() on nested config objects.

The fix uses deep_asdict() to recursively convert all nested structs to dicts.
"""

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("msgspec")
import msgspec
from custos_toolkit.risk import OrderPriceCalculator
from custos_toolkit.signals.types import Signal, SignalDirection
from custos_toolkit_nautilus.adapter.utils import deep_asdict

# =============================================================================
# Local msgspec struct definitions for testing deep_asdict
# These mirror the structure of the actual config classes without importing them
# =============================================================================


class StopLossAtrConfig(msgspec.Struct, frozen=True):
    """Test struct for ATR stop loss config."""

    multiplier: float = 2.0


class StopLossFixedConfig(msgspec.Struct, frozen=True):
    """Test struct for fixed stop loss config."""

    value: float = 0.02


class StopLossTrailingConfig(msgspec.Struct, frozen=True):
    """Test struct for trailing stop loss config."""

    enabled: bool = False
    activation_pct: float = 0.02


class StopLossIndicatorConfig(msgspec.Struct, frozen=True):
    """Test struct for indicator stop loss config."""

    type: str = "supertrend"


class BreakEvenConfig(msgspec.Struct, frozen=True):
    """Test struct for break even config."""

    enabled: bool = False
    activation_pct: float = 0.015


class StopLossConfig(msgspec.Struct, frozen=True):
    """Test struct for stop loss config."""

    method: str = "none"
    atr: StopLossAtrConfig | None = None
    fixed: StopLossFixedConfig | None = None
    trailing: StopLossTrailingConfig | None = None
    indicator: StopLossIndicatorConfig | None = None
    break_even: BreakEvenConfig | None = None


class TakeProfitAtrConfig(msgspec.Struct, frozen=True):
    """Test struct for ATR take profit config."""

    multiplier: float = 3.0


class TakeProfitFixedConfig(msgspec.Struct, frozen=True):
    """Test struct for fixed take profit config."""

    value: float = 0.04


class TakeProfitTrailingConfig(msgspec.Struct, frozen=True):
    """Test struct for trailing take profit config."""

    activation_pct: float = 0.03


class TakeProfitConfig(msgspec.Struct, frozen=True):
    """Test struct for take profit config."""

    method: str = "none"
    atr: TakeProfitAtrConfig | None = None
    fixed: TakeProfitFixedConfig | None = None
    trailing: TakeProfitTrailingConfig | None = None


class GlobalRiskConfig(msgspec.Struct, frozen=True):
    """Test struct for global risk config."""

    max_daily_loss: float = 0.05
    max_drawdown: float = 0.10
    consecutive_loss_pause: int = 3
    max_daily_trades: int = 10
    reset_time: str = "00:00"


class TradeRiskConfig(msgspec.Struct, frozen=True):
    """Test struct for trade risk config."""

    max_loss_pct: float = 0.02
    time_limit: int = 604800
    atr_period: int = 14
    stop_loss: StopLossConfig | None = None
    take_profit: TakeProfitConfig | None = None


# =============================================================================
# Tests for deep_asdict basic functionality
# =============================================================================


class TestDeepAsdictBasic:
    """Test basic functionality of deep_asdict from shared.nautilus.utils."""

    def test_converts_simple_struct_to_dict(self):
        """Simple struct should be converted to dict."""
        config = StopLossAtrConfig(multiplier=2.5)
        result = deep_asdict(config)

        assert isinstance(result, dict)
        assert result == {"multiplier": 2.5}

    def test_converts_nested_struct_to_dict(self):
        """Nested structs should be recursively converted."""
        config = StopLossConfig(
            method="atr",
            atr=StopLossAtrConfig(multiplier=3.0),
            fixed=StopLossFixedConfig(value=0.02),
        )
        result = deep_asdict(config)

        assert isinstance(result, dict)
        assert isinstance(result["atr"], dict)
        assert isinstance(result["fixed"], dict)
        assert result["method"] == "atr"
        assert result["atr"]["multiplier"] == 3.0
        assert result["fixed"]["value"] == 0.02

    def test_converts_deeply_nested_structs(self):
        """Deeply nested structs should all be converted."""
        config = TradeRiskConfig(
            max_loss_pct=0.02,
            atr_period=14,
            stop_loss=StopLossConfig(
                method="atr",
                atr=StopLossAtrConfig(multiplier=2.0),
            ),
            take_profit=TakeProfitConfig(
                method="atr",
                atr=TakeProfitAtrConfig(multiplier=6.0),
            ),
        )
        result = deep_asdict(config)

        # Top level
        assert isinstance(result, dict)
        assert result["max_loss_pct"] == 0.02

        # Second level
        assert isinstance(result["stop_loss"], dict)
        assert isinstance(result["take_profit"], dict)

        # Third level
        assert isinstance(result["stop_loss"]["atr"], dict)
        assert isinstance(result["take_profit"]["atr"], dict)
        assert result["stop_loss"]["atr"]["multiplier"] == 2.0
        assert result["take_profit"]["atr"]["multiplier"] == 6.0

    def test_preserves_primitive_values(self):
        """Primitive values should be preserved unchanged."""
        config = GlobalRiskConfig(
            max_daily_loss=0.05,
            max_drawdown=0.10,
            consecutive_loss_pause=3,
            max_daily_trades=10,
            reset_time="00:00",
        )
        result = deep_asdict(config)

        assert result["max_daily_loss"] == 0.05
        assert result["max_drawdown"] == 0.10
        assert result["consecutive_loss_pause"] == 3
        assert result["max_daily_trades"] == 10
        assert result["reset_time"] == "00:00"

    def test_handles_none_values(self):
        """None values should be preserved."""
        # Create a dict with None value and pass through deep_asdict
        data = {"key": None, "nested": {"inner": None}}
        result = deep_asdict(data)

        assert result["key"] is None
        assert result["nested"]["inner"] is None

    def test_handles_list_of_structs(self):
        """Lists containing structs should have elements converted."""
        # Create a list of simple structs
        configs = [
            StopLossAtrConfig(multiplier=1.0),
            StopLossAtrConfig(multiplier=2.0),
        ]
        result = deep_asdict(configs)

        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], dict)
        assert result[0]["multiplier"] == 1.0
        assert result[1]["multiplier"] == 2.0

    def test_handles_tuple_of_structs(self):
        """Tuples containing structs should have elements converted."""
        configs = (
            StopLossAtrConfig(multiplier=1.0),
            StopLossAtrConfig(multiplier=2.0),
        )
        result = deep_asdict(configs)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], dict)


# =============================================================================
# Tests comparing deep_asdict vs msgspec.structs.asdict
# =============================================================================


class TestDeepAsdictVsMsgspecAsdict:
    """Compare deep_asdict with msgspec.structs.asdict behavior."""

    def test_msgspec_asdict_does_not_convert_nested_structs(self):
        """Demonstrate that msgspec.structs.asdict only converts top level."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(method="atr"),
        )
        result = msgspec.structs.asdict(config)

        # Top level is dict
        assert isinstance(result, dict)

        # But nested structs are NOT converted (this is the bug!)
        assert isinstance(result["stop_loss"], StopLossConfig)
        assert not isinstance(result["stop_loss"], dict)

        # This would cause AttributeError
        with pytest.raises(AttributeError):
            result["stop_loss"].get("method", "none")

    def testdeep_asdict_converts_nested_structs(self):
        """deep_asdict converts all nested structs to dicts."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(method="atr"),
        )
        result = deep_asdict(config)

        # Top level is dict
        assert isinstance(result, dict)

        # Nested structs ARE converted
        assert isinstance(result["stop_loss"], dict)
        assert not isinstance(result["stop_loss"], StopLossConfig)

        # .get() works correctly
        method = result["stop_loss"].get("method", "none")
        assert method == "atr"


# =============================================================================
# Tests for OrderPriceCalculator with deep_asdict converted config
# =============================================================================


class TestOrderPriceCalculatorWithDeepAsdict:
    """Test that OrderPriceCalculator works with deep_asdict converted config."""

    def test_calculate_stop_loss_with_atr_method(self):
        """OrderPriceCalculator should work with ATR stop loss config."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(
                method="atr",
                atr=StopLossAtrConfig(multiplier=2.0),
            ),
        )
        calculator = OrderPriceCalculator(deep_asdict(config))

        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            strength=1.0,
            price=Decimal("100"),
        )

        # This should NOT raise AttributeError
        stop_price = calculator.calculate_stop_loss(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=Decimal("5"),
        )

        # ATR stop: 100 - (2.0 * 5) = 90
        assert stop_price == Decimal("90")

    def test_calculate_stop_loss_with_fixed_method(self):
        """OrderPriceCalculator should work with fixed stop loss config."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(
                method="fixed",
                fixed=StopLossFixedConfig(value=0.02),
            ),
        )
        calculator = OrderPriceCalculator(deep_asdict(config))

        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            strength=1.0,
            price=Decimal("100"),
        )

        stop_price = calculator.calculate_stop_loss(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=None,
        )

        # Fixed stop: 100 * (1 - 0.02) = 98
        assert stop_price == Decimal("98")

    def test_calculate_take_profit_with_atr_method(self):
        """OrderPriceCalculator should work with ATR take profit config."""
        config = TradeRiskConfig(
            take_profit=TakeProfitConfig(
                method="atr",
                atr=TakeProfitAtrConfig(multiplier=3.0),
            ),
        )
        calculator = OrderPriceCalculator(deep_asdict(config))

        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            strength=1.0,
            price=Decimal("100"),
        )

        tp_price = calculator.calculate_take_profit(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=Decimal("5"),
        )

        # ATR TP: 100 + (3.0 * 5) = 115
        assert tp_price == Decimal("115")

    def test_calculate_take_profit_with_fixed_method(self):
        """OrderPriceCalculator should work with fixed take profit config."""
        config = TradeRiskConfig(
            take_profit=TakeProfitConfig(
                method="fixed",
                fixed=TakeProfitFixedConfig(value=0.04),
            ),
        )
        calculator = OrderPriceCalculator(deep_asdict(config))

        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            strength=1.0,
            price=Decimal("100"),
        )

        tp_price = calculator.calculate_take_profit(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=None,
        )

        # Fixed TP: 100 * (1 + 0.04) = 104
        assert tp_price == Decimal("104")

    def test_short_direction_stop_loss(self):
        """Stop loss for short positions should be above entry."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(
                method="atr",
                atr=StopLossAtrConfig(multiplier=2.0),
            ),
        )
        calculator = OrderPriceCalculator(deep_asdict(config))

        signal = Signal(
            direction=SignalDirection.ENTER_SHORT,
            strength=1.0,
            price=Decimal("100"),
        )

        stop_price = calculator.calculate_stop_loss(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=Decimal("5"),
        )

        # Short ATR stop: 100 + (2.0 * 5) = 110
        assert stop_price == Decimal("110")

    def test_short_direction_take_profit(self):
        """Take profit for short positions should be below entry."""
        config = TradeRiskConfig(
            take_profit=TakeProfitConfig(
                method="atr",
                atr=TakeProfitAtrConfig(multiplier=3.0),
            ),
        )
        calculator = OrderPriceCalculator(deep_asdict(config))

        signal = Signal(
            direction=SignalDirection.ENTER_SHORT,
            strength=1.0,
            price=Decimal("100"),
        )

        tp_price = calculator.calculate_take_profit(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=Decimal("5"),
        )

        # Short ATR TP: 100 - (3.0 * 5) = 85
        assert tp_price == Decimal("85")


# =============================================================================
# Regression tests for the original AttributeError bug
# =============================================================================


class TestRegressionStopLossConfigGetAttribute:
    """
    Regression tests for the original AttributeError bug.

    The original error was:
    AttributeError('StopLossConfig' object has no attribute 'get')

    This happened because OrderPriceCalculator.calculate_stop_loss() called:
        sl_config.get("method", "none")

    But sl_config was a StopLossConfig struct, not a dict.
    """

    def test_without_deep_conversion_raises_attribute_error(self):
        """Using msgspec.structs.asdict directly should cause AttributeError."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(method="atr"),
        )
        # This is the OLD buggy way - only converts top level
        buggy_config = msgspec.structs.asdict(config)
        calculator = OrderPriceCalculator(buggy_config)

        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            strength=1.0,
            price=Decimal("100"),
        )

        # This should raise AttributeError (the bug)
        with pytest.raises(AttributeError, match="has no attribute 'get'"):
            calculator.calculate_stop_loss(
                entry_price=Decimal("100"),
                direction=signal.direction,
                atr=Decimal("5"),
            )

    def test_with_deep_conversion_works_correctly(self):
        """Using deep_asdict should work without errors."""
        config = TradeRiskConfig(
            stop_loss=StopLossConfig(
                method="atr",
                atr=StopLossAtrConfig(multiplier=2.0),
            ),
        )
        # This is the NEW correct way - recursively converts all levels
        fixed_config = deep_asdict(config)
        calculator = OrderPriceCalculator(fixed_config)

        signal = Signal(
            direction=SignalDirection.ENTER_LONG,
            strength=1.0,
            price=Decimal("100"),
        )

        # This should work without raising any errors
        stop_price = calculator.calculate_stop_loss(
            entry_price=Decimal("100"),
            direction=signal.direction,
            atr=Decimal("5"),
        )

        assert stop_price is not None

    def test_full_config_with_all_nested_structs(self):
        """Test with a full config containing all nested structs."""
        config = TradeRiskConfig(
            max_loss_pct=0.02,
            time_limit=604800,
            atr_period=14,
            stop_loss=StopLossConfig(
                method="atr",
                atr=StopLossAtrConfig(multiplier=2.0),
                fixed=StopLossFixedConfig(value=0.02),
                trailing=StopLossTrailingConfig(enabled=True, activation_pct=0.02),
                indicator=StopLossIndicatorConfig(type="supertrend"),
                break_even=BreakEvenConfig(enabled=True, activation_pct=0.015),
            ),
            take_profit=TakeProfitConfig(
                method="atr",
                atr=TakeProfitAtrConfig(multiplier=6.0),
                fixed=TakeProfitFixedConfig(value=0.04),
                trailing=TakeProfitTrailingConfig(activation_pct=0.03),
            ),
        )

        # Convert using deep_asdict
        converted = deep_asdict(config)

        # All nested objects should be dicts
        assert isinstance(converted["stop_loss"], dict)
        assert isinstance(converted["stop_loss"]["atr"], dict)
        assert isinstance(converted["stop_loss"]["fixed"], dict)
        assert isinstance(converted["stop_loss"]["trailing"], dict)
        assert isinstance(converted["stop_loss"]["indicator"], dict)
        assert isinstance(converted["stop_loss"]["break_even"], dict)
        assert isinstance(converted["take_profit"], dict)
        assert isinstance(converted["take_profit"]["atr"], dict)
        assert isinstance(converted["take_profit"]["fixed"], dict)
        assert isinstance(converted["take_profit"]["trailing"], dict)

        # OrderPriceCalculator should work with this config
        calculator = OrderPriceCalculator(converted)

        # Both stop loss and take profit calculations should work
        sl_price = calculator.calculate_stop_loss(
            entry_price=Decimal("100"),
            direction=SignalDirection.ENTER_LONG,
            atr=Decimal("5"),
        )
        tp_price = calculator.calculate_take_profit(
            entry_price=Decimal("100"),
            direction=SignalDirection.ENTER_LONG,
            atr=Decimal("5"),
        )

        assert sl_price is not None
        assert tp_price is not None
