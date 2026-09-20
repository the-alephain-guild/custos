"""State persistence helpers for NautilusTrader strategy on_save/on_load.

NautilusTrader's native ``Actor.on_save()``/``on_load()``
lifecycle hooks replace the SnapshotManager state-serialization subsystem. These
pure functions build and restore the v2 snapshot structure (the same shape
``WarmupManager.save_snapshot`` produced) so framework state persistence becomes
the single source of truth.

Kept as module-level pure functions (no Strategy/Cython dependency) so the
round-trip and equivalence behaviour is directly unit-testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, cast

import msgspec

if TYPE_CHECKING:
    from nautilus_trader.model import InstrumentId

    from .pair_context import PairContext


class _Logger(Protocol):
    def warning(self, message: str) -> None: ...


class _SnapshotIndicator(Protocol):
    def to_snapshot(self) -> object: ...


class _RestoreIndicator(Protocol):
    def from_snapshot(self, snapshot: object) -> None: ...


# v2 snapshot schema version (kept identical to the legacy WarmupManager format
# so on_save output is byte-equivalent for the same indicator state).
SNAPSHOT_VERSION = 2

# Key under which the serialized snapshot lives in the framework state dict.
STATE_KEY = "state"


def build_snapshot(
    contexts: Mapping[InstrumentId, PairContext],
    global_state: dict[str, object],
    strategy_id: str,
    timestamp: int,
    logger: _Logger | None = None,
    risk_state: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Build a v2 snapshot dict from per-pair contexts + global strategy state.

    Mirrors the structure ``WarmupManager.save_snapshot`` produced so the new
    on_save path is structurally equivalent for the same indicator state.

    Args:
        contexts: InstrumentId -> PairContext map (uses ``ctx.pair`` + ``ctx.indicators``).
        global_state: Strategy-level state (from ``get_snapshot_state()``).
        strategy_id: Snapshot owner id (e.g. ``"MyStrat-multi"``).
        timestamp: Snapshot time in nanoseconds.
        logger: Optional logger for per-indicator snapshot failures.

    Returns:
        v2 snapshot dict (json-encodable).
    """
    pairs_data: dict[str, object] = {}
    # contexts is keyed by InstrumentId, but the snapshot is still organized by pair
    # string (via ctx.pair) so old snapshots stay restorable after the key change.
    for ctx in contexts.values():
        pair = ctx.pair
        indicator_snapshots: dict[str, object] = {}
        for name, indicator in ctx.indicators.items():
            if hasattr(indicator, "to_snapshot"):
                try:
                    indicator_snapshots[name] = cast(_SnapshotIndicator, indicator).to_snapshot()
                except Exception as exc:  # pragma: no cover - defensive
                    if logger is not None:
                        logger.warning(f"[{pair}] Failed to snapshot indicator {name}: {exc}")
        pairs_data[pair] = {"indicators": indicator_snapshots, "state": {}}

    return {
        "timestamp": timestamp,
        "strategy_id": strategy_id,
        "version": SNAPSHOT_VERSION,
        "global_state": global_state,
        # Risk state gets its own section rather than riding in global_state.
        # That section comes from get_snapshot_state(), a hook strategy authors
        # override; risk limits are not theirs to drop by overriding it, and the
        # daily budget must survive a restart whether or not indicator warmup is
        # being persisted at all.
        "risk": dict(risk_state) if risk_state else {},
        "pairs": pairs_data,
    }


def restore_indicators(
    contexts: Mapping[InstrumentId, PairContext],
    snapshot: dict[str, object],
    logger: _Logger | None = None,
) -> int:
    """Restore per-pair indicator state from a v2 snapshot dict.

    Global strategy state restore is the caller's responsibility (it routes
    through the strategy's ``restore_from_snapshot`` hook).

    Args:
        contexts: InstrumentId -> PairContext map (uses ``ctx.pair`` + ``ctx.indicators``).
        snapshot: v2 snapshot dict (from ``decode_snapshot``).
        logger: Optional logger for per-indicator restore failures.

    Returns:
        Number of indicators successfully restored.
    """
    pairs_data = cast(dict[str, object], snapshot.get("pairs", {}))
    restored_count = 0
    # contexts is keyed by InstrumentId; the snapshot is organized by pair string,
    # so match via ctx.pair.
    for ctx in contexts.values():
        pair = ctx.pair
        pair_snapshot = cast(dict[str, object] | None, pairs_data.get(pair))
        if pair_snapshot is None:
            continue
        indicators_data = cast(dict[str, object], pair_snapshot.get("indicators", {}))
        for name, indicator in ctx.indicators.items():
            if name in indicators_data and hasattr(indicator, "from_snapshot"):
                try:
                    cast(_RestoreIndicator, indicator).from_snapshot(indicators_data[name])
                    restored_count += 1
                except Exception as exc:  # pragma: no cover - defensive
                    if logger is not None:
                        logger.warning(f"[{pair}] Failed to restore {name}: {exc}")
    return restored_count


def encode_snapshot(snapshot: dict[str, object]) -> dict[str, bytes]:
    """Encode a v2 snapshot dict to the framework state dict[str, bytes]."""
    return {STATE_KEY: msgspec.json.encode(snapshot)}


def decode_snapshot(state: dict[str, bytes] | None) -> dict[str, object] | None:
    """Decode the framework state dict[str, bytes] back to a v2 snapshot dict.

    Returns None when the state is empty/missing (e.g. first start, no prior
    snapshot), so callers can cleanly skip restore.
    """
    raw = state.get(STATE_KEY) if state else None
    if not raw:
        return None
    return msgspec.json.decode(raw, type=dict[str, object])
