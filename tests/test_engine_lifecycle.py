from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
from custos.core.engine_lifecycle import (
    EngineLifecycleBlocked,
    EngineLifecycleConfig,
    EngineLifecycleDurableState,
    EngineLifecycleQuarantined,
    EngineLifecycleSupervisor,
)
from custos.core.engine_protocol import (
    EngineLifecycleAuthority,
    EngineReadinessChecks,
    EngineReadyReceipt,
    EngineTerminalEvent,
)

INSTANCE = UUID("20000000-0000-4000-8000-000000000002")
SPEC = UUID("30000000-0000-4000-8000-000000000003")
DIGEST = "a" * 64


def _verified(
    *,
    mode: str = "sandbox",
    generation: int = 1,
    lifecycle_state: str = "running",
):
    command = SimpleNamespace(
        deployment_instance_id=INSTANCE,
        deployment_spec_id=SPEC,
        deployment_spec_digest=DIGEST,
        generation=generation,
        trading_mode=mode,
        lifecycle_state=lifecycle_state,
    )
    return SimpleNamespace(command=command, command_fingerprint="b" * 64)


def _authority(verified=None) -> EngineLifecycleAuthority:
    value = verified or _verified()
    return EngineLifecycleAuthority.from_verified_command(value)


def _ready(verified=None) -> EngineReadyReceipt:
    return EngineReadyReceipt.from_authority(
        _authority(verified),
        checks=EngineReadinessChecks.all_ready(),
        ready_at_ns=1,
    )


@dataclass
class _Store:
    state: EngineLifecycleDurableState = field(
        default_factory=lambda: EngineLifecycleDurableState(
            desired_status="pending",
            applied_generation=None,
            applied_command_fingerprint=None,
            engine_handle=None,
            observed_status=None,
            restart_count=0,
            quarantine_reason=None,
        )
    )
    events: list[str] = field(default_factory=list)
    terminal: list[tuple[str, str]] = field(default_factory=list)

    async def load_engine_lifecycle_state(self, verified):
        self.events.append("load_state")
        return self.state

    async def record_in_progress_lease(self, **kwargs):
        self.events.append("lease")

    async def record_engine_restart(self, **kwargs):
        self.events.append("restart")
        self.state = EngineLifecycleDurableState(
            desired_status=self.state.desired_status,
            applied_generation=self.state.applied_generation,
            applied_command_fingerprint=self.state.applied_command_fingerprint,
            engine_handle=self.state.engine_handle,
            observed_status=self.state.observed_status,
            restart_count=self.state.restart_count + 1,
            quarantine_reason=None,
        )
        return self.state.restart_count

    async def commit_applied_and_enqueue_lifecycle(self, **kwargs):
        self.events.append("commit_ready")
        verified = kwargs["verified"]
        self.state = EngineLifecycleDurableState(
            desired_status="applied",
            applied_generation=verified.command.generation,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle=kwargs["engine_handle"],
            observed_status=kwargs["observed_status"],
            restart_count=self.state.restart_count,
            quarantine_reason=None,
        )

    async def commit_recovered_engine_ready(self, **kwargs):
        self.events.append("commit_recovered_ready")
        verified = kwargs["verified"]
        self.state = EngineLifecycleDurableState(
            desired_status="applied",
            applied_generation=verified.command.generation,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle=kwargs["engine_handle"],
            observed_status=kwargs["observed_status"],
            restart_count=self.state.restart_count,
            quarantine_reason=None,
        )

    async def commit_verified_command_outcome_and_enqueue_fact(self, **kwargs):
        self.events.append("commit_terminal")
        self.terminal.append((kwargs["outcome"], kwargs["reason_code"]))
        self.state = EngineLifecycleDurableState(
            desired_status="quarantined",
            applied_generation=self.state.applied_generation,
            applied_command_fingerprint=self.state.applied_command_fingerprint,
            engine_handle=self.state.engine_handle,
            observed_status="quarantined",
            restart_count=self.state.restart_count,
            quarantine_reason=kwargs["reason_code"],
        )


@dataclass
class _Engine:
    ready_results: list[object]
    terminal_events: list[EngineTerminalEvent] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    deploy_calls: int = 0
    stop_calls: int = 0

    async def deploy(self, spec: dict, credential: dict, artifact: object) -> str:
        self.deploy_calls += 1
        self.events.append("deploy")
        return f"handle-{self.deploy_calls}"

    async def reconfigure(self, spec: dict) -> None:
        return None

    async def stop(self, deployment_instance_id: str) -> None:
        self.stop_calls += 1
        self.events.append("stop")

    def supports_trading_mode(self, mode: str) -> bool:
        return mode == "sandbox"

    def supports_venue(self, venue: str) -> bool:
        return True

    async def get_open_notional(self, deployment_instance_id: str):
        raise NotImplementedError

    async def check_engine_connected(self, deployment_instance_id: str):
        raise NotImplementedError

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        raise NotImplementedError

    async def get_positions(self, deployment_instance_id: str):
        return []

    async def get_orders(self, deployment_instance_id: str):
        return []

    async def get_engine_status(self, deployment_instance_id: str):
        raise NotImplementedError

    async def wait_ready(
        self,
        authority: EngineLifecycleAuthority,
        *,
        timeout_secs: float,
    ) -> EngineReadyReceipt:
        self.events.append("wait_ready")
        result = self.ready_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, EngineReadyReceipt)
        return result

    async def wait_terminal(self, authority: EngineLifecycleAuthority) -> EngineTerminalEvent:
        self.events.append("wait_terminal")
        return self.terminal_events.pop(0)


@dataclass(frozen=True, slots=True)
class _Artifact:
    activation_id: str = "activation-1"
    strategy: object = field(default_factory=object)


def _capability(*, ready: bool = True) -> ArtifactRuntimeCapabilityV1:
    if not ready:
        return ArtifactRuntimeCapabilityV1.blocked("StrategyRelease resolver is not composed")
    return ArtifactRuntimeCapabilityV1.production_ready()


def _supervisor(
    store: _Store,
    engine: _Engine,
    *,
    capability: ArtifactRuntimeCapabilityV1 | None = None,
    restart_budget: int = 2,
) -> EngineLifecycleSupervisor:
    async def no_sleep(_delay: float) -> None:
        return None

    return EngineLifecycleSupervisor(
        engine=engine,
        state_store=store,
        artifact_capability=capability or _capability(),
        config=EngineLifecycleConfig(
            readiness_timeout_secs=0.01,
            restart_budget=restart_budget,
            restart_backoff_initial_secs=0.001,
            restart_backoff_max_secs=0.01,
        ),
        sleep=no_sleep,
        clock_ns=lambda: 10,
    )


@pytest.mark.asyncio
async def test_ready_is_typed_and_committed_after_engine_readiness() -> None:
    verified = _verified()
    store = _Store()
    engine = _Engine([_ready(verified)])
    receipt = await _supervisor(store, engine).apply(
        delivery_id="delivery-1",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=_Artifact(),
    )

    assert receipt.deployment_instance_id == INSTANCE
    assert engine.events == ["deploy", "wait_ready"]
    assert store.events == ["load_state", "lease", "commit_ready"]


@pytest.mark.asyncio
async def test_restart_replay_probes_ready_without_duplicate_deploy() -> None:
    verified = _verified()
    store = _Store(
        state=EngineLifecycleDurableState(
            desired_status="applied",
            applied_generation=1,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle="existing-handle",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    engine = _Engine([_ready(verified)])

    await _supervisor(store, engine).apply(
        delivery_id="redelivery",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=_Artifact(),
    )

    assert engine.deploy_calls == 0
    assert engine.events == ["wait_ready"]
    assert store.events == ["load_state"]


@pytest.mark.asyncio
async def test_process_restart_rebuilds_engine_without_duplicate_applied_fact() -> None:
    verified = _verified()
    store = _Store(
        state=EngineLifecycleDurableState(
            desired_status="applied",
            applied_generation=1,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle="lost-process-handle",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    engine = _Engine([RuntimeError("engine process is absent"), _ready(verified)])

    await _supervisor(store, engine).apply(
        delivery_id="startup-recovery",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=_Artifact(),
    )

    assert engine.events == ["wait_ready", "stop", "deploy", "wait_ready"]
    assert store.events == [
        "load_state",
        "restart",
        "lease",
        "commit_recovered_ready",
    ]
    assert store.state.restart_count == 1
    assert store.state.engine_handle == "handle-1"


@pytest.mark.asyncio
async def test_readiness_timeout_exhausts_durable_budget_and_quarantines() -> None:
    verified = _verified()
    store = _Store()
    engine = _Engine([TimeoutError(), TimeoutError(), TimeoutError()])

    with pytest.raises(EngineLifecycleQuarantined, match="engine_ready_timeout"):
        await _supervisor(store, engine, restart_budget=2).apply(
            delivery_id="delivery-timeout",
            verified=verified,
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )

    assert engine.deploy_calls == 3
    assert engine.stop_calls == 3
    assert store.state.restart_count == 2
    assert store.terminal == [("retry_exhausted", "engine_ready_timeout")]


@pytest.mark.asyncio
async def test_terminal_and_zombie_events_use_same_durable_quarantine_or_restart_path() -> None:
    verified = _verified()
    authority = _authority(verified)
    store = _Store(
        state=EngineLifecycleDurableState(
            desired_status="applied",
            applied_generation=1,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle="handle-1",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    engine = _Engine(
        [_ready(verified)],
        terminal_events=[
            EngineTerminalEvent.from_authority(
                authority, reason_code="zombie_disconnect", retryable=True
            ),
            EngineTerminalEvent.from_authority(
                authority, reason_code="engine_task_failed", retryable=False
            ),
        ],
    )
    subject = _supervisor(store, engine)

    restarted = await subject.supervise_once(
        delivery_id="delivery-zombie",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=_Artifact(),
    )
    assert restarted is not None
    assert engine.stop_calls == 1
    assert engine.deploy_calls == 1

    with pytest.raises(EngineLifecycleQuarantined, match="engine_task_failed"):
        await subject.supervise_once(
            delivery_id="delivery-terminal",
            verified=verified,
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )
    assert store.terminal[-1] == ("retry_exhausted", "engine_task_failed")


@pytest.mark.asyncio
async def test_blocked_artifact_capability_and_live_mode_fail_before_engine_action() -> None:
    store = _Store()
    engine = _Engine([_ready()])
    with pytest.raises(EngineLifecycleBlocked, match="artifact runtime capability"):
        await _supervisor(store, engine, capability=_capability(ready=False)).apply(
            delivery_id="blocked",
            verified=_verified(),
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )
    with pytest.raises(EngineLifecycleBlocked, match="live"):
        await _supervisor(store, engine).apply(
            delivery_id="live",
            verified=_verified(mode="live"),
            runtime_spec={"trading_mode": "live", "connector": "binance"},
            credential={"permission_scope": "trade_no_withdraw"},
            artifact=_Artifact(),
        )
    assert engine.events == []


@pytest.mark.asyncio
async def test_non_running_generation_stops_without_artifact_deploy() -> None:
    prior = _verified()
    verified = _verified(generation=2, lifecycle_state="paused")
    store = _Store(
        state=EngineLifecycleDurableState(
            desired_status="pending",
            applied_generation=1,
            applied_command_fingerprint=prior.command_fingerprint,
            engine_handle="handle-1",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    engine = _Engine([])

    await _supervisor(store, engine).apply_non_running(
        delivery_id="pause-delivery",
        verified=verified,
    )

    assert engine.events == ["stop"]
    assert store.events == ["load_state", "lease", "commit_ready"]
    assert store.state.applied_generation == 2
    assert store.state.engine_handle is None
    assert store.state.observed_status == "paused"
