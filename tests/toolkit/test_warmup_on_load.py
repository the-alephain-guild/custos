"""Tests for block 2b: warmup acceleration rides on on_load-stashed state.

Snapshot-driven warmup acceleration, where request_bars starts at a timestamp, moves
off WarmupManager.try_layered_warmup() onto the framework on_load path.

Critical lifecycle fact (verified against nautilus 1.228.0):
  on_load runs during kernel build (kernel.py:539 trader.load()), BEFORE on_start.
  self._contexts is populated inside on_start, so it is EMPTY at on_load time.
Therefore on_load only STASHES the decoded snapshot; SnapshotCoordinator.apply_loaded_snapshot()
applies it (per-pair indicators + global state + warmup ts) once contexts exist,
gated on warmup mode == "snapshot" to preserve the four-path behaviour.
"""

import inspect

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from datetime import UTC
from unittest.mock import MagicMock  # noqa: E402

from custos_toolkit_nautilus.adapter.coordinators import (
    SnapshotCoordinator,  # noqa: E402
    WarmupCoordinator,  # noqa: E402
)
from custos_toolkit_nautilus.adapter.state_persistence import build_snapshot  # noqa: E402


class _FakeIndicator:
    def __init__(self, value: float):
        self.value = value

    def to_snapshot(self) -> dict:
        return {"value": self.value}

    def from_snapshot(self, data: dict) -> None:
        self.value = data["value"]


class _FakeCtx:
    def __init__(self, indicators: dict, pair: str = "BTCUSDT"):
        self.indicators = indicators
        self.pair = pair


class _FakeStrategy:
    """Strategy stand-in fed to a real SnapshotCoordinator, so the assertions drive the
    real component against a stub strategy rather than rebinding moved methods)."""

    def __init__(
        self,
        mode: str,
        loaded_snapshot: dict | None,
        contexts: dict,
        warmup_config=None,
        snapshot_indicators: dict | None = None,
    ):
        self._warmup_mode = mode
        self._warmup_config = warmup_config
        self._loaded_snapshot = loaded_snapshot
        self._contexts = contexts
        self._snapshot_indicators = snapshot_indicators or {}
        self._snapshot_restored = False
        # apply_loaded_snapshot loads checkpoints on the warmup manager (if any)
        self._warmup_manager = None
        self.log = MagicMock()
        self.restored_global = None

    def _get_warmup_config(self):
        if self._warmup_config is not None:
            return self._warmup_config
        cfg = MagicMock()
        cfg.mode = self._warmup_mode
        return cfg

    def get_snapshot_indicators(self) -> dict:
        return self._snapshot_indicators

    def restore_from_snapshot(self, snapshot: dict) -> bool:
        self.restored_global = snapshot
        return True


def _snapshot(ts: int) -> dict:
    contexts = {"BTCUSDT": _FakeCtx({"atr": _FakeIndicator(100.5)})}
    return build_snapshot(contexts, {"prev_trend": 1}, "S-multi", ts), contexts


class TestApplyLoadedSnapshot:
    def test_snapshot_mode_applies_and_returns_ts(self):
        snap, _ = _snapshot(777)
        contexts = {"BTCUSDT": _FakeCtx({"atr": _FakeIndicator(-1.0)})}
        strat = _FakeStrategy("snapshot", snap, contexts)

        ts = SnapshotCoordinator(strat).apply_loaded_snapshot()

        assert ts == 777
        assert contexts["BTCUSDT"].indicators["atr"].value == 100.5
        assert strat._snapshot_restored is True
        assert strat.restored_global == {"state": {"prev_trend": 1}}

    def test_no_loaded_snapshot_returns_none(self):
        strat = _FakeStrategy("snapshot", None, {})
        assert SnapshotCoordinator(strat).apply_loaded_snapshot() is None
        assert strat._snapshot_restored is False

    def test_non_snapshot_mode_does_not_apply(self):
        snap, _ = _snapshot(777)
        contexts = {"BTCUSDT": _FakeCtx({"atr": _FakeIndicator(-1.0)})}
        strat = _FakeStrategy("warmup", snap, contexts)

        ts = SnapshotCoordinator(strat).apply_loaded_snapshot()

        assert ts is None, "non-snapshot warmup modes must not use snapshot acceleration"
        assert contexts["BTCUSDT"].indicators["atr"].value == -1.0, "indicators untouched"
        assert strat._snapshot_restored is False


class TestRequestHistoricalPerPair:
    def test_unrestored_pair_falls_back_to_full_window(self, monkeypatch):
        """#6: partial snapshot restore — pairs NOT in the snapshot must request a full
        warmup window, not the (recent) snapshot timestamp, otherwise they never warm up.
        """
        from types import SimpleNamespace

        import custos_toolkit_nautilus.adapter.coordinators.warmup as wc_mod
        import pandas as pd

        bar_ns = 60_000_000_000  # 1-minute bars
        monkeypatch.setattr(wc_mod, "get_bar_duration_ns", lambda _bt: bar_ns)

        snap_ts = 1_000_000_000_000_000
        now_ns = snap_ts + bar_ns  # clock just after the snapshot
        ctx_restored = SimpleNamespace(
            pair="AAA", bar_type="AAA-bt", indicators={"i": SimpleNamespace(initialized=True)}
        )
        ctx_fresh = SimpleNamespace(
            pair="BBB", bar_type="BBB-bt", indicators={"i": SimpleNamespace(initialized=False)}
        )
        contexts = {"AAA": ctx_restored, "BBB": ctx_fresh}

        requests: dict = {}
        strat = SimpleNamespace(
            _get_warmup_config=lambda: SimpleNamespace(mode="snapshot", preferred_bars=100),
            _snapshot_coordinator=SimpleNamespace(apply_loaded_snapshot=lambda: snap_ts),
            _contexts=contexts,
            request_bars=lambda bar_type, start: requests.__setitem__(bar_type, start),
            clock=SimpleNamespace(timestamp_ns=lambda: now_ns),
            log=MagicMock(),
        )

        WarmupCoordinator(strat).request_historical_data()

        # restored pair -> continue from the snapshot timestamp
        assert requests["AAA-bt"] == pd.Timestamp(snap_ts, unit="ns", tz="UTC").to_pydatetime()
        # un-restored pair -> full warmup window (preferred_bars back from the clock)
        expected_fresh_ns = now_ns - 100 * bar_ns
        assert (
            requests["BBB-bt"]
            == pd.Timestamp(expected_fresh_ns, unit="ns", tz="UTC").to_pydatetime()
        )


class TestOnLoadStashesOnly:
    def test_on_load_stashes_does_not_restore_directly(self):
        # on_load body moved to SnapshotCoordinator.load_state; it only DECODES + STASHES.
        src = inspect.getsource(SnapshotCoordinator.load_state)
        assert "_loaded_snapshot" in src, "load_state must stash the decoded snapshot"
        assert "restore_indicators" not in src, (
            "load_state must NOT restore indicators directly (contexts empty at on_load time)"
        )

    def test_request_historical_uses_apply_loaded_snapshot(self):
        # request_historical_data (WarmupCoordinator) still drives off the on_load-stashed
        # snapshot, now via SnapshotCoordinator.apply_loaded_snapshot.
        src = inspect.getsource(WarmupCoordinator.request_historical_data)
        assert "apply_loaded_snapshot" in src, (
            "warmup data request must drive off the on_load-stashed snapshot"
        )
        assert "_warmup_manager.try_layered_warmup" not in src, (
            "warmup acceleration should no longer call WarmupManager.try_layered_warmup"
        )

    def test_active_config_reads_strategy_snapshot_restored_flag(self):
        # The configuration log moved into ConfigSummaryLogger; active-config state is read here.
        from custos_toolkit_nautilus.adapter.coordinators import ConfigSummaryLogger

        src = inspect.getsource(ConfigSummaryLogger.log_active_config)
        assert "_snapshot_restored" in src, (
            "restored_from_snapshot status should read the strategy flag set by on_load apply"
        )
        assert "_warmup_manager._snapshot_restored" not in src, (
            "status must no longer read the WarmupManager internal flag"
        )


# ---------------------------------------------------------------------------
# Warmup acceleration must be gated on an actual restore
# ---------------------------------------------------------------------------


class TestSnapshotRestoreGating:
    def test_no_indicators_restored_returns_none(self):
        # snapshot present, but contexts empty -> restore_indicators == 0 -> no acceleration
        snap, _ = _snapshot(777)
        strat = _FakeStrategy("snapshot", snap, contexts={})

        ts = SnapshotCoordinator(strat).apply_loaded_snapshot()

        assert ts is None, "0 indicators restored must NOT trigger warmup acceleration"
        assert strat._snapshot_restored is False

    def test_indicators_restored_sets_flag_and_ts(self):
        snap, _ = _snapshot(777)
        contexts = {"BTCUSDT": _FakeCtx({"atr": _FakeIndicator(-1.0)})}
        strat = _FakeStrategy("snapshot", snap, contexts)

        ts = SnapshotCoordinator(strat).apply_loaded_snapshot()

        assert ts == 777
        assert strat._snapshot_restored is True

    def test_successful_restore_loads_pending_checkpoints(self):
        # On a successful restore (timestamp set), the warmup manager's
        # post-restore checkpoint validation must be loaded (guards the call from
        # being dropped in a future refactor -- codex LOW).
        snap, _ = _snapshot(777)
        contexts = {"BTCUSDT": _FakeCtx({"atr": _FakeIndicator(-1.0)})}
        strat = _FakeStrategy("snapshot", snap, contexts)
        strat._warmup_manager = MagicMock()

        SnapshotCoordinator(strat).apply_loaded_snapshot()

        strat._warmup_manager.load_pending_checkpoints.assert_called_once()

    def test_no_restore_does_not_load_checkpoints(self):
        # No snapshot -> no restore -> checkpoints must NOT be loaded.
        strat = _FakeStrategy("snapshot", None, {})
        strat._warmup_manager = MagicMock()

        SnapshotCoordinator(strat).apply_loaded_snapshot()

        strat._warmup_manager.load_pending_checkpoints.assert_not_called()


# ---------------------------------------------------------------------------
# The YAML config-snapshot warmup layer is restored
# ---------------------------------------------------------------------------


class _MockSnapshotIndicator:
    def __init__(self):
        self.value = 0.0
        self.trend = 0
        self._snapshot_loaded = False

    def load_snapshot(self, values: dict) -> None:
        self.value = values.get("value", 0.0)
        self.trend = int(values.get("trend", 0))
        self._snapshot_loaded = True

    def export_snapshot(self) -> dict:
        return {"value": self.value, "trend": float(self.trend)}


def _yaml_warmup_config():
    from datetime import datetime

    from custos_toolkit.warmup.snapshot import IndicatorSnapshot, WarmupConfig

    snapshot = IndicatorSnapshot(
        indicator_type="supertrend",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        values={"value": 42156.78, "trend": 1.0},
    )
    return WarmupConfig(mode="snapshot", snapshot=snapshot, snapshots={"supertrend": snapshot})


class TestYamlWarmupLayer:
    def test_apply_falls_back_to_yaml_when_redis_empty(self):
        cfg = _yaml_warmup_config()
        ind = _MockSnapshotIndicator()
        strat = _FakeStrategy(
            "snapshot",
            loaded_snapshot=None,  # no Redis/on_load snapshot
            contexts={},
            warmup_config=cfg,
            snapshot_indicators={"supertrend": ind},
        )

        ts = SnapshotCoordinator(strat).apply_loaded_snapshot()

        assert ts is not None, "YAML snapshot warmup must drive acceleration when Redis is empty"
        assert ind._snapshot_loaded is True, "indicator must be warmed via load_snapshot"
        assert strat._snapshot_restored is True

    def test_warm_indicators_from_yaml_no_indicators_returns_none(self):
        strat = _FakeStrategy("snapshot", None, {}, warmup_config=_yaml_warmup_config())
        assert (
            SnapshotCoordinator(strat)._warm_indicators_from_yaml(strat._get_warmup_config())
            is None
        )

    def test_apply_calls_yaml_fallback_in_source(self):
        src = inspect.getsource(SnapshotCoordinator.apply_loaded_snapshot)
        assert "_warm_indicators_from_yaml" in src, (
            "apply_loaded_snapshot must keep the YAML warmup fallback"
        )
