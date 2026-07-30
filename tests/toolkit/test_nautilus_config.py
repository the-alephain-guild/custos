"""Tests for shared.nautilus.config module.

These tests cover the msgspec.Struct based configuration models for NautilusTrader.
"""

import pytest

# Import msgspec for type checking
msgspec = pytest.importorskip("msgspec")

from custos_toolkit_nautilus.adapter.config import (  # noqa: E402
    # Filter configs
    AdxFilterConfig,
    CooldownConfig,
    # Trading configs
    ExecutionConfig,
    FiltersConfig,
    FixedScalingConfig,
    # Risk configs
    GlobalRiskConfig,
    # Platform configs
    HummingbotPlatformConfig,
    # Position configs
    KellyConfig,
    MartingaleScalingConfig,
    NautilusPlatformConfig,
    PlatformsConfig,
    PositionConfig,
    PositionLimitsConfig,
    PyramidScalingConfig,
    RiskConfig,
    ScalingConfig,
    StopLossAtrConfig,
    StopLossConfig,
    StopLossFixedConfig,
    StopLossIndicatorConfig,
    StopLossTrailingConfig,
    TakeProfitAtrConfig,
    TakeProfitConfig,
    TakeProfitFixedConfig,
    TakeProfitTrailingConfig,
    TimeFilterConfig,
    TradeRiskConfig,
    TradingConfig,
    VolatilityFilterConfig,
    VolumeFilterConfig,
    build_filters_config,
    build_platforms_config,
    build_position_config,
    build_risk_config,
    build_trading_config,
)

# =============================================================================
# Position Configuration Tests
# =============================================================================


class TestKellyConfig:
    """Tests for KellyConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = KellyConfig()
        assert config.fraction == 0.25
        assert config.win_rate == 0.55
        assert config.payoff_ratio == 2.0

    def test_custom_values(self):
        """Test custom configuration values."""
        config = KellyConfig(fraction=0.5, win_rate=0.6, payoff_ratio=3.0)
        assert config.fraction == 0.5
        assert config.win_rate == 0.6
        assert config.payoff_ratio == 3.0

    def test_frozen(self):
        """Test that config is frozen (immutable)."""
        config = KellyConfig()
        with pytest.raises(AttributeError):
            config.fraction = 0.5


class TestScalingConfig:
    """Tests for scaling configuration structs."""

    def test_pyramid_scaling_defaults(self):
        """Test pyramid scaling default values."""
        config = PyramidScalingConfig()
        assert config.scale_factor == 0.5

    def test_fixed_scaling_defaults(self):
        """Test fixed scaling default values."""
        config = FixedScalingConfig()
        assert config.size_per_entry == 0.1

    def test_martingale_scaling_defaults(self):
        """Test martingale scaling default values."""
        config = MartingaleScalingConfig()
        assert config.multiplier == 2.0

    def test_scaling_config_defaults(self):
        """Test main scaling config defaults."""
        config = ScalingConfig()
        assert config.enabled is False
        assert config.method == "pyramid"
        assert config.max_entries == 3
        assert config.entry_interval_pct == 0.02
        assert isinstance(config.pyramid, PyramidScalingConfig)
        assert isinstance(config.fixed, FixedScalingConfig)
        assert isinstance(config.martingale, MartingaleScalingConfig)

    def test_scaling_config_custom(self):
        """Test scaling config with custom values."""
        config = ScalingConfig(
            enabled=True,
            method="martingale",
            max_entries=5,
            entry_interval_pct=0.03,
        )
        assert config.enabled is True
        assert config.method == "martingale"
        assert config.max_entries == 5
        assert config.entry_interval_pct == 0.03


class TestPositionConfig:
    """Tests for PositionConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = PositionConfig()
        assert config.size_type == "percentage"
        assert config.size_value == 0.1
        assert config.capital_mode == "compound"
        assert config.initial_capital == 10000.0
        assert isinstance(config.kelly, KellyConfig)
        assert isinstance(config.scaling, ScalingConfig)
        assert isinstance(config.limits, PositionLimitsConfig)

    def test_position_limits_defaults(self):
        """Test position limits default values."""
        config = PositionLimitsConfig()
        assert config.max_positions_per_pair == 1
        assert config.min_order_size == 10.0
        assert config.max_position_pct == 0.5


class TestBuildPositionConfig:
    """Tests for build_position_config function."""

    def test_empty_dict(self):
        """Test build with empty dict returns defaults."""
        config = build_position_config({})
        assert config.size_type == "percentage"
        assert config.size_value == 0.1

    def test_none_returns_defaults(self):
        """Test build with None returns defaults."""
        config = build_position_config(None)
        assert isinstance(config, PositionConfig)

    def test_basic_values(self):
        """Test build with basic values."""
        data = {
            "size_type": "fixed",
            "size_value": 500,
            "capital_mode": "fixed",
            "initial_capital": 50000,
        }
        config = build_position_config(data)
        assert config.size_type == "fixed"
        assert config.size_value == 500
        assert config.capital_mode == "fixed"
        assert config.initial_capital == 50000

    def test_with_kelly(self):
        """Test build with Kelly config."""
        data = {
            "kelly": {
                "fraction": 0.3,
                "win_rate": 0.65,
                "payoff_ratio": 2.5,
            }
        }
        config = build_position_config(data)
        assert config.kelly.fraction == 0.3
        assert config.kelly.win_rate == 0.65
        assert config.kelly.payoff_ratio == 2.5

    def test_with_scaling(self):
        """Test build with scaling config."""
        data = {
            "scaling": {
                "enabled": True,
                "method": "pyramid",
                "max_entries": 4,
                "entry_interval_pct": 0.025,
                "pyramid": {"scale_factor": 0.6},
            }
        }
        config = build_position_config(data)
        assert config.scaling.enabled is True
        assert config.scaling.method == "pyramid"
        assert config.scaling.max_entries == 4
        assert config.scaling.pyramid.scale_factor == 0.6

    def test_with_limits(self):
        """Test build with limits config."""
        data = {
            "limits": {
                "max_positions_per_pair": 3,
                "min_order_size": 50,
                "max_position_pct": 0.3,
            }
        }
        config = build_position_config(data)
        assert config.limits.max_positions_per_pair == 3
        assert config.limits.min_order_size == 50
        assert config.limits.max_position_pct == 0.3


# =============================================================================
# Risk Configuration Tests
# =============================================================================


class TestGlobalRiskConfig:
    """Tests for GlobalRiskConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = GlobalRiskConfig()
        assert config.max_daily_loss == 0.05
        assert config.max_drawdown == 0.10
        assert config.consecutive_loss_pause == 3

    def test_rejects_negative_pause_duration(self):
        """Negative pause_duration must fail-fast at construction."""
        with pytest.raises(ValueError, match="pause_duration"):
            GlobalRiskConfig(pause_duration=-1)

    def test_rejects_invalid_reset_time(self):
        """Malformed reset_time must fail-fast at construction."""
        with pytest.raises(ValueError, match="reset_time"):
            GlobalRiskConfig(reset_time="25:00")
        with pytest.raises(ValueError, match="reset_time"):
            GlobalRiskConfig(reset_time="noon")

    def test_rejects_out_of_range_ratios(self):
        """#10: ratio fields are decimals in [0, 1]; a value like 5.0 is a unit error
        (5% written as 5.0) and must fail-fast instead of meaning 500%."""
        with pytest.raises(ValueError, match="max_daily_loss"):
            GlobalRiskConfig(max_daily_loss=5.0)
        with pytest.raises(ValueError, match="max_drawdown"):
            GlobalRiskConfig(max_drawdown=-0.1)
        with pytest.raises(ValueError, match="max_daily_profit"):
            GlobalRiskConfig(max_daily_profit=2.0)


class TestTakeProfitConfig:
    """Tests for take profit configuration structs."""

    def test_atr_config_defaults(self):
        """Test ATR take profit defaults."""
        config = TakeProfitAtrConfig()
        assert config.multiplier == 6.0

    def test_fixed_config_defaults(self):
        """Test fixed take profit defaults."""
        config = TakeProfitFixedConfig()
        assert config.value == 0.04

    def test_trailing_config_defaults(self):
        """Test trailing take profit defaults."""
        config = TakeProfitTrailingConfig()
        assert config.activation_pct == 0.03
        assert config.callback_pct == 0.01

    def test_take_profit_config_defaults(self):
        """Test main take profit config defaults."""
        config = TakeProfitConfig()
        assert config.method == "atr"
        assert isinstance(config.atr, TakeProfitAtrConfig)
        assert isinstance(config.fixed, TakeProfitFixedConfig)
        assert isinstance(config.trailing, TakeProfitTrailingConfig)


class TestStopLossConfig:
    """Tests for stop loss configuration structs."""

    def test_atr_config_defaults(self):
        """Test ATR stop loss defaults."""
        config = StopLossAtrConfig()
        assert config.multiplier == 2.0

    def test_fixed_config_defaults(self):
        """Test fixed stop loss defaults."""
        config = StopLossFixedConfig()
        assert config.value == 0.02

    def test_trailing_config_defaults(self):
        """Test trailing stop loss defaults."""
        config = StopLossTrailingConfig()
        assert config.enabled is False
        assert config.activation_pct == 0.02
        assert config.trailing_pct == 0.015

    def test_indicator_config_defaults(self):
        """Test indicator stop loss defaults."""
        config = StopLossIndicatorConfig()
        assert config.type == "supertrend"

    def test_stop_loss_config_defaults(self):
        """Test main stop loss config defaults."""
        config = StopLossConfig()
        assert config.method == "atr"
        assert isinstance(config.atr, StopLossAtrConfig)
        assert isinstance(config.fixed, StopLossFixedConfig)
        assert isinstance(config.trailing, StopLossTrailingConfig)
        assert isinstance(config.indicator, StopLossIndicatorConfig)


class TestTradeRiskConfig:
    """Tests for TradeRiskConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = TradeRiskConfig()
        assert config.max_loss_pct == 0.02
        assert config.time_limit == 604800  # 1 week
        assert isinstance(config.take_profit, TakeProfitConfig)
        assert isinstance(config.stop_loss, StopLossConfig)

    def test_rejects_out_of_range_max_loss_pct(self):
        """#10: max_loss_pct is a decimal in (0, 1]; 5.0 (meant as 5%) and 0/negative
        must fail-fast rather than silently arming a 500% / no-op loss limit."""
        with pytest.raises(ValueError, match="max_loss_pct"):
            TradeRiskConfig(max_loss_pct=5.0)
        with pytest.raises(ValueError, match="max_loss_pct"):
            TradeRiskConfig(max_loss_pct=0.0)


class TestRiskConfig:
    """Tests for RiskConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = RiskConfig()
        assert isinstance(config.global_risk, GlobalRiskConfig)
        assert isinstance(config.trade, TradeRiskConfig)


class TestBuildRiskConfig:
    """Tests for build_risk_config function."""

    def test_empty_dict(self):
        """Test build with empty dict returns defaults."""
        config = build_risk_config({})
        assert isinstance(config, RiskConfig)

    def test_none_returns_defaults(self):
        """Test build with None returns defaults."""
        config = build_risk_config(None)
        assert isinstance(config, RiskConfig)

    def test_with_global_risk(self):
        """Test build with global risk config."""
        data = {
            "global": {
                "max_daily_loss": 0.03,
                "max_drawdown": 0.15,
                "consecutive_loss_pause": 5,
            }
        }
        config = build_risk_config(data)
        assert config.global_risk.max_daily_loss == 0.03
        assert config.global_risk.max_drawdown == 0.15
        assert config.global_risk.consecutive_loss_pause == 5

    def test_with_trade_risk(self):
        """Test build with trade risk config."""
        data = {
            "trade": {
                "max_loss_pct": 0.03,
                "time_limit": 86400,
            }
        }
        config = build_risk_config(data)
        assert config.trade.max_loss_pct == 0.03
        assert config.trade.time_limit == 86400

    def test_with_take_profit(self):
        """Test build with take profit config."""
        data = {
            "trade": {
                "take_profit": {
                    "method": "fixed",
                    "atr": {"multiplier": 4.0},
                    "fixed": {"value": 0.06},
                }
            }
        }
        config = build_risk_config(data)
        assert config.trade.take_profit.method == "fixed"
        assert config.trade.take_profit.atr.multiplier == 4.0
        assert config.trade.take_profit.fixed.value == 0.06

    def test_with_stop_loss(self):
        """Test build with stop loss config."""
        data = {
            "trade": {
                "stop_loss": {
                    "method": "trailing",
                    "atr": {"multiplier": 1.5},
                    "trailing": {
                        "enabled": True,
                        "activation_pct": 0.03,
                        "trailing_pct": 0.02,
                    },
                }
            }
        }
        config = build_risk_config(data)
        assert config.trade.stop_loss.method == "trailing"
        assert config.trade.stop_loss.atr.multiplier == 1.5
        assert config.trade.stop_loss.trailing.enabled is True
        assert config.trade.stop_loss.trailing.activation_pct == 0.03

    def test_with_tick_monitoring(self):
        """Test build with tick monitoring config."""
        data = {
            "tick_monitoring": {
                "enabled": True,
                "tick_type": "quote",
            }
        }
        config = build_risk_config(data)
        assert config.tick_monitoring.enabled is True
        assert config.tick_monitoring.tick_type == "quote"


class TestTickMonitoringConfig:
    """Tests for TickMonitoringConfig."""

    def test_default_values(self):
        from custos_toolkit_nautilus.adapter.config import TickMonitoringConfig

        config = TickMonitoringConfig()
        assert config.enabled is False
        assert config.tick_type == "trade"

    def test_custom_values(self):
        from custos_toolkit_nautilus.adapter.config import TickMonitoringConfig

        config = TickMonitoringConfig(enabled=True, tick_type="quote")
        assert config.enabled is True
        assert config.tick_type == "quote"

    def test_tick_type_both(self):
        from custos_toolkit_nautilus.adapter.config import TickMonitoringConfig

        config = TickMonitoringConfig(enabled=True, tick_type="both")
        assert config.tick_type == "both"


# =============================================================================
# Filters Configuration Tests
# =============================================================================


class TestAdxFilterConfig:
    """Tests for AdxFilterConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = AdxFilterConfig()
        assert config.enabled is False
        assert config.period == 14
        assert config.threshold == 25


class TestVolatilityFilterConfig:
    """Tests for VolatilityFilterConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = VolatilityFilterConfig()
        assert config.enabled is False
        assert config.min_atr_pct == 0.003


class TestVolumeFilterConfig:
    """Tests for VolumeFilterConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = VolumeFilterConfig()
        assert config.enabled is False
        assert config.ma_period == 20
        assert config.threshold == 1.2


class TestTimeFilterConfig:
    """Tests for TimeFilterConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = TimeFilterConfig()
        assert config.enabled is False
        assert config.trading_hours == "00:00-23:59"
        assert config.excluded_days == ()
        assert config.excluded_dates == ()

    def test_custom_values(self):
        """Test custom configuration values."""
        config = TimeFilterConfig(
            enabled=True,
            trading_hours="09:00-17:00",
            excluded_days=(5, 6),
            excluded_dates=("2024-12-25",),
        )
        assert config.enabled is True
        assert config.trading_hours == "09:00-17:00"
        assert config.excluded_days == (5, 6)
        assert config.excluded_dates == ("2024-12-25",)


class TestCooldownConfig:
    """Tests for CooldownConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = CooldownConfig()
        assert config.min_holding_time == 0
        assert config.after_exit == 0
        assert config.after_stop_loss == 300
        assert config.after_take_profit == 0


class TestFiltersConfig:
    """Tests for FiltersConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = FiltersConfig()
        assert isinstance(config.adx_filter, AdxFilterConfig)
        assert isinstance(config.volatility_filter, VolatilityFilterConfig)
        assert isinstance(config.volume_filter, VolumeFilterConfig)
        assert isinstance(config.time_filter, TimeFilterConfig)
        assert isinstance(config.cooldown, CooldownConfig)


class TestBuildFiltersConfig:
    """Tests for build_filters_config function."""

    def test_empty_dict(self):
        """Test build with empty dict returns defaults."""
        config = build_filters_config({})
        assert isinstance(config, FiltersConfig)

    def test_none_returns_defaults(self):
        """Test build with None returns defaults."""
        config = build_filters_config(None)
        assert isinstance(config, FiltersConfig)

    def test_with_adx_filter(self):
        """Test build with ADX filter config."""
        data = {
            "adx_filter": {
                "enabled": True,
                "period": 10,
                "threshold": 30,
            }
        }
        config = build_filters_config(data)
        assert config.adx_filter.enabled is True
        assert config.adx_filter.period == 10
        assert config.adx_filter.threshold == 30

    def test_with_volatility_filter(self):
        """Test build with volatility filter config."""
        data = {
            "volatility_filter": {
                "enabled": False,
                "min_atr_pct": 0.005,
            }
        }
        config = build_filters_config(data)
        assert config.volatility_filter.enabled is False
        assert config.volatility_filter.min_atr_pct == 0.005

    def test_with_volume_filter(self):
        """Test build with volume filter config."""
        data = {
            "volume_filter": {
                "enabled": True,
                "ma_period": 30,
                "threshold": 1.5,
            }
        }
        config = build_filters_config(data)
        assert config.volume_filter.enabled is True
        assert config.volume_filter.ma_period == 30
        assert config.volume_filter.threshold == 1.5

    def test_with_time_filter(self):
        """Test build with time filter config."""
        data = {
            "time_filter": {
                "enabled": True,
                "trading_hours": "09:00-17:00",
                "excluded_days": [5, 6],
                "excluded_dates": ["2024-12-25", "2024-01-01"],
            }
        }
        config = build_filters_config(data)
        assert config.time_filter.enabled is True
        assert config.time_filter.trading_hours == "09:00-17:00"
        assert config.time_filter.excluded_days == (5, 6)
        assert config.time_filter.excluded_dates == ("2024-12-25", "2024-01-01")

    def test_with_cooldown(self):
        """Test build with cooldown config."""
        data = {
            "cooldown": {
                "min_holding_time": 60,
                "after_exit": 120,
                "after_stop_loss": 600,
                "after_take_profit": 30,
            }
        }
        config = build_filters_config(data)
        assert config.cooldown.min_holding_time == 60
        assert config.cooldown.after_exit == 120
        assert config.cooldown.after_stop_loss == 600
        assert config.cooldown.after_take_profit == 30

    def test_full_config(self):
        """Test build with full configuration."""
        data = {
            "adx_filter": {"enabled": True, "period": 14, "threshold": 25},
            "volatility_filter": {"enabled": True, "min_atr_pct": 0.003},
            "volume_filter": {"enabled": True, "ma_period": 20, "threshold": 1.2},
            "time_filter": {
                "enabled": True,
                "trading_hours": "09:00-17:00",
                "excluded_days": [5, 6],
            },
            "cooldown": {"after_stop_loss": 300},
        }
        config = build_filters_config(data)

        assert config.adx_filter.enabled is True
        assert config.volatility_filter.enabled is True
        assert config.volume_filter.enabled is True
        assert config.time_filter.enabled is True
        assert config.cooldown.after_stop_loss == 300


# =============================================================================
# Platform Configuration Tests
# =============================================================================


class TestNautilusHummingbotPlatformConfig:
    """Tests for HummingbotPlatformConfig struct (Nautilus version)."""

    def test_default_values(self):
        """Test default configuration values."""
        config = HummingbotPlatformConfig()
        assert config.candles_exchange is None
        assert config.candles_pair is None
        assert config.candles_interval == "1h"

    def test_custom_values(self):
        """Test custom configuration values."""
        config = HummingbotPlatformConfig(
            candles_exchange="binance",
            candles_pair="ETH-USDT",
            candles_interval="4h",
        )
        assert config.candles_exchange == "binance"
        assert config.candles_pair == "ETH-USDT"
        assert config.candles_interval == "4h"

    def test_frozen(self):
        """Test that config is frozen (immutable)."""
        config = HummingbotPlatformConfig()
        with pytest.raises(AttributeError):
            config.candles_interval = "4h"


class TestNautilusNautilusPlatformConfig:
    """Tests for NautilusPlatformConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = NautilusPlatformConfig()
        assert config.venue == "BINANCE"
        assert config.bar_type == "1-HOUR"
        assert config.bar_aggregation == "EXTERNAL"

    def test_custom_values(self):
        """Test custom configuration values."""
        config = NautilusPlatformConfig(
            venue="BYBIT",
            bar_type="4-HOUR",
        )
        assert config.venue == "BYBIT"
        assert config.bar_type == "4-HOUR"
        assert config.bar_aggregation == "EXTERNAL"  # default

    def test_bar_aggregation_internal(self):
        """Test bar_aggregation set to INTERNAL for sandbox mode."""
        config = NautilusPlatformConfig(
            bar_aggregation="INTERNAL",
        )
        assert config.bar_aggregation == "INTERNAL"


class TestNautilusPlatformsConfig:
    """Tests for PlatformsConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = PlatformsConfig()
        assert isinstance(config.hummingbot, HummingbotPlatformConfig)
        assert isinstance(config.nautilus, NautilusPlatformConfig)


class TestBuildNautilusPlatformsConfig:
    """Tests for build_platforms_config function (Nautilus version)."""

    def test_empty_dict(self):
        """Test build with empty dict returns defaults."""
        config = build_platforms_config({})
        assert isinstance(config, PlatformsConfig)

    def test_none_returns_defaults(self):
        """Test build with None returns defaults."""
        config = build_platforms_config(None)
        assert isinstance(config, PlatformsConfig)

    def test_with_hummingbot_config(self):
        """Test build with Hummingbot config."""
        data = {
            "hummingbot": {
                "candles_exchange": "kucoin",
                "candles_pair": "BTC-USDT",
                "candles_interval": "15m",
            }
        }
        config = build_platforms_config(data)
        assert config.hummingbot.candles_exchange == "kucoin"
        assert config.hummingbot.candles_pair == "BTC-USDT"
        assert config.hummingbot.candles_interval == "15m"

    def test_with_nautilus_config(self):
        """Test build with Nautilus config."""
        data = {
            "nautilus": {
                "venue": "OKX",
                "bar_type": "1-DAY",
            }
        }
        config = build_platforms_config(data)
        assert config.nautilus.venue == "OKX"
        assert config.nautilus.bar_type == "1-DAY"

    def test_with_both_platforms(self):
        """Test build with both platform configs."""
        data = {
            "hummingbot": {
                "candles_interval": "30m",
            },
            "nautilus": {
                "venue": "BYBIT",
            },
        }
        config = build_platforms_config(data)
        assert config.hummingbot.candles_interval == "30m"
        assert config.nautilus.venue == "BYBIT"

    def test_filters_null_values(self):
        """Test that null values are filtered out."""
        data = {
            "hummingbot": {
                "candles_exchange": None,
                "candles_pair": None,
                "candles_interval": "2h",
            }
        }
        config = build_platforms_config(data)
        assert config.hummingbot.candles_exchange is None
        assert config.hummingbot.candles_interval == "2h"

    def test_empty_hummingbot_dict(self):
        """Test with empty hummingbot dict uses defaults."""
        data = {
            "hummingbot": {},
            "nautilus": {"venue": "BYBIT"},
        }
        config = build_platforms_config(data)
        assert config.hummingbot.candles_interval == "1h"
        assert config.nautilus.venue == "BYBIT"

    def test_empty_nautilus_dict(self):
        """Test with empty nautilus dict uses defaults."""
        data = {
            "hummingbot": {"candles_interval": "4h"},
            "nautilus": {},
        }
        config = build_platforms_config(data)
        assert config.hummingbot.candles_interval == "4h"
        assert config.nautilus.venue == "BINANCE"

    def test_with_nested_value_format(self):
        """Test build with raw YAML nested {value:...} format.

        This tests handling of raw YAML config that hasn't been processed
        by ConfigWrapper, where values have nested schema format like:
        nautilus:
          bar_type:
            value: "1-HOUR"
            type: string
        """
        data = {
            "nautilus": {
                "venue": {"value": "OKX", "type": "string"},
                "bar_type": {"value": "4-HOUR", "type": "string"},
            }
        }
        config = build_platforms_config(data)
        # Should extract the value, not use the nested dict
        assert config.nautilus.venue == "OKX"
        assert config.nautilus.bar_type == "4-HOUR"


# =============================================================================
# Trading Configuration Tests
# =============================================================================


class TestExecutionConfig:
    """Tests for ExecutionConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = ExecutionConfig()
        assert config.price_type == "mid"
        assert config.limit_order_timeout == 60
        assert config.slippage_tolerance == 0.001
        assert config.retry_on_failure is True
        assert config.max_retries == 3

    def test_custom_values(self):
        """Test custom configuration values."""
        config = ExecutionConfig(
            price_type="last",
            limit_order_timeout=30,
            slippage_tolerance=0.002,
            retry_on_failure=False,
            max_retries=5,
        )
        assert config.price_type == "last"
        assert config.limit_order_timeout == 30
        assert config.slippage_tolerance == 0.002
        assert config.retry_on_failure is False
        assert config.max_retries == 5


class TestNautilusTradingConfig:
    """Tests for TradingConfig struct."""

    def test_default_values(self):
        """Test default configuration values."""
        config = TradingConfig()
        assert config.connector == "binance_perpetual"
        assert config.leverage == 1
        assert config.pairs == ("BTC-USDT",)
        assert config.direction == "both"
        assert config.order_type == "limit"
        assert config.position_mode == "ONEWAY"
        assert isinstance(config.execution, ExecutionConfig)

    def test_custom_values(self):
        """Test custom configuration values."""
        config = TradingConfig(
            connector="bybit_perpetual",
            leverage=10,
            pairs=("ETH-USDT", "BTC-USDT"),
            direction="long",
            order_type="market",
            position_mode="HEDGE",
        )
        assert config.connector == "bybit_perpetual"
        assert config.leverage == 10
        assert config.pairs == ("ETH-USDT", "BTC-USDT")
        assert config.direction == "long"
        assert config.order_type == "market"
        assert config.position_mode == "HEDGE"

    def test_enable_long_both(self):
        """Test enable_long property with direction=both."""
        config = TradingConfig(direction="both")
        assert config.enable_long is True

    def test_enable_long_long_only(self):
        """Test enable_long property with direction=long."""
        config = TradingConfig(direction="long")
        assert config.enable_long is True

    def test_enable_long_short_only(self):
        """Test enable_long property with direction=short."""
        config = TradingConfig(direction="short")
        assert config.enable_long is False

    def test_enable_short_both(self):
        """Test enable_short property with direction=both."""
        config = TradingConfig(direction="both")
        assert config.enable_short is True

    def test_enable_short_short_only(self):
        """Test enable_short property with direction=short."""
        config = TradingConfig(direction="short")
        assert config.enable_short is True

    def test_enable_short_long_only(self):
        """Test enable_short property with direction=long."""
        config = TradingConfig(direction="long")
        assert config.enable_short is False


class TestBuildTradingConfig:
    """Tests for build_trading_config function."""

    def test_empty_dict(self):
        """Test build with empty dict returns defaults."""
        config = build_trading_config({})
        assert config.connector == "binance_perpetual"
        assert config.leverage == 1

    def test_none_returns_defaults(self):
        """Test build with None returns defaults."""
        config = build_trading_config(None)
        assert isinstance(config, TradingConfig)

    def test_basic_values(self):
        """Test build with basic values."""
        data = {
            "connector": "okx_perpetual",
            "leverage": 5,
            "direction": "long",
            "order_type": "market",
            "position_mode": "HEDGE",
        }
        config = build_trading_config(data)
        assert config.connector == "okx_perpetual"
        assert config.leverage == 5
        assert config.direction == "long"
        assert config.order_type == "market"
        assert config.position_mode == "HEDGE"

    def test_with_pairs_list(self):
        """Test build with pairs as list (converted to tuple)."""
        data = {
            "pairs": ["ETH-USDT", "BTC-USDT", "SOL-USDT"],
        }
        config = build_trading_config(data)
        assert config.pairs == ("ETH-USDT", "BTC-USDT", "SOL-USDT")
        assert isinstance(config.pairs, tuple)

    def test_with_pairs_tuple(self):
        """Test build with pairs as tuple."""
        data = {
            "pairs": ("ETH-USDT",),
        }
        config = build_trading_config(data)
        assert config.pairs == ("ETH-USDT",)

    def test_with_execution_config(self):
        """Test build with execution config."""
        data = {
            "execution": {
                "price_type": "last",
                "limit_order_timeout": 120,
                "slippage_tolerance": 0.005,
                "retry_on_failure": False,
                "max_retries": 1,
            }
        }
        config = build_trading_config(data)
        assert config.execution.price_type == "last"
        assert config.execution.limit_order_timeout == 120
        assert config.execution.slippage_tolerance == 0.005
        assert config.execution.retry_on_failure is False
        assert config.execution.max_retries == 1

    def test_empty_execution_uses_defaults(self):
        """Test build with empty execution dict uses defaults."""
        data = {
            "connector": "binance",
            "execution": {},
        }
        config = build_trading_config(data)
        assert config.execution.price_type == "mid"
        assert config.execution.limit_order_timeout == 60

    def test_full_config(self):
        """Test build with full configuration."""
        data = {
            "connector": "bybit_perpetual",
            "leverage": 10,
            "pairs": ["BTC-USDT", "ETH-USDT"],
            "direction": "both",
            "order_type": "limit",
            "position_mode": "ONEWAY",
            "execution": {
                "price_type": "mid",
                "limit_order_timeout": 60,
            },
        }
        config = build_trading_config(data)
        assert config.connector == "bybit_perpetual"
        assert config.leverage == 10
        assert config.pairs == ("BTC-USDT", "ETH-USDT")
        assert config.enable_long is True
        assert config.enable_short is True
