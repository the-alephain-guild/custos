from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.artifacts.release_resolver import (
    StrategyReleaseResolutionRejected,
    StrategyReleaseResolutionUnavailable,
)
from custos.core.runner_command_intake import (
    CommandDeliveryPolicy,
    CommandIntakeResult,
    CommandIntakeStatus,
    InboundCommandDisposition,
)
from custos.core.runner_command_runtime import (
    RunnerCommandRuntimeCoordinator,
    RunnerCommandRuntimeStatus,
)


class _RuntimeSpec:
    credential_scope = SimpleNamespace(scope_id=UUID(int=1), scope_digest="a" * 64)

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "python"
        return {
            "deployment_instance_id": UUID(int=2),
            "deployment_spec_id": UUID(int=3),
            "deployment_spec_digest": "d" * 64,
            "generation": 1,
            "trading_mode": "sandbox",
            "connector": "binance",
        }


class _Command:
    deployment_instance_id = UUID(int=2)
    generation = 1
    trading_mode = "sandbox"
    lifecycle_state = "running"
    is_development_source = False

    def to_runtime_spec(self) -> _RuntimeSpec:
        return _RuntimeSpec()


VERIFIED = SimpleNamespace(command=_Command(), command_fingerprint="f" * 64)


class _DevelopmentCommand(_Command):
    is_development_source = True


VERIFIED_DEVELOPMENT = SimpleNamespace(
    command=_DevelopmentCommand(),
    command_fingerprint="e" * 64,
)


@dataclass
class _Delivery:
    delivered_count: int = 1
    delivery_id: str = "delivery-1"
    subject: str = "subject"
    data: bytes = b"command"

    def __post_init__(self) -> None:
        self.events: list[str] = []

    async def ack(self) -> None:
        self.events.append("ack")

    async def nak(self, delay=None) -> None:
        self.events.append(f"nak:{delay}")

    async def term(self) -> None:
        self.events.append("term")

    async def in_progress(self) -> None:
        self.events.append("in_progress")


class _Intake:
    def __init__(
        self,
        status=CommandIntakeStatus.PREPARED_FOR_APPLY,
        verified=VERIFIED,
    ) -> None:
        self.status = status
        self.verified = verified

    async def process(self, delivery):
        return CommandIntakeResult(
            status=self.status,
            disposition=InboundCommandDisposition.NONE,
            verified=self.verified,
        )


class _Durability:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def commit_verified_terminal_outcome(self, **kwargs):
        self.events.append(f"commit:{kwargs['reason_code']}")
        return SimpleNamespace(committed=True)


class _Resolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def resolve(self, verified):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            release_authority=object(),
            release_statement_bytes=b"statement",
            detached_bundle_path=object(),
            member_paths={"wheel": object()},
            verified_at=object(),
        )


class _ArtifactRuntime:
    async def prepare(self, **kwargs):
        return SimpleNamespace(
            receipt=SimpleNamespace(
                runner_local_policy_decision=SimpleNamespace(policy_id="policy-1")
            )
        )

    async def activate(self, prepared, *, loader):
        return SimpleNamespace(activation_id="activation-1", strategy=object())


class _DevelopmentArtifactRuntime:
    def __init__(self) -> None:
        self.prepared_for: UUID | None = None

    async def prepare(self, *, deployment_instance_id):
        self.prepared_for = deployment_instance_id
        return SimpleNamespace(receipt=SimpleNamespace(artifact_policy_id="dev-policy-1"))

    async def activate(self, prepared, *, loader):
        return SimpleNamespace(activation_id="development-activation-1", strategy=object())


class _CredentialResolver:
    async def resolve(self, verified, credential_scope):
        return {"scope_digest": "a" * 64}


class _Lifecycle:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def apply(self, **kwargs):
        self.events.append("apply")
        return SimpleNamespace(deployment_instance_id=UUID(int=2))

    async def apply_non_running(self, **kwargs):
        self.events.append("apply_non_running")


def _coordinator(
    events: list[str],
    resolver,
    *,
    intake=None,
    development_artifact_runtime=None,
):
    policy = CommandDeliveryPolicy(in_progress_interval_seconds=0.01)
    return RunnerCommandRuntimeCoordinator(
        intake=intake or _Intake(),
        durability=_Durability(events),
        release_resolver=resolver,
        artifact_runtime=_ArtifactRuntime(),
        development_artifact_runtime=development_artifact_runtime,
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=_Lifecycle(events),
        delivery_policy=policy,
    )


@pytest.mark.asyncio
async def test_applied_command_is_acked_only_after_lifecycle_commit() -> None:
    events: list[str] = []
    delivery = _Delivery()

    result = await _coordinator(events, _Resolver()).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert events == ["apply"]
    assert delivery.events == ["ack"]
    assert result.activation_id == "activation-1"


@pytest.mark.asyncio
async def test_unavailable_release_resolver_naks_without_ack() -> None:
    delivery = _Delivery(delivered_count=2)
    result = await _coordinator(
        [],
        _Resolver(StrategyReleaseResolutionUnavailable("offline")),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.RETRY_SCHEDULED
    assert delivery.events == ["nak:30.0"]


@pytest.mark.asyncio
async def test_rejected_release_is_durable_before_term() -> None:
    events: list[str] = []
    delivery = _Delivery()
    result = await _coordinator(
        events,
        _Resolver(StrategyReleaseResolutionRejected("conflict")),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.TERMINAL_REJECTED
    assert events == ["commit:artifact_authority_rejected:strategyreleaseresolutionrejected"]
    assert delivery.events == ["term"]


@pytest.mark.asyncio
async def test_idempotent_pending_redelivery_resumes_the_same_runtime_path() -> None:
    delivery = _Delivery(delivered_count=2)
    result = await _coordinator(
        [],
        _Resolver(),
        intake=_Intake(CommandIntakeStatus.IDEMPOTENT_PENDING),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert delivery.events == ["ack"]


@pytest.mark.asyncio
async def test_development_source_uses_local_runtime_and_shared_apply_path() -> None:
    events: list[str] = []
    delivery = _Delivery()
    development_runtime = _DevelopmentArtifactRuntime()
    resolver = _Resolver(AssertionError("production resolver must not be called"))

    result = await _coordinator(
        events,
        resolver,
        intake=_Intake(verified=VERIFIED_DEVELOPMENT),
        development_artifact_runtime=development_runtime,
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert result.activation_id == "development-activation-1"
    assert development_runtime.prepared_for == UUID(int=2)
    assert events == ["apply"]
    assert delivery.events == ["ack"]


@pytest.mark.asyncio
async def test_durable_development_command_recovers_without_inbound_ack() -> None:
    events: list[str] = []
    development_runtime = _DevelopmentArtifactRuntime()
    subject = _coordinator(
        events,
        _Resolver(AssertionError("production resolver must not be called")),
        intake=_Intake(verified=VERIFIED_DEVELOPMENT),
        development_artifact_runtime=development_runtime,
    )

    ready = await subject.recover(VERIFIED_DEVELOPMENT)

    assert ready.deployment_instance_id == UUID(int=2)
    assert development_runtime.prepared_for == UUID(int=2)
    assert events == ["apply"]


@pytest.mark.asyncio
async def test_development_source_without_local_runtime_retries_fail_closed() -> None:
    delivery = _Delivery()

    result = await _coordinator(
        [],
        _Resolver(),
        intake=_Intake(verified=VERIFIED_DEVELOPMENT),
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.RETRY_SCHEDULED
    assert result.reason_code == (
        "runtime_dependency_unavailable:developmentartifactruntimeblocked"
    )
    assert delivery.events == ["nak:10.0"]


@pytest.mark.asyncio
async def test_non_running_command_skips_artifact_resolution_and_activation() -> None:
    events: list[str] = []
    delivery = _Delivery()
    command = SimpleNamespace(
        deployment_instance_id=UUID(int=2),
        generation=2,
        trading_mode="sandbox",
        lifecycle_state="paused",
        is_development_source=True,
    )
    verified = SimpleNamespace(command=command, command_fingerprint="c" * 64)

    result = await _coordinator(
        events,
        _Resolver(AssertionError("release resolution must not run")),
        intake=_Intake(verified=verified),
        development_artifact_runtime=None,
    ).process(delivery)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert result.activation_id is None
    assert result.ready_receipt is None
    assert events == ["apply_non_running"]
    assert delivery.events == ["ack"]
