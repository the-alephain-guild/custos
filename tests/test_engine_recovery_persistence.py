from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
from custos.core.engine_lifecycle import (
    EngineLifecycleConfig,
    EngineLifecycleQuarantined,
    EngineLifecycleSupervisor,
)
from custos.core.engine_protocol import (
    EngineReadinessChecks,
    EngineReadyReceipt,
    EngineTerminalEvent,
)
from custos.core.runner_command_intake import CommandDeliveryPolicy
from custos.core.runner_command_runtime import RunnerCommandRuntimeCoordinator
from tests.test_runner_fact_store import _runner_fact_store, _verified_command


class Engine:
    def __init__(self, name, *, ready_failures=0):
        self.name = name
        self.ready_failures = ready_failures
        self.deploy_calls = 0
        self.stop_calls = 0
        self.handle = None

    def supports_trading_mode(self, mode):
        return mode == "sandbox"

    def supports_venue(self, venue, mode):
        return venue == "binance" and mode == "sandbox"

    async def deploy(self, spec, credential, artifact):
        assert self.handle is None, "already deployed"
        self.deploy_calls += 1
        self.handle = f"{self.name}-{self.deploy_calls}"
        return self.handle

    async def stop(self, instance):
        self.stop_calls += 1
        self.handle = None

    async def wait_ready(self, authority, *, timeout_secs):
        if self.handle is None:
            raise RuntimeError("no process-local engine")
        if self.ready_failures:
            self.ready_failures -= 1
            raise TimeoutError("controlled readiness failure")
        return EngineReadyReceipt.from_authority(
            authority, checks=EngineReadinessChecks.all_ready(), ready_at_ns=1
        )

    async def wait_terminal(self, authority):
        self.handle = None
        return EngineTerminalEvent.from_authority(
            authority, reason_code="engine_task_failed", retryable=True
        )


async def no_sleep(delay):
    pass


def supervisor(store, engine, *, sleep=no_sleep, budget=2):
    return EngineLifecycleSupervisor(
        engine=engine,
        state_store=store,
        artifact_capability=ArtifactRuntimeCapabilityV1.production_ready(),
        config=EngineLifecycleConfig(
            readiness_timeout_secs=1,
            restart_budget=budget,
            restart_backoff_initial_secs=0.001,
            restart_backoff_max_secs=0.001,
        ),
        sleep=sleep,
    )


async def prepared(database):
    _, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    await store.record_artifact_activation(
        verified=verified,
        activation_id="activation-1",
        artifact_identity_digest="c" * 64,
        artifact_authority_digest="d" * 64,
    )
    kwargs = dict(
        delivery_id="local-probe",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=SimpleNamespace(activation_id="activation-1", strategy=object()),
    )
    return store, verified, kwargs


@pytest.mark.asyncio
async def test_ready_cycles_consume_the_durable_restart_budget(tmp_path):
    store, verified, kwargs = await prepared(tmp_path / "recovery.sqlite")
    engine = Engine("cycle")
    subject = supervisor(store, engine)
    await subject.apply(**kwargs)
    for count in (1, 2):
        await subject.supervise_once(**kwargs)
        state = await store.load_engine_lifecycle_state(verified)
        assert state.restart_count == count
        assert state.observed_status == "ready"
    with pytest.raises(EngineLifecycleQuarantined):
        await subject.supervise_once(**kwargs)
    assert engine.deploy_calls == 3
    assert engine.handle is None
    assert (await store.load_engine_lifecycle_state(verified)).desired_status == "quarantined"


@pytest.mark.asyncio
async def test_process_restart_does_not_grant_an_extra_restart_after_budget_is_spent(tmp_path):
    path = tmp_path / "spent.sqlite"
    store, verified, kwargs = await prepared(path)
    initial = supervisor(store, Engine("old"))
    await initial.apply(**kwargs)
    await initial.supervise_once(**kwargs)
    await initial.supervise_once(**kwargs)
    _, reopened = _runner_fact_store(path)
    replacement = Engine("new")
    with pytest.raises(EngineLifecycleQuarantined):
        await supervisor(reopened, replacement).apply(**kwargs)
    assert replacement.deploy_calls == 0
    assert (await reopened.load_engine_lifecycle_state(verified)).desired_status == "quarantined"


@pytest.mark.asyncio
async def test_interrupted_recovery_persists_the_new_handle_and_keeps_supervision(tmp_path):
    path = tmp_path / "interrupted.sqlite"
    store, verified, kwargs = await prepared(path)
    checkpoint = asyncio.Event()
    never = asyncio.Event()

    async def pause(_):
        checkpoint.set()
        await never.wait()

    initial = supervisor(store, Engine("old"), sleep=pause)
    await initial.apply(**kwargs)
    task = asyncio.create_task(initial.supervise_once(**kwargs))
    await asyncio.wait_for(checkpoint.wait(), timeout=1)
    assert (await store.load_engine_lifecycle_state(verified)).observed_status == "degraded"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    _, reopened = _runner_fact_store(path)
    engine = Engine("reopened")
    replacement = supervisor(reopened, engine)
    await replacement.apply(**kwargs)
    state = await reopened.load_engine_lifecycle_state(verified)
    assert state.observed_status == "ready"
    assert state.engine_handle == engine.handle == "reopened-1"
    assert state.restart_count == 1
    runtime = RunnerCommandRuntimeCoordinator(
        intake=None,
        durability=reopened,
        release_resolver=None,
        artifact_runtime=None,
        entry_point_loader=None,
        credential_resolver=None,
        engine_lifecycle=replacement,
        delivery_policy=CommandDeliveryPolicy(),
    )
    runtime._start_engine_supervision(**kwargs, artifact_policy_id=None)
    watcher = runtime._engine_supervisions[verified.command.deployment_instance_id]
    await asyncio.wait_for(asyncio.gather(watcher, return_exceptions=True), timeout=2)
    state = await reopened.load_engine_lifecycle_state(verified)
    assert state.desired_status == "quarantined"
    assert engine.deploy_calls == 2
    assert engine.stop_calls >= 2
