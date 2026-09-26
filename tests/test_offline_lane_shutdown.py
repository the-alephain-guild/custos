"""Stopping the offline lane stops what it runs, so each strategy gets to clean up.

Before this, the lane ignored SIGTERM until its supervisor killed it, and even a
stop it did notice left every deployment running until the process died -- so a
strategy's own stop handler, which cancels what it left resting on the exchange,
never ran.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from custos.offline.daemon import run_offline_lane
from custos.offline.spec import OfflineDeploymentMessage
from tests.test_offline_lane_daemon import (
    RUNNER,
    RUNNER_ID,
    STRATEGY,
    TENANT,
    TRADING,
    _FakeConnection,
    _FakeEngine,
    _FakeJetStream,
    _spec,
)


class _Engine(_FakeEngine):
    def __init__(self, connection: list[_FakeConnection]) -> None:
        super().__init__()
        self.stops: list[str] = []
        self.stop_delay = 0.0
        self.stop_error: Exception | None = None
        self.drained_at_stop: list[bool] = []
        self._connection = connection

    async def stop(self, deployment_instance_id: str) -> None:
        self.stops.append(deployment_instance_id)
        self.drained_at_stop.append(self._connection[0].drained)
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        if self.stop_error is not None:
            raise self.stop_error
        await super().stop(deployment_instance_id)


class _IdleSubscription:
    """Delivers the given messages, then waits like a quiet subject does."""

    def __init__(self, messages: list[bytes]) -> None:
        self._messages = messages

    async def next_msg(self, timeout: float) -> Any:
        if self._messages:
            return type("_Msg", (), {"data": self._messages.pop(0)})()
        await asyncio.sleep(min(timeout, 0.01))
        raise TimeoutError


class _JetStream(_FakeJetStream):
    async def subscribe(self, subject: str) -> _IdleSubscription:
        self.subscribed.append(subject)
        return _IdleSubscription(self._messages)


def _message(spec_id: str = "supertrend-sandbox", **overrides: Any) -> bytes:
    return OfflineDeploymentMessage.create(
        tenant_id=TENANT, strategy_id=STRATEGY, spec=_spec(spec_id=spec_id, **overrides)
    ).to_bytes()


async def _start(
    tmp_path: Path, messages: list[bytes], **overrides: Any
) -> tuple[asyncio.Task[int], asyncio.Event, _Engine, _JetStream, _FakeConnection]:
    stop = asyncio.Event()
    jetstream = _JetStream(messages, stop)
    connection = _FakeConnection(jetstream)
    engine = _Engine([connection])

    async def connect(url: str) -> _FakeConnection:
        return connection

    lane = asyncio.create_task(
        run_offline_lane(
            tenant_id=TENANT,
            runner_id=RUNNER_ID,
            runner_label=RUNNER,
            strategy_id=STRATEGY,
            nats_url="nats://nats:4222",
            vault_dir=tmp_path / "vault",
            engine=engine,
            ready_file=tmp_path / "ready.json",
            state_path=tmp_path / "state" / "offline.db",
            connect_factory=connect,
            credential_for=lambda spec: {"api_key": "k", "api_secret": "s"},
            strategy_config_for=lambda spec: TRADING,
            stop=stop,
            **overrides,
        )
    )
    return lane, stop, engine, jetstream, connection


async def _until(condition: Any, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


def _statuses(jetstream: _FakeJetStream) -> list[dict[str, Any]]:
    return [
        json.loads(payload)["payload"]
        for subject, payload in jetstream.published
        if ".deployment_status." in subject
    ]


async def test_sigterm_stops_the_lane_and_the_deployments_it_runs(tmp_path: Path) -> None:
    before = signal.getsignal(signal.SIGTERM)
    lane, _, engine, _, _ = await _start(tmp_path, [_message()])
    await _until(lambda: engine.deployed)
    # Sending the signal before the lane owns it would terminate the test run.
    await _until(lambda: signal.getsignal(signal.SIGTERM) != before)

    os.kill(os.getpid(), signal.SIGTERM)

    assert await asyncio.wait_for(lane, timeout=2) == 0
    assert engine.stops == [str(engine.deployed[0]["deployment_instance_id"])]
    assert signal.getsignal(signal.SIGTERM) == before


async def test_every_running_deployment_is_stopped_once_before_the_connection_closes(
    tmp_path: Path,
) -> None:
    lane, stop, engine, jetstream, connection = await _start(
        tmp_path, [_message("first"), _message("second")]
    )
    await _until(lambda: len(engine.deployed) == 2)

    stop.set()

    assert await asyncio.wait_for(lane, timeout=2) == 0
    deployed = sorted(str(spec["deployment_instance_id"]) for spec in engine.deployed)
    assert sorted(engine.stops) == deployed
    assert engine.drained_at_stop == [False, False]
    assert connection.drained is True
    assert not (tmp_path / "ready.json").exists()
    stopped = [status for status in _statuses(jetstream) if status["phase"] == "stopped"]
    assert [status["health"] for status in stopped] == ["healthy", "healthy"]


async def test_a_stop_that_does_not_finish_in_time_fails_the_exit_code(tmp_path: Path) -> None:
    lane, stop, engine, jetstream, _ = await _start(tmp_path, [_message()], shutdown_deadline=0.05)
    await _until(lambda: engine.deployed)
    engine.stop_delay = 5

    stop.set()

    assert await asyncio.wait_for(lane, timeout=2) == 1
    assert _statuses(jetstream)[-1] == {
        "observed_generation": 1,
        "phase": "stopped",
        "health": "unhealthy",
    }


async def test_a_failing_stop_does_not_skip_the_other_deployments(tmp_path: Path) -> None:
    lane, stop, engine, _, _ = await _start(tmp_path, [_message("first"), _message("second")])
    await _until(lambda: len(engine.deployed) == 2)
    engine.stop_error = RuntimeError("node refused")

    stop.set()

    assert await asyncio.wait_for(lane, timeout=2) == 1
    assert len(engine.stops) == 2


async def test_a_lane_that_ran_nothing_stops_without_reporting(tmp_path: Path) -> None:
    lane, stop, engine, jetstream, _ = await _start(tmp_path, [])
    await asyncio.sleep(0.05)

    stop.set()

    assert await asyncio.wait_for(lane, timeout=2) == 0
    assert engine.stops == []
    assert jetstream.published == []


async def test_a_deployment_stopped_by_its_spec_is_not_stopped_again(tmp_path: Path) -> None:
    lane, stop, engine, _, _ = await _start(
        tmp_path, [_message(), _message(generation=2, lifecycle_state="stopped")]
    )
    await _until(lambda: len(engine.stops) == 1)

    stop.set()

    assert await asyncio.wait_for(lane, timeout=2) == 0
    assert len(engine.stops) == 1


async def test_a_second_signal_while_stopping_changes_nothing(tmp_path: Path) -> None:
    lane, stop, engine, _, _ = await _start(tmp_path, [_message()])
    await _until(lambda: engine.deployed)
    engine.stop_delay = 0.1

    stop.set()
    await _until(lambda: engine.stops)
    stop.set()

    assert await asyncio.wait_for(lane, timeout=2) == 0
    assert len(engine.stops) == 1
