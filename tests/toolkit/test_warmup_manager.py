# tests/test_warmup_manager.py
"""Tests for WarmupManager.

Snapshot state serialization moved to the framework's on_save and on_load
path; WarmupManager no longer owns a SnapshotManager. It retains only bar buffering
and post-restore checkpoint validation, covered here.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")


class TestWarmupStrategyCallbacks:
    """Tests for WarmupStrategyCallbacks protocol."""

    def test_protocol_exists(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupStrategyCallbacks

        assert WarmupStrategyCallbacks is not None

    def test_protocol_is_runtime_checkable(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupStrategyCallbacks

        class MockStrategy:
            def get_snapshot_indicators(self) -> dict:
                return {}

            def get_snapshot_state(self) -> dict:
                return {}

            def restore_from_snapshot(self, snapshot: dict) -> bool:
                return True

        mock = MockStrategy()
        assert isinstance(mock, WarmupStrategyCallbacks)


class TestWarmupManagerInit:
    """Tests for WarmupManager initialization (no snapshot_manager param)."""

    def test_creation(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        warmup_config = MagicMock()
        callbacks = MagicMock()
        logger = MagicMock()

        manager = WarmupManager(
            warmup_config=warmup_config,
            strategy_callbacks=callbacks,
            logger=logger,
            strategy_id="test-strategy",
        )

        assert manager._warmup_config == warmup_config
        assert manager._callbacks == callbacks
        assert not hasattr(manager, "_snapshot_manager")

    def test_creation_with_none_config(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        manager = WarmupManager(
            warmup_config=None,
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
        )

        assert manager._warmup_config is None


class TestWarmupManagerContexts:
    """Tests for WarmupManager contexts parameter (multi-pair support)."""

    def test_accepts_contexts_parameter(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        contexts = {"BTC-USDT": MagicMock(), "ETH-USDT": MagicMock()}

        manager = WarmupManager(
            warmup_config=MagicMock(),
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
            contexts=contexts,
        )

        assert manager._contexts == contexts

    def test_contexts_defaults_to_empty_dict(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        manager = WarmupManager(
            warmup_config=MagicMock(),
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
        )

        assert manager._contexts == {}

    def test_contexts_none_becomes_empty_dict(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        manager = WarmupManager(
            warmup_config=MagicMock(),
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
            contexts=None,
        )

        assert manager._contexts == {}

    def test_contexts_is_stored_reference(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        contexts = {"BTC-USDT": MagicMock()}

        manager = WarmupManager(
            warmup_config=MagicMock(),
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
            contexts=contexts,
        )

        assert manager._contexts is contexts
        contexts["ETH-USDT"] = MagicMock()
        assert "ETH-USDT" in manager._contexts


class TestLoadPendingCheckpoints:
    """Checkpoint loading is now triggered by the strategy after on_load restores."""

    def _manager(self, warmup_config):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        return WarmupManager(
            warmup_config=warmup_config,
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
        )

    def test_loads_configured_checkpoints(self):
        cfg = MagicMock()
        cfg.checkpoints.points = ["cp1", "cp2", "cp3"]
        manager = self._manager(cfg)

        manager.load_pending_checkpoints()

        assert manager._pending_checkpoints == ["cp1", "cp2", "cp3"]

    def test_no_config_is_noop(self):
        manager = self._manager(None)
        manager.load_pending_checkpoints()
        assert manager._pending_checkpoints == []

    def test_empty_points_is_noop(self):
        cfg = MagicMock()
        cfg.checkpoints.points = []
        manager = self._manager(cfg)
        manager.load_pending_checkpoints()
        assert manager._pending_checkpoints == []


class TestBarBuffering:
    """Bar buffering during warmup is retained (orthogonal to snapshot deletion)."""

    def _manager(self):
        from custos_toolkit_nautilus.adapter.warmup_manager import WarmupManager

        return WarmupManager(
            warmup_config=MagicMock(),
            strategy_callbacks=MagicMock(),
            logger=MagicMock(),
            strategy_id="test",
        )

    def test_buffer_and_peek_and_clear(self):
        manager = self._manager()
        b1, b2 = MagicMock(), MagicMock()

        manager.buffer_bar(b1)
        manager.buffer_bar(b2)

        assert manager.peek_buffered_bars() == [b1, b2]
        # peek must not clear
        assert manager.peek_buffered_bars() == [b1, b2]

        manager.clear_buffered_bars()
        assert manager.peek_buffered_bars() == []

    def test_get_buffered_bars_clears(self):
        manager = self._manager()
        b1 = MagicMock()
        manager.buffer_bar(b1)

        assert manager.get_buffered_bars() == [b1]
        assert manager.peek_buffered_bars() == [], "get must drain the buffer"

    def test_mark_warmup_complete(self):
        manager = self._manager()
        assert manager.is_warmup_complete is False
        manager.mark_warmup_complete()
        assert manager.is_warmup_complete is True
