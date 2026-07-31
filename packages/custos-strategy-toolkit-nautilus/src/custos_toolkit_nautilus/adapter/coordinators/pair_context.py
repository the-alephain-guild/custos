"""Pair-context component.

Holds the per-pair context construction and on_start bootstrap: building a
PairContext, wiring its execution submitters / ATR indicator / tick monitor and
subscribing bars for every configured pair, plus the tick-stream subscription.
Injects a strategy reference and reaches the strategy
fields/helpers (``_contexts``, ``_mode``, ``_order_calculator``, ``_base_size_factor``,
``config`` / ``order_factory`` / ``cache`` / ``log`` / ``register_indicator_for_bars``
/ ``subscribe_bars`` / ``subscribe_*_ticks``, and the derive helpers
``_derive_instrument_id_for_pair`` / ``_derive_bar_type_for_instrument``) through it.

Stays on the Strategy class: the context lookup/derive accessors
(``_get_context`` / ``_get_context_from_instrument`` / ``_get_pair_from_instrument``
/ ``_derive_*``) are a shared cross-coordinator + subclass API over the ``_contexts``
state, and ``_init_capital_allocator`` is the capital domain — neither belongs here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custos_toolkit.position import PositionTracker
from nautilus_trader.indicators import AverageTrueRange

from custos_toolkit_nautilus.adapter.execution import ExecutionManager
from custos_toolkit_nautilus.adapter.orders import (
    NativeTrailingStopSubmitter,
    OrderTracker,
    StopLossSubmitter,
    TakeProfitSubmitter,
)
from custos_toolkit_nautilus.adapter.pair_context import PairContext
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class PairContextCoordinator:
    """Per-pair context construction and on_start bootstrap.

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def create_context(self, pair: str) -> PairContext:
        """
        Create context for a specific trading pair.

        Args:
            pair: Trading pair (e.g., "BTC-USDT")

        Returns:
            Initialized PairContext
        """
        s = self._strategy
        instrument_id = s._derive_instrument_id_for_pair(pair)
        bar_type = s._derive_bar_type_for_instrument(instrument_id)

        return PairContext(
            pair=pair,
            instrument_id=instrument_id,
            bar_type=bar_type,
            position_tracker=PositionTracker(),
            order_tracker=OrderTracker(),
            size_reduction_factor=s._base_size_factor,
        )

    def setup_pairs(self) -> None:
        """Create context and initialize execution components for each pair."""
        s = self._strategy
        for pair in s.config.trading.pairs:
            ctx = self.create_context(pair)
            s._contexts[ctx.instrument_id] = ctx

            # Initialize per-pair filters
            s._filter_coordinator.init_pair(ctx)

            # Initialize execution components
            ctx.execution_manager = ExecutionManager(s.order_factory, s.cache, s.log)
            ctx.sl_submitter = StopLossSubmitter(
                s.order_factory, s.cache, s.log, s._order_calculator
            )
            ctx.tp_submitter = TakeProfitSubmitter(
                s.order_factory, s.cache, s.log, s._order_calculator
            )
            # Exchange-managed trailing stop submitter (native_trailing mode only).
            # Zero touch to exchange/tick/hybrid paths (minimal risk surface).
            if s._mode.uses_native_trailing:
                ctx.native_trailing_submitter = NativeTrailingStopSubmitter(
                    s.order_factory, s.cache, s.log
                )

            # Register ATR indicator (if needed)
            if self._needs_atr_indicator():
                # ATR period for per-pair ATR indicators (stored in ctx.indicators["atr"])
                _atr_period = s.config.risk.trade.atr_period
                atr = AverageTrueRange(_atr_period)
                ctx.indicators["atr"] = atr
                s.register_indicator_for_bars(ctx.bar_type, atr)

            # Initialize tick monitor (tick/hybrid mode only; native_trailing/exchange skip)
            self._init_tick_monitor(ctx)

            # Subscribe to bar data
            s.subscribe_bars(ctx.bar_type)

            s.log.info(f"Initialized context for {pair}: instrument={ctx.instrument_id}")

    def subscribe_mark_prices(self) -> None:
        """Subscribe the mark price for every pair, unconditionally.

        The portfolio snapshot prices each open position from the mark price, and the
        notional cap, the snapshot publisher and the fallback breaker all read that
        snapshot. Nothing else subscribes it, and its fallback (MID) needs quote ticks
        that this deployment does not take -- so without this the snapshot is unreliable
        whenever a position is open, and the breaker can only fail closed.

        Unconditional on purpose: tick subscriptions are gated on the exit mode and the
        tick-monitoring config, and neither has anything to do with whether the guards
        need a price. Hanging this off that config is what left the breaker depending on
        data nobody had asked for.

        Mark price rather than last trade because that is what a perpetual's unrealised
        PnL and liquidation are marked against.
        """
        s = self._strategy
        for ctx in s._contexts.values():
            s.subscribe_mark_prices(ctx.instrument_id)
        s.log.info(f"Mark price subscribed for {len(s._contexts)} pairs (position pricing)")

    def subscribe_ticks(self) -> None:
        """Subscribe to tick data for all pairs if enabled."""
        s = self._strategy
        # native_trailing exits are venue-managed (tick handlers early-return), so
        # subscribing the tick stream would only waste bandwidth -- skip it.
        if not s._mode.subscribes_tick_stream:
            return
        tick_config = s._get_tick_monitoring_config()
        if not tick_config or not tick_config.enabled:
            return

        for _, ctx in s._contexts.items():
            if tick_config.tick_type in ("trade", "both"):
                s.subscribe_trade_ticks(ctx.instrument_id)
            if tick_config.tick_type in ("quote", "both"):
                s.subscribe_quote_ticks(ctx.instrument_id)

        s.log.info(f"Tick monitoring enabled for {len(s._contexts)} pairs: {tick_config.tick_type}")

    def _init_tick_monitor(self, ctx: PairContext) -> None:
        """Build the tick monitor for a pair context (tick/hybrid mode only).

        exchange and native_trailing modes do NOT use a tick monitor — the
        exchange SL / venue-managed trailing stop is the protective path.
        """
        s = self._strategy
        if s._mode.uses_tick_monitor:
            ctx.tick_monitor = TickMonitorManager.from_config(
                config=s.config.risk.trade,
                mode=s._mode.value,
            )

    def _needs_atr_indicator(self) -> bool:
        """Check if ATR indicator is needed for SL/TP calculations."""
        trade_risk = self._strategy.config.risk.trade
        if trade_risk.stop_loss.method == "atr":
            return True
        if trade_risk.take_profit.method == "atr":
            return True
        return False
