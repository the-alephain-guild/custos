"""Tests for warmup protocol."""


def test_snapshot_support_protocol_exists():
    """Test that SnapshotSupport protocol is defined."""
    from custos_toolkit.warmup.protocol import SnapshotSupport

    assert hasattr(SnapshotSupport, "load_snapshot")
    assert hasattr(SnapshotSupport, "export_snapshot")


def test_snapshot_support_is_protocol():
    """Test that SnapshotSupport is a Protocol."""
    from custos_toolkit.warmup.protocol import SnapshotSupport

    class MockIndicator:
        def load_snapshot(self, values: dict[str, float]) -> None:
            pass

        def export_snapshot(self) -> dict[str, float]:
            return {}

    assert isinstance(MockIndicator(), SnapshotSupport)


def test_snapshot_support_rejects_incomplete():
    """Test that incomplete implementations are rejected."""
    from custos_toolkit.warmup.protocol import SnapshotSupport

    class IncompleteIndicator:
        def load_snapshot(self, values: dict[str, float]) -> None:
            pass

    assert not isinstance(IncompleteIndicator(), SnapshotSupport)
