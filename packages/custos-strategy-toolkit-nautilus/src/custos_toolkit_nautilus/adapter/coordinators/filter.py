"""Filter component.

Holds the FilterManager wiring: global filter init/update/check, per-pair filter
init/check (with MTF bar subscription handling), and multi-timeframe (MTF) bar
short-circuiting. Injects a strategy reference and reaches
``_global_filter_manager`` (a strategy field), ``ctx.filter_manager``,
``config.filters`` / ``subscribe_bars`` / ``log`` / ``_base_size_factor`` through it.

Direction permission (``_is_direction_allowed``) stays on the Strategy class: it
gates entry by ``config.trading.enable_long/short`` and belongs to the signal
path, not the FilterManager pipeline.
"""

from typing import TYPE_CHECKING

from custos_toolkit_nautilus.adapter.filter_manager import FilterManager

if TYPE_CHECKING:
    from custos_toolkit.signals.types import SignalDirection
    from nautilus_trader.model import Bar, BarType

    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class FilterCoordinator:
    """FilterManager wiring (global + per-pair filter gate, MTF handling).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: "NautilusTradingStrategy") -> None:
        self._strategy = strategy

    def init_global(self) -> None:
        """Initialize global filter manager (scope=global filters only)."""
        s = self._strategy
        # Global filters don't need instrument_id
        s._global_filter_manager = FilterManager(
            config=s.config.filters,
            instrument_id=None,
            scope_filter="global",
        )
        s._global_filter_manager.initialize()

        if s._global_filter_manager.filter_count > 0:
            s.log.info(
                f"Global FilterManager initialized: {s._global_filter_manager.filter_count} filters"
            )

    def init_pair(self, ctx: "PairContext") -> None:
        """Initialize per-pair filter manager for a context."""
        s = self._strategy
        ctx.filter_manager = FilterManager(
            config=s.config.filters,
            instrument_id=ctx.instrument_id,
            scope_filter="per_pair",
        )
        subscriptions = ctx.filter_manager.initialize()

        # Handle MTF subscription requests
        for sub in subscriptions:
            if sub.type == "bars" and sub.bar_type:
                bar_type = self._parse_bar_type_string(sub.bar_type)
                if bar_type:
                    s.subscribe_bars(bar_type)
                    s.log.info(f"[{ctx.pair}] Subscribed to MTF bars: {sub.bar_type}")

        if ctx.filter_manager.filter_count > 0:
            s.log.info(
                f"[{ctx.pair}] Per-pair FilterManager: {ctx.filter_manager.filter_count} filters"
            )

    def _parse_bar_type_string(self, bar_type_str: "str | BarType") -> "BarType | None":
        """Parse bar type string to BarType object."""
        try:

            return (
                bar_type_str
                if isinstance(bar_type_str, BarType)
                else BarType.from_str(bar_type_str)
            )
        except Exception as exc:
            # A parse failure returns None for the caller to degrade gracefully, so a
            # single bad config string can't abort startup.
            self._strategy.log.warning(f"Failed to parse bar type: {bar_type_str}: {exc}")
            return None

    def handle_mtf_bar(self, ctx: "PairContext", bar: "Bar") -> bool:
        """Update MTF (multi-timeframe) filters and report whether this bar is an MTF
        bar that should short-circuit the main pipeline.

        Returns True when ``bar`` belongs to a higher-timeframe filter feed (global or
        per-pair): the filter is updated and the caller must return early without
        running the regular signal pipeline on this bar.
        """
        s = self._strategy
        if s._global_filter_manager and s._global_filter_manager.is_mtf_bar(bar):
            s._global_filter_manager.update(bar)
            s.log.info(f"MTF bar detected (global): {bar.bar_type}")
            return True
        if ctx.filter_manager and ctx.filter_manager.is_mtf_bar(bar):
            ctx.filter_manager.update(bar)
            s.log.info(f"[{ctx.pair}] MTF bar detected (per-pair): {bar.bar_type}")
            return True
        return False

    def update_global(self, bar: "Bar") -> None:
        """Update global filter state. Runs every bar; gating happens at entry."""
        s = self._strategy
        if s._global_filter_manager:
            s._global_filter_manager.update(bar)

    def update_pair(self, ctx: "PairContext", bar: "Bar") -> None:
        """Update per-pair filter state. Runs every bar so indicator state stays
        current even on bars where no entry is gated; gating happens at entry."""
        if ctx.filter_manager:
            ctx.filter_manager.update(bar)

    def check_global(self, bar: "Bar", direction: "SignalDirection | None" = None) -> bool:
        """Gate an entry on global filters (direction forwarded to direction-aware
        ones). Entry-only: exits and position management never reach this.

        Global filters have no per-pair context, so the size factor and any delay
        window are held on the strategy: ``_global_size_factor`` is folded into per-pair
        sizing by ``check_pair``; ``_global_filter_delay_until`` blocks all entries until
        it elapses (mirrors the per-pair window)."""
        s = self._strategy
        if not s._global_filter_manager:
            return True

        # Honor an open global delay window before re-running the filters.
        ts = getattr(bar, "ts_event", 0) or 0
        if s._global_filter_delay_until and ts < s._global_filter_delay_until:
            return False

        result = s._global_filter_manager.check(bar, direction)
        s._global_size_factor = float(result.size_factor)
        if result.passed:
            s._global_filter_delay_until = 0
        else:
            s._global_filter_delay_until = result.delay_until
            s.log.debug(f"Global filters failed: {result.failed_filters}")
        return result.passed

    def check_pair(
        self,
        ctx: "PairContext",
        bar: "Bar",
        direction: "SignalDirection | None" = None,
    ) -> bool:
        """Gate an entry on per-pair filters for a context. State is updated
        separately in ``update_pair`` (every bar); this only checks (entry-only)."""
        s = self._strategy
        if not ctx.filter_manager:
            return True

        # Honor an open delay window (on_filter_fail="delay") before re-running filters:
        # entries stay blocked until it elapses even if filters would now pass.
        ts = getattr(bar, "ts_event", 0) or 0
        if ctx.filter_delay_until and ts < ctx.filter_delay_until:
            return False

        result = ctx.filter_manager.check(bar, direction)

        if result.passed:
            # Apply size factor from per-pair filters (stored per-pair to avoid
            # cross-contamination). Multiply with the global base_size_factor and the
            # global filter size factor rather than overwrite -- otherwise a passing
            # filter (factor=1.0) would silently restore full sizing.
            ctx.size_reduction_factor = (
                s._base_size_factor * s._global_size_factor * float(result.size_factor)
            )
            ctx.filter_delay_until = 0
        else:
            # A delay action opens/extends the window; other failures just block.
            ctx.filter_delay_until = result.delay_until
            s.log.debug(f"[{ctx.pair}] Per-pair filters failed: {result.failed_filters}")

        return result.passed
