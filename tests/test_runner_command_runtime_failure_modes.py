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
