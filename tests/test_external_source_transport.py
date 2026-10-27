"""Separate JetStream source publication from deployment command transport."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal
from custos.core.external_source_transport import (
    SIGNAL_STREAM_NAME,
    ExternalSourceTransportError,
    SourcePublicationWorker,
    bind_signal_consumer,
    publisher_permissions,
    signal_subject,
    subscriber_permissions,
    subscription_spec,
)

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
BAR = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
RUNNER_ID = UUID("30000000-0000-4000-8000-000000000001")
AUTHORITY = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000001"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000001"),
    deployment_spec_digest="a" * 64,
    generation=1,
    trading_mode="testnet",
)


def source_event():
    payload = {
        "schemaVersion": 1,
        "sourceId": "synthetic-supertrend",
        "strategyRevision": "v0.5",
        "symbol": "ETH-USDT",
        "timeframe": "30",
        "barCloseTime": "2026-10-27T12:00:00Z",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": 60,
        "target": "SHORT",
        "eventId": make_source_event_id(
            source_id="synthetic-supertrend",
            strategy_revision="v0.5",
            symbol="ETH-USDT",
            timeframe="30",
            bar_close_time=BAR,
            target="SHORT",
            risk_action=None,
        ),
    }
    return parse_external_source_signal(
        payload,
        tenant_id="tenant-a",
        expected_source_id="synthetic-supertrend",
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=NOW,
    )


@dataclass
class Ack:
    stream: str = SIGNAL_STREAM_NAME
    seq: int = 11


class FakeJetStream:
    def __init__(self, ack: Ack | None = None) -> None:
        self.ack = ack or Ack()
        self.calls: list[tuple[str, bytes, str, dict[str, str]]] = []
        self.fail = False

    async def publish(self, subject, payload, *, stream, headers, timeout):
        self.calls.append((subject, payload, stream, headers))
        if self.fail:
            raise ConnectionError("broker unavailable")
        return self.ack


def test_source_subject_is_isolated_by_tenant_and_source() -> None:
    first = signal_subject("tenant-a", "synthetic-supertrend")
    assert first != signal_subject("tenant-b", "synthetic-supertrend")
    assert first != signal_subject("tenant-a", "other-source")
    assert "synthetic-supertrend" not in first
    assert ".." not in first


@pytest.mark.asyncio
async def test_worker_marks_published_only_after_correct_puback(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    outbox.accept(source_event(), received_at=NOW)
    broker = FakeJetStream()
    worker = SourcePublicationWorker(outbox, broker, tenant_id="tenant-a")
    assert await worker.publish_once(at=NOW)
    assert len(broker.calls) == 1
    subject, payload, stream, headers = broker.calls[0]
    assert subject == signal_subject("tenant-a", "synthetic-supertrend")
    assert stream == SIGNAL_STREAM_NAME
    assert b"signalToken" not in payload
    assert len(headers["Nats-Msg-Id"]) == 64
    assert await worker.publish_once(at=NOW) is False


@pytest.mark.asyncio
async def test_broker_failure_or_wrong_stream_leaves_retryable_claim(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    outbox.accept(source_event(), received_at=NOW)
    broker = FakeJetStream()
    broker.fail = True
    worker = SourcePublicationWorker(outbox, broker, tenant_id="tenant-a")
    with pytest.raises(ConnectionError):
        await worker.publish_once(at=NOW)
    broker.fail = False
    broker.ack = Ack(stream="RUNNER_CONTROL_TESTNET")
    with pytest.raises(ExternalSourceTransportError, match="PubAck"):
        await worker.publish_once(at=NOW + timedelta(seconds=11))
    broker.ack = Ack()
    assert await worker.publish_once(at=NOW + timedelta(seconds=22))
    assert len({call[3]["Nats-Msg-Id"] for call in broker.calls}) == 1


def test_each_deployment_has_a_distinct_durable_on_the_same_source() -> None:
    first = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=AUTHORITY,
        source_id="synthetic-supertrend",
    )
    second = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=EngineLifecycleAuthority(
            deployment_instance_id=UUID("10000000-0000-4000-8000-000000000002"),
            deployment_spec_id=AUTHORITY.deployment_spec_id,
            deployment_spec_digest=AUTHORITY.deployment_spec_digest,
            generation=1,
            trading_mode="testnet",
        ),
        source_id="synthetic-supertrend",
    )
    assert first.filter_subject == second.filter_subject
    assert first.durable_name != second.durable_name
    assert first.delivery_subject != second.delivery_subject
    new_generation = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=replace(AUTHORITY, generation=2),
        source_id="synthetic-supertrend",
    )
    new_spec = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=replace(AUTHORITY, deployment_spec_digest="b" * 64),
        source_id="synthetic-supertrend",
    )
    new_spec_id = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=replace(
            AUTHORITY, deployment_spec_id=UUID("20000000-0000-4000-8000-000000000002")
        ),
        source_id="synthetic-supertrend",
    )
    assert (
        len(
            {
                first.durable_name,
                new_generation.durable_name,
                new_spec.durable_name,
                new_spec_id.durable_name,
            }
        )
        == 4
    )
    assert (
        len(
            {
                first.delivery_subject,
                new_generation.delivery_subject,
                new_spec.delivery_subject,
                new_spec_id.delivery_subject,
            }
        )
        == 4
    )
    assert first.consumer_config.deliver_policy.value == "new"
    assert first.consumer_config.ack_policy.value == "explicit"
    assert first.consumer_config.max_ack_pending == 1
    with pytest.raises(ExternalSourceTransportError, match="live"):
        subscription_spec(
            tenant_id="tenant-a",
            runner_id=RUNNER_ID,
            authority=EngineLifecycleAuthority(
                deployment_instance_id=AUTHORITY.deployment_instance_id,
                deployment_spec_id=AUTHORITY.deployment_spec_id,
                deployment_spec_digest=AUTHORITY.deployment_spec_digest,
                generation=1,
                trading_mode="live",
            ),
            source_id="synthetic-supertrend",
        )


def test_broker_permissions_are_exact_for_publisher_and_one_subscriber() -> None:
    spec = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=AUTHORITY,
        source_id="synthetic-supertrend",
    )
    publisher = publisher_permissions("tenant-a", "synthetic-supertrend")
    subscriber = subscriber_permissions(spec)
    assert publisher["publish_allow"] == [spec.filter_subject]
    assert subscriber["subscribe_allow"] == [spec.delivery_subject, "_INBOX.>"]
    assert spec.filter_subject not in subscriber["subscribe_allow"]
    assert f"$JS.ACK.{SIGNAL_STREAM_NAME}.{spec.durable_name}.>" in subscriber["publish_allow"]
    assert not any(subject == ">" for subject in publisher["publish_allow"])


@pytest.mark.asyncio
async def test_bind_refuses_wrong_existing_consumer_without_creating_one() -> None:
    spec = subscription_spec(
        tenant_id="tenant-a",
        runner_id=RUNNER_ID,
        authority=AUTHORITY,
        source_id="synthetic-supertrend",
    )

    class Broker:
        def __init__(self) -> None:
            self.bound = False
            self.config = spec.consumer_config

        async def consumer_info(self, stream, durable):
            assert stream == SIGNAL_STREAM_NAME and durable == spec.durable_name
            return type("Info", (), {"config": self.config})()

        async def subscribe_bind(self, *, stream, config, consumer, manual_ack):
            self.bound = True
            return object()

    broker = Broker()
    assert await bind_signal_consumer(broker, spec) is not None
    assert broker.bound
    broker.bound = False
    broker.config = type(
        "Config",
        (),
        {
            "durable_name": spec.durable_name,
            "filter_subject": "another.tenant.subject",
            "deliver_subject": spec.delivery_subject,
            "deliver_policy": spec.consumer_config.deliver_policy,
            "ack_policy": spec.consumer_config.ack_policy,
            "max_ack_pending": 1,
        },
    )()
    with pytest.raises(ExternalSourceTransportError, match="consumer"):
        await bind_signal_consumer(broker, spec)
    assert broker.bound is False
