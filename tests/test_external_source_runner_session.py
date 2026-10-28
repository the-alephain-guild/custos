"""A subscriber rechecks local consent and active signed scope before intake."""

from __future__ import annotations

import hashlib
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
from custos.engines.nautilus.external_source_runner_session import ExternalSourceRunnerSession
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from tests.test_external_source_authorization import (
    AUTHORITY,
    INSTANCE,
    RUNNER,
    local_files,
    rewrite,
    scope,
)
from tests.test_external_source_subscriber import NOW, Message, published_bytes


class Host:
    def __init__(self) -> None:
        self.scope = scope()
        self.queued = []

    def external_source_runtime_scope(self, instance_id: str):
        assert instance_id == str(INSTANCE)
        return self.scope

    def queue_external_source_signal(self, authority, signal) -> bool:
        assert authority == AUTHORITY
        self.queued.append(signal)
        return True

    async def request_external_source_flatten(self, authority, *, risk_action=None) -> int:
        raise AssertionError("entry test must not request flatten")


class Broker:
    def __init__(self, config) -> None:
        self.config = config
        self.bound = False

    async def consumer_info(self, stream, durable):
        assert stream == SIGNAL_STREAM_NAME
        assert durable == self.config.durable_name
        return SimpleNamespace(config=self.config)

    async def subscribe_bind(self, *, stream, config, consumer, manual_ack):
        self.bound = True
        assert stream == SIGNAL_STREAM_NAME and manual_ack
        assert consumer == self.config.durable_name
        return object()


def setup(tmp_path: Path):
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
    )
    return session, path, credentials, host, broker, inbox


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
