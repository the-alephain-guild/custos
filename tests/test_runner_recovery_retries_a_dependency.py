"""A recovery that meets an unavailable dependency tries again.

A restarted runner recovers its kept commands without an inbound delivery, so
there is no redelivery to retry a start that failed for a reason that may pass:
a registry that dropped one blob request, an authority endpoint still coming up.
Such a recovery used to be logged once and left until the next restart, while
the same failure on a delivered command was retried within the delivery budget.
It is now retried on the same schedule, and refused and reported like the
command once the schedule is spent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from structlog.testing import capture_logs

from custos.artifacts.release_resolver import StrategyReleaseResolutionUnavailable
from custos.core.runner_command_intake import CommandDeliveryPolicy
from custos.core.runner_command_runtime import (
    RunnerCommandRuntimeCoordinator,
    RunnerCommandRuntimeStatus,
)
from tests.test_runner_command_runtime import (
    VERIFIED,
    _ArtifactRuntime,
    _CredentialResolver,
    _Delivery,
    _Durability,
    _Intake,
    _Lifecycle,
)

REASON = "runtime_dependency_unavailable:strategyreleaseresolutionunavailable"


def _unavailable() -> StrategyReleaseResolutionUnavailable:
    try:
        raise URLError("connection reset by peer")
    except URLError as cause:
        try:
            raise StrategyReleaseResolutionUnavailable(
                "authenticated OCI registry blob request failed"
            ) from cause
        except StrategyReleaseResolutionUnavailable as error:
            return error


class _FlakyResolver:
    """Unavailable for the first ``failures`` lookups, then resolves."""

    def __init__(self, events: list[str], *, failures: int) -> None:
        self.events = events
        self.failures = failures

    async def resolve(self, verified):
        self.events.append("resolve")
        if self.failures > 0:
            self.failures -= 1
            raise _unavailable()
        return SimpleNamespace(
            release_authority=object(),
            release_statement_bytes=b"statement",
            detached_bundle_path=object(),
            member_paths={"wheel": object()},
            verified_at=object(),
        )


def _policy(*, attempts: int, backoff: float) -> CommandDeliveryPolicy:
    return CommandDeliveryPolicy(
        max_deliver=attempts,
        backoff_seconds=(backoff,) * attempts,
        in_progress_interval_seconds=0.001,
        quarantine_after_deliveries=attempts,
    )


def _subject(
    events: list[str],
    resolver,
    *,
    policy: CommandDeliveryPolicy,
    durability=None,
    lifecycle=None,
    intake=None,
) -> RunnerCommandRuntimeCoordinator:
    return RunnerCommandRuntimeCoordinator(
        intake=intake or _Intake(),
        durability=durability or _Durability(events),
        release_resolver=resolver,
        artifact_runtime=_ArtifactRuntime(),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=lifecycle or _Lifecycle(events),
        delivery_policy=policy,
        capability_binding=lambda verified: None,
    )


def _recovery_logs(logs: list[dict]) -> list[dict]:
    return [log for log in logs if log["event"].startswith("durable_command_recover")]


@pytest.mark.asyncio
async def test_a_recovery_whose_dependency_is_briefly_unavailable_starts_on_a_retry() -> None:
    events: list[str] = []
    subject = _subject(
        events,
        _FlakyResolver(events, failures=1),
        policy=_policy(attempts=3, backoff=0.01),
    )

    with capture_logs() as logs:
        subject.schedule_recovery(VERIFIED)
        await asyncio.wait_for(subject.recoveries_settled(), timeout=5)

    assert events == ["resolve", "resolve", "apply"]
    recovery = _recovery_logs(logs)
    assert [log["event"] for log in recovery] == [
        "durable_command_recovery_retry_scheduled",
        "durable_command_recovered",
    ]
    retry = recovery[0]
    assert retry["attempt"] == 1
    assert retry["max_attempts"] == 3
    assert retry["retry_in_seconds"] == 0.01
    assert retry["reason_code"] == REASON
    # The cause has to be readable from the log: the type alone did not say
    # which dependency failed or how.
    assert retry["error_type"] == "StrategyReleaseResolutionUnavailable"
    assert retry["error_message"] == "authenticated OCI registry blob request failed"
    assert retry["error_cause_types"] == ["URLError"]


@pytest.mark.asyncio
async def test_a_recovery_whose_dependency_stays_unavailable_is_refused_and_reported(
    tmp_path,
) -> None:
    from tests.test_engine_recovery_persistence import supervisor
    from tests.test_runner_command_runtime_failure_modes import (
        _lifecycle_facts,
        _recorded,
        _SandboxEngine,
    )
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await _recorded(store, verified)
    facts_before = _lifecycle_facts(database)
    events: list[str] = []
    subject = _subject(
        events,
        _FlakyResolver(events, failures=10),
        policy=_policy(attempts=2, backoff=0.01),
        durability=store,
        lifecycle=supervisor(store, _SandboxEngine()),
    )

    with capture_logs() as logs:
        subject.schedule_recoveries([verified])
        await asyncio.wait_for(subject.recoveries_settled(), timeout=5)

    assert events == ["resolve", "resolve"]
    reason_code = f"retry_exhausted:{REASON}"
    assert [(log["event"], log.get("reason_code")) for log in _recovery_logs(logs)] == [
        ("durable_command_recovery_retry_scheduled", REASON),
        ("durable_command_recovery_refused", reason_code),
    ]
    state = await store.load_engine_lifecycle_state(verified)
    assert state.desired_status == "quarantined"
    assert state.quarantine_reason == reason_code
    new_facts = _lifecycle_facts(database)[len(facts_before) :]
    assert [(fact["lifecycle_state"], fact["outcome"]) for fact in new_facts] == [
        ("stopped", "retry_exhausted")
    ]


@pytest.mark.asyncio
async def test_a_stop_for_a_recovery_waiting_to_retry_takes_effect_at_once() -> None:
    events: list[str] = []
    stop = SimpleNamespace(
        command=SimpleNamespace(
            deployment_instance_id=VERIFIED.command.deployment_instance_id,
            generation=2,
            trading_mode="sandbox",
            lifecycle_state="stopped",
            is_development_source=False,
        ),
        command_fingerprint="c" * 64,
    )
    subject = _subject(
        events,
        _FlakyResolver(events, failures=10),
        policy=_policy(attempts=3, backoff=60.0),
        intake=_Intake(verified=stop),
    )
    subject.schedule_recovery(VERIFIED)

    async def first_attempt_failed() -> None:
        while events != ["resolve"]:
            await asyncio.sleep(0)

    await asyncio.wait_for(first_attempt_failed(), timeout=1.0)
    # Let the failed attempt reach its back-off.
    for _ in range(10):
        await asyncio.sleep(0)

    result = await asyncio.wait_for(subject.process(_Delivery()), timeout=1.0)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert events == ["resolve", "apply_non_running"]
    assert not subject.recovery_in_progress(VERIFIED.command.deployment_instance_id)


class _BlockedStopLifecycle(_Lifecycle):
    async def apply_non_running(self, **kwargs):
        from custos.core.engine_lifecycle import EngineLifecycleBlocked

        self.events.append("apply_non_running")
        raise EngineLifecycleBlocked("engine is not available")


@pytest.mark.asyncio
async def test_a_kept_stop_still_failing_after_its_last_attempt_is_kept_not_refused() -> None:
    # The instance was never started by this process; refusing the stop would
    # drop the outcome it still owes. It stays for the next restart.
    events: list[str] = []
    stop = SimpleNamespace(
        command=SimpleNamespace(
            deployment_instance_id=VERIFIED.command.deployment_instance_id,
            generation=2,
            trading_mode="sandbox",
            lifecycle_state="stopped",
            is_development_source=False,
        ),
        command_fingerprint="c" * 64,
    )
    subject = _subject(
        events,
        _FlakyResolver(events, failures=0),
        policy=_policy(attempts=2, backoff=0.01),
        lifecycle=_BlockedStopLifecycle(events),
    )

    with capture_logs() as logs:
        subject.schedule_recovery(stop)
        await asyncio.wait_for(subject.recoveries_settled(), timeout=5)

    assert events == ["apply_non_running", "apply_non_running"]
    assert [log["event"] for log in _recovery_logs(logs)] == [
        "durable_command_recovery_retry_scheduled",
        "durable_command_recovery_failed",
    ]
