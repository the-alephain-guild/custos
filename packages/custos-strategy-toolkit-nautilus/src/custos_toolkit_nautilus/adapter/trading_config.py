"""
NautilusTrader base configuration for strategies.

Provides a base config class that includes common sections shared by all strategies:
- trading: Order execution, connectors, fees
- position: Size, scaling, limits, dynamic sizing
- risk: Global risk, trade risk, stop loss, take profit
- filters: ADX, volatility, volume, time, MTF, regime, momentum
- platforms: Hummingbot and Nautilus platform settings

Strategies extend NautilusTradingStrategyConfig and add their own parameters section.
"""

from typing import Literal, TypedDict, cast

from custos_toolkit.config.loader import ConfigWrapper
from custos_toolkit.warmup.snapshot import WarmupConfig, warmup_config_from_dict
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import InstrumentId

from custos_toolkit_nautilus.adapter.config import (
    BacktestingConfig,
    FiltersConfig,
    PlatformsConfig,
    PositionConfig,
    RiskConfig,
    SignalConfig,
    SnapshotConfig,
    TradingConfig,
    build_backtesting_config,
    build_filters_config,
    build_platforms_config,
    build_position_config,
    build_risk_config,
    build_signal_config,
    build_snapshot_config,
    build_trading_config,
)
from custos_toolkit_nautilus.adapter.utils import get_venue_from_connector, is_futures_connector

# Valid level values in the (level, message) tuples returned by validation_warnings();
# they map one-to-one onto the strategy's _validate_startup_config log-dispatch branches.
WarningLevel = Literal["error", "warning", "info"]


class NautilusBaseConfigSections(TypedDict):
    """Exact common kwargs supplied to every registered strategy config."""

    oms_type: str
    external_order_claims: list[InstrumentId]
    use_uuid_client_order_ids: bool
    use_hyphens_in_client_order_ids: bool
    trading: TradingConfig
    position: PositionConfig
    risk: RiskConfig
    filters: FiltersConfig
    platforms: PlatformsConfig
    backtesting: BacktestingConfig
    snapshot: SnapshotConfig
    warmup: WarmupConfig | None
    signal: SignalConfig


class NautilusTradingStrategyConfig(StrategyConfig, frozen=True):
    """
    Base configuration for Nautilus strategies.

    Contains common configuration sections shared by all strategies.
    Strategy-specific configs extend this class and add their own
    parameters field.

    Example:
        class SuperTrendStrategyConfig(NautilusTradingStrategyConfig, frozen=True):
            parameters: SuperTrendParametersConfig

    Attributes:
        trading: Trading configuration (order types, execution settings)
        position: Position management (sizing, scaling, limits)
        risk: Risk management (stop loss, take profit, global limits)
        filters: Signal filters (volatility, ADX, volume, etc.)
        platforms: Platform-specific settings (Nautilus venue, bar type)
        backtesting: Backtesting-specific settings (Speculum integration)
        snapshot: Snapshot persistence settings (Redis-based indicator state)
        warmup: Indicator warmup configuration (snapshot restore, history requests)
        signal: Signal processing configuration (OKX Signal Bot compatibility)
    """

    trading: TradingConfig
    position: PositionConfig
    risk: RiskConfig
    filters: FiltersConfig
    platforms: PlatformsConfig
    backtesting: BacktestingConfig
    snapshot: SnapshotConfig = SnapshotConfig()
    warmup: WarmupConfig | None = None
    signal: SignalConfig = SignalConfig()

    def __post_init__(self) -> None:
        # Boundary fail-fast for non-Optional sections: msgspec's plain constructor
        # does not type-check, so explicitly reject None to make "non-Optional sections
        # are never None" a runtime guarantee rather than just a static contract (warmup
        # is the only Optional section). Internal code reads these fields directly and
        # adds no None defenses.
        for _name, _value in (
            ("trading", self.trading),
            ("position", self.position),
            ("risk", self.risk),
            ("filters", self.filters),
            ("platforms", self.platforms),
            ("backtesting", self.backtesting),
            ("snapshot", self.snapshot),
            ("signal", self.signal),
        ):
            if _value is None:
                raise ValueError(
                    f"[STARTUP] config.{_name} must not be None (non-Optional section); "
                    "construct the config via build_nautilus_base_config."
                )

        # Config is valid by construction (fail-fast): cross-section invariants are
        # enforced here so the coordinator never has to re-check them. Validations that
        # need the runtime account balance stay in the strategy
        # (_validate_initial_capital_vs_balance).
        if self.position.size_type == "fixed_risk" and self.risk.trade.stop_loss.method == "none":
            raise ValueError(
                "[STARTUP] size_type='fixed_risk' requires a stop-loss "
                "(risk.trade.stop_loss.method != 'none'), but none is configured. "
                "Fixed-risk sizing is derived from the stop distance, so without a stop "
                "the position cannot be computed; refusing to start to avoid a "
                "zero-size or risk-uncontrolled order."
            )

    def validation_warnings(self) -> list[tuple[WarningLevel, str]]:
        """Collect non-blocking config warnings as a list of (level, message) tuples
        (level in error/warning/info).

        Pure config checks with no side effects (a frozen struct has no logger); the
        strategy's on_start dispatches the logging by level. Validations that need the
        runtime account balance (initial_capital) are not done here.
        """
        warnings: list[tuple[WarningLevel, str]] = []
        warnings.extend(self._warn_sl_vs_liquidation())
        warnings.extend(self._warn_snapshot_persistence())
        return warnings

    def _warn_sl_vs_liquidation(self) -> list[tuple[WarningLevel, str]]:
        """Fixed stop-loss vs estimated liquidation distance (liq ~= 1/leverage), checked
        only when leverage > 1.

        - fixed SL >= 1/leverage   -> error (the stop may never trigger before liquidation)
        - fixed SL >= 0.8/leverage -> warning (less than a 20% safety buffer)
        - ATR SL + leverage >= 5   -> info (prompt to confirm ATR output vs liq distance)
        """
        leverage = self.trading.leverage
        if leverage <= 1:
            return []  # No leverage, no liquidation risk

        liq_distance = 1.0 / leverage  # Simplified estimate
        stop_loss = self.risk.trade.stop_loss

        if stop_loss.method == "fixed":
            sl_pct = stop_loss.fixed.value
            if sl_pct <= 0:
                return []
            if sl_pct >= liq_distance:
                return [
                    (
                        "error",
                        f"[STARTUP] fixed stop-loss ({sl_pct:.1%}) >= estimated liquidation "
                        f"distance ({liq_distance:.1%}) (leverage={leverage}x). The stop may "
                        f"never trigger before liquidation! Review the stop-loss config or "
                        f"reduce leverage now.",
                    )
                ]
            if sl_pct >= 0.8 * liq_distance:
                return [
                    (
                        "warning",
                        f"[STARTUP] fixed stop-loss ({sl_pct:.1%}) leaves under a 20% safety "
                        f"buffer to the estimated liquidation distance ({liq_distance:.1%}) "
                        f"(leverage={leverage}x). Consider a stop < {0.8 * liq_distance:.1%} to "
                        f"keep enough buffer.",
                    )
                ]
            return []

        if stop_loss.method == "atr" and leverage >= 5:
            return [
                (
                    "info",
                    f"[STARTUP] ATR stop-loss + {leverage}x leverage: estimated liquidation "
                    f"distance is about {liq_distance:.1%}. Confirm the actual ATR stop output "
                    f"is smaller than this distance, otherwise the stop may not trigger before "
                    f"liquidation.",
                )
            ]
        return []

    def _warn_snapshot_persistence(self) -> list[tuple[WarningLevel, str]]:
        """When snapshot.enabled but the cache database is off, the framework
        on_save/on_load does not persist state."""
        if not self.snapshot.enabled:
            return []
        if self.platforms.nautilus.trading_node.database.enabled:
            return []
        return [
            (
                "warning",
                "snapshot.enabled=true but platforms.nautilus.trading_node.database is "
                "disabled -- framework on_save/on_load state persistence requires a cache "
                "database (Redis); restarts will not restore indicator/strategy state.",
            )
        ]


def build_nautilus_base_config(config_wrapper: ConfigWrapper) -> NautilusBaseConfigSections:
    """
    Build common Nautilus config sections from ConfigWrapper.

    This function extracts and builds all common configuration sections
    that are shared across strategies. The returned dict can be unpacked
    with **kwargs when constructing a strategy config.

    Args:
        config_wrapper: ConfigWrapper with merged configuration

    Returns:
        Dictionary with keys: trading, position, risk, filters, platforms
        Each value is a built config struct ready for use.

    Example:
        wrapper = load_config("trend/supertrend/config.yaml")
        base_sections = build_nautilus_base_config(wrapper)

        config = SuperTrendStrategyConfig(
            parameters=build_parameters_config(wrapper),
            **base_sections,
        )
    """
    # Map position_mode to oms_type
    # ONEWAY -> NETTING (single position per instrument)
    # HEDGE -> HEDGING (separate long/short positions)
    position_mode = config_wrapper.trading.get("position_mode", "ONEWAY")
    oms_type = "HEDGING" if position_mode == "HEDGE" else "NETTING"

    # Build external_order_claims from trading config.
    # Multi-asset strategies such as rebalancing must claim all configured
    # instruments instead of only the first pair.
    trading_cfg = config_wrapper.trading
    trading_pairs_value = trading_cfg.get("pairs", ["BTC-USDT"])
    connector = cast(str, trading_cfg.get("connector", "binance_perpetual"))
    if not isinstance(trading_pairs_value, list) or not trading_pairs_value:
        trading_pairs = ["BTC-USDT"]
    else:
        trading_pairs = cast(list[str], trading_pairs_value)

    venue = get_venue_from_connector(connector)
    suffix = "-PERP" if is_futures_connector(connector) else ""
    external_order_claims: list[InstrumentId] = []
    for pair in trading_pairs:
        symbol = pair.replace("-", "")
        external_order_claims.append(InstrumentId.from_str(f"{symbol}{suffix}.{venue}"))

    # Get snapshot config from wrapper if available
    snapshot_dict = getattr(config_wrapper, "snapshot", None)

    # Build warmup config if available
    warmup_config = None
    if hasattr(config_wrapper, "warmup") and config_wrapper.warmup:
        warmup_config = warmup_config_from_dict(config_wrapper.warmup)

    # Get signal config from wrapper if available
    signal_dict = getattr(config_wrapper, "signal", None)

    raw = config_wrapper.raw
    return {
        "oms_type": oms_type,
        "external_order_claims": external_order_claims,
        # Client order ids are a bare UUID with the hyphens removed: 32 characters, fixed.
        #
        # The framework's default builds them from the trader tag, the strategy tag and a
        # per-strategy order counter. Binance refuses anything not shorter than 36, and a
        # deployment instance tag alone made that 44 — every order on the venue came back
        # -4015. Shortening the tag would have held until the counter grew a digit, so the
        # length is taken off the counter and the tags entirely instead.
        #
        # Both flags are needed: a UUID with hyphens is exactly 36, which is still refused.
        #
        # Nothing is lost by dropping the tags. Attribution comes from the trader id the
        # engine prefixes onto every log line and from the deployment instance this runner
        # records on the order path, not from reading an id on an exchange screen — which
        # only ever showed a truncated tag anyway.
        #
        # This is not read from the strategy's YAML on purpose. The limit belongs to the
        # venue, not to a strategy, so no strategy author should have to know it or be able
        # to get it wrong.
        "use_uuid_client_order_ids": True,
        "use_hyphens_in_client_order_ids": False,
        "trading": build_trading_config(
            config_wrapper.trading, cast(dict[str, object] | None, raw.get("trading"))
        ),
        "position": build_position_config(
            config_wrapper.position, cast(dict[str, object] | None, raw.get("position"))
        ),
        "risk": build_risk_config(
            config_wrapper.risk, cast(dict[str, object] | None, raw.get("risk"))
        ),
        "filters": build_filters_config(
            config_wrapper.filters, cast(dict[str, object] | None, raw.get("filters"))
        ),
        "platforms": build_platforms_config(
            config_wrapper.platforms, cast(dict[str, object] | None, raw.get("platforms"))
        ),
        "backtesting": build_backtesting_config(
            config_wrapper.backtesting,
            cast(dict[str, object] | None, raw.get("backtesting")),
        ),
        "snapshot": build_snapshot_config(
            snapshot_dict, cast(dict[str, object] | None, raw.get("snapshot"))
        ),
        "warmup": warmup_config,
        "signal": build_signal_config(
            signal_dict, cast(dict[str, object] | None, raw.get("signal"))
        ),
    }
