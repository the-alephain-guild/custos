"""
NautilusTrader-specific shared modules.

Provides msgspec.Struct-based configuration models for NautilusTrader strategies.

These imports are deliberately not wrapped in a try/except: a swallowed
ImportError makes "the package imports" and "its contents exist" two different
things, and `__all__` keeps advertising names that are not there.
"""

from .capital_allocator import CapitalAllocator
from .config import (
    # Filters
    AdxFilterConfig,
    # Allocation
    AllocationConfig,
    CooldownConfig,
    # Trading
    ExecutionConfig,
    FiltersConfig,
    # Position
    FixedRiskConfig,
    FixedScalingConfig,
    # Risk
    GlobalRiskConfig,
    # Platforms
    HummingbotPlatformConfig,
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
    build_allocation_config,
    build_filters_config,
    build_platforms_config,
    build_position_config,
    build_risk_config,
    build_trading_config,
)
from .coordinators import (
    ConfigSummaryLogger,
    EquityProvider,
    OrderReconciler,
    StartupValidator,
)
from .filter_manager import FilterManager, FilterResult, SubscriptionRequest
from .orders import OrderTracker
from .pair_context import PairContext
from .registry import (
    create_strategy,
    get_strategy_info,
    is_registered,
    list_strategies,
    register_strategy,
    unregister_strategy,
)
from .signal_processor import SignalProcessorConfig, SignalProcessorStrategy
from .sizing import compute_fixed_risk_qty
from .strategy_core import NautilusStrategyCore
from .tick_monitor import ExitAction, TickMonitorManager, TrailingStopManager
from .trading_config import (
    NautilusTradingStrategyConfig,
    build_nautilus_base_config,
)
from .trading_strategy import NautilusTradingStrategy
from .utils import (
    VENUE_MAP,
    deep_asdict,
    derive_bar_type,
    derive_instrument_id,
    get_bar_duration_ns,
    get_venue_from_connector,
    is_futures_connector,
)
from .warmup_manager import WarmupManager

# Backward compatibility aliases (deprecated)
NautilusStrategyBase = NautilusStrategyCore
NautilusBaseStrategy = NautilusTradingStrategy
NautilusBaseStrategyConfig = NautilusTradingStrategyConfig

__all__ = [
    # New names
    "NautilusTradingStrategyConfig",
    "build_nautilus_base_config",
    "NautilusStrategyCore",
    "NautilusTradingStrategy",
    # Backward compatibility aliases (deprecated)
    "NautilusBaseStrategyConfig",
    "NautilusStrategyBase",
    "NautilusBaseStrategy",
    # Signal processor
    "SignalProcessorConfig",
    "SignalProcessorStrategy",
    # Filter manager
    "FilterManager",
    "FilterResult",
    "SubscriptionRequest",
    # Order tracking
    "OrderTracker",
    # Multi-pair support
    "PairContext",
    "CapitalAllocator",
    # Sizing
    "compute_fixed_risk_qty",
    # Coordinator components
    "EquityProvider",
    "ConfigSummaryLogger",
    "StartupValidator",
    "OrderReconciler",
    # Tick monitoring
    "ExitAction",
    "TrailingStopManager",
    "TickMonitorManager",
    # Warmup
    "WarmupManager",
    # Registry
    "create_strategy",
    "register_strategy",
    "unregister_strategy",
    "list_strategies",
    "get_strategy_info",
    "is_registered",
    # Utils
    "deep_asdict",
    "derive_instrument_id",
    "derive_bar_type",
    "get_bar_duration_ns",
    "get_venue_from_connector",
    "is_futures_connector",
    "VENUE_MAP",
    # Trading
    "ExecutionConfig",
    "TradingConfig",
    "build_trading_config",
    # Position
    "KellyConfig",
    "FixedRiskConfig",
    "PyramidScalingConfig",
    "FixedScalingConfig",
    "MartingaleScalingConfig",
    "ScalingConfig",
    "PositionLimitsConfig",
    "PositionConfig",
    "build_position_config",
    # Risk
    "GlobalRiskConfig",
    "TakeProfitAtrConfig",
    "TakeProfitFixedConfig",
    "TakeProfitTrailingConfig",
    "TakeProfitConfig",
    "StopLossAtrConfig",
    "StopLossFixedConfig",
    "StopLossTrailingConfig",
    "StopLossIndicatorConfig",
    "StopLossConfig",
    "TradeRiskConfig",
    "RiskConfig",
    "build_risk_config",
    # Filters
    "AdxFilterConfig",
    "VolatilityFilterConfig",
    "VolumeFilterConfig",
    "TimeFilterConfig",
    "CooldownConfig",
    "FiltersConfig",
    "build_filters_config",
    # Platforms
    "HummingbotPlatformConfig",
    "NautilusPlatformConfig",
    "PlatformsConfig",
    "build_platforms_config",
    # Allocation
    "AllocationConfig",
    "build_allocation_config",
]
