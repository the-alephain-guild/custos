"""Shutting down must shut things down, and a record is not the world.

Two defects that meet at the same consequence -- the runner cannot be stopped.

``wait_terminal`` shields the node task and then reported every ``CancelledError``
as the node having been cancelled. Cancelling the *watcher* raises there too, while
the shielded node keeps running, so a normal daemon shutdown restarted the engine it
was shutting down; and because the cancel was swallowed rather than propagated, the
supervision task went back to waiting and the gather that follows never finished.

Separately, the two ``commit_*`` calls that record a started engine sit outside the
cleanup that covers deploy and readiness. A commit failure left the engine running
with nothing recording it, and the retry could not stop what it had no handle for --
so the durable record reached ``quarantined`` while the engine traded on.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from custos.core.engine_protocol import EngineTerminalEvent
from custos.engines.nautilus.host import NtTradingNodeHost
from tests.test_engine_lifecycle import (
    _Artifact,
    _authority,
    _Engine,
    _ready,
    _Store,
    _supervisor,
    _verified,
)

_RUNTIME_SPEC = {"trading_mode": "sandbox", "connector": "binance"}


async def _never_ending_task() -> asyncio.Task[None]:
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    return task


async def _drain(task: asyncio.Task[object]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _host_with_running_node(authority, task: asyncio.Task[None]) -> NtTradingNodeHost:
    host = NtTradingNodeHost()
    instance = str(authority.deployment_instance_id)
    host._lifecycle_authorities[instance] = authority  # noqa: SLF001 - no real venue here
    host._active_nodes[instance] = NS(task=task)  # noqa: SLF001 - no real venue here
    return host


# --------------------------------------------------------------------------- LB-2


@pytest.mark.asyncio
async def test_cancelling_the_watcher_does_not_invent_a_terminal_event() -> None:
    """The cancel was aimed at the waiter; shield kept the node running."""
    authority = _authority()
    node = await _never_ending_task()
    host = _host_with_running_node(authority, node)
    watcher = asyncio.create_task(host.wait_terminal(authority))
    await asyncio.sleep(0)

    watcher.cancel()

    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert watcher.cancelled(), "a swallowed cancel leaves the task alive for a gather"
    assert not node.done(), "the node was shielded and must still be running"
    await _drain(node)


@pytest.mark.asyncio
async def test_a_genuinely_cancelled_node_is_still_a_terminal_event() -> None:
    """The control. This behaviour is correct and must survive the fix."""
    authority = _authority()
    node = await _never_ending_task()
    host = _host_with_running_node(authority, node)
    watcher = asyncio.create_task(host.wait_terminal(authority))
    await asyncio.sleep(0)

    node.cancel()

    event = await watcher
    assert isinstance(event, EngineTerminalEvent)
    assert event.reason_code == "engine_task_cancelled"
    assert node.cancelled()


@pytest.mark.asyncio
async def test_a_node_that_fails_is_still_a_terminal_event() -> None:
    authority = _authority()

    async def explode() -> None:
        raise RuntimeError("venue connection lost")

    node = asyncio.create_task(explode())
    host = _host_with_running_node(authority, node)

    event = await host.wait_terminal(authority)

    assert event.reason_code == "engine_task_failed"


@pytest.mark.asyncio
async def test_a_node_that_exits_on_its_own_is_still_a_terminal_event() -> None:
    authority = _authority()

    async def finish() -> None:
        return None

    node = asyncio.create_task(finish())
    host = _host_with_running_node(authority, node)

    event = await host.wait_terminal(authority)

    assert event.reason_code == "engine_task_exited"


@pytest.mark.asyncio
async def test_cancelling_supervision_neither_redeploys_nor_spends_the_budget() -> None:
    """The whole point: stopping must not restart what it is stopping."""
    verified = _verified()
    authority = _authority(verified)
    store = _Store()
    await store.commit_applied_and_enqueue_lifecycle(
        verified=verified, engine_handle="healthy", observed_status="ready"
    )
    node = await _never_ending_task()
    host = _host_with_running_node(authority, node)
    entered = asyncio.Event()

    class _HostBackedEngine(_Engine):
        async def wait_terminal(self, value):
            entered.set()
            return await host.wait_terminal(value)

    engine = _HostBackedEngine([_ready(verified)])
    supervisor = _supervisor(store, engine)
    watcher = asyncio.create_task(
        supervisor.supervise_once(
            delivery_id="shutdown",
            verified=verified,
            runtime_spec=_RUNTIME_SPEC,
            credential={},
            artifact=_Artifact(),
        )
    )
    await entered.wait()
    await asyncio.sleep(0)

    watcher.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher, timeout=1)
    assert engine.deploy_calls == 0, "cancelling supervision must not start an engine"
    assert engine.stop_calls == 0, "nor stop the one it was watching"
    assert store.state.restart_count == 0, "nor spend the restart budget"
    assert not node.done()
    await _drain(node)


@pytest.mark.asyncio
async def test_cancelling_before_the_node_is_registered_is_still_a_clean_cancel() -> None:
    """Cancelling during start-up: there is no node to report a terminal for."""
    authority = _authority()
    host = NtTradingNodeHost()
    instance = str(authority.deployment_instance_id)
    host._lifecycle_authorities[instance] = authority  # noqa: SLF001 - no real venue here

    event = await host.wait_terminal(authority)

    assert event.reason_code == "engine_task_missing"


# --------------------------------------------------------------------------- LB-1


class _CommitFailsOnce(_Store):
    """A store whose first ready-commit fails, the way a locked database would."""

    def __init__(self, *, method: str = "commit_applied_and_enqueue_lifecycle") -> None:
        super().__init__()
        self._method = method
        self.failed = False

    async def commit_applied_and_enqueue_lifecycle(self, **kwargs):
        if self._method == "commit_applied_and_enqueue_lifecycle" and not self.failed:
            self.failed = True
            raise RuntimeError("database is locked")
        await super().commit_applied_and_enqueue_lifecycle(**kwargs)

    async def commit_recovered_engine_ready(self, **kwargs):
        if self._method == "commit_recovered_engine_ready" and not self.failed:
            self.failed = True
            raise RuntimeError("database is locked")
        await super().commit_recovered_engine_ready(**kwargs)


class _RefusesSecondDeploy(_Engine):
    """A host that refuses to deploy an instance it already has, like the real one."""

    def __init__(self, ready_results: list[object]) -> None:
        super().__init__(ready_results)
        self.live = False

    async def deploy(self, spec: dict, credential: dict, artifact: object) -> str:
        if self.live:
            raise RuntimeError("instance already deployed; call stop first")
        self.live = True
        return await super().deploy(spec, credential, artifact)

    async def stop(self, deployment_instance_id: str) -> None:
        self.live = False
        await super().stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_a_failed_ready_commit_stops_the_engine_it_could_not_record(
    tmp_path,
) -> None:
    verified = _verified()
    store = _CommitFailsOnce()
    engine = _RefusesSecondDeploy([_ready(verified), _ready(verified)])
    supervisor = _supervisor(store, engine)

    receipt = await supervisor.apply(
        delivery_id="commit-fails",
        verified=verified,
        runtime_spec=_RUNTIME_SPEC,
        credential={},
        artifact=_Artifact(),
    )

    assert receipt is not None, "the retry must be able to start cleanly"
    assert engine.stop_calls == 1, "the engine nobody recorded must not be left running"
    assert engine.deploy_calls == 2
    assert store.state.applied_generation == verified.command.generation


@pytest.mark.asyncio
async def test_a_failed_recovery_commit_stops_the_engine_too() -> None:
    """The recovered branch is the same defect's other exit."""
    verified = _verified()
    store = _CommitFailsOnce(method="commit_recovered_engine_ready")
    await store.commit_applied_and_enqueue_lifecycle(
        verified=verified, engine_handle="old", observed_status="ready"
    )
    engine = _RefusesSecondDeploy([_ready(verified), _ready(verified)])
    engine.terminal_events.append(
        EngineTerminalEvent.from_authority(
            _authority(verified), reason_code="engine_task_exited", retryable=True
        )
    )
    supervisor = _supervisor(store, engine)

    receipt = await supervisor.supervise_once(
        delivery_id="recover-commit-fails",
        verified=verified,
        runtime_spec=_RUNTIME_SPEC,
        credential={},
        artifact=_Artifact(),
    )

    assert receipt is not None
    # One stop for the terminal event, one for the engine the commit could not record.
    assert engine.stop_calls == 2
    assert store.state.applied_generation == verified.command.generation


@pytest.mark.asyncio
async def test_quarantine_means_the_engine_is_actually_stopped() -> None:
    """Isolating the record proves nothing about the process."""
    from custos.core.engine_lifecycle import EngineLifecycleQuarantined

    verified = _verified()

    class _AlwaysFails(_Store):
        async def commit_applied_and_enqueue_lifecycle(self, **kwargs):
            raise RuntimeError("database is locked")

    store = _AlwaysFails()
    engine = _RefusesSecondDeploy([_ready(verified) for _ in range(6)])
    supervisor = _supervisor(store, engine, restart_budget=1)

    with pytest.raises((EngineLifecycleQuarantined, RuntimeError)):
        await supervisor.apply(
            delivery_id="always-fails",
            verified=verified,
            runtime_spec=_RUNTIME_SPEC,
            credential={},
            artifact=_Artifact(),
        )

    assert engine.live is False, "a quarantined record must not leave an engine trading"
    assert engine.stop_calls == engine.deploy_calls


@pytest.mark.asyncio
async def test_a_failure_before_the_commit_still_behaves_as_it_did() -> None:
    """Regression: the pre-existing cleanup path must not change."""
    verified = _verified()
    store = _Store()
    engine = _Engine([TimeoutError("ready timed out"), _ready(verified)])
    supervisor = _supervisor(store, engine)

    receipt = await supervisor.apply(
        delivery_id="ready-times-out",
        verified=verified,
        runtime_spec=_RUNTIME_SPEC,
        credential={},
        artifact=_Artifact(),
    )

    assert receipt is not None
    assert engine.deploy_calls == 2
    assert engine.stop_calls == 1, "the first deploy's handle is stopped before the retry"


# ------------------------------------------------------- the two meet at shutdown


@pytest.mark.asyncio
async def test_shutdown_returns_and_starts_nothing() -> None:
    """Both defects end here: the daemon could not stop.

    This drives the real supervision task through the real host ``wait_terminal``.
    Before the fix the cancel in ``run_engine_supervision``'s finally was swallowed,
    so the task reported a terminal it had invented and went straight back to
    waiting -- the gather never returned, and on its way out it restarted the engine
    the shutdown was tearing down.

    The waiting is done with ``asyncio.wait``, which does **not** cancel what it is
    waiting on: under the defect a cancel-based timeout is itself swallowed, so the
    test would hang instead of failing. The double also refuses to be asked more than
    twice, which is what lets the teardown below finish; a real supervisor loops.
    """
    from tests.test_runner_command_runtime import (
        _coordinator,
        _Delivery,
        _Lifecycle,
        _Resolver,
    )

    authority = _authority()
    node = await _never_ending_task()
    host = _host_with_running_node(authority, node)
    started: list[str] = []
    watching = asyncio.Event()

    class _HostBackedLifecycle(_Lifecycle):
        calls = 0

        async def supervise_once(self, **kwargs):
            type(self).calls += 1
            if type(self).calls > 2:
                return None
            watching.set()
            event = await host.wait_terminal(authority)
            # Reaching here means a terminal was reported. During a shutdown that is
            # an invented one, and returning a receipt sends the loop round again --
            # which is what a restart looks like from here.
            started.append(event.reason_code)
            return NS(deployment_instance_id=authority.deployment_instance_id)

    events: list[str] = []
    coordinator = _coordinator(events, _Resolver(), lifecycle=_HostBackedLifecycle(events))
    await coordinator.process(_Delivery())
    await asyncio.wait_for(watching.wait(), timeout=1)
    await asyncio.sleep(0)

    stop = asyncio.Event()
    stop.set()
    shutdown = asyncio.create_task(coordinator.run_engine_supervision(stop))
    done, _pending = await asyncio.wait({shutdown}, timeout=2)

    try:
        assert shutdown in done, (
            "shutdown must return; a swallowed cancel leaves the supervision task "
            "waiting and the gather never finishes"
        )
        assert started == [], "shutting down must not report a terminal and restart"
        assert not node.done(), "the node itself was never cancelled"
    finally:
        await _drain(node)
        await asyncio.wait({shutdown}, timeout=2)
        if not shutdown.done():  # pragma: no cover - teardown safety net
            await _drain(shutdown)
