"""Snapshot component.

Holds the snapshot lifecycle: state serialization (``save_state``, the framework
``on_save`` body), decode + stash (``load_state``, the ``on_load`` body), the
layered restore orchestrator
(``apply_loaded_snapshot``: Layer 1 framework on_load snapshot + Layer 2 YAML
config-embedded warmup), and the YAML warmup helper. Injects a strategy reference
(same pattern as the other coordinators) and reaches the strategy fields/hooks
(``_contexts`` / ``_loaded_snapshot`` / ``_snapshot_restored`` / ``_warmup_manager``,
``clock`` / ``log`` / ``_get_warmup_config`` and the snapshot hooks
``get_snapshot_state`` / ``get_snapshot_indicators`` / ``restore_from_snapshot``)
through it.

Stays on the Strategy class: ``on_save`` / ``on_load`` are framework (Actor)
callbacks dispatched by name, so the thin try/except shells stay there and delegate
here; ``get_snapshot_indicators`` / ``get_snapshot_state`` / ``restore_from_snapshot``
are hooks subclasses override; ``_loaded_snapshot`` / ``_snapshot_restored`` are state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from nautilus_trader.common import LogColor

from custos_toolkit_nautilus.adapter.state_persistence import (
    build_snapshot,
    decode_snapshot,
    encode_snapshot,
    restore_indicators,
)

if TYPE_CHECKING:
    from custos_toolkit.warmup import WarmupConfig

    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class SnapshotCoordinator:
    """Snapshot lifecycle (save / load / layered restore).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def save_state(self) -> dict[str, bytes]:
        """Serialize strategy state to dict[str, bytes] (framework on_save body).

        Collects per-pair indicator snapshots (``ctx.indicators``) + global state
        (``get_snapshot_state``) into the v2 snapshot structure, msgspec-encoded.

        Best-effort: a serialization failure is logged by the on_save shell and
        yields an empty state rather than crashing the framework persistence cycle.
        """
        s = self._strategy
        ts = s.clock.timestamp_ns() if s.clock else 0
        snapshot = build_snapshot(
            s._contexts,
            s.get_snapshot_state(),
            f"{s.__class__.__name__}-multi",
            ts,
            logger=s.log,
        )
        return encode_snapshot(snapshot)

    def load_state(self, state: dict[str, bytes]) -> None:
        """Decode + stash the loaded snapshot for application in on_start.

        Native ``Actor.on_load`` runs during kernel build (before ``on_start``), so
        per-pair contexts do not exist yet. We decode and stash the snapshot here;
        ``apply_loaded_snapshot()`` applies it once contexts/indicators are built.
        """
        s = self._strategy
        snapshot = decode_snapshot(state)
        if snapshot is None:
            return
        s._loaded_snapshot = snapshot
        s.log.info(
            "Framework state loaded; will apply after contexts are built",
            color=LogColor.GREEN,
        )

    def apply_loaded_snapshot(self) -> int | None:
        """Apply the on_load-stashed snapshot to per-pair indicators + global state.

        Must be called from ``on_start`` after contexts/indicators are built. Only
        applies in warmup ``mode == "snapshot"`` (the mode that opts into
        snapshot-based warmup acceleration), preserving the legacy four-path
        behaviour (snapshot/warmup/none/validate).

        Returns:
            Snapshot timestamp (nanoseconds) to drive ``request_bars(start=ts)``
            warmup acceleration, or None when no snapshot applies.
        """
        s = self._strategy
        warmup_config = s._get_warmup_config()
        mode = warmup_config.mode if warmup_config else None
        if mode != "snapshot":
            return None
        assert warmup_config is not None

        timestamp: int | None = None

        # Layer 1: framework on_load snapshot (Redis state) -- per-pair indicators +
        # global state. Only drive warmup acceleration when indicators were actually
        # restored (0 restored must NOT skip warmup).
        snapshot = s._loaded_snapshot
        if snapshot is not None:
            restored = restore_indicators(s._contexts, snapshot, logger=s.log)
            global_state = cast(dict[str, object], snapshot.get("global_state", {}))
            if global_state:
                try:
                    s.restore_from_snapshot({"state": global_state})
                except Exception as exc:
                    s.log.warning(f"Failed to restore global state: {exc}")
            if restored > 0:
                timestamp = cast(int | None, snapshot.get("timestamp"))
                s.log.info(
                    f"Applied loaded snapshot: {restored} indicators restored",
                    color=LogColor.GREEN,
                )

        # Layer 2: when the on_load Redis snapshot restored no indicator, warm from
        # the YAML config-embedded snapshot (IndicatorWarmer + load_snapshot). This
        # layer is orthogonal to the framework on_load path.
        if timestamp is None:
            timestamp = self._warm_indicators_from_yaml(warmup_config)

        if timestamp is not None:
            s._snapshot_restored = True
            # Load post-restore checkpoints for validation.
            if s._warmup_manager:
                s._warmup_manager.load_pending_checkpoints()
        return timestamp

    def _warm_indicators_from_yaml(self, warmup_config: WarmupConfig) -> int | None:
        """Warm flat indicators from a YAML config snapshot.

        Uses the ``get_snapshot_indicators()`` hook + each indicator's
        ``load_snapshot`` via ``IndicatorWarmer``. Orthogonal to the framework
        on_load Redis path -- this warms indicators from config-embedded (e.g.
        TradingView-exported) snapshot values.

        Returns:
            Snapshot timestamp (nanoseconds) if any indicator was warmed, else None.
        """
        from custos_toolkit.warmup import IndicatorWarmer, SnapshotSupport

        s = self._strategy
        indicators = s.get_snapshot_indicators()
        if not indicators:
            return None

        warmer = IndicatorWarmer(warmup_config)
        timestamp = None
        for name, indicator in indicators.items():
            if not hasattr(indicator, "load_snapshot"):
                continue
            result = warmer.warm_indicator(cast(SnapshotSupport, indicator), indicator_type=name)
            if result.success and result.snapshot_time:
                timestamp = int(result.snapshot_time.timestamp() * 1e9)
                s.log.debug(f"Indicator {name} warmed from YAML snapshot")
        return timestamp
