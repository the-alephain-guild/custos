from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from nats.aio.client import Client

from custos.core.runner_fact import (
    RunnerFactError,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
    heartbeat,
)
from tests.test_a_blocked_stream_cannot_starve_the_rest import (
    _AnyTransport,
    _authority,
    _fact,
    _identity,
    _publisher,
    _SelectiveJetStream,
)


@pytest.mark.asyncio
async def test_initial_connection_refusal_leaves_the_healthy_mode_a_turn(tmp_path):
    outbox = RunnerFactOutbox(tmp_path / "facts.sqlite")
    for authority in (
        _authority(trading_mode="live"),
        _authority(trading_mode="sandbox", deployment_instance_id=uuid4()),
    ):
        await outbox.enqueue(authority, _identity(), [_fact(authority, generation=1)])
    native = Client()

    async def refuse(_):
        await asyncio.sleep(0)
        raise OSError("fixture connection refused")

    async def ignore_error(_):
        pass

    native._connect_to_server = refuse

    class Unavailable(_AnyTransport):
        async def connect(self, **options):
            options.setdefault("max_reconnect_attempts", -1)
            await native.connect(
                servers=["nats://127.0.0.1:4222"],
                reconnect_time_wait=0,
                error_cb=ignore_error,
                **options,
            )
            return native

    broker = _SelectiveJetStream(refuses=uuid4())
    publisher = _publisher(outbox, {"live": object(), "sandbox": broker})
    publisher._nats.pop("live")
    publisher._connection_profiles["live"] = Unavailable()
    try:
        assert await asyncio.wait_for(publisher.drain_once(), timeout=1) == 1
        assert len(broker.accepted) == 1
        assert len(await outbox.pending()) == 1
    finally:
        await native.close()


@pytest.mark.asyncio
async def test_reconnecting_session_is_retained_until_it_is_closed():
    class Profile(_AnyTransport):
        calls = 0

        async def connect(self, **_):
            self.calls += 1
            return SimpleNamespace(is_connected=True, jetstream=lambda: object())

    profile = Profile()
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"sandbox": profile},
        outbox=object(),
        runner_id=uuid4(),
        authority_guard=lambda: None,
    )
    native = Client()
    native._flush_queue = asyncio.Queue()
    native._status = Client.RECONNECTING
    publisher._nats["sandbox"] = native
    publisher._jetstreams["sandbox"] = object()
    try:
        for _ in range(3):
            with pytest.raises(RunnerFactError):
                await publisher.connect("sandbox")
        assert profile.calls == 0
        assert publisher._nats["sandbox"] is native
    finally:
        await native.close()


@pytest.mark.asyncio
async def test_close_handles_native_reconnection_and_still_drains_other_sessions():
    native = Client()
    native._flush_queue = asyncio.Queue()
    native._status = Client.RECONNECTING

    class Healthy:
        is_connected = True
        is_closed = False

        async def drain(self):
            self.is_closed = True

    healthy = Healthy()
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"sandbox": object()},
        outbox=object(),
        runner_id=uuid4(),
        authority_guard=lambda: None,
    )
    publisher._nats = {"sandbox": native, "testnet": healthy}
    await publisher.close()
    assert native.is_closed and healthy.is_closed
    assert not publisher._nats


@pytest.mark.asyncio
async def test_sustained_fact_arrivals_do_not_starve_a_pending_signal(tmp_path):
    outbox = RunnerFactOutbox(tmp_path / "outbox.sqlite")
    authority, identity = _authority(), _identity()

    async def enqueue():
        await outbox.enqueue(
            authority,
            identity,
            [heartbeat(event_id=uuid4(), status="online", observed_at=datetime.now(UTC))],
        )

    await enqueue()
    await outbox.enqueue_strategy_signal(
        authority,
        identity,
        fact_id=uuid4(),
        instrument="BTC-USDT",
        timeframe="1m",
        direction="long",
        occurred_at=datetime.now(UTC),
        input_digest="c" * 64,
        strategy_version="test-v1",
        trace_id=uuid4(),
    )

    class Broker:
        signals = 0
        facts = 0

        async def publish(self, subject, payload, **_):
            if ".strategy-signal." in subject:
                self.signals += 1
            else:
                self.facts += 1
                await enqueue()
            return SimpleNamespace(
                stream="FIXTURE", seq=self.facts + self.signals, domain="SIM", duplicate=False
            )

    broker = Broker()
    publisher = _publisher(outbox, {"sandbox": broker})
    await asyncio.wait_for(publisher.drain_once(), timeout=1)
    assert broker.signals == 1
    assert await outbox.pending_strategy_signals() == []
    assert await outbox.pending()
