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
