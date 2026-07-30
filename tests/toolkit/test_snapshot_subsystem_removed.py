"""SnapshotManager and SnapshotHelper are gone.

The framework's Actor.on_save and on_load, which are now the live path,
is the sole strategy-state persistence path. The SnapshotManager Cache-I/O layer,
the SnapshotHelper driver, and WarmupManager's snapshot-serialization methods are
deleted. WarmupManager keeps only bar buffering + checkpoint validation.
"""

import importlib

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")


class TestSnapshotModulesDeleted:
    def test_snapshot_module_gone(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("custos_toolkit_nautilus.adapter.snapshot")

    def test_snapshot_helper_module_gone(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("custos_toolkit_nautilus.adapter.snapshot_helper")


class TestExportsRemoved:
    def test_not_in_all(self):
        import custos_toolkit_nautilus.adapter as ns

        assert "SnapshotManager" not in ns.__all__
        assert "SnapshotHelper" not in ns.__all__

    def test_not_attributes(self):
        import custos_toolkit_nautilus.adapter as ns

        assert not hasattr(ns, "SnapshotManager")
        assert not hasattr(ns, "SnapshotHelper")


class TestWarmupManagerSnapshotMethodsRemoved:
    def test_snapshot_serialization_methods_gone(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        for name in (
            "save_snapshot",
            "maybe_save_snapshot",
            "try_layered_warmup",
            "try_restore_snapshot",
            "restore_from_snapshot",
        ):
            assert not hasattr(WarmupManager, name), (
                f"WarmupManager.{name} should be removed (snapshot serialization deleted)"
            )

    def test_checkpoint_and_buffering_kept(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        for name in (
            "validate_on_historical_bar",
            "buffer_bar",
            "peek_buffered_bars",
            "clear_buffered_bars",
            "mark_warmup_complete",
        ):
            assert hasattr(WarmupManager, name), (
                f"WarmupManager.{name} must be kept (orthogonal warmup feature)"
            )

    def test_constructor_drops_snapshot_manager_param(self):
        import inspect

        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        params = inspect.signature(WarmupManager.__init__).parameters
        assert "snapshot_manager" not in params, (
            "WarmupManager must no longer take a snapshot_manager"
        )


class TestCoreUsesFrameworkHooks:
    def test_core_overrides_on_save_on_load(self):
        from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore

        assert "on_save" in NautilusStrategyCore.__dict__, "Core must override on_save"
        assert "on_load" in NautilusStrategyCore.__dict__, "Core must override on_load"

    def test_core_snapshot_driver_removed(self):
        from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore

        assert not hasattr(NautilusStrategyCore, "_init_snapshot_driver"), (
            "Core SnapshotHelper driver should be replaced by on_save/on_load"
        )
