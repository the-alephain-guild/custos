"""Source faults suspend intake without stopping signed trading or flattening positions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custos.engines.nautilus.external_source_authorization import load_source_subscription
from custos.engines.nautilus.external_source_nats import ExternalSourceNatsProfile
from custos.engines.nautilus.external_source_runner_runtime import (
    ExternalSourceRunnerRuntime,
    SourceRunnerOptions,
)
from tests.test_external_source_authorization import RUNNER, local_files, rewrite, scope
from tests.test_external_source_runner_session import Broker, Host


def options(tmp_path: Path) -> tuple[SourceRunnerOptions, Broker]:
    authorization, credentials = local_files(tmp_path)
    rewrite(authorization, sourceId="synthetic-supertrend", sourceSymbol="ETH-USDT")
    grant = load_source_subscription(
        authorization,
        credentials_path=credentials,
        expected_tenant_id="tenant-a",
        expected_runner_id=RUNNER,
        active_scope=scope(),
    )
    return (
        SourceRunnerOptions(
            instance_id=scope().authority.deployment_instance_id,
            authorization_path=authorization,
            credentials_path=credentials,
            inbox_path=tmp_path / "target.sqlite",
            nats_url="nats://broker.example:4222",
            nats_ca=tmp_path / "ca.pem",
            nats_server_name="broker.example",
        ),
        Broker(grant.spec.consumer_config),
    )


@pytest.mark.asyncio
async def test_invalid_local_grant_reports_fault_without_host_flatten(
    tmp_path: Path, monkeypatch
) -> None:
    config, broker = options(tmp_path)
    rewrite(config.authorization_path, generation=8)
    stop = asyncio.Event()
    states = []
    host = Host()

    async def unexpected_connect(self, **kwargs):
        raise AssertionError("invalid grant must not connect")

    monkeypatch.setattr(ExternalSourceNatsProfile, "connect", unexpected_connect)

    def report(state: str, reason: str | None) -> None:
        states.append((state, reason))
        if state == "fault":
            stop.set()

    runtime = ExternalSourceRunnerRuntime(
        options=config,
        tenant_id="tenant-a",
        runner_id=RUNNER,
        host=host,
        report=report,
    )
    await asyncio.wait_for(runtime.run(stop), timeout=2)
    assert states == [("fault", "authorization_invalid")]
    assert host.queued == []
    assert broker.bound is False


@pytest.mark.asyncio
async def test_active_source_session_uses_separate_connection_and_stops_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    config, broker = options(tmp_path)
    stop = asyncio.Event()
    states = []
    host = Host()

    class Subscription:
        unsubscribed = False

        async def next_msg(self, *, timeout: float):
            await asyncio.sleep(timeout)
            raise TimeoutError

        async def unsubscribe(self) -> None:
            self.unsubscribed = True

    broker.subscription = Subscription()

    class Connection:
        closed = False

        def jetstream(self):
            return broker

        async def close(self):
            self.closed = True

    connection = Connection()

    async def connect(self, **kwargs):
        assert self.grant.binding.authority == scope().authority
        assert self.grant.credentials_path == config.credentials_path
        return connection

    monkeypatch.setattr(ExternalSourceNatsProfile, "connect", connect)

    def report(state: str, reason: str | None) -> None:
        states.append((state, reason))
        if state == "active":
            stop.set()

    runtime = ExternalSourceRunnerRuntime(
        options=config,
        tenant_id="tenant-a",
        runner_id=RUNNER,
        host=host,
        report=report,
    )
    await asyncio.wait_for(runtime.run(stop), timeout=2)
    assert states == [("active", None)]
    assert connection.closed
    assert broker.subscription.unsubscribed
    assert host.queued == []


@pytest.mark.asyncio
async def test_broker_disconnect_suspends_source_without_flattening_host(
    tmp_path: Path, monkeypatch
) -> None:
    config, broker = options(tmp_path)
    stop = asyncio.Event()
    states = []
    host = Host()
    callback = None

    class Subscription:
        unsubscribed = False

        async def next_msg(self, *, timeout: float):
            await asyncio.sleep(timeout)
            raise TimeoutError

        async def unsubscribe(self) -> None:
            self.unsubscribed = True

    broker.subscription = Subscription()

    class Connection:
        closed = False

        def jetstream(self):
            return broker

        async def close(self):
            self.closed = True

    connection = Connection()

    async def connect(self, *, disconnected_cb):
        nonlocal callback
        callback = disconnected_cb
        return connection

    monkeypatch.setattr(ExternalSourceNatsProfile, "connect", connect)

    def report(state: str, reason: str | None) -> None:
        states.append((state, reason))
        if state == "active":
            asyncio.create_task(callback())
        if state == "fault":
            stop.set()

    runtime = ExternalSourceRunnerRuntime(
        options=config,
        tenant_id="tenant-a",
        runner_id=RUNNER,
        host=host,
        report=report,
    )
    await asyncio.wait_for(runtime.run(stop), timeout=2)
    assert states == [("active", None), ("fault", "broker_binding_invalid")]
    assert connection.closed
    assert broker.subscription.unsubscribed
    assert host.queued == []


@pytest.mark.asyncio
async def test_broker_close_failure_is_reported_without_killing_signed_host(
    tmp_path: Path, monkeypatch
) -> None:
    config, broker = options(tmp_path)
    stop = asyncio.Event()
    states = []
    host = Host()

    class Subscription:
        async def next_msg(self, *, timeout: float):
            await asyncio.sleep(timeout)
            raise TimeoutError

        async def unsubscribe(self) -> None:
            return None

    broker.subscription = Subscription()

    class Connection:
        def jetstream(self):
            return broker

        async def close(self):
            raise RuntimeError("synthetic broker close failure")

    async def connect(self, **kwargs):
        return Connection()

    monkeypatch.setattr(ExternalSourceNatsProfile, "connect", connect)

    def report(state: str, reason: str | None) -> None:
        states.append((state, reason))
        if state == "active":
            stop.set()

    runtime = ExternalSourceRunnerRuntime(
        options=config,
        tenant_id="tenant-a",
        runner_id=RUNNER,
        host=host,
        report=report,
    )
    await asyncio.wait_for(runtime.run(stop), timeout=2)
    assert states == [("active", None), ("fault", "source_unavailable")]
    assert host.queued == []
