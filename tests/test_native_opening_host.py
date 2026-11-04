"""Actual signed host composition with an explicitly future native handle fixture."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.runner_fact_producer import RunnerFactObservationRetired
from custos.engines.nautilus import host as host_module
from custos.engines.nautilus.native_opening_inventory import NativeRuntimeArtifactIdentity
from tests.engines.nautilus.test_runner_safety_execution_boundary import _GatedStrategy
from tests.fixtures.fake_live_node import FakeLiveNode, FakeLiveNodeType, only_exec_client
from tests.fixtures.platform_opening import closed_platform_runtime
from tests.test_native_opening_inventory import _manifest, _response
from tests.test_nt_trading_node_host import (
    _Artifact,
    _credential,
    _fact_spec,
    _FactCapabilityReceipt,
    _FactEmitterStub,
    _StrategyDouble,
)


@pytest.fixture
async def native_host(tmp_path, monkeypatch):
    monkeypatch.setattr(host_module, "LiveNode", FakeLiveNodeType)
    handles = []

    class Capture:
        def __init__(self, node, client_id):
            assert node._handle.state == host_module.NodeState.IDLE
            name, _factory, config = only_exec_client(node)
            assert str(client_id) == name
            self.response = deepcopy(_response())
            source = self.response["capture"]["snapshot"]["source"]
            source.update(
                client_id=name,
                account_id=str(config.account_id),
                venue=str(config.venue),
                account_type=str(config.account_type),
            )
            configuration = json.dumps(
                {
                    "client_config": {
                        "account_id": source["account_id"],
                        "venue": source["venue"],
                        "account_type": source["account_type"],
                        "base_currency": None,
                    },
                    "starting_currency_definitions": [
                        {"code": "USDT", "precision": 8, "currency_type": "CRYPTO", "iso4217": 0}
                    ],
                    "base_currency_definition": None,
                }
            )
            source.update(
                configuration_json=configuration,
                configuration_sha256=sha256(configuration.encode()).hexdigest(),
            )
            self.calls = 0
            self.entered = asyncio.Event()
            self.resume = asyncio.Event()
            self.resume.set()

        def source_json(self):
            return json.dumps(self.response["capture"]["snapshot"]["source"])

        async def capture_json(self, timeout_ms=1000):
            self.calls += 1
            self.entered.set()
            await self.resume.wait()
            self.response["capture"]["capture_sequence"] = self.calls
            return json.dumps(self.response)

    def capture_handle(node, client_id):
        capture = Capture(node, client_id)
        handles.append(capture)
        return capture

    monkeypatch.setattr(FakeLiveNode, "account_capture_handle", capture_handle, raising=False)
    spec = _fact_spec(
        "native-opening", "binance", trading_mode="sandbox", strategy_id=str(UUID(int=9))
    )
    runtime = await closed_platform_runtime(tmp_path / "native.sqlite3", spec)
    receipt = _FactCapabilityReceipt()
    receipt.runner_id = runtime.verified.command.runner_id
    receipt.capability_manifest = _manifest(True)
    host = host_module.NtTradingNodeHost(
        tenant_id="tenant-a",
        runner_id=str(receipt.runner_id),
        capability_receipt=receipt,
        runner_fact_emitter=_FactEmitterStub(),
        runner_safety_boundary_factory=runtime.factory,
        native_runtime_artifact_identity=NativeRuntimeArtifactIdentity("e" * 64, "0.64.0"),
    )

    class Strategy(_StrategyDouble, _GatedStrategy):
        pass

    strategy = Strategy(spec)
    strategy.submitted = []
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    await asyncio.sleep(0)
    try:
        yield SimpleNamespace(
            host=host,
            spec=spec,
            runtime=runtime,
            capture=handles[0],
            authority=EngineLifecycleAuthority.from_verified_command(runtime.verified),
        )
    finally:
        handles[0].resume.set()
        await host.stop(spec["deployment_instance_id"])
        await asyncio.gather(
            *(task for _, task in host._opening_fact_reads.values()), return_exceptions=True
        )


async def test_signed_host_uses_the_owner_handle_and_keeps_portfolio_failure_independent(
    native_host,
):
    from custos.core.opening_observation import RiskSampleUnavailable

    subject = native_host
    instance = subject.spec["deployment_instance_id"]
    gate_before = subject.runtime.store.load_opening_gate_state_sync(subject.authority)
    first = await subject.host.runner_fact_opening_bundle(instance, "USDT")
    assert first.inventory["status"] == "unavailable"
    assert isinstance(first.independent, RiskSampleUnavailable)
    await asyncio.gather(*(task for _, task in subject.host._opening_fact_reads.values()))
    bundle = await subject.host.runner_fact_opening_bundle(instance, "USDT")
    assert bundle.native.equity == 1
    assert bundle.native.observed_at == "2026-11-04T00:00:00.123456789Z"
    assert isinstance(bundle.independent, RiskSampleUnavailable)
    assert subject.capture.calls == 1
    assert subject.runtime.store.load_opening_gate_state_sync(subject.authority) == gate_before
    assert await subject.runtime.outbox.pending() == []


async def test_native_host_singleflight_and_stop_retire_inflight_observation(native_host):
    subject = native_host
    instance = subject.spec["deployment_instance_id"]
    subject.capture.resume.clear()
    await subject.host.runner_fact_opening_snapshot(instance)
    await asyncio.wait_for(subject.capture.entered.wait(), timeout=2)
    for _ in range(3):
        _gate, inventory = await subject.host.runner_fact_opening_snapshot(instance)
        assert inventory["reasons"] == ["observation_pending"]
    assert subject.capture.calls == 1
    task = subject.host._opening_fact_reads[instance][1]
    await subject.host.stop(instance)
    subject.capture.resume.set()
    with pytest.raises(RunnerFactObservationRetired):
        await task
    with pytest.raises(RunnerFactObservationRetired):
        await subject.host.runner_fact_opening_bundle(instance, "USDT")
    assert await subject.runtime.outbox.pending() == []


@pytest.mark.parametrize("lifecycle", ["running", "paused", "stopped"])
async def test_native_network_wait_does_not_lock_desired_and_discards_replaced_scope(
    native_host,
    tmp_path,
    lifecycle,
):
    subject = native_host
    subject.capture.resume.clear()
    task = asyncio.create_task(subject.host.read_opening_account_inventory(subject.authority))
    await asyncio.wait_for(subject.capture.entered.wait(), timeout=2)
    try:
        newer = await closed_platform_runtime(
            tmp_path / "new-generation.sqlite3",
            {**subject.spec, "generation": 2},
            lifecycle_state=lifecycle,
        )
        await asyncio.wait_for(
            subject.runtime.store.record_desired_command(
                command=newer.verified.command,
                command_fingerprint=newer.verified.command_fingerprint,
                verification_receipt=newer.verified.verification_receipt,
            ),
            timeout=1,
        )
    finally:
        subject.capture.resume.set()
    with pytest.raises(RunnerFactObservationRetired):
        await asyncio.wait_for(task, timeout=2)
    assert await subject.runtime.outbox.pending() == []


async def test_actual_host_portfolio_runtime_failure_cannot_block_an_admitted_native_capture(
    native_host,
    monkeypatch,
):
    from custos.core.runner_fact_producer import RunnerFactProductionLoop

    subject = native_host
    deployment = subject.host.runner_fact_deployments()[0]

    def failed_portfolio(*_args, **_kwargs):
        raise RuntimeError("portfolio backend failed")

    with monkeypatch.context() as patch:
        patch.setattr(subject.host._portfolio_snapshot_provider, "snapshot", failed_portfolio)

        class Emitter:
            plans = []

            async def emit_observability(self, authority, plan, fingerprint):
                self.plans.append(plan)

        emitter = Emitter()
        loop = RunnerFactProductionLoop(
            host=subject.host,
            emitter=emitter,
            snapshot_interval_secs=1,
            period_secs=60,
            period_retry_secs=1,
        )
        await loop._emit_observability(deployment)
        await asyncio.gather(*(task for _, task in subject.host._opening_fact_reads.values()))
        await loop._emit_observability(deployment)
        assert subject.capture.calls == 1
        equity, positions, beat = emitter.plans[1].primary
        assert equity["account_inventory"]["inventory_complete"] is True
        assert equity["amount"] == "1"
        assert positions["positions"] == []
        assert beat["status"] == "degraded"
        assert emitter.plans[1].fallback is None
        assert await subject.runtime.outbox.pending() == []
