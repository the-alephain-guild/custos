"""A runner that cannot start an instance says so instead of quarantining it.

Three ways a valid command used to end in the wrong place:

* a runner whose one engine node is held by another instance imported the new
  artifact first and only then refused, inside the restart budget;
* a command for an instance the runner's capability does not bind yet was
  imported, failed to sign its rejection, and was redelivered until the queue
  behind it stalled;
* a rejection that could not be signed was negatively acknowledged, which holds
  every later command, including a stop, behind it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from custos.artifacts.release_resolver import StrategyReleaseResolutionRejected
from custos.core.runner_command_intake import CommandDeliveryPolicy
from custos.core.runner_command_runtime import (
    RunnerCommandRuntimeCoordinator,
    RunnerCommandRuntimeStatus,
)
from custos.core.runner_fact import RunnerFactContractError
from tests.test_runner_command_runtime import (
    VERIFIED,
    _ArtifactRuntime,
    _CredentialResolver,
    _Delivery,
    _Durability,
    _Intake,
    _Lifecycle,
    _Resolver,
)

INSTANCE = UUID(int=2)
OTHER_INSTANCE = UUID(int=9)


class _RecordingArtifactRuntime(_ArtifactRuntime):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def prepare(self, **kwargs):
        self.events.append("prepare")
        return await super().prepare(**kwargs)

    async def activate(self, prepared, *, loader):
        self.events.append("activate")
        return await super().activate(prepared, loader=loader)


class _RecordingResolver(_Resolver):
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        super().__init__(error)
        self.events = events

    async def resolve(self, verified):
        self.events.append("resolve")
        return await super().resolve(verified)


class _Capacity:
    def __init__(self, holder: object | None) -> None:
        self.holder = holder
        self.asked = 0

    def node_holder(self):
        self.asked += 1
        return self.holder


def _holder(instance: UUID, *, releasing: bool = False) -> SimpleNamespace:
    return SimpleNamespace(deployment_instance_id=str(instance), releasing=releasing)


def _subject(
    events: list[str],
    *,
    resolver=None,
    durability=None,
    lifecycle=None,
    intake=None,
    binding_gap: str | None = None,
    capacity: _Capacity | None = None,
) -> RunnerCommandRuntimeCoordinator:
    return RunnerCommandRuntimeCoordinator(
        intake=intake or _Intake(),
        durability=durability or _Durability(events),
        release_resolver=resolver or _RecordingResolver(events),
        artifact_runtime=_RecordingArtifactRuntime(events),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=lifecycle or _Lifecycle(events),
        delivery_policy=CommandDeliveryPolicy(in_progress_interval_seconds=0.01),
        capability_binding=lambda verified: binding_gap,
        node_capacity=capacity,
    )


@pytest.mark.asyncio
async def test_an_unbound_start_is_acknowledged_and_waits_without_activation() -> None:
    events: list[str] = []
    delivery = _Delivery()

    with capture_logs() as logs:
        result = await _subject(
            events,
            binding_gap="capability has no unique settlement binding",
        ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING
    assert delivery.events == ["ack"]
    assert events == [], "nothing may be resolved, imported, started or committed"
    waiting = [log for log in logs if log["event"] == "runner_command_awaiting_capability_binding"]
    assert len(waiting) == 1
    assert waiting[0]["deployment_instance_id"] == str(INSTANCE)


@pytest.mark.asyncio
async def test_an_occupied_runner_refuses_before_any_import() -> None:
    events: list[str] = []
    delivery = _Delivery()
    capacity = _Capacity(_holder(OTHER_INSTANCE))

    result = await _subject(events, capacity=capacity).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.TERMINAL_REJECTED
    assert result.reason_code == "runtime_capacity_rejected:runner_engine_occupied"
    assert events == ["commit:runtime_capacity_rejected:runner_engine_occupied"]
    assert delivery.events == ["term"]


@pytest.mark.asyncio
async def test_an_unsignable_rejection_is_acknowledged_instead_of_redelivered() -> None:
    events: list[str] = []
    delivery = _Delivery()

    class UnsignableDurability(_Durability):
        async def commit_verified_terminal_outcome(self, **kwargs):
            self.events.append("commit_attempted")
            raise RunnerFactContractError(
                "capability has no unique deployment_lifecycle binding for DeploymentInstance"
            )

    result = await _subject(
        events,
        resolver=_Resolver(StrategyReleaseResolutionRejected("conflict")),
        durability=UnsignableDurability(events),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING
    assert delivery.events == ["ack"]
    assert events == ["commit_attempted"]


def test_verified_fixture_addresses_the_instance_these_tests_name() -> None:
    assert VERIFIED.command.deployment_instance_id == INSTANCE


@pytest.mark.asyncio
async def test_a_new_generation_of_the_holding_instance_proceeds() -> None:
    events: list[str] = []
    delivery = _Delivery()
    capacity = _Capacity(_holder(INSTANCE))

    result = await _subject(events, capacity=capacity).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert events == ["resolve", "prepare", "activate", "apply"]
    assert capacity.asked == 1


@pytest.mark.asyncio
async def test_a_free_runner_proceeds() -> None:
    events: list[str] = []
    delivery = _Delivery()

    result = await _subject(events, capacity=_Capacity(None)).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert delivery.events == ["ack"]


@pytest.mark.asyncio
async def test_a_holder_being_released_is_waited_for_not_refused() -> None:
    events: list[str] = []
    delivery = _Delivery(delivered_count=1)

    result = await _subject(
        events,
        capacity=_Capacity(_holder(OTHER_INSTANCE, releasing=True)),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.RETRY_SCHEDULED
    assert result.reason_code == "runtime_capacity_unavailable:runner_engine_releasing"
    assert delivery.events == [f"nak:{CommandDeliveryPolicy().backoff_for(1)}"]
    assert events == []


@pytest.mark.asyncio
async def test_a_release_that_outlasts_every_delivery_ends_in_a_refusal() -> None:
    events: list[str] = []
    policy = CommandDeliveryPolicy()
    delivery = _Delivery(delivered_count=policy.max_deliver)

    result = await _subject(
        events,
        capacity=_Capacity(_holder(OTHER_INSTANCE, releasing=True)),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.TERMINAL_REJECTED
    assert events == ["commit:retry_exhausted:runtime_capacity_unavailable:runner_engine_releasing"]
    assert delivery.events == ["term"]


@pytest.mark.asyncio
async def test_an_occupied_refusal_is_signed_as_a_stopped_retry_exhausted_fact(
    tmp_path,
) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    assert verified.command.lifecycle_state == "running"
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    events: list[str] = []
    delivery = _Delivery()

    result = await _subject(
        events,
        durability=store,
        intake=_Intake(verified=verified),
        capacity=_Capacity(_holder(OTHER_INSTANCE)),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.TERMINAL_REJECTED
    assert delivery.events == ["term"]
    assert events == [], "nothing was resolved or imported"
    facts = _lifecycle_facts(database)
    assert [(fact["lifecycle_state"], fact["outcome"]) for fact in facts] == [
        ("stopped", "retry_exhausted")
    ]
    state = await store.load_engine_lifecycle_state(verified)
    assert state.desired_status == "quarantined"
    assert state.quarantine_reason == "runtime_capacity_rejected:runner_engine_occupied"


def _lifecycle_facts(database) -> list[dict]:
    import json
    import sqlite3

    def walk(value):
        if isinstance(value, dict):
            if "lifecycle_state" in value and "outcome" in value:
                yield value
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    with sqlite3.connect(database) as connection:
        payloads = [
            row[0]
            for row in connection.execute(
                "SELECT payload FROM runner_fact_outbox ORDER BY stream_key, source_seq_start"
            )
        ]
    facts: list[dict] = []
    for payload in payloads:
        facts.extend(walk(json.loads(payload)))
    return facts


class _SequencedIntake:
    """Hand the coordinator one verified command per delivery, in order."""

    def __init__(self, *verified: object) -> None:
        self.remaining = list(verified)

    async def process(self, delivery):
        return await _Intake(verified=self.remaining.pop(0)).process(delivery)


def _other_instance_command() -> SimpleNamespace:
    from tests.test_runner_command_runtime import _Command

    class OtherCommand(_Command):
        deployment_instance_id = OTHER_INSTANCE

    return SimpleNamespace(command=OtherCommand(), command_fingerprint="9" * 64)


async def _recorded(store, verified) -> None:
    """What intake has done for every command that reaches the coordinator."""
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )


def _command_outcome_count(database) -> int:
    import sqlite3

    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT count(*) FROM command_outcomes").fetchone()[0]


@pytest.mark.asyncio
async def test_an_unbound_start_stays_recorded_and_nothing_starts(tmp_path) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await _recorded(store, verified)
    events: list[str] = []
    delivery = _Delivery()

    result = await _subject(
        events,
        durability=store,
        intake=_Intake(verified=verified),
        binding_gap="capability has no unique settlement binding for DeploymentInstance",
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING
    assert delivery.events == ["ack"]
    assert events == []
    state = await store.load_engine_lifecycle_state(verified)
    assert state.desired_status == "recorded"
    assert _command_outcome_count(database) == 0
    assert _lifecycle_facts(database) == []


@pytest.mark.asyncio
async def test_an_unbound_command_does_not_hold_the_next_one() -> None:
    events: list[str] = []
    other = _other_instance_command()
    gaps = {INSTANCE: "capability has no unique settlement binding for DeploymentInstance"}
    subject = RunnerCommandRuntimeCoordinator(
        intake=_SequencedIntake(VERIFIED, other),
        durability=_Durability(events),
        release_resolver=_RecordingResolver(events),
        artifact_runtime=_RecordingArtifactRuntime(events),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=_Lifecycle(events),
        delivery_policy=CommandDeliveryPolicy(in_progress_interval_seconds=0.01),
        capability_binding=lambda verified: gaps.get(verified.command.deployment_instance_id),
    )
    unbound, bound = _Delivery(delivery_id="unbound"), _Delivery(delivery_id="bound")

    first = await subject.process(unbound)
    second = await subject.process(bound)

    assert first.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING
    assert second.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert unbound.events == ["ack"]
    assert bound.events == ["ack"]
    assert events == ["resolve", "prepare", "activate", "apply"]


@pytest.mark.asyncio
async def test_an_unbound_stop_is_acknowledged_and_leaves_the_engine_alone() -> None:
    events: list[str] = []
    stop = SimpleNamespace(
        command=SimpleNamespace(
            deployment_instance_id=INSTANCE,
            generation=2,
            trading_mode="sandbox",
            lifecycle_state="stopped",
            is_development_source=False,
        ),
        command_fingerprint="c" * 64,
    )
    delivery = _Delivery()

    result = await _subject(
        events,
        intake=_Intake(verified=stop),
        binding_gap="capability has no unique deployment_lifecycle binding for DeploymentInstance",
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING
    assert delivery.events == ["ack"]
    assert events == [], "an unbound instance was never started here, so nothing is stopped"


@pytest.mark.asyncio
async def test_an_unsignable_refusal_keeps_the_command_recorded(tmp_path) -> None:
    from custos.core.runner_fact import RunnerStateStore
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    outbox, signing_store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await _recorded(signing_store, verified)

    def unbound(_verified):
        raise RunnerFactContractError(
            "capability has no unique deployment_lifecycle binding for DeploymentInstance"
        )

    store = RunnerStateStore(
        outbox=outbox,
        identity=signing_store._identity,
        tenant_id="acme",
        runner_id=verified.command.runner_id,
        authority_resolver=unbound,
    )
    events: list[str] = []
    delivery = _Delivery()

    result = await _subject(
        events,
        durability=store,
        intake=_Intake(verified=verified),
        capacity=_Capacity(_holder(OTHER_INSTANCE)),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING
    assert delivery.events == ["ack"]
    state = await store.load_engine_lifecycle_state(verified)
    assert state.desired_status == "recorded"
    assert _command_outcome_count(database) == 0


class _SandboxEngine:
    """A sandbox engine that stays running until stopped."""

    def __init__(self) -> None:
        from tests.test_engine_recovery_persistence import Engine

        self._engine = Engine("recovered")

    def __getattr__(self, name):
        return getattr(self._engine, name)

    def supports_venue(self, venue, mode):
        return mode == "sandbox"

    async def wait_terminal(self, authority):
        await asyncio.Event().wait()


class _ActivatingArtifactRuntime(_RecordingArtifactRuntime):
    def __init__(self, events: list[str], store, verified) -> None:
        super().__init__(events)
        self.store = store
        self.verified = verified

    async def activate(self, prepared, *, loader):
        await self.store.record_artifact_activation(
            verified=self.verified,
            activation_id="activation-1",
            artifact_identity_digest="c" * 64,
            artifact_authority_digest="d" * 64,
        )
        return await super().activate(prepared, loader=loader)


class _BindingCapability:
    def __init__(self, gap: str | None) -> None:
        self.gap = gap

    def require_scope_bindings(self, **kwargs) -> None:
        if self.gap is not None:
            raise RunnerFactContractError(self.gap)


async def _restart_and_recover(store, verified, capability, events):
    from custos.cli._daemon import _recover_durable_running_commands
    from tests.test_engine_recovery_persistence import supervisor

    restarted = RunnerCommandRuntimeCoordinator(
        intake=_Intake(),
        durability=store,
        release_resolver=_RecordingResolver(events),
        artifact_runtime=_ActivatingArtifactRuntime(events, store, verified),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=supervisor(store, _SandboxEngine()),
        delivery_policy=CommandDeliveryPolicy(in_progress_interval_seconds=0.01),
        capability_binding=lambda verified: None,
    )
    await _recover_durable_running_commands(
        state_store=store,
        command_runtime=restarted,
        capability=capability,
    )
    await asyncio.wait_for(restarted.recoveries_settled(), timeout=5)
    stop = asyncio.Event()
    stop.set()
    await restarted.run_engine_supervision(stop)


@pytest.mark.asyncio
async def test_a_kept_start_is_applied_and_reported_after_a_restart_that_binds_it(
    tmp_path,
) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await _recorded(store, verified)
    deferred = await _subject(
        [],
        durability=store,
        intake=_Intake(verified=verified),
        binding_gap="capability has no unique settlement binding for DeploymentInstance",
    ).process(_Delivery())
    assert deferred.status is RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING

    events: list[str] = []
    await _restart_and_recover(store, verified, _BindingCapability(None), events)

    assert events == ["resolve", "prepare", "activate"]
    state = await store.load_engine_lifecycle_state(verified)
    assert state.desired_status == "applied"
    assert [(fact["lifecycle_state"], fact["outcome"]) for fact in _lifecycle_facts(database)] == [
        ("running", "applied")
    ]


@pytest.mark.asyncio
async def test_a_kept_start_still_unbound_after_a_restart_is_skipped_and_logged(
    tmp_path,
) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await _recorded(store, verified)

    events: list[str] = []
    with capture_logs() as logs:
        await _restart_and_recover(
            store,
            verified,
            _BindingCapability(
                "capability has no unique settlement binding for DeploymentInstance"
            ),
            events,
        )

    assert events == []
    assert (await store.load_engine_lifecycle_state(verified)).desired_status == "recorded"
    skipped = [log for log in logs if log["event"] == "durable_command_recovery_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["deployment_instance_id"] == str(verified.command.deployment_instance_id)
    assert _lifecycle_facts(database) == []


class _ProjectorCapability:
    """Binds exactly the projectors it is given, for every instance."""

    def __init__(self, bound: set[str]) -> None:
        self.bound = bound
        self.asked: list[tuple[str, ...]] = []

    def require_scope_bindings(self, *, projectors, **kwargs) -> None:
        requested = tuple(projectors)
        self.asked.append(requested)
        missing = [projector for projector in requested if projector not in self.bound]
        if missing:
            raise RunnerFactContractError(
                f"capability has no unique {missing[0]} binding for DeploymentInstance"
            )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sandbox", ("deployment_lifecycle", "settlement", "risk", "health")),
        ("testnet", ("deployment_lifecycle", "settlement", "risk", "health", "reconciliation")),
        ("live", ("deployment_lifecycle", "settlement", "risk", "health", "reconciliation")),
    ],
)
def test_a_command_and_a_recovery_need_the_same_projectors(mode, expected) -> None:
    from custos.core.runner_fact import runner_instance_binding_gap

    capability = _ProjectorCapability(set(expected))

    gap = runner_instance_binding_gap(
        capability,
        trading_mode=mode,
        deployment_instance_id=INSTANCE,
        deployment_spec_id=UUID(int=3),
        deployment_spec_digest="d" * 64,
        strategy_id=UUID(int=4),
    )

    assert gap is None
    assert capability.asked == [expected]


@pytest.mark.asyncio
async def test_recovery_skips_an_instance_whose_outcomes_it_could_not_sign(tmp_path) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await _recorded(store, verified)
    events: list[str] = []

    with capture_logs() as logs:
        await _restart_and_recover(
            store,
            verified,
            _ProjectorCapability({"settlement", "risk", "health"}),
            events,
        )

    assert events == []
    skipped = [log for log in logs if log["event"] == "durable_command_recovery_skipped"]
    assert [log["binding_gap"] for log in skipped] == [
        "capability has no unique deployment_lifecycle binding for DeploymentInstance"
    ]


async def _applied(store, verified) -> None:
    """A command that was applied and running before the runner restarted."""
    import time

    await _recorded(store, verified)
    await store.record_in_progress_lease(
        delivery_id="delivery-applied",
        verified=verified,
        lease_until_ns=time.time_ns() + 60_000_000_000,
    )
    await store.commit_applied_and_enqueue_lifecycle(
        delivery_id="delivery-applied",
        verified=verified,
        engine_handle="node-before-restart",
        observed_status="ready",
    )


class _QuarantinedArtifactRuntime(_RecordingArtifactRuntime):
    async def activate(self, prepared, *, loader):
        from custos.artifacts.runtime import ArtifactRuntimeActivationError

        self.events.append("activate")
        raise ArtifactRuntimeActivationError("artifact activation is durably quarantined")


async def _recover_once(store, verified, *, artifact_runtime, capacity=None) -> list[dict]:
    from tests.test_engine_recovery_persistence import supervisor

    events: list[str] = []
    restarted = RunnerCommandRuntimeCoordinator(
        intake=_Intake(),
        durability=store,
        release_resolver=_RecordingResolver(events),
        artifact_runtime=artifact_runtime(events),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=supervisor(store, _SandboxEngine()),
        delivery_policy=CommandDeliveryPolicy(in_progress_interval_seconds=0.01),
        capability_binding=lambda verified: None,
        node_capacity=capacity,
    )
    with capture_logs() as logs:
        restarted.schedule_recoveries([verified])
        await asyncio.wait_for(restarted.recoveries_settled(), timeout=5)
    return [log for log in logs if log["event"].startswith("durable_command_recovery")]


@pytest.mark.parametrize("before_restart", ["recorded", "applied"])
@pytest.mark.parametrize(
    ("cause", "reason_code"),
    [
        ("quarantined_activation", "runtime_authority_rejected:artifactruntimeactivationerror"),
        ("occupied_runner", "runtime_capacity_rejected:runner_engine_occupied"),
    ],
)
@pytest.mark.asyncio
async def test_a_recovery_that_cannot_start_is_refused_and_reported(
    tmp_path, before_restart, cause, reason_code
) -> None:
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    if before_restart == "applied":
        await _applied(store, verified)
    else:
        await _recorded(store, verified)
    facts_before = _lifecycle_facts(database)

    logs = await _recover_once(
        store,
        verified,
        artifact_runtime=(
            _QuarantinedArtifactRuntime
            if cause == "quarantined_activation"
            else _RecordingArtifactRuntime
        ),
        capacity=_Capacity(_holder(OTHER_INSTANCE)) if cause == "occupied_runner" else None,
    )

    assert [(log["event"], log["reason_code"]) for log in logs] == [
        ("durable_command_recovery_refused", reason_code)
    ]
    state = await store.load_engine_lifecycle_state(verified)
    assert state.desired_status == "quarantined"
    assert state.quarantine_reason == reason_code
    new_facts = _lifecycle_facts(database)[len(facts_before) :]
    assert [(fact["lifecycle_state"], fact["outcome"]) for fact in new_facts] == [
        ("stopped", "retry_exhausted")
    ]


class _OneNodeEngineLifecycle(_Lifecycle):
    """A one-node engine: the first instance applied holds the node."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.holder: SimpleNamespace | None = None

    def node_holder(self):
        return self.holder

    async def apply(self, **kwargs):
        instance = kwargs["verified"].command.deployment_instance_id
        self.events.append(f"apply:{instance.int}")
        self.holder = _holder(instance)
        return SimpleNamespace(deployment_instance_id=instance)

    async def commit_refusal(self, **kwargs):
        instance = kwargs["verified"].command.deployment_instance_id
        self.events.append(f"refusal:{instance.int}:{kwargs['reason_code']}")


@pytest.mark.asyncio
async def test_two_running_instances_recover_in_turn_and_the_recorded_one_is_refused() -> None:
    from custos.cli._daemon import _recover_durable_running_commands

    recorded_verified = VERIFIED
    applied_verified = _other_instance_command()
    by_instance = {INSTANCE: recorded_verified, OTHER_INSTANCE: applied_verified}
    status = {INSTANCE: "recorded", OTHER_INSTANCE: "applied"}

    class StateStore:
        async def list_recoverable_desired_command_identities(self):
            # The store lists the recorded instance first.
            return tuple(
                SimpleNamespace(
                    trading_mode="sandbox",
                    deployment_instance_id=instance,
                    deployment_spec_id=UUID(int=4),
                    deployment_spec_digest="a" * 64,
                    generation=1,
                    strategy_id=UUID(int=5),
                )
                for instance in (INSTANCE, OTHER_INSTANCE)
            )

        async def load_durable_desired_command(self, instance):
            verified = by_instance[instance]
            return SimpleNamespace(
                command=verified.command,
                command_fingerprint=verified.command_fingerprint,
                verification_receipt=object(),
            )

        async def load_engine_lifecycle_state(self, verified):
            return SimpleNamespace(desired_status=status[verified.command.deployment_instance_id])

    events: list[str] = []
    lifecycle = _OneNodeEngineLifecycle(events)
    subject = RunnerCommandRuntimeCoordinator(
        intake=_Intake(),
        durability=_Durability(events),
        release_resolver=_Resolver(),
        artifact_runtime=_ArtifactRuntime(),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=lifecycle,
        delivery_policy=CommandDeliveryPolicy(in_progress_interval_seconds=0.01),
        capability_binding=lambda verified: None,
        node_capacity=lifecycle,
    )

    await _recover_durable_running_commands(
        state_store=StateStore(),
        command_runtime=subject,
        capability=_BindingCapability(None),
    )
    await asyncio.wait_for(subject.recoveries_settled(), timeout=5)
    stop = asyncio.Event()
    stop.set()
    await subject.run_engine_supervision(stop)

    assert events == [
        f"apply:{OTHER_INSTANCE.int}",
        f"refusal:{INSTANCE.int}:runtime_capacity_rejected:runner_engine_occupied",
    ]
