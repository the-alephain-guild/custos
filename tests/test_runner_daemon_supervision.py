from __future__ import annotations

import asyncio

import pytest

from custos.cli._daemon import _shutdown_in_order, _supervise_long_running_tasks


@pytest.mark.asyncio
async def test_unexpected_long_running_task_failure_cancels_siblings_and_raises() -> None:
    stop = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def fails() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("engine loop failed")

    async def sibling() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    tasks = [asyncio.create_task(fails()), asyncio.create_task(sibling())]
    with pytest.raises(RuntimeError, match="engine loop failed"):
        await _supervise_long_running_tasks(tasks, stop)

    assert stop.is_set()
    assert sibling_cancelled.is_set()
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_daemon_shutdown_stops_deployments_flushes_facts_then_closes_transports() -> None:
    events: list[str] = []
    stop = asyncio.Event()

    class Host:
        async def close(self) -> None:
            events.append("stop_deployments")

    class Outbox:
        async def pending(self):
            return []

    class Publisher:
        async def drain_once(self) -> int:
            events.append("flush_facts")
            return 0

        async def close(self) -> None:
            events.append("close_publisher")

    class Client:
        async def close(self) -> None:
            events.append("close_nats")

    class Runtime:
        async def close_recoveries(self) -> None:
            events.append("cancel_recoveries")

    await _shutdown_in_order(
        stop=stop,
        tasks=[],
        host=Host(),
        fact_outbox=Outbox(),
        fact_publisher=Publisher(),
        clients={"sandbox": Client()},
        command_runtime=Runtime(),
    )

    assert stop.is_set()
    # A recovery still starting an engine is cancelled before the deployments
    # are stopped, or it could start one after the host was closed.
    assert events == [
        "cancel_recoveries",
        "stop_deployments",
        "flush_facts",
        "close_publisher",
        "close_nats",
    ]
