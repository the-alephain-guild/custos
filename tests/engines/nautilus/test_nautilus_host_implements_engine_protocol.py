"""Assert both shipped NT hosts implement ExecutionEngineProtocol."""

from __future__ import annotations

from custos.core.engine_protocol import ExecutionEngineProtocol
from custos.engines.nautilus.host import NtTradingNodeHost, SandboxSimulationHost
from custos.offline.safety import OfflineSafetyEngine


def _nt_host() -> NtTradingNodeHost:
    return NtTradingNodeHost(
        tenant_id="test",
        runner_id="r1",
    )


def test_sandbox_simulation_host_implements_protocol() -> None:
    assert isinstance(SandboxSimulationHost(), ExecutionEngineProtocol)


def test_nt_trading_node_host_implements_protocol() -> None:
    assert isinstance(_nt_host(), ExecutionEngineProtocol)


def test_sandbox_simulation_host_can_be_guarded_by_the_offline_lane() -> None:
    """The guard ends a latched deployment, so its engine must be endable."""

    assert isinstance(SandboxSimulationHost(), OfflineSafetyEngine)


def test_nt_trading_node_host_can_be_guarded_by_the_offline_lane() -> None:
    assert isinstance(_nt_host(), OfflineSafetyEngine)
