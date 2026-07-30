"""
Tests for Decimal precision in position sizing pipeline.

These tests verify that the position sizing pipeline uses Decimal consistently
to avoid IEEE-754 floating-point precision errors that cause order rejections
like: "quantity 1000.001 invalid (> maximum trade size of 1000.000)"

The root cause was float-to-Decimal conversions throughout the pipeline, where
operations like 10000 * 0.1 produce 1000.0000000000001 in float, exceeding
the max_trade_size of 1000.

Solution: Use Decimal uniformly with ROUND_DOWN to ensure quantities never
exceed limits.
"""

from decimal import ROUND_DOWN, Decimal

from custos_toolkit.position.sizer import PositionSizer
from custos_toolkit.position.tracker import PositionTracker
from custos_toolkit.risk.controller import RiskController
from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit.signals.types import SignalDirection


class TestDecimalPrecisionInPositionSizer:
    """Tests for Decimal precision in PositionSizer."""

    def test_base_size_never_exceeds_max_trade_size(self):
        """
        Test that calculated size never exceeds max_trade_size.

        This is the original bug: 10000 * 0.1 = 1000.0000000001 in float,
        which exceeds max_trade_size of 1000.
        """
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},  # 10% of capital
            }
        )

        # Use the exact values that caused the original bug
        capital = Decimal("10000")
        max_trade_size = Decimal("1000")

        base_size = sizer.calculate_base_size(capital)

        # Apply limits
        limits = {"max_trade_size": 1000}
        final_size = sizer.check_limits(base_size, capital, limits)

        # The result must not exceed max_trade_size
        assert final_size <= max_trade_size, (
            f"Size {final_size} exceeds max_trade_size {max_trade_size}"
        )

    def test_round_down_applied_at_boundary(self):
        """Test that ROUND_DOWN is applied to avoid exceeding limits."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        capital = Decimal("10000")
        limits = {"max_trade_size": 1000}

        final_size = sizer.check_limits(sizer.calculate_base_size(capital), capital, limits)

        # Check it's rounded to 3 decimal places with ROUND_DOWN
        # The result should be 1000.000 or less
        expected_precision = Decimal("0.001")
        remainder = final_size % expected_precision
        assert remainder == Decimal("0"), f"Size {final_size} not rounded to 3 decimal places"

    def test_percentage_sizing_returns_decimal(self):
        """Test that percentage sizing returns Decimal type."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        result = sizer.calculate_base_size(Decimal("10000"))
        assert isinstance(result, Decimal)

    def test_kelly_sizing_returns_decimal(self):
        """Test that Kelly sizing returns Decimal type."""
        sizer = PositionSizer(
            {
                "method": "kelly",
                "kelly": {
                    "fraction": 0.25,
                    "win_rate": 0.55,
                    "payoff_ratio": 2.0,
                },
            }
        )

        result = sizer.calculate_base_size(Decimal("10000"))
        assert isinstance(result, Decimal)

    def test_signal_strength_applied_with_decimal(self):
        """Test that signal strength is applied using Decimal arithmetic."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        base_size = Decimal("1000")
        strength = Decimal("0.8")

        result = sizer.apply_signal_strength(base_size, strength)

        assert isinstance(result, Decimal)
        assert result == Decimal("800")

    def test_scaling_applied_with_decimal(self):
        """Test that scaling is applied using Decimal arithmetic."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
                "scaling": {
                    "enabled": True,
                    "method": "pyramid",
                    "max_entries": 5,
                    "entry_interval_pct": 0.02,
                    "pyramid": {"scale_factor": 0.5},
                },
            }
        )

        base_size = Decimal("1000")

        # Second entry should be half
        result = sizer.apply_scaling(base_size, entry_count=1)
        assert isinstance(result, Decimal)
        assert result == Decimal("500")

    def test_min_max_limits_with_decimal(self):
        """Test min/max limits are enforced with Decimal."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.001},  # Very small percentage
            }
        )

        capital = Decimal("10000")
        limits = {
            "min_trade_size": 100,
            "max_trade_size": 1000,
        }

        # Base size would be 10, but min is 100
        result = sizer.check_limits(sizer.calculate_base_size(capital), capital, limits)

        assert result >= Decimal("100"), "Should enforce min_trade_size"
        assert result <= Decimal("1000"), "Should enforce max_trade_size"


class TestDecimalPrecisionInPositionTracker:
    """Tests for Decimal precision in PositionTracker."""

    def test_record_entry_accepts_decimal(self):
        """Test that record_entry accepts Decimal parameters."""
        tracker = PositionTracker()

        price = Decimal("50000.12345")
        quantity = Decimal("0.001")

        # Should not raise
        tracker.record_entry(price, quantity)

        assert tracker.entry_count == 1
        assert tracker.first_entry_price == price

    def test_record_partial_exit_accepts_decimal(self):
        """Test that record_partial_exit accepts Decimal parameter."""
        tracker = PositionTracker()

        tracker.record_entry(Decimal("50000"), Decimal("1.0"))
        tracker.record_partial_exit(Decimal("0.5"))

        assert tracker.total_quantity == Decimal("0.5")

    def test_unrealized_pnl_returns_decimal(self):
        """Test that get_unrealized_pnl returns Decimal."""
        tracker = PositionTracker()

        tracker.record_entry(Decimal("50000"), Decimal("1.0"))

        pnl = tracker.get_unrealized_pnl(Decimal("51000"), is_long=True)

        assert isinstance(pnl, Decimal)
        assert pnl == Decimal("1000")

    def test_should_scale_in_accepts_decimal_price(self):
        """Test that should_scale_in accepts Decimal current_price."""
        tracker = PositionTracker()

        tracker.record_entry(Decimal("50000"), Decimal("1.0"))

        scaling_config = {
            "enabled": True,
            "max_entries": 5,
            "entry_interval_pct": 0.02,
        }

        # Price dropped 3%, should allow scale-in
        result = tracker.should_scale_in(
            Decimal("48500"),  # 3% drop
            is_long=True,
            scaling_config=scaling_config,
        )

        assert isinstance(result, bool)
        assert result is True


class TestDecimalPrecisionInRiskController:
    """Tests for Decimal precision in RiskController."""

    def test_init_accepts_decimal_capital(self):
        """Test that RiskController accepts Decimal initial_capital."""
        capital = Decimal("10000.50")

        controller = RiskController(
            config={"max_daily_trades": 10},
            initial_capital=capital,
            capital_mode="compound",
        )

        assert controller.initial_capital == capital

    def test_check_limits_accepts_decimal_equity(self):
        """Test that check_limits accepts Decimal current_equity."""
        controller = RiskController(
            config={"max_daily_loss": 0.05},
            initial_capital=Decimal("10000"),
            capital_mode="compound",
        )

        equity = Decimal("9800.123")

        allowed, reason = controller.check_limits(equity, current_ts=0)

        assert isinstance(allowed, bool)

    def test_record_trade_accepts_decimal_pnl(self):
        """Test that record_trade accepts Decimal pnl."""
        controller = RiskController(
            config={},
            initial_capital=Decimal("10000"),
            capital_mode="compound",
        )

        pnl = Decimal("150.75")

        # Should not raise
        controller.record_trade(pnl)

        assert controller.session_pnl == pnl

    def test_update_peak_equity_accepts_decimal(self):
        """Test that update_peak_equity accepts Decimal."""
        controller = RiskController(
            config={},
            initial_capital=Decimal("10000"),
            capital_mode="compound",
        )

        new_equity = Decimal("10500.25")

        # Should not raise
        controller.update_peak_equity(new_equity)


class TestDecimalPrecisionInOrderCalculator:
    """Tests for Decimal precision in OrderPriceCalculator."""

    def test_stop_loss_accepts_decimal_entry_price(self):
        """Test that calculate_stop_loss accepts Decimal entry_price."""
        calculator = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "fixed",
                    "fixed": {"value": 0.02},
                },
            }
        )

        entry_price = Decimal("50000.12345")

        sl_price = calculator.calculate_stop_loss(entry_price, SignalDirection.ENTER_LONG)

        assert isinstance(sl_price, Decimal)
        expected = entry_price * Decimal("0.98")
        assert sl_price == expected

    def test_stop_loss_accepts_decimal_atr(self):
        """Test that calculate_stop_loss accepts Decimal atr."""
        calculator = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "atr",
                    "atr": {"multiplier": 2.0},
                },
            }
        )

        entry_price = Decimal("50000")
        atr = Decimal("500.50")

        sl_price = calculator.calculate_stop_loss(entry_price, SignalDirection.ENTER_LONG, atr=atr)

        assert isinstance(sl_price, Decimal)
        expected = entry_price - (atr * Decimal("2.0"))
        assert sl_price == expected

    def test_take_profit_accepts_decimal_prices(self):
        """Test that calculate_take_profit accepts Decimal prices."""
        calculator = OrderPriceCalculator(
            {
                "take_profit": {
                    "method": "risk_reward",
                    "risk_reward_ratio": 2.0,
                },
            }
        )

        entry_price = Decimal("50000")
        stop_loss = Decimal("49000")

        tp_price = calculator.calculate_take_profit(
            entry_price,
            SignalDirection.ENTER_LONG,
            stop_loss=stop_loss,
        )

        assert isinstance(tp_price, Decimal)
        # Risk is 1000, reward is 2000
        expected = entry_price + Decimal("2000")
        assert tp_price == expected

    def test_trailing_stop_accepts_decimal_prices(self):
        """Test that calculate_trailing_stop accepts Decimal prices."""
        calculator = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "fixed",
                    "fixed": {"value": 0.02},
                    "trailing": {
                        "enabled": True,
                        "activation_pct": 0.02,
                        "trailing_pct": 0.01,
                    },
                },
            }
        )

        entry_price = Decimal("50000")
        current_price = Decimal("52000")  # 4% profit
        current_stop = Decimal("49000")

        new_stop = calculator.calculate_trailing_stop(
            entry_price,
            current_price,
            current_stop,
            SignalDirection.ENTER_LONG,
        )

        assert new_stop is None or isinstance(new_stop, Decimal)


class TestFloatPrecisionEdgeCases:
    """
    Tests for specific floating-point precision edge cases.

    These tests verify that the Decimal implementation avoids
    IEEE-754 floating-point precision errors.
    """

    def test_ten_percent_of_ten_thousand(self):
        """
        Test the exact case that caused the original bug.

        In float: operations can accumulate precision errors.
        In Decimal: exact arithmetic is maintained.

        Note: 10000 * 0.1 may or may not show precision error depending on
        the platform, but chained operations reliably do.
        """
        # Using float - chained operations show precision issues
        _float_result = (10000.0 * 0.1) * 1.0000000001
        # This may show precision drift

        # Using Decimal (the new way - correct)
        decimal_result = Decimal("10000") * Decimal("0.1")
        assert decimal_result == Decimal("1000")

        # The key test: Decimal gives exact result
        assert decimal_result == Decimal("1000.0")
        assert decimal_result == Decimal("1000.00")

    def test_repeated_multiplications(self):
        """Test that repeated multiplications don't accumulate errors."""
        # Start with base value
        value = Decimal("1000")

        # Apply multiple operations
        value = value * Decimal("1.1")  # 1100
        value = value * Decimal("0.9")  # 990
        value = value * Decimal("1.01010101")  # ~1000

        # Round to compare
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Should be close to 1000 without accumulated floating-point errors
        assert abs(rounded - Decimal("1000")) < Decimal("1")

    def test_division_precision(self):
        """Test that division maintains precision."""
        # In float: 1 / 3 * 3 != 1
        decimal_result = (Decimal("1") / Decimal("3")) * Decimal("3")

        # Should be exactly 1 (or very close with controlled rounding)
        rounded = decimal_result.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        assert rounded == Decimal("0.9999") or rounded == Decimal("1.0000")

    def test_max_trade_size_boundary(self):
        """Test behavior at exact max_trade_size boundary."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        # Capital that produces exactly max_trade_size
        capital = Decimal("10000")
        limits = {"max_trade_size": 1000}

        result = sizer.check_limits(sizer.calculate_base_size(capital), capital, limits)

        # Should be exactly 1000.000 or less, never 1000.001
        assert result <= Decimal("1000")
        # Check it's rounded to 3 decimal places
        assert result == result.quantize(Decimal("0.001"))

    def test_max_trade_size_caps_when_capital_exceeds_boundary(self):
        """
        Test that max_trade_size caps the order when capital produces size > limit.

        This is the real-world bug: capital of 10000.01 USDT produces size=1000.001,
        which should be capped to 1000.000 by max_trade_size.
        """
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        # Capital slightly over 10000 (real-world scenario)
        capital = Decimal("10000.01")
        limits = {"max_trade_size": 1000}

        base_size = sizer.calculate_base_size(capital)
        # 10000.01 * 0.1 = 1000.001
        assert base_size == Decimal("1000.001")

        result = sizer.check_limits(base_size, capital, limits)

        # max_trade_size should cap it at 1000.000
        assert result == Decimal("1000.000"), f"Expected 1000.000, got {result}"
        assert result <= Decimal("1000"), f"Size {result} exceeds max_trade_size"


class TestEndToEndPositionSizingPipeline:
    """
    End-to-end tests for the position sizing pipeline.

    These tests verify that Decimal values flow correctly through
    the entire pipeline from capital to final order size.
    """

    def test_full_pipeline_with_max_trade_size(self):
        """Test full pipeline respects max_trade_size."""
        # Setup
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )
        tracker = PositionTracker()

        # Input values
        capital = Decimal("10000")
        signal_strength = Decimal("1.0")
        limits = {"max_trade_size": 1000}

        # Calculate size through pipeline
        base_size = sizer.calculate_base_size(capital)
        adjusted_size = sizer.apply_signal_strength(base_size, signal_strength)
        final_size = sizer.check_limits(adjusted_size, capital, limits)

        # Verify
        assert final_size <= Decimal("1000")
        assert isinstance(final_size, Decimal)

        # Record entry
        tracker.record_entry(Decimal("50000"), final_size)
        assert tracker.entry_count == 1

    def test_pipeline_with_scaling(self):
        """Test pipeline with position scaling."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
                "scaling": {
                    "enabled": True,
                    "method": "pyramid",
                    "max_entries": 3,
                    "entry_interval_pct": 0.02,
                    "pyramid": {"scale_factor": 0.5},
                },
            }
        )

        capital = Decimal("10000")
        limits = {"max_trade_size": 1000}

        # First entry
        base_size = sizer.calculate_base_size(capital)
        size_1 = sizer.check_limits(base_size, capital, limits)
        assert size_1 <= Decimal("1000")

        # Second entry (scaled down)
        scaled_size = sizer.apply_scaling(base_size, entry_count=1)
        size_2 = sizer.check_limits(scaled_size, capital, limits)
        assert size_2 <= Decimal("500")
        assert isinstance(size_2, Decimal)

    def test_pipeline_with_risk_controller(self):
        """Test pipeline integration with risk controller."""
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        controller = RiskController(
            config={"max_daily_trades": 5},
            initial_capital=Decimal("10000"),
            capital_mode="compound",
        )

        capital = Decimal("10000")
        limits = {"max_trade_size": 1000}

        # Calculate position size
        base_size = sizer.calculate_base_size(capital)
        _final_size = sizer.check_limits(base_size, capital, limits)

        # Check risk limits
        allowed, _ = controller.check_limits(capital)
        assert allowed is True

        # Record trade
        pnl = Decimal("50.25")
        controller.record_trade(pnl)

        # Update equity
        new_equity = capital + pnl
        controller.update_peak_equity(new_equity)

        # All operations should work with Decimal
        assert controller.session_pnl == pnl


class TestRegressionPrevention:
    """
    Regression tests to prevent reintroduction of float precision bugs.

    These tests document specific scenarios that previously failed
    due to floating-point precision errors.
    """

    def test_regression_1000_001_exceeds_1000(self):
        """
        Regression test for: quantity 1000.001 invalid (> max 1000.000)

        Original bug: Position size calculated as 1000.0000000001 due to
        float arithmetic, causing order rejection.
        """
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
            }
        )

        # Exact scenario from production
        capital = Decimal("10000")
        limits = {"max_trade_size": 1000}

        final_size = sizer.check_limits(sizer.calculate_base_size(capital), capital, limits)

        # This assertion would fail with the old float-based code
        assert final_size <= Decimal("1000"), f"REGRESSION: Size {final_size} exceeds max 1000"

    def test_regression_accumulated_precision_errors(self):
        """
        Regression test for accumulated precision errors across multiple operations.

        Original bug: Multiple float operations (base size * strength * scaling)
        accumulated precision errors.
        """
        sizer = PositionSizer(
            {
                "method": "percentage",
                "percentage": {"value": 0.1},
                "scaling": {
                    "enabled": True,
                    "method": "pyramid",
                    "max_entries": 5,
                    "entry_interval_pct": 0.02,
                    "pyramid": {"scale_factor": 0.5},
                },
            }
        )

        capital = Decimal("10000")
        strength = Decimal("0.9")  # 90% signal strength
        limits = {"max_trade_size": 450}  # Set limit at expected scaled size

        # Calculate: 10000 * 0.1 * 0.9 * 0.5 = 450
        base_size = sizer.calculate_base_size(capital)
        with_strength = sizer.apply_signal_strength(base_size, strength)
        with_scaling = sizer.apply_scaling(with_strength, entry_count=1)
        final_size = sizer.check_limits(with_scaling, capital, limits)

        # Should not exceed limit even after multiple operations
        assert final_size <= Decimal("450"), (
            f"REGRESSION: Size {final_size} exceeds max 450 after multiple ops"
        )
