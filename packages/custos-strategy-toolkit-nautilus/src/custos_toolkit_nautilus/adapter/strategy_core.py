"""
Minimal strategy base class for NautilusTrader strategies.

Defines the minimal interface that all strategies must implement to work
with Runner (production/test) and Speculum (backtesting).

Inheritance hierarchy:
    nautilus_trader.Strategy
            │
            ▼
    NautilusStrategyCore  ← This class (minimal interface)
            │
       ┌────┴────┐
       ▼         ▼
    NautilusBaseStrategy    CustomStrategy
    (feature-rich)          (maximum flexibility)

Strategies that want full functionality (filters, risk management, position
management, etc.) should extend NautilusBaseStrategy.

Strategies that need maximum flexibility can extend NautilusStrategyCore
directly and implement all trading logic themselves.
"""

import logging
import os
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.trading.strategy import Strategy

from custos_toolkit_nautilus.adapter.state_persistence import (
    build_snapshot,
    decode_snapshot,
    encode_snapshot,
)

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.orders import OrderTracker

# Module-level logger, used as a fallback when self.log is unavailable.
_logger = logging.getLogger(__name__)


class CloseAttempt(Enum):
    """Which form the next close of a position may take."""

    REDUCE_ONLY = "reduce_only"
    """The protective default: the venue rejects a duplicate rather than reversing."""

    PLAIN = "plain"
    """The escape hatch, earned by a refusal of the reduce-only form itself."""

    PLAIN_ALREADY_SPENT = "plain_already_spent"
    """The hatch was used for this position. What to do now is the caller's decision."""


def plan_close_attempt(
    *,
    reduce_only_refused: bool,
    plain_close_submitted: bool,
) -> CloseAttempt:
    """Decide which form a close of this position may take.

    ``reduce_only`` is the protective default and it is there for a reason: when a close
    is re-sent while the local cache still lags a fill that already closed the position,
    the venue rejects the duplicate instead of opening a reverse position. A plain order
    in that situation opens the reverse position. So the hatch needs two things.

    *Positive evidence.* ``reduce_only_refused`` means the venue named reduce-only as the
    problem -- not merely that something was rejected. The incident of 2026-07-31 is why
    that distinction is spelt out: arming keyed off the rejection classifier's ``logic``
    tier, which is documented as the bucket for *unrecognised* reasons, so a rejection
    carrying no reason at all armed the hatch while the venue was answering ``-1007``,
    execution status unknown -- the one context where a close may already have filled.

    *A single attempt.* The hatch is spent once used. Callers differ in what they do
    then, and the difference is deliberate: a per-bar path should send nothing, because
    re-sending on every bar is how a stale view turns into a position; a one-shot
    containment path should fall back to the reduce-only form, which may well be refused
    but cannot reverse anything, because attempting beats abstaining when the alternative
    is leaving exposure uncontained.
    """
    if not reduce_only_refused:
        return CloseAttempt.REDUCE_ONLY
    if plain_close_submitted:
        return CloseAttempt.PLAIN_ALREADY_SPENT
    return CloseAttempt.PLAIN


class NautilusStrategyCore(Strategy, ABC):
    """
    Abstract base class defining minimal strategy interface.

    All strategies (whether using NautilusBaseStrategy or custom implementation)
    must satisfy this interface for Runner and Speculum compatibility.

    Required methods (abstract):
        get_indicator_history(): Return indicator data for backtest visualization

    Optional methods (have default implementations):
        get_snapshot_state(): Return strategy state for persistence
        get_snapshot_indicators(): Return indicator state for persistence
        restore_from_snapshot(): Restore state from snapshot

    NautilusTrader lifecycle methods (inherited from Strategy):
        on_start(): Called when strategy starts
        on_stop(): Called when strategy stops
        on_bar(): Called on each bar event
        on_reset(): Called to reset strategy state

    Example (custom strategy without NautilusBaseStrategy):
        class MyCustomStrategy(NautilusStrategyCore):
            def __init__(self, config: MyCustomConfig):
                super().__init__(config)
                self.my_indicator = None

            def on_start(self) -> None:
                self.my_indicator = MyIndicator(period=20)
                self.register_indicator_for_bars(self.bar_type, self.my_indicator)

            def on_stop(self) -> None:
                pass

            def on_core_bar(self, bar: Bar) -> None:
                # Custom trading logic (Core template handles exceptions + pause)
                if self.my_indicator.value > threshold:
                    self.submit_order(...)

            def on_reset(self) -> None:
                if self.my_indicator:
                    self.my_indicator.reset()

            def get_indicator_history(self) -> dict:
                return {"my_indicator": {"points": [...]}}
    """

    # ---- Required extension methods ----

    @abstractmethod
    def get_indicator_history(self) -> dict[str, object]:
        """
        Return indicator history data for backtest visualization.

        This method is required for Speculum to visualize indicator data
        alongside price charts in backtest results.

        Returns:
            Dictionary mapping indicator names to their data.
            Format: {
                "indicator_name": {
                    "type": "INDICATOR_TYPE",
                    "config": {...},
                    "points": [
                        {"time": unix_timestamp, "value": float, ...},
                        ...
                    ]
                }
            }

        Example:
            return {
                "supertrend": {
                    "type": "SUPERTREND",
                    "config": {"atr_period": 10, "multiplier": 3.0},
                    "points": [
                        {"time": 1706745600, "value": 42150.5, "direction": 1},
                        {"time": 1706745660, "value": 42180.2, "direction": 1},
                    ]
                }
            }
        """
        ...

    # ---- Optional extension methods (with default implementations) ----

    def get_snapshot_state(self) -> dict[str, object]:
        """
        Return strategy-specific state for snapshot persistence.

        Override this method to include custom state that should be
        persisted and restored across strategy restarts.

        Returns:
            Dictionary of state to persist.

        Example:
            return {
                "prev_trend": self._prev_trend,
                "last_signal_time": self._last_signal_time,
            }
        """
        return {}

    def get_snapshot_indicators(self) -> dict[str, object]:
        """
        Return indicators for snapshot persistence.

        Override this method to include indicators that should be
        snapshotted. Indicators must support `to_snapshot()` method.

        Returns:
            Dictionary mapping indicator names to indicator objects.

        Example:
            return {
                "supertrend": self.supertrend,
                "atr": self.atr,
            }
        """
        return {}

    def restore_from_snapshot(self, snapshot: dict[str, object]) -> bool:
        """
        Restore strategy state from a snapshot.

        Override this method to restore custom state and indicators
        from a previously saved snapshot.

        Args:
            snapshot: Dictionary containing:
                - "state": Result of get_snapshot_state()
                - "indicators": Serialized indicator data

        Returns:
            True if restoration succeeded, False otherwise.

        Example:
            try:
                state = snapshot.get("state", {})
                self._prev_trend = state.get("prev_trend", 0)

                indicators = snapshot.get("indicators", {})
                if "supertrend" in indicators:
                    self.supertrend.from_snapshot(indicators["supertrend"])

                return True
            except Exception:
                # Restore failed: return False so the caller can fall back to a
                # cold start without snapshot state.
                return False
        """
        return True

    # ---- Sidecar control capabilities (pushed down to Core) ----

    _TEMPLATE_METHODS = frozenset({"on_bar", "on_trade_tick", "on_quote_tick"})

    def __init_subclass__(cls, **kw: object) -> None:
        super().__init_subclass__(**kw)
        for m in cls._TEMPLATE_METHODS:
            if m in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} must not override {m}; override on_core_{m[3:]} instead"
                )

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self._paused: bool = False
        self._ready: bool = False
        self._ready_file: str = os.environ.get("STRATEGY_READY_FILE", "/tmp/strategy_ready")

    # ---- Control: pause/resume/is_paused + ready ----

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        super().resume()
        self._paused = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        if self._ready:
            return
        try:
            Path(self._ready_file).touch()
            self._ready = True
        except OSError as exc:
            self._log_warning(f"mark_ready: failed to touch ready file: {exc}")

    _BASE_COMMANDS = frozenset({"pause", "resume", "shutdown", "emergency_close"})

    def supported_commands(self) -> frozenset[str]:
        return self._BASE_COMMANDS

    # ---- Template methods: top-level exception guard + pause short-circuit ----
    # The engine does not guard strategy callbacks: a single-event error must be
    # caught here so it never kills the strategy process (only the current event
    # is skipped). on_core_* hold the real per-strategy logic.

    # Engine callbacks: NautilusTrader does NOT shield strategy callbacks from
    # exceptions — an uncaught error here exits the strategy process and can strand
    # an open position. The three callbacks below swallow, log, and skip the current
    # bar/tick so the engine keeps delivering the next one.
    def on_bar(self, bar: Bar) -> None:
        try:
            self._on_bar_risk_hygiene(bar)
            if self._paused:
                return
            self.on_core_bar(bar)
        except Exception as exc:
            self._log_error(f"on_bar: {type(exc).__name__}: {exc}")

    def on_trade_tick(self, tick: TradeTick) -> None:
        try:
            if self._paused:
                return
            self.on_core_trade_tick(tick)
        except Exception as exc:
            self._log_error(f"on_trade_tick: {type(exc).__name__}: {exc}")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        try:
            if self._paused:
                return
            self.on_core_quote_tick(tick)
        except Exception as exc:
            self._log_error(f"on_quote_tick: {type(exc).__name__}: {exc}")

    def on_core_bar(self, bar: Bar) -> None: ...

    def on_core_trade_tick(self, tick: TradeTick) -> None: ...

    def on_core_quote_tick(self, tick: QuoteTick) -> None: ...

    def _on_bar_risk_hygiene(self, bar: Bar) -> None: ...

    # ---- Framework state persistence (native on_save/on_load) ----

    def on_save(self) -> dict[str, bytes]:
        """Framework lifecycle hook: persist strategy global state.

        Core strategies have no per-pair contexts, so only the
        ``get_snapshot_state()`` global state is persisted. NautilusTradingStrategy
        overrides this with a per-pair version. The framework calls ``save()`` at
        stop/dispose when save_state=True and a cache database is configured.
        Best-effort: failures are logged, never raised.
        """
        try:
            ts = self.clock.timestamp_ns() if self.clock else 0
            snapshot = build_snapshot({}, self.get_snapshot_state(), self.__class__.__name__, ts)
            return encode_snapshot(snapshot)
        except Exception as exc:
            self._log_warning(f"on_save failed: {exc}")
            return {}

    def on_load(self, state: dict[str, bytes]) -> None:
        """Framework lifecycle hook: restore strategy global state.

        A Core subclass's ``restore_from_snapshot`` only restores scalar state
        (not indicators), so it is safe to restore directly in on_load (kernel
        build, before on_start). Best-effort.
        """
        try:
            snapshot = decode_snapshot(state)
            if snapshot is None:
                return
            global_state = snapshot.get("global_state", {})
            if global_state:
                self.restore_from_snapshot({"state": global_state})
        except Exception as exc:
            self._log_warning(f"on_load failed: {exc}")

    # ---- Closing a position when the venue refuses the protective form ----

    def order_tracker_for(self, instrument_id: Any) -> "OrderTracker | None":
        """The tracker holding this instrument's venue-refusal evidence, if any.

        Core keeps no per-instrument order state, so it has no evidence and the close
        paths below take the protective default. Subclasses that do keep it -- such as
        ``NautilusTradingStrategy``, whose pair contexts each own an ``OrderTracker`` --
        override this so the close paths can see what the venue has already refused.
        """
        return None

    def close_all_positions_with_fallback(self, instrument_id: Any) -> None:
        """Close this instrument's open positions, dropping reduce-only only on evidence.

        The containment entry point: the runner's breaker calls this when it needs
        exposure gone. It is the same decision the regular exit path makes, because a
        venue that refuses the reduce-only form refuses it for every path, and the
        breaker's flatten is the moment that most needs a close to land.

        One thing it cannot do, said plainly so the record does not read otherwise: a
        rejection reaches the strategy asynchronously as an event, while this is a single
        synchronous pass. A refusal that happens *during* this call is not visible to it.
        This helps when the refusal was already recorded, which is the shape the real
        incident had.
        """
        for position in self.cache.positions_open(instrument_id=instrument_id):
            self._close_position_with_fallback(position)

    def _close_position_with_fallback(self, position: Any) -> None:
        """Submit one close, in the strongest form the evidence allows."""
        from nautilus_trader.model.enums import TimeInForce

        try:
            tracker = self.order_tracker_for(position.instrument_id)
        except Exception as exc:
            # No readable evidence is no evidence, and no evidence means the protective
            # form. This close runs on shutdown and containment paths that must not raise.
            self._log_warning(f"close fallback: could not read order state: {exc}")
            tracker = None

        attempt = plan_close_attempt(
            reduce_only_refused=bool(tracker is not None and tracker.reduce_only_refused),
            plain_close_submitted=bool(tracker is not None and tracker.plain_close_submitted),
        )
        if attempt is CloseAttempt.PLAIN:
            self._log_warning(
                f"Closing {position.instrument_id} without reduce_only: the venue refused "
                f"the reduce-only form for this position. This is the one plain attempt"
            )
        elif attempt is CloseAttempt.PLAIN_ALREADY_SPENT:
            self._log_warning(
                f"Closing {position.instrument_id} with reduce_only although the venue "
                f"refused it: the one plain attempt is spent. This will likely be refused "
                f"too, but a refused reduce-only order cannot open a reverse position"
            )

        self.close_position(
            position,
            reduce_only=attempt is not CloseAttempt.PLAIN,
            time_in_force=TimeInForce.IOC,
        )
        if attempt is CloseAttempt.PLAIN and tracker is not None:
            tracker.mark_plain_close_submitted()

    # ---- Emergency close (Crucible graceful-close layer) ----

    def emergency_close(self) -> None:
        """Best-effort close of all open positions on Crucible emergency.

        Market IOC. Iterate ``cache.positions_open()``; for each position first
        ``cancel_all_orders`` (so resting reduce_only orders do not consume
        capacity and cause -2022 rejection), then close it through
        ``_close_position_with_fallback`` -- reduce-only unless the venue has
        already refused that form for the position, in which case the one plain
        attempt applies. See ``plan_close_attempt``.

        best-effort + fail-safe: one position's failure does not abort the rest,
        and this method **never propagates**. docker stop is the hard fallback
        layer; closing is only the best-effort layer in front of it and must
        never delay or block shutdown on close failure. Acquiring
        ``positions_open()`` is itself within the fail-safe scope (a throw on the
        first resource-acquisition line must not escape either). One-shot: no
        retry, no tight loop.

        Subclasses such as NautilusTradingStrategy may override for finer-grained
        serialized cancel/replace.
        """
        try:
            # Materialize inside the try: if positions_open() returns a lazy
            # iterable, an iteration error is also covered by fail-safe and will
            # not escape the method.
            positions = list(self.cache.positions_open())
        except Exception as exc:
            self._log_warning(f"emergency_close: failed to read open positions: {exc}")
            return

        for pos in positions:
            # Cancel and close use separate try blocks: cancel is a best-effort
            # step to lower -2022 risk; if it fails we must still attempt the
            # close and never skip it.
            try:
                self.cancel_all_orders(pos.instrument_id)
            except Exception as exc:
                self._log_warning(f"emergency_close cancel {pos.instrument_id} failed: {exc}")
            try:
                self._close_position_with_fallback(pos)
            except Exception as exc:
                self._log_warning(f"emergency_close {pos.instrument_id} failed: {exc}")

    # ---- Logging helpers (self.log may be unavailable in tests) ----

    def _log_warning(self, msg: str) -> None:
        log = getattr(self, "log", None)
        if log is not None and hasattr(log, "warning"):
            try:
                log.warning(msg)
                return
            except Exception:
                # Framework log raised: fall through to the module logger below.
                pass
        _logger.warning(msg)

    def _log_error(self, msg: str) -> None:
        log = getattr(self, "log", None)
        if log is not None and hasattr(log, "error"):
            try:
                log.error(msg)
                return
            except Exception:
                # Framework log raised: fall through to the module logger below.
                pass
        _logger.error(msg)
