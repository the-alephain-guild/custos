from __future__ import annotations

import asyncio
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
    EngineDependencyUnavailable,
    EngineDeploymentRefused,
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

    def supports_venue(self, venue: str, mode: str) -> bool:
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


def test_default_readiness_budget_outlives_the_engine_connection_deadline() -> None:
    assert EngineLifecycleConfig().readiness_timeout_secs > 60.0


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
async def test_new_running_generation_stops_old_engine_before_deploy() -> None:
    prior = _verified()
    verified = _verified(generation=2)
    store = _Store(
        state=EngineLifecycleDurableState(
            desired_status="pending",
            applied_generation=1,
            applied_command_fingerprint=prior.command_fingerprint,
            engine_handle="old-handle",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    engine = _Engine([_ready(verified)])

    await _supervisor(store, engine).apply(
        delivery_id="replacement",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=_Artifact(),
    )

    assert engine.events == ["stop", "deploy", "wait_ready"]
    assert engine.stop_calls == 1
    assert engine.deploy_calls == 1
    assert store.state.applied_generation == 2


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


class _EngineThatNeverBecomesReady(_Engine):
    async def wait_ready(
        self,
        authority: EngineLifecycleAuthority,
        *,
        timeout_secs: float,
    ) -> EngineReadyReceipt:
        self.events.append("wait_ready")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_a_start_cancelled_while_waiting_stops_the_engine_it_deployed() -> None:
    # A newer command or a shutdown can cancel a start that is still waiting for
    # the venue. The node it deployed must not be left running without an owner,
    # and the cancellation is not a failed attempt: nothing is recorded for it.
    verified = _verified()
    store = _Store()
    engine = _EngineThatNeverBecomesReady([])

    async def no_sleep(_delay: float) -> None:
        return None

    supervisor = EngineLifecycleSupervisor(
        engine=engine,
        state_store=store,
        artifact_capability=_capability(),
        config=EngineLifecycleConfig(readiness_timeout_secs=30.0, restart_budget=2),
        sleep=no_sleep,
        clock_ns=lambda: 10,
    )
    start = asyncio.create_task(
        supervisor.apply(
            delivery_id="delivery-cancelled",
            verified=verified,
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )
    )
    while "wait_ready" not in engine.events:
        await asyncio.sleep(0)

    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert engine.events == ["deploy", "wait_ready", "stop"]
    assert store.state.restart_count == 0
    assert store.terminal == []


@pytest.mark.asyncio
async def test_a_refused_deployment_is_quarantined_without_retrying() -> None:
    # A strategy whose trading scope differs from what the deployment authorizes
    # will differ on every attempt. Retrying only delays the quarantine and runs
    # the backoff against a decision that cannot change.
    verified = _verified()
    store = _Store()
    engine = _Engine([_ready(verified)])

    async def refused(spec: dict, credential: dict, artifact: object) -> str:
        engine.deploy_calls += 1
        engine.events.append("deploy")
        raise EngineDeploymentRefused(
            "strategy_trading_scope_mismatch",
            "strategy trades BTCUSDT-PERP.BINANCE; deployment authorizes BTCUSDT.BINANCE",
        )

    engine.deploy = refused  # type: ignore[method-assign]

    with pytest.raises(EngineLifecycleQuarantined, match="strategy_trading_scope_mismatch"):
        await _supervisor(store, engine, restart_budget=2).apply(
            delivery_id="delivery-refused",
            verified=verified,
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )

    assert engine.deploy_calls == 1
    assert store.state.restart_count == 0
    assert store.terminal == [("retry_exhausted", "strategy_trading_scope_mismatch")]


@pytest.mark.asyncio
async def test_missing_runtime_authority_does_not_consume_engine_restart_budget() -> None:
    verified = _verified()
    store = _Store()
    engine = _Engine([_ready(verified)])

    async def dependency_blocked(spec: dict, credential: dict, artifact: object) -> str:
        engine.deploy_calls += 1
        engine.events.append("deploy")
        raise EngineDependencyUnavailable("runner safety policy is not available yet")

    engine.deploy = dependency_blocked  # type: ignore[method-assign]

    with pytest.raises(EngineLifecycleBlocked, match="runner safety policy"):
        await _supervisor(store, engine).apply(
            delivery_id="delivery-policy-pending",
            verified=verified,
            runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
            credential={},
            artifact=_Artifact(),
        )

    assert engine.deploy_calls == 1
    assert engine.events == ["deploy"]
    assert store.state.restart_count == 0
    assert store.terminal == []


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
    assert "commit_recovered_ready" in store.events
    assert "commit_ready" not in store.events

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
async def test_replaced_desired_generation_retires_old_terminal_watcher_before_stop() -> None:
    verified = _verified()
    authority = _authority(verified)
    store = _Store(
        state=EngineLifecycleDurableState(
            desired_status="applied",
            applied_generation=1,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle="old-handle",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
    )
    engine = _Engine(
        [],
        terminal_events=[
            EngineTerminalEvent.from_authority(
                authority,
                reason_code="engine_task_failed",
                retryable=True,
            )
        ],
    )
    original_wait_terminal = engine.wait_terminal

    async def replaced_while_waiting(value):
        event = await original_wait_terminal(value)
        store.state = EngineLifecycleDurableState(
            desired_status="recorded",
            applied_generation=1,
            applied_command_fingerprint=verified.command_fingerprint,
            engine_handle="old-handle",
            observed_status="ready",
            restart_count=0,
            quarantine_reason=None,
        )
        return event

    engine.wait_terminal = replaced_while_waiting  # type: ignore[method-assign]

    result = await _supervisor(store, engine).supervise_once(
        delivery_id="old-generation-watcher",
        verified=verified,
        runtime_spec={"trading_mode": "sandbox", "connector": "binance"},
        credential={},
        artifact=_Artifact(),
    )

    assert result is None
    assert engine.stop_calls == 0
    assert engine.deploy_calls == 0
    assert store.events == ["load_state"]


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


@pytest.mark.asyncio
async def test_an_unknown_live_venue_is_refused_before_credential_admission() -> None:
    """Unknown venues fail before credentials; supported venues enforce scope."""
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.host import NtTradingNodeHost

    real = NtTradingNodeHost()

    @dataclass
    class _RealCapabilityEngine(_Engine):
        def supports_trading_mode(self, mode: str) -> bool:
            return real.supports_trading_mode(mode)

        def supports_venue(self, venue: str, mode: str) -> bool:
            return real.supports_venue(venue, mode)

    engine = _RealCapabilityEngine([_ready()])
    with pytest.raises(EngineLifecycleBlocked, match="does not support the signed venue"):
        await _supervisor(_Store(), engine).apply(
            delivery_id="sodex-live",
            verified=_verified(mode="live"),
            runtime_spec={"trading_mode": "live", "connector": "unsupported_perpetual"},
            credential={"permission_scope": "trade_no_withdraw"},
            artifact=_Artifact(),
        )

    with pytest.raises(EngineLifecycleBlocked, match="trade_no_withdraw"):
        await _supervisor(_Store(), engine).apply(
            delivery_id="binance-live",
            verified=_verified(mode="live"),
            runtime_spec={"trading_mode": "live", "connector": "binance_perpetual"},
            credential={"permission_scope": "read_only"},
            artifact=_Artifact(),
        )

    assert engine.events == []


@pytest.mark.asyncio
async def test_sodex_is_admitted_for_sandbox_and_testnet() -> None:
    """The other half of the same allow-list: it is a per-mode table, not a ban."""
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.host import NtTradingNodeHost

    real = NtTradingNodeHost()
    for mode in ("sandbox", "testnet"):
        for connector in ("sodex", "sodex_perpetual"):
            assert real.supports_venue(connector, mode) is True
