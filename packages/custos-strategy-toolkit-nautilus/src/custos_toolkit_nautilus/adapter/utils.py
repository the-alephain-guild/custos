"""
NautilusTrader utility functions.

Provides helper functions for deriving runtime values from configuration:
- InstrumentId from trading config (pairs + connector)
- BarType from platforms config
"""

from typing import TYPE_CHECKING, Protocol, cast, overload

import msgspec
from nautilus_trader.model import BarAggregation, BarType, InstrumentId

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.platforms import PlatformsConfig
    from custos_toolkit_nautilus.adapter.config.trading import TradingConfig

# Mapping from connector name to Nautilus venue
VENUE_MAP = {
    "binance": "BINANCE",
    "binance_perpetual": "BINANCE",
    "okx": "OKX",
    "okx_perpetual": "OKX",
    "bybit": "BYBIT",
    "bybit_perpetual": "BYBIT",
    "kucoin": "KUCOIN",
    "kucoin_perpetual": "KUCOIN",
    "gate": "GATE",
    "gate_perpetual": "GATE",
    # Spot and perpetuals are separate venues here rather than one venue with a
    # product type: they differ in signing domain, key set, balances and reference
    # price, and the adapter models them apart for that reason.
    "sodex": "SODEX_SPOT",
    "sodex_perpetual": "SODEX_PERPS",
}

# Connectors whose configured pairs are already the venue's own symbols.
#
# The usual rule -- drop the dash, append -PERP for a perpetual -- is a convention of
# the dash-joined CEX names above. It does not describe SoDEX, whose spot engine lists
# the venue's v-prefixed tokens joined by an underscore (``vBTC_vUSDC``) and whose
# perpetuals engine lists a dash-joined pair quoted in USD (``BTC-USD``). There is no
# canonical BASE-QUOTE to translate from: the two engines quote different assets, so a
# translation rule would have to invent one.
#
# Getting this wrong does not raise. It builds an instrument id the venue has never
# listed, and the instrument set simply loads empty.
PAIRS_ARE_VENUE_SYMBOLS = frozenset({"sodex", "sodex_perpetual"})


def derive_instrument_id(trading_config: "TradingConfig") -> InstrumentId:
    """
    Derive InstrumentId from trading configuration.

    Converts trading pair and connector to Nautilus InstrumentId format.
    Example: pairs=["BTC-USDT"], connector="binance_perpetual"
             -> InstrumentId("BTCUSDT-PERP.BINANCE")

    Args:
        trading_config: TradingConfig with 'pairs' and 'connector'

    Returns:
        InstrumentId for use with Nautilus
    """
    pairs = trading_config.pairs
    connector = trading_config.connector

    pair = pairs[0] if pairs else "BTC-USDT"
    return InstrumentId.from_str(instrument_id_str(pair, trading_config.connector))


def instrument_id_str(pair: str, connector: str) -> str:
    """Nautilus instrument id for one pair on one connector.

    The single derivation. Three callers used to carry a copy of it, and a copy is
    where a venue whose symbols do not follow the Binance convention gets missed.
    """
    venue = get_venue_from_connector(connector)
    if connector in PAIRS_ARE_VENUE_SYMBOLS:
        return f"{pair}.{venue}"
    symbol = pair.replace("-", "")
    if is_futures_connector(connector):
        symbol += "-PERP"
    return f"{symbol}.{venue}"


# Mapping from common timeframe strings to Nautilus bar format
TIMEFRAME_MAP = {
    "1m": "1-MINUTE",
    "5m": "5-MINUTE",
    "15m": "15-MINUTE",
    "30m": "30-MINUTE",
    "1h": "1-HOUR",
    "4h": "4-HOUR",
    "1d": "1-DAY",
    "1w": "1-WEEK",
}


def derive_bar_type(
    platforms_config: "PlatformsConfig",
    instrument_id: InstrumentId,
    timeframe_override: str | None = None,
) -> BarType:
    """
    Derive BarType from platforms configuration.

    Combines instrument_id with bar interval and aggregation from Nautilus platform config.
    Example: bar_type="1-HOUR", bar_aggregation="EXTERNAL", instrument_id="BTCUSDT-PERP.BINANCE"
             -> BarType("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

    Bar aggregation modes:
        - EXTERNAL: Exchange provides pre-aggregated bars (production, paper trading)
        - INTERNAL: NautilusTrader aggregates bars from ticks (sandbox/testnet)

    Args:
        platforms_config: PlatformsConfig with a 'nautilus' section
        instrument_id: The instrument to create bar type for
        timeframe_override: Optional timeframe override (e.g., "4h" for MTF filter)

    Returns:
        BarType for use with Nautilus
    """
    nautilus_config = platforms_config.nautilus
    bar_interval = nautilus_config.bar_type
    bar_aggregation = nautilus_config.bar_aggregation

    # Apply timeframe override if provided (for MTF filter)
    if timeframe_override:
        bar_interval = TIMEFRAME_MAP.get(timeframe_override.lower(), timeframe_override.upper())

    return BarType.from_str(f"{instrument_id}-{bar_interval}-LAST-{bar_aggregation}")


def get_venue_from_connector(connector: str) -> str:
    """
    Get Nautilus venue name from connector string.

    Args:
        connector: Connector name (e.g., "binance_perpetual")

    Returns:
        Venue name (e.g., "BINANCE")
    """
    return VENUE_MAP.get(connector, "BINANCE")


def is_futures_connector(connector: str) -> bool:
    """
    Check if connector is for futures/perpetual trading.

    Args:
        connector: Connector name (e.g., "binance_perpetual")

    Returns:
        True if futures/perpetual connector
    """
    return "perpetual" in connector.lower()


class _StructLike(Protocol):
    __struct_fields__: tuple[str, ...]


@overload
def deep_asdict(obj: msgspec.Struct) -> dict[str, object]: ...


@overload
def deep_asdict(obj: dict[object, object]) -> dict[object, object]: ...


@overload
def deep_asdict(obj: list[object]) -> list[object]: ...


@overload
def deep_asdict(obj: tuple[object, ...]) -> tuple[object, ...]: ...


@overload
def deep_asdict(obj: object) -> object: ...


def deep_asdict(obj: object) -> object:
    """
    Recursively convert msgspec struct to dict.

    Unlike msgspec.structs.asdict() which only converts the top level,
    this function recursively converts all nested struct objects to dicts.
    This is necessary for components that expect nested dicts (e.g., OrderPriceCalculator).

    Args:
        obj: Object to convert (msgspec struct, dict, list, tuple, or primitive)

    Returns:
        Recursively converted dictionary/list/tuple or original primitive

    Example:
        >>> class InnerConfig(msgspec.Struct):
        ...     value: float = 0.02
        >>> class OuterConfig(msgspec.Struct):
        ...     inner: InnerConfig = InnerConfig()
        >>> config = OuterConfig()
        >>> deep_asdict(config)
        {'inner': {'value': 0.02}}
    """
    if hasattr(obj, "__struct_fields__"):
        fields = cast(_StructLike, obj).__struct_fields__
        return {field: deep_asdict(getattr(obj, field)) for field in fields}
    elif isinstance(obj, dict):
        return {k: deep_asdict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [deep_asdict(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(deep_asdict(item) for item in obj)
    else:
        return obj


def get_bar_duration_ns(bar_type: BarType) -> int:
    """
    Get duration of a single bar in nanoseconds.

    Calculates the time duration of one bar based on its specification.
    Supports MINUTE, HOUR, and DAY aggregations.

    Args:
        bar_type: NautilusTrader BarType with spec containing step and aggregation

    Returns:
        Bar duration in nanoseconds, 0 if unknown aggregation

    Example:
        >>> bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")
        >>> get_bar_duration_ns(bar_type)
        3600000000000  # 1 hour in nanoseconds
    """

    bar_spec = bar_type.spec
    step = bar_spec.step
    aggregation = bar_spec.aggregation

    if aggregation == BarAggregation.MINUTE:
        return step * 60 * 1_000_000_000
    elif aggregation == BarAggregation.HOUR:
        return step * 3600 * 1_000_000_000
    elif aggregation == BarAggregation.DAY:
        return step * 86400 * 1_000_000_000
    else:
        return 0
