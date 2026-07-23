"""Assert both shipped NT hosts implement ExecutionEngineProtocol."""

from __future__ import annotations

from custos.core.engine_protocol import ExecutionEngineProtocol
from custos.engines.nautilus.host import NtTradingNodeHost, SandboxSimulationHost


def test_sandbox_simulation_host_implements_protocol() -> None:
    assert isinstance(SandboxSimulationHost(), ExecutionEngineProtocol)


def test_nt_trading_node_host_implements_protocol() -> None:
    host = NtTradingNodeHost(
        tenant_id="test",
        runner_id="r1",
    )
    assert isinstance(host, ExecutionEngineProtocol)
