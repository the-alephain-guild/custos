"""
NautilusTrader base strategy with common trading, risk, and position logic.

Provides an abstract base strategy class (ABC) that implements common functionality
shared by all strategies:
- Signal filtering (volatility, ADX, volume, time, cooldown)
- Risk controls (daily loss limits, drawdown, consecutive losses)
- Position management (sizing, scaling, limits)
- Stop loss management (fixed, ATR, indicator, trailing)
- Take profit management (fixed, ATR, risk_reward, scaled exits)

Strategies extend NautilusTradingStrategy and MUST implement abstract methods:
- on_strategy_start(): Initialize strategy-specific indicators
- on_strategy_stop(): Cleanup resources
- on_reset(): Reset strategy state
- calculate_signal(ctx, bar): Return Signal object for trading decision
- get_indicator_history(): Return indicator data for visualization

Optional hooks (have default implementations):
- on_pre_bar(ctx, bar): Pre-bar custom filtering before standard filters
- on_post_bar(ctx, bar): Post-bar processing after position management
- calculate_position_size(ctx, signal): Custom position sizing logic

Per-bar subclass hooks receive ctx: PairContext (carrying .pair / .instrument_id).
Build Signal with ctx.pair (Signal is a platform-neutral DTO and, per mandatory-rules
rule 5, may only carry the pair string).
"""

import inspect
import os
from abc import abstractmethod
from decimal import Decimal
from pathlib import Path as _Path
from typing import Protocol, cast

import msgspec
from custos_toolkit.position import PositionSizer
from custos_toolkit.risk import OrderPriceCalculator, RiskController, RiskManager
from custos_toolkit.signals.types import Signal, SignalDirection
from custos_toolkit.warmup import WarmupConfig
from custos_toolkit.warmup.exceptions import CheckpointValidationError
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderCancelRejected,
    OrderFilled,
    OrderRejected,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import InstrumentId

from custos_toolkit_nautilus.adapter.cancel_audit import (
    record_cancel_confirmed,
    record_cancel_refused,
)
from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
from custos_toolkit_nautilus.adapter.config import TickMonitoringConfig
from custos_toolkit_nautilus.adapter.coordinators import (
    ConfigSummaryLogger,
    EquityProvider,
    ExecutionCoordinator,
    FilterCoordinator,
    OrderReconciler,
    PairContextCoordinator,
    RiskControlCoordinator,
    SignalExecutionCoordinator,
    SizingCoordinator,
    SLTPCoordinator,
    SnapshotCoordinator,
    StartupValidator,
    TradeEventHandler,
    WarmupCoordinator,
)
from custos_toolkit_nautilus.adapter.event_publisher import (
    EventPublisher,
    generate_signal_id,
    resolve_event_strategy_id,
    strategy_id_env_missing,
)
from custos_toolkit_nautilus.adapter.filter_manager import FilterManager
from custos_toolkit_nautilus.adapter.orders import OrderTracker
from custos_toolkit_nautilus.adapter.pair_context import PairContext
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore
from custos_toolkit_nautilus.adapter.trading_config import NautilusTradingStrategyConfig
from custos_toolkit_nautilus.adapter.utils import (
    deep_asdict,
    derive_bar_type,
)
from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager


class _StrategyId(Protocol):
    @property
    def value(self) -> str: ...


class NautilusTradingStrategy(NautilusStrategyCore):
    """
    Abstract base strategy with common trading, risk, and position logic.

    Concrete strategies override the abstract methods (``calculate_signal`` etc.; see each
    ``@abstractmethod`` signature and the module docstring for the contract); filters /
    risk controls / position management are handled uniformly by this base class.

    Example:
        class SuperTrendStrategy(NautilusTradingStrategy):
            def on_strategy_start(self) -> None:
                self.supertrend = SuperTrendIndicator(...)
                self.register_indicator(self.supertrend)

            def calculate_signal(self, ctx: PairContext, bar: Bar) -> Signal:
                if self.supertrend.direction == 1:
                    return Signal.enter_long(price=bar.close, pair=ctx.pair)
                elif self.supertrend.direction == -1:
                    return Signal.enter_short(price=bar.close, pair=ctx.pair)
                return Signal.neutral(price=bar.close, pair=ctx.pair)
    """

    # InstrumentId-centric contract for per-bar subclass hooks: the business parameters
    # (after self) of any override must match the table below exactly and be ordinary
    # positional parameters -- ctx: PairContext carries .pair / .instrument_id. The full
    # signature is validated (not just the first parameter name) to close the gaps where
    # (self, ctx) missing a param, keyword-only, *args, or a wrong name would raise
    # TypeError only when _process_bar actually calls the hook.
    _CTX_HOOK_SIGNATURES = {
        "calculate_signal": ("ctx", "bar"),
        "on_pre_bar": ("ctx", "bar"),
        "on_post_bar": ("ctx", "bar"),
        "on_trade_closed": ("ctx", "realized_pnl"),
        "on_indicator_update": ("ctx", "bar"),
        "calculate_position_size": ("ctx", "signal"),
    }

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Guard: forbid subclasses from overriding per-bar hooks with a pair-string or
        non-callable signature (fires along the whole inheritance chain)."""
        super().__init_subclass__(**kwargs)
        positional = (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
        for name, expected in NautilusTradingStrategy._CTX_HOOK_SIGNATURES.items():
            method = cls.__dict__.get(name)
            if not callable(method):
                continue
            params = list(inspect.signature(method).parameters.values())[1:]  # drop self
            actual = tuple(p.name for p in params)
            kinds_ok = all(p.kind in positional for p in params)
            if actual != expected or not kinds_ok:
                raise TypeError(
                    f"{cls.__name__}.{name} signature must be (self, {', '.join(expected)}) "
                    f"(InstrumentId-centric contract, ctx: PairContext); current business "
                    f"params are {actual}. Use ctx.pair to build Signals and ctx.instrument_id "
                    f"for instrument operations."
                )

    @property
    def config(self) -> NautilusTradingStrategyConfig:
        """Typed config property.

        Python property overrides Cython read-only Actor.config for IDE navigation.
        """
        return self._strat_config

    def __init__(self, config: NautilusTradingStrategyConfig):
        """
        Initialize base strategy.

        Args:
            config: Strategy configuration with common sections
        """
        super().__init__(config)
        # Actor.config is a Cython read-only descriptor; store a typed reference
        # so Python-side access (self.config.trading, etc.) resolves the correct type.
        self._strat_config: NautilusTradingStrategyConfig = config

        # Global FilterManager (scope=global filters only; initialized by
        # FilterCoordinator.init_global, which this field-typed annotation references)
        self._global_filter_manager: FilterManager | None = None

        # Position size factors
        # base_size_factor: Global multiplier from config (always applied)
        # Per-pair size_reduction_factor is stored in PairContext to avoid cross-pair contamination
        self._base_size_factor = config.position.base_size_factor

        # Global-scope filter effects (no per-pair context): size factor folded into
        # per-pair sizing by FilterCoordinator.check_pair; delay window (ns) blocks all
        # entries until it elapses. Set by FilterCoordinator.check_global.
        self._global_size_factor: float = 1.0
        self._global_filter_delay_until: int = 0

        # Initialize shared modules
        self._position_sizer = PositionSizer(msgspec.structs.asdict(config.position))
        # Initialized by RiskControlCoordinator.init_risk_controls (on_start).
        self._risk_controller: RiskController | None = None
        self._order_calculator = OrderPriceCalculator(deep_asdict(config.risk.trade))
        self._risk_manager = RiskManager({})

        # Smart Hybrid SL/TP Mode state
        # Modes: EXCHANGE (all on exchange), TICK (all tick monitoring),
        # HYBRID (SL exchange + TP/trailing tick), NATIVE_TRAILING.
        # Validity is guaranteed by TradeRiskConfig.__post_init__ at construction
        # time; the config string maps 1:1 onto SLTPMode.
        self._mode: SLTPMode = SLTPMode(config.risk.trade.sl_tp_mode)

        # Layered warmup manager (bar buffering + checkpoint validation). State
        # persistence is delegated to the framework's on_save/on_load. Initialized in
        # on_start().
        self._warmup_manager: WarmupManager | None = None

        # Extracted coordinator components (self injected). Logic is cohered into the
        # components; the base-class methods / call sites delegate:
        #   EquityProvider      <- effective/actual/fallback/risk equity getters
        #   ConfigSummaryLogger <- config + active-config logging
        #   StartupValidator    <- startup config + initial-capital-vs-balance checks
        #   OrderReconciler     <- restart recovery / SL claim / orphan sweep / reject breaker
        #   TradeEventHandler   <- order/position event telemetry / tracker cleanup /
        #                          post-fill protection dispatch / close cleanup
        #   SLTPCoordinator     <- SL/TP submission / cancellation / break-even relocation
        #   ExecutionCoordinator <- tick routing + partial/trailing/full exit execution
        #   SizingCoordinator   <- position sizing: default notional + fixed-risk path
        #   FilterCoordinator   <- FilterManager wiring: global/per-pair init + check + MTF
        #   WarmupCoordinator   <- warmup lifecycle: init + historical request + gate + replay
        #   PairContextCoordinator <- per-pair context construction + on_start bootstrap
        #   SnapshotCoordinator <- snapshot lifecycle: save/load state + layered restore + YAML warm
        #   SignalExecutionCoordinator <- per-bar signal execution: entry/exit/manage dispatch
        #   RiskControlCoordinator <- RiskController lifecycle + per-bar risk gate
        # These components only hold a strategy reference and consume no runtime resource at
        # construction, so they are built in __init__ -- eliminating the pre-on_start None
        # window (a delegate called before construction would raise AttributeError).
        self._equity_provider = EquityProvider(self)
        self._config_logger = ConfigSummaryLogger(self)
        self._startup_validator = StartupValidator(self)
        self._reconciler = OrderReconciler(self)
        self._trade_event_handler = TradeEventHandler(self)
        self._sltp_coordinator = SLTPCoordinator(self)
        self._execution_coordinator = ExecutionCoordinator(self)
        self._sizing_coordinator = SizingCoordinator(self)
        self._filter_coordinator = FilterCoordinator(self)
        self._warmup_coordinator = WarmupCoordinator(self)
        self._pair_context_coordinator = PairContextCoordinator(self)
        self._snapshot_coordinator = SnapshotCoordinator(self)
        self._signal_execution_coordinator = SignalExecutionCoordinator(self)
        self._risk_control_coordinator = RiskControlCoordinator(self)

        # The framework's on_load stashes the decoded snapshot here (on_load runs before
        # on_start, when contexts is still empty); once contexts are built,
        # SnapshotCoordinator.apply_loaded_snapshot() applies it once. _snapshot_restored
        # drives the state log.
        self._loaded_snapshot: dict[str, object] | None = None
        self._snapshot_restored: bool = False
        # One-shot warmup-complete gate: fires on_warmup_complete once all indicators are
        # ready, then short-circuits.
        self._warmup_complete_called: bool = False

        # Multi-pair support
        # The pair list is derived from config (config.trading.pairs is the single source
        # of truth), no longer cached into _pairs.
        # InstrumentId -> context map (initialized in on_start). Keyed by InstrumentId
        # (which includes the venue): the same pair string is two different instruments on
        # two exchanges, so a pair-string key would collide; and per bar/tick the lookup
        # drops from a linear scan to an O(1) dict.get.
        self._contexts: dict[InstrumentId, PairContext] = {}
        # Shared capital allocator (initialized in on_start if allocation configured)
        self._capital_allocator: CapitalAllocator | None = None

        # Risk limit log dedup: only log when reason changes
        self._last_risk_reason: str = ""

        # Order → signal_id mapping: MARKET orders lose tags after fill in NautilusTrader
        # cache (exchange fill events don't carry tags). This map preserves the linkage.
        self._order_signal_map: dict[str, str] = {}

        # Event publishing: placeholder, re-initialized in on_start once self.msgbus and
        # self.clock are available. Toggled by env var EVENT_PUBLISHING_ENABLED (default off).
        self._event_publisher: EventPublisher = EventPublisher(
            msgbus=None, strategy_id="", enabled=False
        )

        # Note: the _paused flag has been pushed down to NautilusStrategyCore.__init__ and
        # is inherited automatically via super().__init__(config); no longer redefined here.

    # ---- Abstract methods (subclasses must implement) ----

    @abstractmethod
    def on_strategy_start(self) -> None:
        """Initialize strategy-specific indicators and state."""
        ...

    @abstractmethod
    def on_strategy_stop(self) -> None:
        """Clean up strategy resources."""
        ...

    @abstractmethod
    def calculate_signal(self, ctx: PairContext, bar: Bar) -> Signal:
        """Calculate trading signal from bar data for a pair's context."""
        ...

    # ---- Optional hooks (override as needed) ----

    def on_reset(self) -> None:
        """Optional hook: reset the strategy to its initial state.

        Aligns with NautilusTrader's empty optional-hook convention -- most strategies
        have no custom state to reset, so this defaults to a no-op; subclasses that need
        it override.
        """
        pass

    def on_trade_closed(self, ctx: PairContext, realized_pnl: Decimal) -> None:
        """Optional hook: receive the trade result (realized PnL) after a position closes.

        Fires only **after** record_trade, capital release, SL/TP cleanup, and tracker
        reset have all completed -- placing it after cleanup is deliberate: if the hook
        raises, on_position_closed's top-level exception guard catches it and the SL/TP
        cancellation and tracker reset are never affected. Intended for martingale /
        adaptive strategies that retune by trade result, avoiding an override of the heavy
        on_position_closed callback. Defaults to a no-op.
        """
        pass

    def on_pre_bar(self, ctx: PairContext, bar: Bar) -> None:
        """Hook: Pre-bar processing before filters and signals for a pair's context."""
        pass

    def on_post_bar(self, ctx: PairContext, bar: Bar) -> None:
        """Hook: Post-bar processing after position management for a pair's context."""
        pass

    def calculate_position_size(self, ctx: PairContext, signal: Signal) -> Decimal:
        """Hook: Calculate position size for a signal within a pair's context."""
        return self._sizing_coordinator.default_position_size(ctx, signal)

    def get_snapshot_indicators(self) -> dict[str, object]:
        """
        Hook: Return indicators to snapshot.

        Override this method to specify which indicators should be included
        in state snapshots. Each indicator must implement to_snapshot() and
        from_snapshot() methods.

        Returns:
            Dictionary mapping indicator names to indicator instances.

        Example:
            def get_snapshot_indicators(self) -> dict:
                return {"supertrend": self.supertrend}
        """
        return {}

    def get_snapshot_state(self) -> dict[str, object]:
        """
        Hook: Return strategy-specific state to snapshot.

        Override this method to specify additional strategy state that should
        be persisted in snapshots (e.g., prev_trend, entry_count).

        Returns:
            Dictionary of strategy state to persist.

        Example:
            def get_snapshot_state(self) -> dict:
                return {"prev_trend": self._prev_trend}
        """
        return {}

    def restore_from_snapshot(self, snapshot: dict[str, object]) -> bool:
        """
        Hook: Restore strategy state from snapshot.

        Override this method to restore strategy-specific state from a
        previously saved snapshot.

        Args:
            snapshot: Snapshot dictionary containing indicators and state.

        Returns:
            True if restore succeeded, False otherwise.

        Example:
            def restore_from_snapshot(self, snapshot: dict) -> bool:
                try:
                    if "supertrend" in snapshot.get("indicators", {}):
                        self.supertrend.from_snapshot(snapshot["indicators"]["supertrend"])
                    state = snapshot.get("state", {})
                    self._prev_trend = state.get("prev_trend", 0)
                    return True
                except Exception as exc:
                    self.log.error(f"snapshot restore failed: {exc}")
                    return False
        """
        return False

    # ---- Framework state persistence (native on_save/on_load) ----

    def on_save(self) -> dict[str, bytes]:
        """Framework lifecycle hook: serialize strategy state (body in SnapshotCoordinator).

        Native ``Actor.on_save`` is dispatched by name, so this thin shell stays on
        the Strategy class. Best-effort: a serialization failure is logged and yields
        an empty state rather than crashing the framework persistence cycle.
        """
        try:
            return self._snapshot_coordinator.save_state()
        except Exception as exc:
            self.log.warning(f"on_save failed: {exc}")
            return {}

    def on_load(self, state: dict[str, bytes]) -> None:
        """Framework lifecycle hook: decode + stash loaded snapshot (body in SnapshotCoordinator).

        Native ``Actor.on_load`` is dispatched by name, so this thin shell stays on
        the Strategy class. ``SnapshotCoordinator.apply_loaded_snapshot()`` applies the
        stashed snapshot once contexts are built. Best-effort: failures are logged.
        """
        try:
            self._snapshot_coordinator.load_state(state)
        except Exception as exc:
            self.log.warning(f"on_load failed: {exc}")

    # ---- Config logging ----

    def _log_config_summary(self) -> None:
        """Log expected configuration before initialization (delegates to ConfigSummaryLogger)."""
        self._config_logger.log_config_summary()

    def _log_active_config(self) -> None:
        """Log active configuration after initialization (delegates to ConfigSummaryLogger)."""
        self._config_logger.log_active_config()

    # ---- Startup validation ----

    def _validate_startup_config(self) -> None:
        """Startup configuration sanity check (thin delegate to StartupValidator)."""
        self._startup_validator.validate_startup_config()

    def _validate_initial_capital_vs_balance(self) -> None:
        """initial_capital vs actual-balance check (thin delegate to StartupValidator)."""
        self._startup_validator.validate_initial_capital_vs_balance()

    # ---- Lifecycle methods (common implementation) ----

    def on_start(self) -> None:
        """Initialize strategy with multi-pair support.

        Note: this method owns its full initialization flow (filters / risk / warmup
        manager) and deliberately does not call super().on_start(). The ready file is
        written by on_warmup_complete() once warmup finishes, not at the end of on_start.
        Snapshot state persistence is driven by the framework's Actor.on_save/on_load
        (SnapshotManager/SnapshotHelper have been removed).
        """
        # Step 1: log expected configuration before initialization
        self._log_config_summary()

        # Initialize EventPublisher with the live msgbus/clock
        events_enabled = os.environ.get("EVENT_PUBLISHING_ENABLED", "").lower() in ("1", "true")
        self._event_publisher = EventPublisher(
            msgbus=self.msgbus,
            # Use the Crucible slug (STRATEGY_ID env) rather than the Nautilus internal id,
            # otherwise the EventPersister upsert hits orders_strategy_id_fkey -> the SSE
            # persistence path dies silently.
            strategy_id=resolve_event_strategy_id(
                cast(_StrategyId, self.id).value if self.id else ""
            ),
            clock=self.clock,
            enabled=events_enabled,
        )
        if events_enabled:
            self.log.info("Event publishing ENABLED", color=LogColor.GREEN)
            # Enabled but STRATEGY_ID missing -> events fall back to the Nautilus internal
            # id and hit the Crucible FK -> Path B is silently lost entirely. Warn so the
            # silent fallback can't recur unnoticed.
            if strategy_id_env_missing():
                self.log.warning(
                    "EVENT_PUBLISHING_ENABLED but STRATEGY_ID env missing — events will use "
                    "Nautilus internal id and fail Crucible FK. Set STRATEGY_ID.",
                    color=LogColor.RED,
                )

        # 1. Initialize global components
        self._filter_coordinator.init_global()
        self._risk_control_coordinator.init_risk_controls()

        # 2. Create context and initialize execution components for each pair
        self._pair_context_coordinator.setup_pairs()

        # 3. Initialize capital allocator
        self._init_capital_allocator()

        # 4. Initialize warmup manager (bar buffering + checkpoint validation)
        self._warmup_coordinator.init_manager()

        # 5. Call strategy hook
        self.on_strategy_start()

        # 6. Request historical data (warmup)
        self._warmup_coordinator.request_historical_data()

        # 7. Subscribe to tick data (if enabled)
        self._pair_context_coordinator.subscribe_ticks()

        # 7b. Subscribe the mark price the guards price positions from. Not optional and
        # not tied to the tick config above: the snapshot the notional cap, the snapshot
        # publisher and the fallback breaker all read is unreliable without it whenever a
        # position is open.
        self._pair_context_coordinator.subscribe_mark_prices()

        # 8. Recover from existing positions
        self._reconciler.recover_from_existing_positions()

        self.log.info(
            f"Strategy started: {len(self._contexts)} pairs, sl_tp_mode={self._mode.value}",
            color=LogColor.GREEN,
        )

        # Warn if tick mode has no stop loss protection
        if self._mode is SLTPMode.TICK:
            has_sl = self.config.risk.trade.stop_loss.method != "none"
            if has_sl:
                self.log.warning(
                    "sl_tp_mode='tick': Stop loss is configured but NOT submitted to exchange. "
                    "Tick monitoring only handles take profit. Positions have NO stop loss "
                    "protection until trailing stop activates. Consider 'hybrid' mode for "
                    "exchange-level safety SL.",
                    color=LogColor.RED,
                )

        # 9. Validate startup configuration
        self._validate_startup_config()

        # Step 2: log the actually-active configuration after initialization
        self._log_active_config()

    def on_stop(self) -> None:
        """Clean up all pair contexts.

        This method deliberately does not call ``super().on_stop()`` (consistent with
        ``on_start``, it owns its full cleanup flow). The ready file is removed explicitly
        at the end of the method so the sidecar ``/health`` no longer reports
        ``ready=True`` after the strategy stops.
        """
        # snapshot-on-stop has been removed -- state is persisted by the framework via
        # on_save at trader.save() (stop/dispose) time (when save_state is enabled).

        # Clean up each pair
        for _, ctx in self._contexts.items():
            # Cancel all orders
            self.cancel_all_orders(ctx.instrument_id)

            # Unsubscribe from bar data
            self.unsubscribe_bars(ctx.bar_type)

            # Unsubscribe from tick data if enabled
            tick_config = self._get_tick_monitoring_config()
            if tick_config and tick_config.enabled:
                if tick_config.tick_type in ("trade", "both"):
                    self.unsubscribe_trade_ticks(ctx.instrument_id)
                if tick_config.tick_type in ("quote", "both"):
                    self.unsubscribe_quote_ticks(ctx.instrument_id)

        # Call strategy hook
        self.on_strategy_stop()

        # Ensure the ready file is removed so the sidecar /health stops reporting
        # ready=True after shutdown. Kept local because we deliberately skip
        # super().on_stop() (see the method docstring).
        try:
            _Path(self._ready_file).unlink(missing_ok=True)
            self._ready = False
        except OSError as exc:
            self.log.warning(f"[READY] Failed to remove ready file on stop: {exc}")

        self.log.info(f"Strategy stopped: {len(self._contexts)} pairs", color=LogColor.YELLOW)

    def _on_bar_risk_hygiene(self, bar: Bar) -> None:
        """Risk hygiene that runs even during soft pause and before warmup.

        Order reconciliation (orphan sweep + native-trailing self-heal) depends only on
        live order/position state, not on indicator warmup, so it must run for any bar
        with a known context. Gating it behind ``warmed_up`` would leave a hot-restarted
        position unprotected for the whole warmup window.
        """
        ctx = self._get_context_from_instrument(bar.bar_type.instrument_id)
        if ctx is None:
            return
        self._reconciler.sweep_stale_orders_for_pair(ctx)
        self._reconciler.ensure_native_trailing_protection(ctx)

    def on_core_bar(self, bar: Bar) -> None:
        """Route bar to corresponding pair context."""
        self._process_bar(bar)

    def _process_bar(self, bar: Bar) -> None:
        """Core bar processing logic.

        Filters and risk limits gate *entries only*. Signal generation, exits, and
        position management always run -- a blocked entry filter or a paused risk
        controller must never suppress a reversal/exit or strand an open position.
        Direction-aware filters are checked after the signal direction is known, so a
        short entry is gated against short thresholds rather than long ones.
        """
        # 1. Identify trading pair (O(1) lookup; pair is derived from ctx for downstream
        #    business/logging use)
        ctx = self._get_context_from_instrument(bar.bar_type.instrument_id)
        if ctx is None:
            return
        pair = ctx.pair

        # 2. Warmup gate FIRST (buffer during warmup, short-circuit until ready;
        # also writes the one-shot ready signal once all indicators are initialized)
        if self._warmup_coordinator.handle_warmup_gate(ctx, bar):
            return

        # 3. Pre-bar hook
        self.on_pre_bar(ctx, bar)

        # 4. Handle MTF bars (multi-timeframe feeds short-circuit the pipeline)
        if self._filter_coordinator.handle_mtf_bar(ctx, bar):
            return

        # 5. Update filter state every bar (gating happens at the entry branch below)
        self._filter_coordinator.update_global(bar)
        self._filter_coordinator.update_pair(ctx, bar)

        # 6. Calculate signal (always -- exits/reversals are never suppressed by gates)
        signal = self.calculate_signal(ctx, bar)
        # safety net: ensure signal.pair is correct even if a subclass forgot to set it
        signal.pair = ctx.pair
        metadata_str = (
            ", ".join(f"{k}={v}" for k, v in signal.metadata.items()) if signal.metadata else ""
        )
        self.log.info(
            f"[{pair}] Signal: {signal.direction.name} | "
            f"price={bar.close} | strength={signal.strength:.2f}"
            + (f" | {metadata_str}" if metadata_str else "")
        )

        # 6b. Event emission: signal event
        if self._event_publisher.enabled and signal.is_actionable():
            _signal_id = generate_signal_id()
            signal.metadata["_signal_id"] = _signal_id
            self._event_publisher.publish_signal(
                signal_id=_signal_id,
                direction=signal.direction.name,
                pair=pair,
                price=str(signal.price),
                strength=signal.strength,
                metadata={k: v for k, v in signal.metadata.items() if not k.startswith("_")},
            )

        # 7. Process signal. Exits bypass entry filters and risk limits; entries are
        # gated by direction permission, risk limits, and direction-aware filters.
        if signal.is_actionable():
            if signal.is_exit():
                self._signal_execution_coordinator.execute_exit_for_pair(ctx, signal, bar)
            elif self._is_direction_allowed(signal.direction) and self._entry_gates_pass(
                ctx, bar, signal.direction
            ):
                size = self.calculate_position_size(ctx, signal)
                self._signal_execution_coordinator.execute_entry_for_pair(ctx, signal, size, bar)

        # 8. Manage positions for this pair (SL/TP/trailing -- always, never gated)
        self._signal_execution_coordinator.manage_positions_for_pair(ctx, bar)

        # 9. Post-bar hook
        self.on_post_bar(ctx, bar)

        # per-bar snapshot saving has been removed -- state is now persisted by the
        # framework's on_save (at trader.save() on stop/dispose).

    def _entry_gates_pass(self, ctx: PairContext, bar: Bar, direction: SignalDirection) -> bool:
        """Risk limits + risk-equity reliability + global/per-pair filters that gate a
        candidate entry.

        Short-circuit so a failing earlier gate skips the rest. ``check_risk_limits``
        evaluates the mark-to-market equity; if that equity is unreliable (unpriced
        positions / lookup failure) new entries are blocked fail-closed — exits and
        position management bypass this gate entirely. Direction-aware filters
        (momentum) receive ``direction`` and read the matching long/short band.
        """
        if not self._risk_control_coordinator.check_risk_limits(bar.ts_event):
            return False
        if not self._equity_provider.is_risk_equity_reliable():
            self.log.warning(
                "[RISK] Risk equity is unreliable (unpriced position / fetch failure); new "
                "entries are blocked, while exits and position management are unaffected."
            )
            return False
        return self._filter_coordinator.check_global(
            bar, direction
        ) and self._filter_coordinator.check_pair(ctx, bar, direction)

    def _is_direction_allowed(self, direction: SignalDirection) -> bool:
        """Check if signal direction is allowed by config."""
        trading = self.config.trading
        if direction == SignalDirection.ENTER_LONG and not trading.enable_long:
            return False
        if direction == SignalDirection.ENTER_SHORT and not trading.enable_short:
            return False
        return True

    def _get_effective_capital(self) -> Decimal:
        """Capital basis for position sizing (delegates to EquityProvider)."""
        return self._equity_provider.get_effective_capital()

    def _get_actual_balance(self) -> Decimal:
        """Available quote-currency balance (delegates to EquityProvider)."""
        return self._equity_provider.get_actual_balance()

    def _fallback_capital(self) -> Decimal:
        """Configured initial_capital fallback (delegates to EquityProvider)."""
        return self._equity_provider.fallback_capital()

    def _get_risk_equity(self) -> Decimal:
        """Risk-control mark-to-market equity basis (delegates to EquityProvider)."""
        return self._equity_provider.get_risk_equity()

    def _get_tick_monitoring_config(self) -> TickMonitoringConfig:
        """Get tick monitoring config if available."""
        return self.config.risk.tick_monitoring

    def _is_tick_monitoring_enabled(self) -> bool:
        """Check if tick monitoring is enabled."""
        return self._get_tick_monitoring_config().enabled

    def on_indicator_update(self, ctx: PairContext, bar: Bar) -> None:
        """
        Hook: Update strategy-specific indicators with a bar.

        Called during buffered bar replay to keep indicators up-to-date.
        Subclasses should override to update their custom indicators.

        Note: NautilusTrader's registered indicators (ATR, etc.) are updated
        automatically. This hook is for custom indicator updates.

        Args:
            ctx: PairContext for the pair (carries .pair / .instrument_id)
            bar: Bar to process
        """
        pass  # Subclasses can override if needed

    # ---- Components exposed to subclasses ----

    @property
    def position_sizer(self) -> PositionSizer:
        """Position calculator, available for subclasses to compute position sizes."""
        return self._position_sizer

    @property
    def order_calculator(self) -> OrderPriceCalculator:
        """Order price calculator, available for subclasses to compute SL/TP prices."""
        return self._order_calculator

    @property
    def risk_controller(self) -> RiskController | None:
        """Risk controller, available for subclasses to check risk status."""
        return self._risk_controller

    # ---- Convenience methods (for subclasses to use in calculate_signal) ----

    def get_effective_capital(self) -> Decimal:
        """
        Get current effective capital for position sizing.

        Returns:
            Available capital based on config (compound or fixed)
        """
        return self._get_effective_capital()

    # ---- Trade result tracking ----

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """Handle order accepted with top-level exception protection."""
        try:
            self._trade_event_handler.handle_order_accepted(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_order_accepted: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    def on_order_filled(self, event: OrderFilled) -> None:
        """Handle order filled with top-level exception protection."""
        try:
            self._trade_event_handler.handle_order_filled(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_order_filled: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    def on_position_opened(self, event: PositionOpened) -> None:
        """Handle position opened with top-level exception protection."""
        try:
            self._trade_event_handler.handle_position_opened(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_position_opened: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    def on_position_closed(self, event: PositionClosed) -> None:
        """Handle position closed with top-level exception protection."""
        try:
            self._trade_event_handler.handle_position_closed(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_position_closed: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """Handle order canceled with top-level exception protection.

        The record goes first, ahead of the guarded body: a body that throws would
        otherwise take the confirmation with it, and a confirmation that goes missing
        reads exactly like a cancel that never landed.
        """
        record_cancel_confirmed(
            self.log,
            order_id=event.client_order_id,
            instrument_id=event.instrument_id,
        )
        try:
            self._trade_event_handler.handle_order_canceled(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_order_canceled: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    def on_order_rejected(self, event: OrderRejected) -> None:
        """Handle order rejected event (rejection-breaker body delegated to OrderReconciler).

        The engine dispatches this callback by name, so the thin shell must stay on the
        Strategy class; the shell keeps the top-level try/except guard (engine callback
        exceptions must not break through the strategy process) while the body lives in
        the component.
        """
        try:
            self._reconciler.handle_order_rejected(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(f"on_order_rejected handler failed: {exc}")

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        """Handle order cancel rejected with top-level exception protection.

        Body delegated to OrderReconciler; the engine dispatches by name, so the thin
        shell stays on the Strategy class.

        The record goes first, for the same reason as ``on_order_canceled`` -- and with
        more force here, since the body currently does nothing at all for a stop-loss,
        so without this line a refused stop-loss cancel leaves no trace whatsoever.
        """
        record_cancel_refused(
            self.log,
            order_id=event.client_order_id,
            instrument_id=event.instrument_id,
            reason=event.reason,
        )
        try:
            self._reconciler.handle_order_cancel_rejected(event)
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_order_cancel_rejected: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    # ---- Tick-level SL/TP monitoring ----

    def on_core_trade_tick(self, tick: TradeTick) -> None:
        self._execution_coordinator.handle_trade_tick(tick)

    def on_core_quote_tick(self, tick: QuoteTick) -> None:
        self._execution_coordinator.handle_quote_tick(tick)

    # ---- Historical data warmup ----

    def _get_warmup_config(self) -> WarmupConfig | None:
        """Get warmup config from strategy config."""
        # self.config.warmup is already a WarmupConfig object (built by base_config.py)
        return self.config.warmup

    def on_historical_data(self, data: object) -> None:
        """Handle historical bar data received from request_bars().

        NautilusTrader callback (dispatched by name); delegates to the coordinator.
        The engine does not guard callbacks, so a bare exception here would break
        through and kill the strategy process during warmup replay.
        """
        try:
            self._warmup_coordinator.handle_historical_data(data)
        except CheckpointValidationError as exc:
            # Replayed warmup state failed integrity checks. Stop in a controlled way
            # instead of crashing the engine — trading on a mismatched warmup state is
            # worse than not trading.
            self.log.error(
                f"[STARTUP] Historical warmup checkpoint validation failed; the strategy has "
                f"been stopped to avoid trading in a bad state: {exc}. Verify the snapshot / "
                f"historical data and restart.",
                color=LogColor.RED,
            )
            self.stop()
        except Exception as exc:  # noqa: BLE001 — engine doesn't guard callbacks; log and continue
            self.log.error(
                f"Exception in on_historical_data: {type(exc).__name__}: {exc}",
                color=LogColor.RED,
            )

    def on_warmup_complete(self) -> None:
        """Hook: Called when historical data warmup is complete."""
        self.mark_ready()
        self.log.info(f"[READY] Strategy ready (file: {self._ready_file})")

    # ---- Multi-pair support methods ----

    def _derive_instrument_id_for_pair(self, pair: str) -> InstrumentId:
        """
        Derive instrument ID for a specific trading pair.

        Args:
            pair: Trading pair (e.g., "BTC-USDT")

        Returns:
            NautilusTrader InstrumentId
        """
        from custos_toolkit_nautilus.adapter.utils import VENUE_MAP

        connector = self.config.trading.connector
        venue = VENUE_MAP.get(connector, "BINANCE")
        is_futures = "perpetual" in connector

        symbol = pair.replace("-", "")
        if is_futures:
            symbol += "-PERP"

        return InstrumentId.from_str(f"{symbol}.{venue}")

    def _derive_bar_type_for_instrument(self, instrument_id: InstrumentId) -> BarType:
        """Derive BarType directly from an InstrumentId.

        L1: reused when the caller already holds the instrument_id, avoiding a re-derive.
        """
        return derive_bar_type(self.config.platforms, instrument_id)

    def _derive_bar_type_for_pair(self, pair: str) -> BarType:
        """Derive BarType from a pair string via InstrumentId, then the instrument variant."""
        return self._derive_bar_type_for_instrument(self._derive_instrument_id_for_pair(pair))

    def _get_pair_from_instrument(self, instrument_id: InstrumentId) -> str | None:
        """Look up the pair string from an InstrumentId (O(1); _contexts keyed by InstrumentId)."""
        ctx = self._contexts.get(instrument_id)
        return ctx.pair if ctx else None

    def _get_context_from_instrument(self, instrument_id: InstrumentId) -> PairContext | None:
        """Get the context for an InstrumentId (O(1) dict.get)."""
        return self._contexts.get(instrument_id)

    def order_tracker_for(self, instrument_id: InstrumentId) -> OrderTracker | None:
        """Hand the base class's close paths this instrument's venue-refusal evidence.

        The evidence lives in the pair context's tracker, which the base class cannot
        reach on its own -- so without this override the containment flatten and
        ``emergency_close`` would keep using the reduce-only form on a venue that has
        already refused it.
        """
        ctx = self._contexts.get(instrument_id)
        return ctx.order_tracker if ctx is not None else None

    def _get_context(self, pair: str) -> PairContext | None:
        """Get the context for a pair string.

        _contexts is keyed by InstrumentId, so derive the InstrumentId first, then look it
        up -- consistent with ``PairContextCoordinator.create_context`` (which also uses
        ``_derive_instrument_id_for_pair``), so no separate pair->InstrumentId map is needed.
        """
        return self._contexts.get(self._derive_instrument_id_for_pair(pair))

    def _init_capital_allocator(self) -> None:
        """
        Initialize capital allocator if allocation config is present.

        Called during on_start() to set up multi-pair capital management.
        """
        if not self.config.trading.allocation:
            return

        initial_capital = self._get_effective_capital()

        self._capital_allocator = CapitalAllocator(
            config=self.config.trading.allocation,
            initial_capital=initial_capital,
            cache=self.cache,
        )

        # Register all pairs from the already-built contexts -- each ctx already carries
        # its derived instrument_id, so there's no need to re-derive from the config
        # string. contexts are inserted in config.trading.pairs order (dict preserves
        # order), so the registration order matches the original.
        for ctx in self._contexts.values():
            self._capital_allocator.register_pair(ctx.pair, ctx.instrument_id)

        self.log.info(
            f"CapitalAllocator initialized: pairs={len(self._contexts)}, "
            f"capital={initial_capital}, mode={self.config.trading.allocation.mode}"
        )
