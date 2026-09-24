"""Warmup component.

Holds the warmup lifecycle: warmup manager init, historical-data request, per-bar
warmup gate (buffer during warmup, short-circuit until ready), buffered-bar
replay, and the one-shot completion gate. Injects a strategy reference and reaches
``_warmup_manager`` / ``_warmup_complete_called`` (strategy fields), ``_contexts``,
``config`` / ``request_bars`` / ``clock`` / ``log`` and the strategy-side helper
``_get_warmup_config``; warmup acceleration reads the staged snapshot via
``_snapshot_coordinator.apply_loaded_snapshot()`` through it.

Stays on the Strategy class: ``on_warmup_complete`` and ``on_indicator_update`` are
hooks (subclasses override them); ``on_historical_data`` is the nautilus callback
(it delegates here); ``_get_warmup_config`` stays on the strategy. Snapshot restore
lives in SnapshotCoordinator (``apply_loaded_snapshot``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, cast

import pandas as pd
from nautilus_trader.common import LogColor
from nautilus_trader.model import Bar

from custos_toolkit_nautilus.adapter.utils import get_bar_duration_ns
from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


def _history_start(timestamp_ns: int) -> datetime:
    """The datetime a history request starts from, for a nanosecond timestamp.

    datetime holds microseconds, so the timestamp is floored to the microsecond
    before conversion; pandas would otherwise warn on every nonzero nanosecond
    remainder. Flooring keeps the start at or before the requested instant.
    """
    microseconds = timestamp_ns - timestamp_ns % 1_000
    return pd.Timestamp(microseconds, unit="ns", tz="UTC").to_pydatetime()


class _InitializedIndicator(Protocol):
    @property
    def initialized(self) -> bool: ...


class WarmupCoordinator:
    """Warmup lifecycle (warmup gate, buffered-bar replay, completion gate).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def init_manager(self) -> None:
        """Initialize warmup manager (bar buffering + checkpoint validation).

        State persistence is delegated to the framework on_save/on_load; the
        WarmupManager no longer holds a SnapshotManager.
        """
        s = self._strategy
        # Use "multi" suffix for strategy ID
        strategy_id = f"{s.__class__.__name__}-multi"

        warmup_config = s._get_warmup_config()
        if warmup_config:
            s._warmup_manager = WarmupManager(
                warmup_config=warmup_config,
                strategy_callbacks=s,
                logger=s.log,
                strategy_id=strategy_id,
                contexts=s._contexts,  # Pass contexts for multi-pair support
            )

    def request_historical_data(self) -> None:
        """Request historical data for all pairs.

        Warmup acceleration relies on the snapshot the framework staged at on_load
        (applied via ``SnapshotCoordinator.apply_loaded_snapshot``), not
        ``WarmupManager.try_layered_warmup()``.
        """
        s = self._strategy
        warmup_config = s._get_warmup_config()
        snapshot_timestamp = s._snapshot_coordinator.apply_loaded_snapshot()

        for _, ctx in s._contexts.items():
            # Only a pair whose indicators were actually restored/warmed (so it is
            # already warmed up) may ride the snapshot timestamp. A pair absent from
            # the snapshot (e.g. newly added pair, or one whose indicators failed to
            # serialize) must request a full history window or it would receive too few
            # bars and stay stuck in warmup forever (partial-restore regression).
            if snapshot_timestamp and self.check_pair_warmup(ctx):
                s.request_bars(ctx.bar_type, start=_history_start(snapshot_timestamp))
            elif warmup_config and warmup_config.mode in ("warmup", "snapshot"):
                bar_duration_ns = get_bar_duration_ns(ctx.bar_type)
                current_ns = s.clock.timestamp_ns()
                start_ns = current_ns - (warmup_config.preferred_bars * bar_duration_ns)
                s.request_bars(ctx.bar_type, start=_history_start(start_ns))

        # Log warmup info once
        if snapshot_timestamp:
            s.log.info(f"Requested historical bars from snapshot for {len(s._contexts)} pairs")
        elif warmup_config and warmup_config.mode == "warmup":
            s.log.info(
                f"Requested {warmup_config.preferred_bars} historical bars "
                f"for {len(s._contexts)} pairs"
            )

    def handle_warmup_gate(self, ctx: PairContext, bar: Bar) -> bool:
        """Run the per-pair warmup gate before the signal pipeline.

        Returns True when ``bar`` was buffered because the pair is still warming up
        and the caller must short-circuit; False when the pair is ready and the
        pipeline should continue. Always re-checks the one-shot ready signal so
        warmup.mode=none (no historical replay) still writes the ready file.
        """
        s = self._strategy
        if not ctx.warmed_up:
            if self.check_pair_warmup(ctx):
                ctx.warmed_up = True
                s.log.info(f"[{ctx.pair}] Warmup complete", color=LogColor.GREEN)

                # Mark warmup complete and replay buffered bars
                if s._warmup_manager:
                    s._warmup_manager.mark_warmup_complete()
                    self.replay_buffered_bars(ctx.pair, ctx)
            else:
                # Buffer live bars during warmup (to avoid indicator gaps)
                if s._warmup_manager:
                    s._warmup_manager.buffer_bar(bar)
                return True

        # Ready signal: write the ready file once all indicators are initialized
        # (one-shot, internally short-circuited). For warmup.mode=none this is the
        # only write path (no historical replay).
        self.maybe_mark_complete()
        return False

    def check_pair_warmup(self, ctx: PairContext) -> bool:
        """Check if all of the pair's indicators are warmed up."""
        for indicator in ctx.indicators.values():
            if (
                hasattr(indicator, "initialized")
                and not cast(_InitializedIndicator, indicator).initialized
            ):
                return False
        return True

    def replay_buffered_bars(self, pair: str, ctx: PairContext) -> None:
        """Replay buffered bars to update indicators after warmup.

        During warmup, live bars that arrive via on_bar are buffered. After warmup
        completes, we replay only bars belonging to this pair to update indicators
        and avoid gaps in indicator calculations. The buffer is kept intact until
        all pairs are warmed up, so each pair can replay its own bars independently.

        Args:
            pair: Trading pair name
            ctx: PairContext for the pair
        """
        s = self._strategy
        if not s._warmup_manager:
            return

        # Peek at buffered bars without clearing (other pairs may still need them)
        buffered_bars = s._warmup_manager.peek_buffered_bars()
        if not buffered_bars:
            return

        # Filter bars belonging to this pair only (avoid cross-pair contamination)
        pair_bars = [b for b in buffered_bars if b.bar_type.instrument_id == ctx.instrument_id]
        if not pair_bars:
            return

        s.log.info(f"[{pair}] Replaying {len(pair_bars)} buffered bars to update indicators")

        for bar in pair_bars:
            # Update strategy-specific indicators via hook
            s.on_indicator_update(ctx, bar)

        # Clear buffer once all pairs are warmed up
        if all(c.warmed_up for c in s._contexts.values()):
            s._warmup_manager.clear_buffered_bars()

    def handle_historical_data(self, data: object) -> None:
        """Handle historical bar data received from request_bars().

        NautilusTrader automatically feeds bars to registered indicators. This also
        performs checkpoint validation during historical bar replay (timestamp matching).
        """
        s = self._strategy
        # Checkpoint validation during historical data replay
        if s._warmup_manager and isinstance(data, Bar):
            all_indicators = {}
            for c in s._contexts.values():
                all_indicators.update(c.indicators)
            s._warmup_manager.validate_on_historical_bar(data, all_indicators)

        # Only call on_warmup_complete once when all indicators are actually initialized
        self.maybe_mark_complete()

    def maybe_mark_complete(self) -> None:
        """Mark warmup complete (once) when all indicators are initialized.

        Called from two paths: ``handle_historical_data`` (snapshot/warmup-mode
        historical replay) and ``handle_warmup_gate`` (live bar). For warmup.mode=none
        there is no historical replay, so the ready file must be written by the live
        bar path -- otherwise sidecar readiness stays False, financial-metrics
        collection is gated off, and the strategy is marked degraded after 600s.
        """
        s = self._strategy
        if not s._warmup_complete_called:
            if self._check_all_indicators_initialized():
                s._warmup_complete_called = True
                s.on_warmup_complete()

    def _check_all_indicators_initialized(self) -> bool:
        """Check if all registered indicators across all pairs are initialized."""
        for ctx in self._strategy._contexts.values():
            for indicator in ctx.indicators.values():
                if hasattr(indicator, "initialized") and not indicator.initialized:
                    return False
        return True
