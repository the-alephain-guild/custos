from __future__ import annotations

from custos.engines.nautilus.runner_safety import GuardedLiveExecutionClient


class _InnerClient:
    def __init__(self) -> None:
        self.is_connected = False
        self.account_id = "BINANCE-001"

    def connect(self) -> None:
        self.is_connected = True

    def disconnect(self) -> None:
        self.is_connected = False


class _LifecycleHarness:
    def __init__(self) -> None:
        self._inner = _InnerClient()
        self.account_id = None

    def _set_account_id(self, account_id: str) -> None:
        self.account_id = account_id


async def test_guarded_client_uses_native_live_client_connection_lifecycle() -> None:
    assert "connect" not in GuardedLiveExecutionClient.__dict__
    assert "disconnect" not in GuardedLiveExecutionClient.__dict__
    assert "is_connected" not in GuardedLiveExecutionClient.__dict__
    assert "account_id" not in GuardedLiveExecutionClient.__dict__

    facade = _LifecycleHarness()
    await GuardedLiveExecutionClient._connect(facade)  # type: ignore[arg-type]

    assert facade._inner.is_connected is True
    assert facade.account_id == "BINANCE-001"

    await GuardedLiveExecutionClient._disconnect(facade)  # type: ignore[arg-type]
    assert facade._inner.is_connected is False
