from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custos.core.runner_fact import RunnerFactContractError
from custos.engines.nautilus.host import SandboxSimulationHost
from custos.engines.nautilus.sandbox_runner_fact_host import SandboxRunnerFactHost


class _CapabilityReceipt:
    def __init__(self) -> None:
        self.runner_id = uuid4()
        self.capability_version_id = uuid4()
        self.capability_version = 3
        self.manifest_digest = "c" * 64
        self.required: dict | None = None

    def require_scope_bindings(self, **bindings) -> None:
        self.required = bindings


def _spec(**overrides) -> dict:
    spec = {
        "deployment_instance_id": str(uuid4()),
        "deployment_spec_id": str(uuid4()),
        "deployment_spec_digest": "d" * 64,
        "generation": 1,
        "strategy_id": str(uuid4()),
        "trading_mode": "sandbox",
        "pairs": ["BTC-USDT"],
        "sandbox": {"starting_balances": ["10_000 USDT", "250.50 USDT"]},
    }
    spec.update(overrides)
    return spec


async def test_sandbox_host_publishes_capability_bound_initial_equity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def deploy(self, spec, credential, artifact):
        del self, spec, credential, artifact
        return SimpleNamespace(engine_handle="sandbox-engine")

    async def stop(self, deployment_instance_id):
        del self, deployment_instance_id

    monkeypatch.setattr(SandboxSimulationHost, "deploy", deploy)
    monkeypatch.setattr(SandboxSimulationHost, "stop", stop)
    capability = _CapabilityReceipt()
    host = SandboxRunnerFactHost(tenant_id="tenant-a", capability_receipt=capability)
    spec = _spec()

    await host.deploy(spec, {}, object())

    deployments = host.runner_fact_deployments()
    assert len(deployments) == 1
    assert deployments[0].deployment_instance_id == spec["deployment_instance_id"]
    assert deployments[0].currency == "USDT"
    assert deployments[0].reconciliation_available is False
    assert capability.required == {
        "projectors": ["settlement", "risk", "health"],
        "trading_mode": "sandbox",
        "deployment_instance_id": spec["deployment_instance_id"],
        "deployment_spec_id": spec["deployment_spec_id"],
        "deployment_spec_digest": spec["deployment_spec_digest"],
        "strategy_id": spec["strategy_id"],
    }
    equity, positions = await host.runner_fact_risk_snapshot(spec["deployment_instance_id"], "USDT")
    assert equity == Decimal("10250.50")
    assert positions == ()

    await host.stop(spec["deployment_instance_id"])
    assert host.runner_fact_deployments() == ()


async def test_sandbox_host_rejects_non_settlement_starting_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed = False

    async def deploy(self, spec, credential, artifact):
        nonlocal deployed
        del self, spec, credential, artifact
        deployed = True

    monkeypatch.setattr(SandboxSimulationHost, "deploy", deploy)
    host = SandboxRunnerFactHost(tenant_id="tenant-a", capability_receipt=_CapabilityReceipt())

    with pytest.raises(
        RunnerFactContractError,
        match="all sandbox starting balances must use the settlement currency",
    ):
        await host.deploy(
            _spec(sandbox={"starting_balances": ["10_000 USDT", "1 BTC"]}),
            {},
            object(),
        )

    assert deployed is False
