"""Tests for block 2a: native on_save/on_load alongside SnapshotManager.

The framework's Actor.on_save() and on_load() lifecycle hooks
replace the SnapshotManager state-serialization subsystem. This task adds them
*alongside* the legacy path and proves the produced snapshot is equivalent
(behavioural equivalence had to be shown before the old path was deleted).

The serialize/restore core lives in shared.nautilus.state_persistence as pure
functions, so round-trip and equivalence are directly testable without
instantiating the Cython-backed Strategy.
"""

import inspect

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.state_persistence import (  # noqa: E402
    build_snapshot,
    decode_snapshot,
    encode_snapshot,
    restore_indicators,
)


class _FakeIndicator:
    """Minimal indicator implementing to_snapshot/from_snapshot."""

    def __init__(self, value: float):
        self.value = value

    def to_snapshot(self) -> dict:
        return {"value": self.value}

    def from_snapshot(self, data: dict) -> None:
        self.value = data["value"]


class _NoSnapshotIndicator:
    """Indicator without snapshot support (should be skipped)."""

    def __init__(self, value: float):
        self.value = value


class _FakeCtx:
    def __init__(self, indicators: dict, pair: str = "BTCUSDT"):
        self.indicators = indicators
        self.pair = pair


def _contexts() -> dict:
    # _contexts is keyed by InstrumentId. A string placeholder is fine here, because
    # state_persistence walks values() and groups the snapshot by ctx.pair.
    return {
        "BTCUSDT": _FakeCtx({"atr": _FakeIndicator(100.5), "st": _FakeIndicator(42000.0)}),
        "ETHUSDT": _FakeCtx({"atr": _FakeIndicator(5.25)}, "ETHUSDT"),
    }


class TestOnSaveOnLoadOverride:
    """NautilusTradingStrategy must override the framework lifecycle hooks."""

    def test_on_save_is_overridden(self):
        """on_save is the framework callback shell; body delegates to SnapshotCoordinator."""
        from custos_toolkit_nautilus.adapter.coordinators import SnapshotCoordinator
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert "on_save" in NautilusTradingStrategy.__dict__, (
            "NautilusTradingStrategy must override on_save"
        )
        src = inspect.getsource(NautilusTradingStrategy.on_save)
        assert "_snapshot_coordinator.save_state" in src, (
            "on_save should delegate to the coordinator"
        )
        coord_src = inspect.getsource(SnapshotCoordinator.save_state)
        assert "encode_snapshot" in coord_src, "save_state should encode via state_persistence"

    def test_on_load_is_overridden(self):
        """on_load is the framework callback shell; body delegates to SnapshotCoordinator."""
        from custos_toolkit_nautilus.adapter.coordinators import SnapshotCoordinator
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        assert "on_load" in NautilusTradingStrategy.__dict__, (
            "NautilusTradingStrategy must override on_load"
        )
        src = inspect.getsource(NautilusTradingStrategy.on_load)
        assert "_snapshot_coordinator.load_state" in src, (
            "on_load should delegate to the coordinator"
        )
        # load_state runs before on_start (contexts empty), so it only DECODES + STASHES;
        # the actual indicator/global restore happens in apply_loaded_snapshot.
        coord_src = inspect.getsource(SnapshotCoordinator.load_state)
        assert "decode_snapshot" in coord_src, "load_state should decode the framework state"
        assert "_loaded_snapshot" in coord_src, "load_state should stash the decoded snapshot"

    def test_apply_loaded_snapshot_restores(self):
        from custos_toolkit_nautilus.adapter.coordinators import SnapshotCoordinator

        src = inspect.getsource(SnapshotCoordinator.apply_loaded_snapshot)
        assert "restore_indicators" in src, (
            "apply_loaded_snapshot should restore per-pair indicators"
        )
        assert "restore_from_snapshot" in src, (
            "apply_loaded_snapshot should restore global state via the hook"
        )


class TestSnapshotRoundTrip:
    """save -> encode -> decode -> load must restore indicator + global state."""

    def test_round_trip_restores_indicator_state(self):
        contexts = _contexts()
        global_state = {"prev_trend": 1, "entry_count": 3}

        snap = build_snapshot(contexts, global_state, "MyStrat-multi", 123)
        state = encode_snapshot(snap)
        assert set(state.keys()) == {"state"}
        assert isinstance(state["state"], bytes)

        # Corrupt live indicator values to prove restore actually rewrites them
        for ctx in contexts.values():
            for ind in ctx.indicators.values():
                ind.value = -1.0

        decoded = decode_snapshot(state)
        n = restore_indicators(contexts, decoded)

        assert n == 3, "all 3 snapshot-capable indicators must be restored"
        assert contexts["BTCUSDT"].indicators["atr"].value == 100.5
        assert contexts["BTCUSDT"].indicators["st"].value == 42000.0
        assert contexts["ETHUSDT"].indicators["atr"].value == 5.25
        assert decoded["global_state"] == global_state
        assert decoded["version"] == 2
        assert decoded["timestamp"] == 123

    def test_indicator_without_snapshot_is_skipped(self):
        contexts = {"BTCUSDT": _FakeCtx({"plain": _NoSnapshotIndicator(7.0)})}
        snap = build_snapshot(contexts, {}, "S-multi", 1)
        assert snap["pairs"]["BTCUSDT"]["indicators"] == {}

    def test_decode_empty_state_returns_none(self):
        assert decode_snapshot({}) is None
        assert decode_snapshot({"state": b""}) is None
        assert decode_snapshot(None) is None


# NOTE: the legacy-equivalence test (build_snapshot == WarmupManager.save_snapshot)
# was a transitional gate: it proved on_save produced the same snapshot
# structure before SnapshotManager and WarmupManager.save_snapshot were deleted.
# With the legacy path now removed, the gate has served its purpose and is dropped.


# NOTE: the snapshot-persistence-gap warning moved into
# NautilusTradingStrategyConfig.validation_warnings(). Its regression assertions live in
# test_config_self_validation.py::test_warnings_snapshot_without_db_is_warning and siblings.
