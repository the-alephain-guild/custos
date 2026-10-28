"""A subscriber rechecks local consent and active signed scope before intake."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from nats.js.api import ReplayPolicy

from custos.core.external_source_transport import (
    SIGNAL_STREAM_NAME,
    ExternalSourceTransportError,
    signal_subject,
)
from custos.engines.nautilus.external_source_authorization import (
    ExternalSourceAuthorizationError,
    load_source_subscription,
)
from custos.engines.nautilus.external_source_delivery import make_target_delivery_id
from custos.engines.nautilus.external_source_runner_session import ExternalSourceRunnerSession
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from custos.engines.nautilus.host import ExternalSourceRuntimeScope
from tests.test_external_source_authorization import (
    AUTHORITY,
    INSTANCE,
    RUNNER,
    local_files,
    rewrite,
    scope,
)
from tests.test_external_source_subscriber import NOW, Message, published_bytes
from tests.test_external_target_flat_verifier import Reader, observation
from tests.test_external_target_inbox import directive


class Host:
    def __init__(self) -> None:
        self.scope = scope()
        self.queued = []
        self.queued_event = asyncio.Event()
        self.flat_reader = None
        self.flat_local_clear = True

    def external_source_runtime_scope(self, instance_id: str):
        assert instance_id == str(INSTANCE)
        return self.scope

    def queue_external_source_signal(self, authority, signal) -> bool:
        assert authority == AUTHORITY
        self.queued.append(signal)
        self.queued_event.set()
        return True

    async def request_external_source_flatten(self, authority, *, risk_action=None) -> int:
        raise AssertionError("entry test must not request flatten")

    def external_source_flat_reader(self, authority):
        assert authority == AUTHORITY
        return self.flat_reader

    async def external_source_flat_local_clear(self, authority, pair):
        assert authority == AUTHORITY and pair == "ETH-USDT"
        return self.flat_local_clear


class Broker:
    def __init__(self, config) -> None:
        self.config = config
        self.bound = False
        self.subscription = object()

    async def consumer_info(self, stream, durable):
        assert stream == SIGNAL_STREAM_NAME
        assert durable == self.config.durable_name
        return SimpleNamespace(config=self.config)

    async def subscribe_bind(self, *, stream, config, consumer, manual_ack):
        self.bound = True
        assert stream == SIGNAL_STREAM_NAME and manual_ack
        assert consumer == self.config.durable_name
        return self.subscription


def setup(tmp_path: Path, *, clock=None):
    path, credentials = local_files(tmp_path)
    rewrite(path, sourceId="synthetic-supertrend", sourceSymbol="ETH-USDT")
    authorized = load_source_subscription(
        path,
        credentials_path=credentials,
        expected_tenant_id="tenant-a",
        expected_runner_id=RUNNER,
        active_scope=scope(),
    )
    host = Host()
    broker = Broker(authorized.spec.consumer_config)
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    session = ExternalSourceRunnerSession(
        authorization_path=path,
        credentials_path=credentials,
        tenant_id="tenant-a",
        runner_id=RUNNER,
        authority=AUTHORITY,
        host=host,
        inbox=inbox,
        broker=broker,
        clock=clock,
    )
    return session, path, credentials, host, broker, inbox


@pytest.mark.asyncio
async def test_runner_recovers_and_confirms_only_venue_observed_flat(tmp_path: Path) -> None:
    observed_at = NOW + timedelta(seconds=1)
    session, _, _, host, broker, inbox = setup(tmp_path, clock=lambda: NOW + timedelta(seconds=2))
    risk = directive(AUTHORITY, target="FLAT", risk_action="FLATTEN")
    risk = replace(
        risk,
        connector="okx_perpetual",
        delivery_id=make_target_delivery_id(
            tenant_id=risk.tenant_id,
            source_event_id=risk.source_event_id,
            authority=AUTHORITY,
            connector="okx_perpetual",
            pair=risk.pair,
        ),
    )
    inbox.accept(risk, received_at=NOW)
    assert inbox.begin_delivery(risk.delivery_id, authority=AUTHORITY, at=NOW)
    assert inbox.mark_handoff(risk.delivery_id, authority=AUTHORITY, at=NOW)
    host.flat_reader = Reader(observation(observed_at=observed_at))
    stop = asyncio.Event()

    class Subscription:
        async def next_msg(self, *, timeout):
            await asyncio.sleep(timeout)
            raise TimeoutError

        async def unsubscribe(self):
            return None

    broker.subscription = Subscription()
    task = asyncio.create_task(session.run(stop, poll_seconds=0.01))
    try:
        for _ in range(100):
            if inbox.pending_flat_request(risk.delivery_id, authority=AUTHORITY) is None:
                break
            await asyncio.sleep(0.01)
        assert inbox.pending_flat_request(risk.delivery_id, authority=AUTHORITY) is None
        assert host.flat_reader.calls == [("okx_perpetual", "ETH-USDT")]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_revoked_grant_during_venue_read_keeps_flat_fence(tmp_path: Path) -> None:
    session, authorization, _, host, _, inbox = setup(
        tmp_path, clock=lambda: NOW + timedelta(seconds=2)
    )
    risk = directive(AUTHORITY, target="FLAT", risk_action="FLATTEN")
    risk = replace(
        risk,
        connector="okx_perpetual",
        delivery_id=make_target_delivery_id(
            tenant_id=risk.tenant_id,
            source_event_id=risk.source_event_id,
            authority=AUTHORITY,
            connector="okx_perpetual",
            pair=risk.pair,
        ),
    )
    inbox.accept(risk, received_at=NOW)
    assert inbox.begin_delivery(risk.delivery_id, authority=AUTHORITY, at=NOW)
    assert inbox.mark_handoff(risk.delivery_id, authority=AUTHORITY, at=NOW)

    class RevokingReader(Reader):
        async def observe_flat(self, *, connector: str, pair: str):
            rewrite(authorization, generation=8)
            return await super().observe_flat(connector=connector, pair=pair)

    host.flat_reader = RevokingReader(observation())
    await session.bind(at=NOW)
    assert session.flat_verifier is not None
    with pytest.raises(ExternalSourceAuthorizationError, match="active"):
        await session.flat_verifier.confirm_if_flat(risk.delivery_id, authority=AUTHORITY)
    assert inbox.pending_flat_request(risk.delivery_id, authority=AUTHORITY) is not None


@pytest.mark.asyncio
async def test_exact_grant_binds_existing_consumer_then_acks_and_handoffs(tmp_path: Path) -> None:
    session, _, _, host, broker, inbox = setup(tmp_path)
    await session.bind(at=NOW)
    assert broker.bound
    message = Message(published_bytes(tmp_path), signal_subject("tenant-a", "synthetic-supertrend"))
    receipt = await session.handle_message(message, at=NOW)
    assert receipt is not None and receipt.status == "queued"
    assert message.acked == 1 and message.termed == 0
    assert len(host.queued) == 1 and host.queued[0].amount is None
    assert inbox.pending(AUTHORITY, at=NOW) == []


@pytest.mark.asyncio
async def test_changed_grant_or_credential_stops_ack_and_handoff(tmp_path: Path) -> None:
    session, path, credentials, host, _, _ = setup(tmp_path)
    await session.bind(at=NOW)
    payload = published_bytes(tmp_path)
    rewrite(path, generation=8)
    changed = Message(payload, signal_subject("tenant-a", "synthetic-supertrend"))
    with pytest.raises(ExternalSourceAuthorizationError, match="active"):
        await session.handle_message(changed, at=NOW)
    assert changed.acked == changed.termed == 0 and host.queued == []

    rewrite(path, generation=7)
    credentials.write_bytes(b"replaced-credential")
    replaced = Message(payload, signal_subject("tenant-a", "synthetic-supertrend"))
    with pytest.raises(ExternalSourceAuthorizationError, match="credential"):
        await session.handle_message(replaced, at=NOW)
    assert replaced.acked == replaced.termed == 0 and host.queued == []

    rewrite(path, natsCredentialsSha256=hashlib.sha256(credentials.read_bytes()).hexdigest())
    rebinding = Message(payload, signal_subject("tenant-a", "synthetic-supertrend"))
    with pytest.raises(ExternalSourceAuthorizationError, match="changed"):
        await session.handle_message(rebinding, at=NOW)
    assert rebinding.acked == rebinding.termed == 0 and host.queued == []


@pytest.mark.asyncio
async def test_venue_scope_change_stops_intake_even_if_signed_identity_is_same(
    tmp_path: Path,
) -> None:
    session, _, _, host, _, _ = setup(tmp_path)
    await session.bind(at=NOW)
    host.scope = ExternalSourceRuntimeScope(
        authority=AUTHORITY,
        connector="binance_perpetual",
        allowed_pairs=frozenset({"ETH-USDT"}),
        authorized_leverage=3,
    )
    message = Message(published_bytes(tmp_path), signal_subject("tenant-a", "synthetic-supertrend"))
    with pytest.raises(ExternalSourceAuthorizationError, match="active"):
        await session.handle_message(message, at=NOW)
    assert message.acked == message.termed == 0 and host.queued == []


@pytest.mark.asyncio
async def test_wrong_preprovisioned_consumer_is_never_bound(tmp_path: Path) -> None:
    session, _, _, _, broker, _ = setup(tmp_path)
    broker.config.filter_subject = signal_subject("tenant-b", "synthetic-supertrend")
    with pytest.raises(ExternalSourceTransportError, match="existing signal consumer"):
        await session.bind(at=NOW)
    assert not broker.bound

    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    second, _, _, _, replay_broker, _ = setup(replay_dir)
    replay_broker.config.replay_policy = ReplayPolicy.ORIGINAL
    with pytest.raises(ExternalSourceTransportError, match="existing signal consumer"):
        await second.bind(at=NOW)
    assert not replay_broker.bound


@pytest.mark.asyncio
async def test_runner_loop_hands_off_pending_message_and_unsubscribes_on_stop(
    tmp_path: Path,
) -> None:
    session, _, _, host, broker, _ = setup(tmp_path)
    stop = asyncio.Event()
    acked = asyncio.Event()

    class QueuedMessage(Message):
        async def ack(self) -> None:
            await super().ack()
            acked.set()

    class Subscription:
        def __init__(self) -> None:
            self.messages: asyncio.Queue = asyncio.Queue()
            self.unsubscribed = False

        async def next_msg(self, *, timeout: float):
            return await asyncio.wait_for(self.messages.get(), timeout=timeout)

        async def unsubscribe(self) -> None:
            self.unsubscribed = True

    broker.subscription = Subscription()
    task = asyncio.create_task(session.run(stop, clock=lambda: NOW, poll_seconds=0.01))
    try:
        for _ in range(100):
            if broker.bound:
                break
            await asyncio.sleep(0.01)
        assert broker.bound
        message = QueuedMessage(
            published_bytes(tmp_path), signal_subject("tenant-a", "synthetic-supertrend")
        )
        await broker.subscription.messages.put(message)
        await asyncio.wait_for(acked.wait(), timeout=2)
        await asyncio.wait_for(host.queued_event.wait(), timeout=2)
        assert len(host.queued) == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert broker.subscription.unsubscribed


@pytest.mark.asyncio
async def test_runner_loop_stops_receiving_when_authorization_changes(tmp_path: Path) -> None:
    session, path, _, host, broker, _ = setup(tmp_path)

    class IdleSubscription:
        unsubscribed = False

        async def next_msg(self, *, timeout: float):
            await asyncio.sleep(timeout)
            raise TimeoutError

        async def unsubscribe(self) -> None:
            self.unsubscribed = True

    broker.subscription = IdleSubscription()
    task = asyncio.create_task(session.run(asyncio.Event(), clock=lambda: NOW, poll_seconds=0.01))
    for _ in range(100):
        if broker.bound:
            break
        await asyncio.sleep(0.01)
    assert broker.bound
    rewrite(path, generation=8)
    with pytest.raises(ExternalSourceAuthorizationError, match="active"):
        await asyncio.wait_for(task, timeout=2)
    assert broker.subscription.unsubscribed
    assert host.queued == []
