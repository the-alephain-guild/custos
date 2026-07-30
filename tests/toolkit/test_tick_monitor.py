"""The engine adapter's tick monitor."""

import pytest

pytest.importorskip("msgspec")

from decimal import Decimal

from custos_toolkit_nautilus.adapter.config.risk import (
    ScaledTakeProfitConfig,
    ScaledTakeProfitLevelConfig,
    StopLossConfig,
    StopLossTrailingConfig,
    TakeProfitConfig,
    TakeProfitFixedConfig,
    TakeProfitTrailingConfig,
    TradeRiskConfig,
)
from custos_toolkit_nautilus.adapter.tick_monitor import (
    ExitAction,
    TickMonitorManager,
    TrailingStopManager,
)


class TestExitAction:
    """Tests for ExitAction dataclass."""

    def test_full_exit_creation(self):
        """Test creating a full exit action."""
        action = ExitAction(
            exit_type="trailing_stop",
            price=Decimal("100.50"),
            reason="Trailing stop triggered at 2% drawdown from peak",
        )
        assert action.exit_type == "trailing_stop"
        assert action.price == Decimal("100.50")
        assert action.reason == "Trailing stop triggered at 2% drawdown from peak"
        assert action.partial_pct is None
        assert action.level is None

    def test_partial_exit_creation(self):
        """Test creating a partial exit action for scaled take profit."""
        action = ExitAction(
            exit_type="partial_tp",
            price=Decimal("105.00"),
            reason="Take profit level 1 reached",
            partial_pct=Decimal("0.33"),
            level=1,
        )
        assert action.exit_type == "partial_tp"
        assert action.price == Decimal("105.00")
        assert action.partial_pct == Decimal("0.33")
        assert action.level == 1

    def test_take_profit_exit(self):
        """Test creating a take profit exit action."""
        action = ExitAction(
            exit_type="take_profit",
            price=Decimal("110.00"),
            reason="Full take profit reached",
        )
        assert action.exit_type == "take_profit"
        assert action.price == Decimal("110.00")
        assert action.partial_pct is None
        assert action.level is None


class TestTrailingStopManagerInit:
    """Tests for TrailingStopManager initialization."""

    def test_init_with_decimal_values(self):
        """Test initialization with Decimal values."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        assert manager._activation_pct == Decimal("0.02")
        assert manager._trailing_pct == Decimal("0.01")
        assert manager.activated is False
        assert manager.peak_price is None

    def test_init_with_float_values(self):
        """Test initialization converts float to Decimal."""
        manager = TrailingStopManager(
            activation_pct=0.03,
            trailing_pct=0.015,
        )
        assert isinstance(manager._activation_pct, Decimal)
        assert isinstance(manager._trailing_pct, Decimal)
        assert manager._activation_pct == Decimal("0.03")
        assert manager._trailing_pct == Decimal("0.015")

    def test_initial_state(self):
        """Test initial state after construction."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        assert manager.activated is False
        assert manager.peak_price is None


class TestTrailingStopManagerInitPosition:
    """Tests for TrailingStopManager.init_position method."""

    def test_init_position_sets_peak_to_entry(self):
        """Test init_position sets peak price to entry price."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        assert manager.peak_price == Decimal("100.00")
        assert manager.activated is False

    def test_init_position_resets_activated_state(self):
        """Test init_position resets activated state."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        # Simulate previous activation
        manager._activated = True
        manager._peak_price = Decimal("110.00")

        # Init new position
        manager.init_position(entry_price=Decimal("95.00"), is_long=True)
        assert manager.activated is False
        assert manager.peak_price == Decimal("95.00")


class TestTrailingStopManagerReset:
    """Tests for TrailingStopManager.reset method."""

    def test_reset_clears_state(self):
        """Test reset clears all state."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        manager._activated = True
        manager._peak_price = Decimal("105.00")

        manager.reset()
        assert manager.activated is False
        assert manager.peak_price is None


class TestTrailingStopManagerUpdatePeak:
    """Tests for TrailingStopManager.update_peak method."""

    def test_update_peak_long_favorable_move(self):
        """Test peak updates when price moves up for long position."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # Price moves up - peak should update
        manager.update_peak(current_price=Decimal("105.00"), is_long=True)
        assert manager.peak_price == Decimal("105.00")

    def test_update_peak_long_unfavorable_move(self):
        """Test peak does NOT update when price moves down for long position."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        manager._peak_price = Decimal("105.00")

        # Price moves down - peak should NOT update
        manager.update_peak(current_price=Decimal("103.00"), is_long=True)
        assert manager.peak_price == Decimal("105.00")

    def test_update_peak_short_favorable_move(self):
        """Test peak updates when price moves down for short position."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # Price moves down - peak should update (favorable for short)
        manager.update_peak(current_price=Decimal("95.00"), is_long=False)
        assert manager.peak_price == Decimal("95.00")

    def test_update_peak_short_unfavorable_move(self):
        """Test peak does NOT update when price moves up for short position."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)
        manager._peak_price = Decimal("95.00")

        # Price moves up - peak should NOT update
        manager.update_peak(current_price=Decimal("97.00"), is_long=False)
        assert manager.peak_price == Decimal("95.00")


class TestTrailingStopManagerActivation:
    """Tests for TrailingStopManager activation threshold."""

    def test_not_activated_below_threshold_long(self):
        """Test trailing stop not activated when profit below threshold for long."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # 1% profit - should not activate (below 2% threshold)
        result = manager.check(
            current_price=Decimal("101.00"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is None
        assert manager.activated is False

    def test_activated_at_threshold_long(self):
        """Test trailing stop activates when profit reaches threshold for long."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # 2% profit - should activate
        result = manager.check(
            current_price=Decimal("102.00"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        # No exit yet since no drawdown from peak
        assert result is None
        assert manager.activated is True

    def test_not_activated_below_threshold_short(self):
        """Test trailing stop not activated when profit below threshold for short."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # 1% profit - should not activate
        result = manager.check(
            current_price=Decimal("99.00"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is None
        assert manager.activated is False

    def test_activated_at_threshold_short(self):
        """Test trailing stop activates when profit reaches threshold for short."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # 2% profit - should activate
        result = manager.check(
            current_price=Decimal("98.00"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is None
        assert manager.activated is True


class TestTrailingStopManagerExitTrigger:
    """Tests for TrailingStopManager exit trigger on drawdown."""

    def test_exit_triggered_long_on_drawdown(self):
        """Test exit triggered when drawdown exceeds trailing_pct for long."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),  # 1% trailing
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # First, move to 3% profit to activate
        manager._peak_price = Decimal("103.00")
        manager._activated = True

        # Now drop by 1.5% from peak (drawdown exceeds 1%)
        # 103 * 0.985 = 101.455
        result = manager.check(
            current_price=Decimal("101.40"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is not None
        assert isinstance(result, ExitAction)
        assert result.exit_type == "trailing_stop"
        assert result.price == Decimal("101.40")

    def test_no_exit_long_drawdown_below_threshold(self):
        """Test no exit when drawdown is below trailing_pct for long."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),  # 1% trailing
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        manager._peak_price = Decimal("103.00")
        manager._activated = True

        # Drop by 0.5% from peak (below 1% threshold)
        # 103 * 0.995 = 102.485
        result = manager.check(
            current_price=Decimal("102.50"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is None

    def test_exit_triggered_short_on_drawdown(self):
        """Test exit triggered when drawdown exceeds trailing_pct for short."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),  # 1% trailing
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # First, move to 3% profit to activate (price dropped to 97)
        manager._peak_price = Decimal("97.00")
        manager._activated = True

        # Now rise by 1.5% from peak (drawdown exceeds 1%)
        # 97 * 1.015 = 98.455
        result = manager.check(
            current_price=Decimal("98.50"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is not None
        assert isinstance(result, ExitAction)
        assert result.exit_type == "trailing_stop"
        assert result.price == Decimal("98.50")

    def test_no_exit_short_drawdown_below_threshold(self):
        """Test no exit when drawdown is below trailing_pct for short."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),  # 1% trailing
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)
        manager._peak_price = Decimal("97.00")
        manager._activated = True

        # Rise by 0.5% from peak (below 1% threshold)
        # 97 * 1.005 = 97.485
        result = manager.check(
            current_price=Decimal("97.40"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is None


class TestTrailingStopManagerIntegration:
    """Integration tests for full trailing stop lifecycle."""

    def test_full_long_lifecycle(self):
        """Test complete lifecycle: init -> activation -> exit for long."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )

        # 1. Initialize position
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        assert manager.peak_price == Decimal("100.00")
        assert manager.activated is False

        # 2. Price rises to 1% - not activated
        manager.update_peak(current_price=Decimal("101.00"), is_long=True)
        result = manager.check(
            current_price=Decimal("101.00"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is None
        assert manager.activated is False
        assert manager.peak_price == Decimal("101.00")

        # 3. Price rises to 2.5% - activates
        manager.update_peak(current_price=Decimal("102.50"), is_long=True)
        result = manager.check(
            current_price=Decimal("102.50"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is None
        assert manager.activated is True
        assert manager.peak_price == Decimal("102.50")

        # 4. Price rises to 3% - still no exit
        manager.update_peak(current_price=Decimal("103.00"), is_long=True)
        result = manager.check(
            current_price=Decimal("103.00"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is None
        assert manager.peak_price == Decimal("103.00")

        # 5. Price drops to 1.5% below peak - triggers exit
        # 103 * (1 - 0.015) = 101.455
        result = manager.check(
            current_price=Decimal("101.40"),
            entry_price=Decimal("100.00"),
            is_long=True,
        )
        assert result is not None
        assert result.exit_type == "trailing_stop"

    def test_full_short_lifecycle(self):
        """Test complete lifecycle: init -> activation -> exit for short."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )

        # 1. Initialize position
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)
        assert manager.peak_price == Decimal("100.00")
        assert manager.activated is False

        # 2. Price drops to 1% - not activated
        manager.update_peak(current_price=Decimal("99.00"), is_long=False)
        result = manager.check(
            current_price=Decimal("99.00"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is None
        assert manager.activated is False
        assert manager.peak_price == Decimal("99.00")

        # 3. Price drops to 2.5% - activates
        manager.update_peak(current_price=Decimal("97.50"), is_long=False)
        result = manager.check(
            current_price=Decimal("97.50"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is None
        assert manager.activated is True
        assert manager.peak_price == Decimal("97.50")

        # 4. Price drops to 3% - still no exit
        manager.update_peak(current_price=Decimal("97.00"), is_long=False)
        result = manager.check(
            current_price=Decimal("97.00"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is None
        assert manager.peak_price == Decimal("97.00")

        # 5. Price rises to 1.5% above peak - triggers exit
        # 97 * (1 + 0.015) = 98.455
        result = manager.check(
            current_price=Decimal("98.50"),
            entry_price=Decimal("100.00"),
            is_long=False,
        )
        assert result is not None
        assert result.exit_type == "trailing_stop"

    def test_reset_between_positions(self):
        """Test that reset properly clears state between positions."""
        manager = TrailingStopManager(
            activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )

        # First position
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        manager._peak_price = Decimal("105.00")
        manager._activated = True

        # Reset
        manager.reset()
        assert manager.peak_price is None
        assert manager.activated is False

        # Second position should start fresh
        manager.init_position(entry_price=Decimal("200.00"), is_long=False)
        assert manager.peak_price == Decimal("200.00")
        assert manager.activated is False


# =============================================================================
# TickMonitorManager Tests
# =============================================================================


class TestTickMonitorManagerModeHandling:
    """Tests for TickMonitorManager mode handling."""

    def test_exchange_mode_is_inactive(self):
        """Test that exchange mode returns is_active=False."""
        manager = TickMonitorManager(mode="exchange")
        assert manager.is_active is False

    def test_tick_mode_is_active(self):
        """Test that tick mode returns is_active=True."""
        manager = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.04"))
        assert manager.is_active is True

    def test_hybrid_mode_is_active(self):
        """Test that hybrid mode returns is_active=True."""
        manager = TickMonitorManager(mode="hybrid", tp_method="fixed", tp_fixed_pct=Decimal("0.04"))
        assert manager.is_active is True

    def test_check_returns_none_when_inactive(self):
        """Test that check returns None when mode is exchange."""
        manager = TickMonitorManager(mode="exchange")
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        result = manager.check(current_price=Decimal("110.00"))
        assert result is None


class TestTickMonitorManagerInit:
    """Tests for TickMonitorManager initialization."""

    def test_init_with_fixed_tp(self):
        """Test initialization with fixed take profit."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),
        )
        assert manager.is_active is True
        assert manager._tp_method == "fixed"
        assert manager._tp_fixed_pct == Decimal("0.04")

    def test_init_with_scaled_tp(self):
        """Test initialization with scaled take profit levels."""
        levels = [
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.06"), "exit_pct": Decimal("0.34")},
        ]
        manager = TickMonitorManager(
            mode="tick",
            tp_method="scaled",
            tp_levels=levels,
        )
        assert manager._tp_method == "scaled"
        assert len(manager._tp_levels) == 3

    def test_init_with_trailing_tp(self):
        """Test initialization with trailing take profit."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal("0.03"),
            trailing_pct=Decimal("0.01"),
        )
        assert manager._tp_method == "trailing"
        assert manager._trailing_manager is not None

    def test_peak_price_property_with_trailing(self):
        """Test peak_price property delegates to trailing manager."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal("0.03"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        assert manager.peak_price == Decimal("100.00")

    def test_peak_price_property_without_trailing(self):
        """Test peak_price property returns None when not in trailing mode."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),
        )
        assert manager.peak_price is None


class TestTickMonitorManagerInitPosition:
    """Tests for TickMonitorManager.init_position method."""

    def test_init_position_sets_entry_and_direction(self):
        """Test init_position sets entry price and direction."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        assert manager._entry_price == Decimal("100.00")
        assert manager._is_long is True

    def test_init_position_resets_scaled_levels(self):
        """Test init_position resets scaled TP levels hit tracking."""
        levels = [
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.33")},
        ]
        manager = TickMonitorManager(
            mode="tick",
            tp_method="scaled",
            tp_levels=levels,
        )
        # Simulate some levels being hit
        manager._tp_levels_hit = [True, False]

        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        assert manager._tp_levels_hit == [False, False]


class TestTickMonitorManagerReset:
    """Tests for TickMonitorManager.reset method."""

    def test_reset_clears_all_state(self):
        """Test reset clears all state."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        manager.reset()
        assert manager._entry_price is None
        assert manager._is_long is None

    def test_reset_clears_trailing_manager(self):
        """Test reset clears trailing manager state."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal("0.03"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)
        assert manager.peak_price is not None

        manager.reset()
        assert manager.peak_price is None


class TestTickMonitorManagerFixedTP:
    """Tests for TickMonitorManager fixed take profit."""

    def test_fixed_tp_triggers_on_target_long(self):
        """Test fixed TP triggers when target reached for long position."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),  # 4%
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # 5% profit - should trigger (>= 4%)
        result = manager.check(current_price=Decimal("105.00"))
        assert result is not None
        assert result.exit_type == "take_profit"
        assert result.price == Decimal("105.00")

    def test_fixed_tp_no_trigger_below_target_long(self):
        """Test fixed TP does not trigger below target for long position."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),  # 4%
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # 3% profit - should not trigger
        result = manager.check(current_price=Decimal("103.00"))
        assert result is None

    def test_fixed_tp_triggers_on_target_short(self):
        """Test fixed TP triggers when target reached for short position."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),  # 4%
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # 5% profit for short - price dropped to 95
        result = manager.check(current_price=Decimal("95.00"))
        assert result is not None
        assert result.exit_type == "take_profit"
        assert result.price == Decimal("95.00")

    def test_fixed_tp_no_trigger_below_target_short(self):
        """Test fixed TP does not trigger below target for short position."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.04"),  # 4%
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # 3% profit for short - price at 97
        result = manager.check(current_price=Decimal("97.00"))
        assert result is None


class TestTickMonitorManagerScaledTP:
    """Tests for TickMonitorManager scaled take profit."""

    def test_scaled_tp_first_level_triggers(self):
        """Test first scaled TP level triggers partial exit."""
        levels = [
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.06"), "exit_pct": Decimal("0.34")},
        ]
        manager = TickMonitorManager(
            mode="tick",
            tp_method="scaled",
            tp_levels=levels,
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # 2.5% profit - hits level 1 (2%)
        result = manager.check(current_price=Decimal("102.50"))
        assert result is not None
        assert result.exit_type == "partial_tp"
        assert result.partial_pct == Decimal("0.33")
        assert result.level == 1

    def test_scaled_tp_level_only_triggers_once(self):
        """Test each scaled TP level only triggers once."""
        levels = [
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.33")},
        ]
        manager = TickMonitorManager(
            mode="tick",
            tp_method="scaled",
            tp_levels=levels,
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # First check at 2.5% - hits level 1
        result1 = manager.check(current_price=Decimal("102.50"))
        assert result1 is not None
        assert result1.level == 1

        # Second check at same level - should not trigger again
        result2 = manager.check(current_price=Decimal("102.50"))
        assert result2 is None

    def test_scaled_tp_multiple_levels_sequential(self):
        """Test multiple scaled TP levels trigger sequentially."""
        levels = [
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.33")},
            {"target_pct": Decimal("0.06"), "exit_pct": Decimal("0.34")},
        ]
        manager = TickMonitorManager(
            mode="tick",
            tp_method="scaled",
            tp_levels=levels,
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # Hit level 1
        result1 = manager.check(current_price=Decimal("102.50"))
        assert result1.level == 1

        # Hit level 2
        result2 = manager.check(current_price=Decimal("104.50"))
        assert result2 is not None
        assert result2.level == 2
        assert result2.partial_pct == Decimal("0.33")

        # Hit level 3
        result3 = manager.check(current_price=Decimal("106.50"))
        assert result3 is not None
        assert result3.level == 3
        assert result3.partial_pct == Decimal("0.34")

    def test_scaled_tp_works_for_short(self):
        """Test scaled TP works correctly for short positions."""
        levels = [
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.50")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.50")},
        ]
        manager = TickMonitorManager(
            mode="tick",
            tp_method="scaled",
            tp_levels=levels,
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # For short: 2.5% profit means price at 97.50
        result = manager.check(current_price=Decimal("97.50"))
        assert result is not None
        assert result.exit_type == "partial_tp"
        assert result.level == 1


class TestTickMonitorManagerTrailingTP:
    """Tests for TickMonitorManager trailing take profit integration."""

    def test_trailing_tp_activates_and_triggers(self):
        """Test trailing TP activates at threshold and triggers on drawdown."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal("0.02"),  # 2% activation
            trailing_pct=Decimal("0.01"),  # 1% trailing
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # Not yet activated at 1%
        result1 = manager.check(current_price=Decimal("101.00"))
        assert result1 is None

        # Activated at 2%, no exit yet
        result2 = manager.check(current_price=Decimal("102.50"))
        assert result2 is None
        assert manager._trailing_manager.activated is True

        # Peak updates to 103
        result3 = manager.check(current_price=Decimal("103.00"))
        assert result3 is None
        assert manager.peak_price == Decimal("103.00")

        # Drawdown of 1.5% from peak triggers exit
        # 103 * (1 - 0.015) = 101.455
        result4 = manager.check(current_price=Decimal("101.40"))
        assert result4 is not None
        assert result4.exit_type == "trailing_stop"

    def test_trailing_tp_works_for_short(self):
        """Test trailing TP works correctly for short positions."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal("0.02"),
            trailing_pct=Decimal("0.01"),
        )
        manager.init_position(entry_price=Decimal("100.00"), is_long=False)

        # 2.5% profit for short (price at 97.50) - activates
        manager.check(current_price=Decimal("97.50"))
        assert manager._trailing_manager.activated is True

        # Price drops to 97.00 - updates peak
        manager.check(current_price=Decimal("97.00"))
        assert manager.peak_price == Decimal("97.00")

        # Price rises 1.5% from peak - triggers exit
        # 97 * 1.015 = 98.455
        result = manager.check(current_price=Decimal("98.50"))
        assert result is not None
        assert result.exit_type == "trailing_stop"


class TestTickMonitorManagerFromConfig:
    """Tests for TickMonitorManager.from_config factory method."""

    def test_from_config_exchange_mode(self):
        """Test from_config creates inactive manager for exchange mode."""
        config = TradeRiskConfig(sl_tp_mode="exchange")
        manager = TickMonitorManager.from_config(config, mode="exchange")
        assert manager.is_active is False

    def test_from_config_fixed_tp(self):
        """Test from_config with fixed take profit method."""
        config = TradeRiskConfig(
            sl_tp_mode="tick",
            take_profit=TakeProfitConfig(
                method="fixed",
                fixed=TakeProfitFixedConfig(value=0.05),
            ),
        )
        manager = TickMonitorManager.from_config(config, mode="tick")

        assert manager.is_active is True
        assert manager._tp_method == "fixed"
        assert manager._tp_fixed_pct == Decimal("0.05")

    def test_from_config_scaled_tp(self):
        """Test from_config with scaled take profit method."""
        config = TradeRiskConfig(
            sl_tp_mode="tick",
            take_profit=TakeProfitConfig(
                method="scaled",
                scaled=ScaledTakeProfitConfig(
                    levels=3,
                    level_1=ScaledTakeProfitLevelConfig(target_pct=0.02, exit_pct=0.30),
                    level_2=ScaledTakeProfitLevelConfig(target_pct=0.04, exit_pct=0.30),
                    level_3=ScaledTakeProfitLevelConfig(target_pct=0.06, exit_pct=0.40),
                ),
            ),
        )
        manager = TickMonitorManager.from_config(config, mode="tick")

        assert manager.is_active is True
        assert manager._tp_method == "scaled"
        assert len(manager._tp_levels) == 3
        assert manager._tp_levels[0]["target_pct"] == Decimal("0.02")
        assert manager._tp_levels[0]["exit_pct"] == Decimal("0.30")

    def test_from_config_trailing_tp(self):
        """Test from_config with trailing take profit method."""
        config = TradeRiskConfig(
            sl_tp_mode="tick",
            take_profit=TakeProfitConfig(
                method="trailing",
                trailing=TakeProfitTrailingConfig(
                    activation_pct=0.03,
                    callback_pct=0.01,
                ),
            ),
        )
        manager = TickMonitorManager.from_config(config, mode="tick")

        assert manager.is_active is True
        assert manager._tp_method == "trailing"
        assert manager._trailing_manager is not None

    def test_from_config_trailing_from_stop_loss(self):
        """Test from_config uses stop_loss trailing config when enabled."""
        config = TradeRiskConfig(
            sl_tp_mode="hybrid",
            take_profit=TakeProfitConfig(method="fixed"),
            stop_loss=StopLossConfig(
                trailing=StopLossTrailingConfig(
                    enabled=True,
                    activation_pct=0.025,
                    trailing_pct=0.012,
                ),
            ),
        )
        # When using trailing from stop_loss, we need to specify trailing method
        manager = TickMonitorManager.from_config(config, mode="hybrid")

        # The manager should be active for hybrid mode
        assert manager.is_active is True


class TestTickMonitorManagerIntegration:
    """Integration tests for TickMonitorManager full lifecycle."""

    def test_full_fixed_tp_lifecycle(self):
        """Test complete lifecycle with fixed TP."""
        manager = TickMonitorManager(
            mode="tick",
            tp_method="fixed",
            tp_fixed_pct=Decimal("0.03"),
        )

        # Initialize position
        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # Price below target
        assert manager.check(Decimal("101.00")) is None
        assert manager.check(Decimal("102.00")) is None

        # Price reaches target
        result = manager.check(Decimal("103.50"))
        assert result is not None
        assert result.exit_type == "take_profit"

        # Reset for new position
        manager.reset()
        manager.init_position(entry_price=Decimal("200.00"), is_long=False)

        # Short position - needs price drop
        assert manager.check(Decimal("196.00")) is None  # 2%
        result = manager.check(Decimal("193.00"))  # 3.5%
        assert result is not None
        assert result.exit_type == "take_profit"

    def test_full_scaled_tp_lifecycle(self):
        """Test complete lifecycle with scaled TP."""
        levels = [
            {"target_pct": Decimal("0.01"), "exit_pct": Decimal("0.25")},
            {"target_pct": Decimal("0.02"), "exit_pct": Decimal("0.25")},
            {"target_pct": Decimal("0.03"), "exit_pct": Decimal("0.25")},
            {"target_pct": Decimal("0.04"), "exit_pct": Decimal("0.25")},
        ]
        manager = TickMonitorManager(
            mode="hybrid",
            tp_method="scaled",
            tp_levels=levels,
        )

        manager.init_position(entry_price=Decimal("100.00"), is_long=True)

        # Progressively hit all levels
        results = []
        for price in [Decimal("101.50"), Decimal("102.50"), Decimal("103.50"), Decimal("104.50")]:
            result = manager.check(price)
            if result:
                results.append(result)

        assert len(results) == 4
        for i, result in enumerate(results, start=1):
            assert result.level == i
            assert result.partial_pct == Decimal("0.25")
