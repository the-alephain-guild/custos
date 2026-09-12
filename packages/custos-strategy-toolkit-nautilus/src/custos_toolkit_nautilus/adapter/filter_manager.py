"""
Filter manager data classes for NautilusTrader strategies.

This module provides data classes for managing filter results and subscription
requests in the strategy framework.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

import msgspec
from custos_toolkit.protocols.bar import BarProtocol
from custos_toolkit.protocols.filter import FilterResult as CheckResult
from custos_toolkit.signals.types import SignalDirection
from nautilus_trader.model import Bar, BarType, InstrumentId

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.filters import BehaviorConfig, FiltersConfig

logger = logging.getLogger(__name__)


@dataclass
class SubscriptionRequest:
    """
    Request for strategy to subscribe to data.

    Attributes:
        type: Subscription type ("bars", "ticks")
        bar_type: Bar type string for bar subscriptions
        instrument_id: Instrument ID if different from strategy
    """

    type: str
    bar_type: str | BarType | None = None
    instrument_id: InstrumentId | None = None


@dataclass
class FilterResult:
    """
    Result of filter check.

    Attributes:
        passed: True if all required filters passed
        failed_filters: Names of filters that failed
        passed_filters: Names of filters that passed
        size_factor: Combined size reduction factor (1.0 = no reduction)
        delay_until: Timestamp to delay trading until (0 = no delay)
    """

    passed: bool
    failed_filters: list[str]
    passed_filters: list[str]
    size_factor: Decimal = field(default_factory=lambda: Decimal("1.0"))
    delay_until: int = 0


class _ScopedConfig(Protocol):
    @property
    def scope(self) -> str: ...


class _Filter(Protocol):
    @property
    def name(self) -> str: ...
    def update(self, bar: BarProtocol) -> None: ...


class _StandardFilter(_Filter, Protocol):
    def check(self, bar: BarProtocol) -> CheckResult: ...


class _DirectionFilter(_Filter, Protocol):
    def check(self, bar: BarProtocol, direction: SignalDirection | None) -> CheckResult: ...


class FilterManager:
    """
    Manages filter lifecycle, updates, and checks.

    Coordinates multiple trading filters, handling their initialization,
    updates with bar data, and collective check operations.
    """

    def __init__(
        self,
        config: "FiltersConfig | None",
        instrument_id: InstrumentId | None,
        scope_filter: str = "all",
    ) -> None:
        """
        Initialize FilterManager.

        Args:
            config: FiltersConfig or None if no filters configured
            instrument_id: NautilusTrader InstrumentId for bar type construction
            scope_filter: Filter by scope - "global", "per_pair", or "all"
        """
        self._config: FiltersConfig | None = config
        self._instrument_id = instrument_id
        self._scope_filter = scope_filter
        self._filters: list[_Filter] = []
        self._mtf_filter: _Filter | None = None
        self._mtf_bar_type: str | None = None
        self._initialized: bool = False

    @property
    def is_initialized(self) -> bool:
        """Check if filters have been initialized."""
        return self._initialized

    @property
    def filter_count(self) -> int:
        """Return number of configured filters."""
        return len(self._filters)

    def _should_create_filter(self, filter_config: _ScopedConfig) -> bool:
        """
        Check if filter should be created based on scope_filter.

        Args:
            filter_config: Filter configuration object with scope attribute

        Returns:
            True if filter should be created
        """
        if self._scope_filter == "all":
            return True

        return filter_config.scope == self._scope_filter

    def initialize(self) -> list[SubscriptionRequest]:
        """
        Create filters from config and return subscription requests.

        Returns:
            List of SubscriptionRequest for any additional data needed (e.g., MTF bars)
        """
        subscriptions: list[SubscriptionRequest] = []

        # Mark as initialized regardless of config
        self._initialized = True

        # Return empty if no config configured
        if self._config is None:
            return subscriptions

        # Import filters inside method to avoid circular imports.
        # Indicator filters (volatility/adx/momentum/volume/regime) use the
        # nautilus-backed implementations (nautilus indicators); time/cooldown/mtf are
        # pure business (no nautilus indicator) and stay in custos_toolkit.filters.
        from custos_toolkit.filters import CooldownFilter, MTFFilter, TimeFilter

        from custos_toolkit_nautilus.adapter.filters import (
            NautilusAdxFilter,
            NautilusMomentumFilter,
            NautilusRegimeFilter,
            NautilusVolatilityFilter,
            NautilusVolumeFilter,
        )

        # Create time filter. TimeFilter consumes the same fields TimeFilterConfig
        # carries (trading_hours/excluded_days/excluded_dates), so the typed config is
        # projected to a dict at this platform-neutral boundary via asdict, like
        # cooldown/mtf below.
        time_config = self._config.time_filter
        if time_config.enabled and self._should_create_filter(time_config):
            self._filters.append(TimeFilter(msgspec.structs.asdict(time_config)))

        # Create volatility filter
        volatility_config = self._config.volatility_filter
        if volatility_config.enabled and self._should_create_filter(volatility_config):
            self._filters.append(NautilusVolatilityFilter(volatility_config))

        # Create momentum filter
        momentum_config = self._config.momentum_filter
        if momentum_config.enabled and self._should_create_filter(momentum_config):
            self._filters.append(NautilusMomentumFilter(momentum_config))

        # Create ADX filter
        adx_config = self._config.adx_filter
        if adx_config.enabled and self._should_create_filter(adx_config):
            self._filters.append(NautilusAdxFilter(adx_config))

        # Create volume filter
        volume_config = self._config.volume_filter
        if volume_config.enabled and self._should_create_filter(volume_config):
            self._filters.append(NautilusVolumeFilter(volume_config))

        # Create regime filter
        regime_config = self._config.regime_filter
        if regime_config.enabled and self._should_create_filter(regime_config):
            self._filters.append(NautilusRegimeFilter(regime_config))

        # Create cooldown filter. Cooldown has no enabled flag — it is added
        # whenever any cooldown window is configured. Time/cooldown/mtf are
        # platform-neutral (custos_toolkit.filters, no msgspec) so the typed sub-config is
        # projected to a dict at this boundary via msgspec.structs.asdict.
        cooldown_config = self._config.cooldown
        if self._should_create_filter(cooldown_config):
            has_any_cooldown = (
                cooldown_config.after_exit > 0
                or cooldown_config.after_stop_loss > 0
                or cooldown_config.after_take_profit > 0
            )
            if has_any_cooldown:
                self._filters.append(CooldownFilter(msgspec.structs.asdict(cooldown_config)))

        # Create MTF filter (requires subscription)
        mtf_config = self._config.mtf_filter
        if mtf_config.enabled and self._should_create_filter(mtf_config):
            mtf_filter = MTFFilter(msgspec.structs.asdict(mtf_config))
            self._filters.append(mtf_filter)
            self._mtf_filter = mtf_filter

            # Build MTF bar type string for subscription
            # Format: SYMBOL.VENUE-TIMEFRAME-LAST-EXTERNAL
            symbol = getattr(getattr(self._instrument_id, "symbol", None), "value", "UNKNOWN")
            venue = getattr(getattr(self._instrument_id, "venue", None), "value", "UNKNOWN")

            # Normalize timeframe format (e.g., "4h" -> "4-HOUR", "1h" -> "1-HOUR")
            htf_normalized = self._normalize_timeframe(mtf_config.higher_timeframe)
            self._mtf_bar_type = f"{symbol}.{venue}-{htf_normalized}-LAST-EXTERNAL"

            # Return subscription request for HTF bars
            subscriptions.append(
                SubscriptionRequest(
                    type="bars",
                    bar_type=self._mtf_bar_type,
                )
            )

        return subscriptions

    def _normalize_timeframe(self, tf: str) -> str:
        """
        Normalize timeframe string to NautilusTrader format.

        Converts formats like "4h", "1d", "15m" to "4-HOUR", "1-DAY", "15-MINUTE".

        Args:
            tf: Timeframe string in various formats

        Returns:
            Normalized timeframe string
        """
        tf = tf.strip().upper()

        # Already in correct format
        if "-" in tf:
            return tf

        # Map suffixes to full names
        suffix_map = {
            "H": "HOUR",
            "D": "DAY",
            "M": "MINUTE",
            "W": "WEEK",
        }

        # Extract number and suffix
        import re

        match = re.match(r"(\d+)([HDMW])", tf)
        if match:
            num = match.group(1)
            suffix = match.group(2)
            return f"{num}-{suffix_map.get(suffix, suffix)}"

        return tf

    def update(self, bar: Bar) -> None:
        """
        Update all filters with new bar data.

        Args:
            bar: NautilusTrader Bar object
        """
        for f in self._filters:
            try:
                update_method = getattr(f, "update", None)
                if update_method is not None and callable(update_method):
                    update_method(cast(BarProtocol, bar))
            except Exception:
                filter_name = getattr(f, "name", type(f).__name__)
                logger.warning(f"Filter '{filter_name}' update failed", exc_info=True)

    def is_mtf_bar(self, bar: Bar) -> bool:
        """
        Check if bar is from higher timeframe.

        Args:
            bar: NautilusTrader Bar object

        Returns:
            True if bar matches the configured MTF bar type
        """
        if self._mtf_bar_type is None:
            return False

        bar_type = getattr(bar, "bar_type", None)
        if bar_type is None:
            return False

        return str(bar_type) == self._mtf_bar_type

    def check(self, bar: Bar, direction: SignalDirection | None = None) -> FilterResult:
        """
        Check all filters and return aggregated result.

        Args:
            bar: NautilusTrader Bar object
            direction: Candidate entry SignalDirection, forwarded only to
                direction-aware filters (those declaring ``direction_aware``);
                direction-agnostic filters keep the single-arg ``check(bar)``.

        Returns:
            FilterResult with pass/fail status and filter details
        """
        # If not initialized, pass by default
        if not self._initialized:
            return FilterResult(
                passed=True,
                failed_filters=[],
                passed_filters=[],
            )

        # If no filters, pass by default
        if not self._filters:
            return FilterResult(
                passed=True,
                failed_filters=[],
                passed_filters=[],
            )

        passed_filters: list[str] = []
        failed_filters: list[str] = []
        combined_size_factor = Decimal("1.0")

        for f in self._filters:
            filter_name = getattr(f, "name", "unknown")
            try:
                # Only direction-aware filters (momentum) receive the entry direction;
                # the rest keep their direction-agnostic single-arg contract.
                if getattr(f, "direction_aware", False):
                    result = cast(_DirectionFilter, f).check(cast(BarProtocol, bar), direction)
                else:
                    result = cast(_StandardFilter, f).check(cast(BarProtocol, bar))
                if getattr(result, "passed", False):
                    passed_filters.append(filter_name)
                    # Combine size factors (safely handle non-numeric values)
                    try:
                        size_factor = getattr(result, "size_factor", 1.0)
                        if isinstance(size_factor, (int, float, Decimal)) and size_factor < 1.0:
                            combined_size_factor = min(
                                combined_size_factor, Decimal(str(size_factor))
                            )
                    except (TypeError, ValueError):
                        logger.warning(f"Filter '{filter_name}' returned invalid size_factor")
                else:
                    failed_filters.append(filter_name)
            except Exception:
                failed_filters.append(filter_name)
                logger.warning(f"Filter '{filter_name}' check failed", exc_info=True)

        return self._aggregate(bar, passed_filters, failed_filters, combined_size_factor)

    def _aggregate(
        self,
        bar: Bar,
        passed_filters: list[str],
        failed_filters: list[str],
        combined_size_factor: "Decimal",
    ) -> FilterResult:
        """Combine per-filter results per ``behavior.mode`` and, on a combined failure,
        apply ``behavior.on_filter_fail`` (skip / reduce_size / delay)."""
        behavior = self._config.behavior if self._config is not None else None

        if self._mode_passed(passed_filters, failed_filters, behavior):
            return FilterResult(
                passed=True,
                failed_filters=failed_filters,
                passed_filters=passed_filters,
                size_factor=combined_size_factor,
            )

        on_fail = behavior.on_filter_fail if behavior is not None else "skip"
        if on_fail == "reduce_size":
            # Allow the entry but at a reduced size rather than skipping it.
            assert behavior is not None
            factor = combined_size_factor * Decimal(str(behavior.reduce_size_factor))
            return FilterResult(
                passed=True,
                failed_filters=failed_filters,
                passed_filters=passed_filters,
                size_factor=factor,
            )
        if on_fail == "delay":
            # Block and open a delay window; the per-pair gate keeps blocking entries
            # until it elapses. Window length reuses the after-exit cooldown (default 300s).
            cooldown = self._config.cooldown if self._config is not None else None
            delay_seconds = (cooldown.after_exit if cooldown is not None else 0) or 300
            ts = getattr(bar, "ts_event", 0) or 0
            return FilterResult(
                passed=False,
                failed_filters=failed_filters,
                passed_filters=passed_filters,
                size_factor=combined_size_factor,
                delay_until=ts + delay_seconds * 1_000_000_000,
            )
        # "skip" (default): block this bar, no persistent window.
        return FilterResult(
            passed=False,
            failed_filters=failed_filters,
            passed_filters=passed_filters,
            size_factor=combined_size_factor,
        )

    def _mode_passed(
        self,
        passed_filters: list[str],
        failed_filters: list[str],
        behavior: "BehaviorConfig | None",
    ) -> bool:
        """Whether the combined filter set passes under ``behavior.mode``."""
        if not failed_filters:
            return True
        mode = behavior.mode if behavior is not None else "all"
        if mode == "any":
            return len(passed_filters) >= 1
        if mode == "weighted" and behavior is not None:
            # Weight keys are "<name>_filter" (adx_filter, ...); filter names are "adx".
            # Filters absent from the weights config contribute 0 (don't affect score).
            configured = behavior.weights
            weights = {
                "adx_filter": configured.adx_filter,
                "volatility_filter": configured.volatility_filter,
                "volume_filter": configured.volume_filter,
                "regime_filter": configured.regime_filter,
                "momentum_filter": configured.momentum_filter,
                "mtf_filter": configured.mtf_filter,
            }
            total = sum(weights.get(f"{n}_filter", 0.0) for n in (passed_filters + failed_filters))
            if total <= 0:
                return False
            score = sum(weights.get(f"{n}_filter", 0.0) for n in passed_filters)
            return (score / total) >= behavior.min_score
        # "all" (default): any failure blocks.
        return False
