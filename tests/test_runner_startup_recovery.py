"""A restarted runner recovers its deployments without waiting on them.

Recovering a deployment waits for its engine to become ready, which waits for
the venue. A daemon that recovered inline before starting anything else stayed
unready, produced no RunnerFacts and could not receive a stop for as long as a
venue was unreachable, and a recovery that ran out of attempts took the whole
process down with it. Recovery now runs per deployment in the background.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from custos.core.engine_lifecycle import EngineLifecycleQuarantined
from custos.core.runner_command_runtime import RunnerCommandRuntimeStatus
from tests.test_runner_command_runtime import (
    VERIFIED,
    _Command,
    _coordinator,
    _Delivery,
    _Intake,
    _Lifecycle,
    _Resolver,
)


class _SecondCommand(_Command):
    deployment_instance_id = UUID(int=3)


VERIFIED_SECOND = SimpleNamespace(command=_SecondCommand(), command_fingerprint="d" * 64)


class _GatedLifecycle(_Lifecycle):
    """An engine start that waits for the venue until the test lets it through."""

    def __init__(self, events: list[str], *, failing: set[UUID] | None = None) -> None:
        super().__init__(events)
        self.gate = asyncio.Event()
        self.failing = failing or set()

    async def apply(self, **kwargs):
        instance = kwargs["verified"].command.deployment_instance_id
        self.events.append(f"apply:{instance.int}")
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.events.append(f"apply_cancelled:{instance.int}")
            raise
        if instance in self.failing:
            raise EngineLifecycleQuarantined("engine_ready_timeout")
        self.events.append(f"ready:{instance.int}")
        return SimpleNamespace(deployment_instance_id=instance)


async def _until(predicate, *, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_scheduling_a_recovery_does_not_wait_for_the_engine() -> None:
    events: list[str] = []
    lifecycle = _GatedLifecycle(events)
    subject = _coordinator(events, _Resolver(), lifecycle=lifecycle)

    subject.schedule_recovery(VERIFIED)
    await _until(lambda: "apply:2" in events)

    assert subject.recovery_in_progress(UUID(int=2))
    lifecycle.gate.set()
    await subject.recoveries_settled()
    assert "ready:2" in events
    assert not subject.recovery_in_progress(UUID(int=2))


@pytest.mark.asyncio
async def test_one_deployment_that_cannot_recover_does_not_stop_the_others() -> None:
    events: list[str] = []
    lifecycle = _GatedLifecycle(events, failing={UUID(int=2)})
    subject = _coordinator(events, _Resolver(), lifecycle=lifecycle)

    with capture_logs() as logs:
        subject.schedule_recovery(VERIFIED)
        subject.schedule_recovery(VERIFIED_SECOND)
        await _until(lambda: {"apply:2", "apply:3"} <= set(events))
        lifecycle.gate.set()
        await subject.recoveries_settled()

    assert "ready:3" in events
    failures = [log for log in logs if log["event"] == "durable_command_recovery_failed"]
    assert len(failures) == 1
    assert failures[0]["deployment_instance_id"] == str(UUID(int=2))
    assert failures[0]["error_type"] == "EngineLifecycleQuarantined"


@pytest.mark.asyncio
async def test_a_stop_for_a_deployment_still_recovering_takes_effect_at_once() -> None:
    # The venue may stay unreachable for minutes. A stop must not queue behind
    # every recovery attempt: it cancels the recovery and applies right away.
    events: list[str] = []
    lifecycle = _GatedLifecycle(events)
    stop = SimpleNamespace(
        command=SimpleNamespace(
            deployment_instance_id=UUID(int=2),
            generation=2,
            trading_mode="sandbox",
            lifecycle_state="stopped",
            is_development_source=False,
        ),
        command_fingerprint="c" * 64,
    )
    subject = _coordinator(events, _Resolver(), intake=_Intake(verified=stop), lifecycle=lifecycle)
    subject.schedule_recovery(VERIFIED)
    await _until(lambda: "apply:2" in events)

    result = await asyncio.wait_for(subject.process(_Delivery()), timeout=1.0)

    assert result.status is RunnerCommandRuntimeStatus.APPLIED_ACKED
    assert events.index("apply_cancelled:2") < events.index("apply_non_running")
    assert "ready:2" not in events
    assert not subject.recovery_in_progress(UUID(int=2))


@pytest.mark.asyncio
async def test_closing_cancels_recoveries_still_in_flight() -> None:
    events: list[str] = []
    lifecycle = _GatedLifecycle(events)
    subject = _coordinator(events, _Resolver(), lifecycle=lifecycle)
    subject.schedule_recovery(VERIFIED)
    await _until(lambda: "apply:2" in events)

    await asyncio.wait_for(subject.close_recoveries(), timeout=1.0)

    assert "apply_cancelled:2" in events
    assert not subject.recovery_in_progress(UUID(int=2))


@pytest.mark.asyncio
async def test_the_daemon_schedules_recoveries_instead_of_waiting_for_them() -> None:
    from custos.cli._daemon import _recover_durable_running_commands

    identity = SimpleNamespace(
        trading_mode="sandbox",
        deployment_instance_id=UUID(int=2),
        deployment_spec_id=UUID(int=4),
        deployment_spec_digest="a" * 64,
        strategy_id=UUID(int=5),
    )

    class StateStore:
        async def list_recoverable_desired_command_identities(self):
            return (identity,)

        async def load_durable_desired_command(self, instance):
            return SimpleNamespace(
                command=_Command(),
                command_fingerprint="f" * 64,
                verification_receipt=object(),
            )

    class Capability:
        def require_scope_bindings(self, **kwargs) -> None:
            return None

    class Runtime:
        def __init__(self) -> None:
            self.scheduled: list[UUID] = []

        def schedule_recovery(self, verified) -> None:
            self.scheduled.append(verified.command.deployment_instance_id)

        async def recover(self, verified):
            raise AssertionError("startup must not wait for an engine to recover")

    runtime = Runtime()
    await _recover_durable_running_commands(
        state_store=StateStore(), command_runtime=runtime, capability=Capability()
    )

    assert runtime.scheduled == [UUID(int=2)]
