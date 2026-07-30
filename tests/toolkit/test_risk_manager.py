"""Tests for shared.risk.manager module."""

from decimal import Decimal

from custos_toolkit.risk.manager import (
    RiskConfig,
    RiskManager,
    TrailingStopConfig,
)
from custos_toolkit.signals.types import SignalDirection


class TestTrailingStopConfig:
    """Tests for TrailingStopConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = TrailingStopConfig()
        assert config.enabled is False
        assert config.activation_pct == Decimal("0.02")
        assert config.trailing_pct == Decimal("0.01")

    def test_post_init_float_conversion(self):
        """Test __post_init__ converts float to Decimal."""
        config = TrailingStopConfig(
            enabled=True,
            activation_pct=0.03,
            trailing_pct=0.015,
        )
        assert isinstance(config.activation_pct, Decimal)
        assert config.activation_pct == Decimal("0.03")
        assert isinstance(config.trailing_pct, Decimal)
        assert config.trailing_pct == Decimal("0.015")

    def test_from_dict(self):
        """Test from_dict factory method."""
        data = {
            "enabled": True,
            "activation_pct": 0.05,
            "trailing_pct": 0.02,
        }
        config = TrailingStopConfig.from_dict(data)
        assert config.enabled is True
        assert config.activation_pct == Decimal("0.05")
        assert config.trailing_pct == Decimal("0.02")

    def test_from_dict_with_defaults(self):
        """Test from_dict uses defaults for missing keys."""
        config = TrailingStopConfig.from_dict({})
        assert config.enabled is False
        assert config.activation_pct == Decimal("0.02")


class TestRiskConfig:
    """Tests for RiskConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = RiskConfig()
        assert config.stop_loss_atr_multiplier == Decimal("2.0")
        assert config.take_profit_atr_multiplier == Decimal("4.0")
        assert config.stop_loss_pct is None
        assert config.take_profit_pct is None
        assert config.trailing_stop is None
        assert config.max_loss_per_trade_pct == Decimal("0.02")

    def test_post_init_float_conversion(self):
        """Test __post_init__ converts float to Decimal."""
        config = RiskConfig(
            stop_loss_atr_multiplier=1.5,
            take_profit_atr_multiplier=3.0,
            stop_loss_pct=0.02,
            take_profit_pct=0.06,
            max_loss_per_trade_pct=0.03,
        )
        assert isinstance(config.stop_loss_atr_multiplier, Decimal)
        assert isinstance(config.take_profit_atr_multiplier, Decimal)
        assert isinstance(config.stop_loss_pct, Decimal)
        assert isinstance(config.take_profit_pct, Decimal)
        assert isinstance(config.max_loss_per_trade_pct, Decimal)

    def test_from_dict_basic(self):
        """Test from_dict with basic values."""
        data = {
            "stop_loss_atr_multiplier": 1.5,
            "take_profit_atr_multiplier": 3.0,
        }
        config = RiskConfig.from_dict(data)
        assert config.stop_loss_atr_multiplier == Decimal("1.5")
        assert config.take_profit_atr_multiplier == Decimal("3.0")

    def test_from_dict_with_trailing_stop(self):
        """Test from_dict with trailing stop config."""
        data = {
            "trailing_stop": {
                "enabled": True,
                "activation_pct": 0.03,
                "trailing_pct": 0.015,
            }
        }
        config = RiskConfig.from_dict(data)
        assert config.trailing_stop is not None
        assert config.trailing_stop.enabled is True
        assert config.trailing_stop.activation_pct == Decimal("0.03")

    def test_from_dict_with_fixed_sl_tp(self):
        """Test from_dict with fixed SL/TP."""
        data = {
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.06,
        }
        config = RiskConfig.from_dict(data)
        assert config.stop_loss_pct == Decimal("0.02")
        assert config.take_profit_pct == Decimal("0.06")


class TestRiskManager:
    """Tests for RiskManager class."""

    def test_init_with_config(self):
        """Test initialization with RiskConfig."""
        config = RiskConfig(stop_loss_atr_multiplier=Decimal("1.5"))
        manager = RiskManager(config)
        assert manager.config.stop_loss_atr_multiplier == Decimal("1.5")

    def test_init_with_dict(self):
        """Test initialization with dictionary."""
        manager = RiskManager({"stop_loss_atr_multiplier": 1.5})
        assert manager.config.stop_loss_atr_multiplier == Decimal("1.5")

    # Stop Loss Tests

    def test_get_stop_loss_long_atr_based(self):
        """Test ATR-based stop loss for long position."""
        config = RiskConfig(stop_loss_atr_multiplier=Decimal("2.0"))
        manager = RiskManager(config)

        # Entry: 50000, ATR: 500, SL = 50000 - (500 * 2) = 49000
        sl = manager.get_stop_loss(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.ENTER_LONG,
        )
        assert sl == Decimal("49000")

    def test_get_stop_loss_short_atr_based(self):
        """Test ATR-based stop loss for short position."""
        config = RiskConfig(stop_loss_atr_multiplier=Decimal("2.0"))
        manager = RiskManager(config)

        # Entry: 50000, ATR: 500, SL = 50000 + (500 * 2) = 51000
        sl = manager.get_stop_loss(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.ENTER_SHORT,
        )
        assert sl == Decimal("51000")

    def test_get_stop_loss_long_percentage_based(self):
        """Test percentage-based stop loss for long position."""
        config = RiskConfig(stop_loss_pct=Decimal("0.02"))
        manager = RiskManager(config)

        # Entry: 50000, 2% = 1000, SL = 50000 - 1000 = 49000
        sl = manager.get_stop_loss(
            entry_price=50000,
            atr=500,  # Ignored when pct is set
            direction=SignalDirection.ENTER_LONG,
        )
        assert sl == Decimal("49000")

    def test_get_stop_loss_short_percentage_based(self):
        """Test percentage-based stop loss for short position."""
        config = RiskConfig(stop_loss_pct=Decimal("0.02"))
        manager = RiskManager(config)

        # Entry: 50000, 2% = 1000, SL = 50000 + 1000 = 51000
        sl = manager.get_stop_loss(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.ENTER_SHORT,
        )
        assert sl == Decimal("51000")

    def test_get_stop_loss_neutral_returns_entry(self):
        """Test stop loss for neutral direction returns entry price."""
        manager = RiskManager({})
        sl = manager.get_stop_loss(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.NEUTRAL,
        )
        assert sl == Decimal("50000")

    # Take Profit Tests

    def test_get_take_profit_long_atr_based(self):
        """Test ATR-based take profit for long position."""
        config = RiskConfig(take_profit_atr_multiplier=Decimal("4.0"))
        manager = RiskManager(config)

        # Entry: 50000, ATR: 500, TP = 50000 + (500 * 4) = 52000
        tp = manager.get_take_profit(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.ENTER_LONG,
        )
        assert tp == Decimal("52000")

    def test_get_take_profit_short_atr_based(self):
        """Test ATR-based take profit for short position."""
        config = RiskConfig(take_profit_atr_multiplier=Decimal("4.0"))
        manager = RiskManager(config)

        # Entry: 50000, ATR: 500, TP = 50000 - (500 * 4) = 48000
        tp = manager.get_take_profit(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.ENTER_SHORT,
        )
        assert tp == Decimal("48000")

    def test_get_take_profit_long_percentage_based(self):
        """Test percentage-based take profit for long position."""
        config = RiskConfig(take_profit_pct=Decimal("0.04"))
        manager = RiskManager(config)

        # Entry: 50000, 4% = 2000, TP = 50000 + 2000 = 52000
        tp = manager.get_take_profit(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.ENTER_LONG,
        )
        assert tp == Decimal("52000")

    def test_get_take_profit_neutral_returns_entry(self):
        """Test take profit for neutral direction returns entry price."""
        manager = RiskManager({})
        tp = manager.get_take_profit(
            entry_price=50000,
            atr=500,
            direction=SignalDirection.NEUTRAL,
        )
        assert tp == Decimal("50000")

    # Trailing Stop Tests

    def test_update_trailing_stop_disabled(self):
        """Test trailing stop returns current stop when disabled."""
        config = RiskConfig(trailing_stop=TrailingStopConfig(enabled=False))
        manager = RiskManager(config)

        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=52000,
            current_stop=49000,
            direction=SignalDirection.ENTER_LONG,
        )
        assert stop == Decimal("49000")

    def test_update_trailing_stop_no_config(self):
        """Test trailing stop returns current stop when no config."""
        manager = RiskManager({})

        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=52000,
            current_stop=49000,
            direction=SignalDirection.ENTER_LONG,
        )
        assert stop == Decimal("49000")

    def test_update_trailing_stop_long_not_activated(self):
        """Test trailing stop not activated for long when profit below threshold."""
        config = RiskConfig(
            trailing_stop=TrailingStopConfig(
                enabled=True,
                activation_pct=Decimal("0.02"),  # 2% activation
                trailing_pct=Decimal("0.01"),
            )
        )
        manager = RiskManager(config)

        # Entry: 50000, Current: 50500 (1% profit), needs 2% to activate
        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=50500,
            current_stop=49000,
            direction=SignalDirection.ENTER_LONG,
        )
        assert stop == Decimal("49000")  # Unchanged

    def test_update_trailing_stop_long_activated(self):
        """Test trailing stop activated for long when profit above threshold."""
        config = RiskConfig(
            trailing_stop=TrailingStopConfig(
                enabled=True,
                activation_pct=Decimal("0.02"),  # 2% activation
                trailing_pct=Decimal("0.01"),  # 1% trailing
            )
        )
        manager = RiskManager(config)

        # Entry: 50000, Current: 52000 (4% profit), activated
        # New stop = 52000 * (1 - 0.01) = 51480
        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=52000,
            current_stop=49000,
            direction=SignalDirection.ENTER_LONG,
        )
        assert stop == Decimal("51480")

    def test_update_trailing_stop_long_only_moves_up(self):
        """Test trailing stop for long only moves up, never down."""
        config = RiskConfig(
            trailing_stop=TrailingStopConfig(
                enabled=True,
                activation_pct=Decimal("0.02"),
                trailing_pct=Decimal("0.01"),
            )
        )
        manager = RiskManager(config)

        # Current stop is already at 51500
        # New calculated stop would be 51480, but should keep 51500
        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=52000,
            current_stop=51500,
            direction=SignalDirection.ENTER_LONG,
        )
        assert stop == Decimal("51500")  # Kept higher stop

    def test_update_trailing_stop_short_activated(self):
        """Test trailing stop activated for short when profit above threshold."""
        config = RiskConfig(
            trailing_stop=TrailingStopConfig(
                enabled=True,
                activation_pct=Decimal("0.02"),  # 2% activation
                trailing_pct=Decimal("0.01"),  # 1% trailing
            )
        )
        manager = RiskManager(config)

        # Entry: 50000, Current: 48000 (4% profit for short), activated
        # New stop = 48000 * (1 + 0.01) = 48480
        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=48000,
            current_stop=51000,
            direction=SignalDirection.ENTER_SHORT,
        )
        assert stop == Decimal("48480")

    def test_update_trailing_stop_short_only_moves_down(self):
        """Test trailing stop for short only moves down, never up."""
        config = RiskConfig(
            trailing_stop=TrailingStopConfig(
                enabled=True,
                activation_pct=Decimal("0.02"),
                trailing_pct=Decimal("0.01"),
            )
        )
        manager = RiskManager(config)

        # Current stop is already at 48400 (lower)
        # New calculated stop would be 48480, but should keep 48400
        stop = manager.update_trailing_stop(
            entry_price=50000,
            current_price=48000,
            current_stop=48400,
            direction=SignalDirection.ENTER_SHORT,
        )
        assert stop == Decimal("48400")  # Kept lower stop

    # Should Stop Out Tests

    def test_should_stop_out_long_triggered(self):
        """Test stop out triggered for long position."""
        manager = RiskManager({})

        # Price at or below stop
        assert (
            manager.should_stop_out(
                current_price=48999,
                stop_loss=49000,
                direction=SignalDirection.ENTER_LONG,
            )
            is True
        )

        assert (
            manager.should_stop_out(
                current_price=49000,
                stop_loss=49000,
                direction=SignalDirection.ENTER_LONG,
            )
            is True
        )

    def test_should_stop_out_long_not_triggered(self):
        """Test stop out not triggered for long position."""
        manager = RiskManager({})

        assert (
            manager.should_stop_out(
                current_price=50000,
                stop_loss=49000,
                direction=SignalDirection.ENTER_LONG,
            )
            is False
        )

    def test_should_stop_out_short_triggered(self):
        """Test stop out triggered for short position."""
        manager = RiskManager({})

        # Price at or above stop
        assert (
            manager.should_stop_out(
                current_price=51001,
                stop_loss=51000,
                direction=SignalDirection.ENTER_SHORT,
            )
            is True
        )

        assert (
            manager.should_stop_out(
                current_price=51000,
                stop_loss=51000,
                direction=SignalDirection.ENTER_SHORT,
            )
            is True
        )

    def test_should_stop_out_short_not_triggered(self):
        """Test stop out not triggered for short position."""
        manager = RiskManager({})

        assert (
            manager.should_stop_out(
                current_price=50000,
                stop_loss=51000,
                direction=SignalDirection.ENTER_SHORT,
            )
            is False
        )

    def test_should_stop_out_neutral_returns_false(self):
        """Test stop out for neutral direction returns False."""
        manager = RiskManager({})

        assert (
            manager.should_stop_out(
                current_price=50000,
                stop_loss=49000,
                direction=SignalDirection.NEUTRAL,
            )
            is False
        )

    # Should Take Profit Tests

    def test_should_take_profit_long_triggered(self):
        """Test take profit triggered for long position."""
        manager = RiskManager({})

        assert (
            manager.should_take_profit(
                current_price=52001,
                take_profit=52000,
                direction=SignalDirection.ENTER_LONG,
            )
            is True
        )

        assert (
            manager.should_take_profit(
                current_price=52000,
                take_profit=52000,
                direction=SignalDirection.ENTER_LONG,
            )
            is True
        )

    def test_should_take_profit_long_not_triggered(self):
        """Test take profit not triggered for long position."""
        manager = RiskManager({})

        assert (
            manager.should_take_profit(
                current_price=51000,
                take_profit=52000,
                direction=SignalDirection.ENTER_LONG,
            )
            is False
        )

    def test_should_take_profit_short_triggered(self):
        """Test take profit triggered for short position."""
        manager = RiskManager({})

        assert (
            manager.should_take_profit(
                current_price=47999,
                take_profit=48000,
                direction=SignalDirection.ENTER_SHORT,
            )
            is True
        )

        assert (
            manager.should_take_profit(
                current_price=48000,
                take_profit=48000,
                direction=SignalDirection.ENTER_SHORT,
            )
            is True
        )

    def test_should_take_profit_short_not_triggered(self):
        """Test take profit not triggered for short position."""
        manager = RiskManager({})

        assert (
            manager.should_take_profit(
                current_price=49000,
                take_profit=48000,
                direction=SignalDirection.ENTER_SHORT,
            )
            is False
        )

    def test_should_take_profit_neutral_returns_false(self):
        """Test take profit for neutral direction returns False."""
        manager = RiskManager({})

        assert (
            manager.should_take_profit(
                current_price=55000,
                take_profit=52000,
                direction=SignalDirection.NEUTRAL,
            )
            is False
        )

    # Risk Reward Tests

    def test_calculate_risk_reward(self):
        """Test risk/reward ratio calculation."""
        manager = RiskManager({})

        # Entry: 50000, SL: 49000 (risk: 1000), TP: 52000 (reward: 2000)
        # R:R = 2000 / 1000 = 2.0
        rr = manager.calculate_risk_reward(
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
        )
        assert rr == Decimal("2")

    def test_calculate_risk_reward_short(self):
        """Test risk/reward ratio for short position."""
        manager = RiskManager({})

        # Entry: 50000, SL: 51000 (risk: 1000), TP: 48000 (reward: 2000)
        # R:R = 2000 / 1000 = 2.0
        rr = manager.calculate_risk_reward(
            entry_price=50000,
            stop_loss=51000,
            take_profit=48000,
        )
        assert rr == Decimal("2")

    def test_calculate_risk_reward_zero_risk(self):
        """Test risk/reward returns 0 when risk is zero."""
        manager = RiskManager({})

        # Entry == SL, risk is 0
        rr = manager.calculate_risk_reward(
            entry_price=50000,
            stop_loss=50000,
            take_profit=52000,
        )
        assert rr == Decimal("0")

    def test_calculate_risk_reward_fractional(self):
        """Test risk/reward with fractional result."""
        manager = RiskManager({})

        # Entry: 50000, SL: 49500 (risk: 500), TP: 51000 (reward: 1000)
        # R:R = 1000 / 500 = 2.0
        rr = manager.calculate_risk_reward(
            entry_price=50000,
            stop_loss=49500,
            take_profit=51000,
        )
        assert rr == Decimal("2")

    # Input Type Conversion Tests

    def test_to_decimal_float(self):
        """Test _to_decimal converts float."""
        result = RiskManager._to_decimal(50000.5)
        assert isinstance(result, Decimal)
        assert result == Decimal("50000.5")

    def test_to_decimal_int(self):
        """Test _to_decimal converts int."""
        result = RiskManager._to_decimal(50000)
        assert isinstance(result, Decimal)
        assert result == Decimal("50000")

    def test_to_decimal_decimal(self):
        """Test _to_decimal preserves Decimal."""
        original = Decimal("50000.12345")
        result = RiskManager._to_decimal(original)
        assert result is original

    # Should Move to Break Even Tests

    def test_should_move_to_break_even_long_triggered(self):
        """Test break-even triggered for long position."""
        manager = RiskManager({})
        # Long position, price up 2%, trigger at 0.01 — a fraction, meaning 1%
        assert manager.should_move_to_break_even(100.0, 102.0, True, 0.01) is True

    def test_should_move_to_break_even_long_not_triggered(self):
        """Test break-even not triggered for long position."""
        manager = RiskManager({})
        # Long position, price up 0.5%, trigger at 0.01 (1%)
        assert manager.should_move_to_break_even(100.0, 100.5, True, 0.01) is False

    def test_should_move_to_break_even_short_triggered(self):
        """Test break-even triggered for short position."""
        manager = RiskManager({})
        # Short position, price down 2%, trigger at 0.01 (1%)
        assert manager.should_move_to_break_even(100.0, 98.0, False, 0.01) is True

    def test_should_move_to_break_even_short_not_triggered(self):
        """Test break-even not triggered for short position."""
        manager = RiskManager({})
        # Short position, price down 0.5%, trigger at 0.01 (1%)
        assert manager.should_move_to_break_even(100.0, 99.5, False, 0.01) is False

    def test_should_move_to_break_even_zero_entry_price(self):
        """Test break-even returns False for zero entry price."""
        manager = RiskManager({})
        assert manager.should_move_to_break_even(0, 100.0, True, 0.01) is False


class TestBreakEvenDecimalSemantics:
    """The break-even trigger is a fraction on both sides of the comparison.

    It once scaled the profit by 100 and then compared it against an unscaled
    trigger, so a configured 0.015 meaning 1.5% was read as 0.015%. Almost any
    favourable tick moved the stop to break even and the strategy collapsed into a
    zero-tolerance stop. These pin the fraction semantics.
    """

    def test_config_realistic_trigger_not_hit_below_threshold(self):
        """entry=100, current=101 (+1%), trigger=0.015 (1.5%) — does not trigger."""
        manager = RiskManager({})
        assert manager.should_move_to_break_even(100.0, 101.0, True, 0.015) is False

    def test_config_realistic_trigger_hit_above_threshold(self):
        """entry=100, current=102 (+2%), trigger=0.015 (1.5%) — triggers."""
        manager = RiskManager({})
        assert manager.should_move_to_break_even(100.0, 102.0, True, 0.015) is True

    def test_short_symmetry_not_hit(self):
        """short: entry=100, current=99 (-1%), trigger=0.015 — does not trigger."""
        manager = RiskManager({})
        assert manager.should_move_to_break_even(100.0, 99.0, False, 0.015) is False

    def test_short_symmetry_hit(self):
        """short: entry=100, current=98 (-2%), trigger=0.015 — triggers."""
        manager = RiskManager({})
        assert manager.should_move_to_break_even(100.0, 98.0, False, 0.015) is True

    def test_exact_threshold_triggers(self):
        """entry=100, current=101.5 (+1.5%), trigger=0.015 — triggers, the bound is inclusive."""
        manager = RiskManager({})
        assert manager.should_move_to_break_even(100.0, 101.5, True, 0.015) is True
