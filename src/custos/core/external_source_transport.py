"""An isolated JetStream lane for target-neutral source events."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, ReplayPolicy

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_outbox import ExternalSourceOutbox

SIGNAL_STREAM_NAME = "CUSTOS_EXTERNAL_SOURCE_V1"
_SOURCE_SUBJECT_PREFIX = "custos.external-source.v1"
_DELIVERY_SUBJECT_PREFIX = "custos.external-source.delivery.v1"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class ExternalSourceTransportError(RuntimeError):
    """The broker did not prove the expected publication or subscription."""


def _safe_key(value: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ExternalSourceTransportError("signal transport identity is invalid")
    return hashlib.sha256(value.encode()).hexdigest()


def signal_subject(tenant_id: str, source_id: str) -> str:
    """A fixed-depth subject that cannot be widened by source labels."""
    return f"{_SOURCE_SUBJECT_PREFIX}.{_safe_key(tenant_id)}.{_safe_key(source_id)}"


def publisher_permissions(tenant_id: str, source_id: str) -> dict[str, list[str]]:
    """Minimal broker permissions for one source's separate publisher identity."""
    return {
        "publish_allow": [signal_subject(tenant_id, source_id)],
        "subscribe_allow": ["_INBOX.>"],
        "publish_deny": ["$JS.API.STREAM.>", "$JS.API.CONSUMER.CREATE.>", "$SYS.>"],
        "subscribe_deny": ["$SYS.>"],
    }


@dataclass(frozen=True, slots=True)
class SignalSubscriptionSpec:
    tenant_id: str
    runner_id: UUID
    authority: EngineLifecycleAuthority
    source_id: str
    filter_subject: str
    durable_name: str
    delivery_subject: str
    consumer_config: ConsumerConfig


def subscriber_permissions(spec: SignalSubscriptionSpec) -> dict[str, list[str]]:
    """Permit one existing durable, its Ack subject and exact delivery inbox."""
    return {
        "publish_allow": [
            f"$JS.API.CONSUMER.INFO.{SIGNAL_STREAM_NAME}.{spec.durable_name}",
            f"$JS.ACK.{SIGNAL_STREAM_NAME}.{spec.durable_name}.>",
        ],
        "subscribe_allow": [spec.delivery_subject, "_INBOX.>"],
        "publish_deny": ["$JS.API.STREAM.>", "$JS.API.CONSUMER.CREATE.>", "$SYS.>"],
        "subscribe_deny": ["$SYS.>"],
    }


def subscription_spec(
    *,
    tenant_id: str,
    runner_id: UUID,
    authority: EngineLifecycleAuthority,
    source_id: str,
) -> SignalSubscriptionSpec:
    """Describe one pre-provisioned consumer; this does not authorize a run."""
    if authority.trading_mode not in {"sandbox", "testnet"}:
        raise ExternalSourceTransportError("live signal subscriptions are not admitted")
    if not isinstance(runner_id, UUID):
        raise ExternalSourceTransportError("runner identity is invalid")
    if not isinstance(authority.deployment_spec_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", authority.deployment_spec_digest
    ):
        raise ExternalSourceTransportError("deployment spec digest is invalid")
    tenant_key = _safe_key(tenant_id)
    _safe_key(source_id)
    scope_key = hashlib.sha256(
        (
            f"{runner_id}\0{authority.deployment_instance_id}\0"
            f"{authority.deployment_spec_id}\0"
            f"{authority.deployment_spec_digest}\0{authority.generation}\0"
            f"{authority.trading_mode}\0{source_id}"
        ).encode()
    ).hexdigest()
    durable = f"custos-signal-v1-{tenant_key}-{scope_key}"
    delivery = f"{_DELIVERY_SUBJECT_PREFIX}.{tenant_key}.{scope_key}"
    subject = signal_subject(tenant_id, source_id)
    config = ConsumerConfig(
        durable_name=durable,
        deliver_subject=delivery,
        filter_subject=subject,
        deliver_policy=DeliverPolicy.NEW,
        ack_policy=AckPolicy.EXPLICIT,
        replay_policy=ReplayPolicy.INSTANT,
        max_ack_pending=1,
    )
    return SignalSubscriptionSpec(
        tenant_id=tenant_id,
        runner_id=runner_id,
        authority=authority,
        source_id=source_id,
        filter_subject=subject,
        durable_name=durable,
        delivery_subject=delivery,
        consumer_config=config,
    )


async def bind_signal_consumer(broker: Any, spec: SignalSubscriptionSpec) -> Any:
    """Bind only to an existing consumer with the exact target's durable scope."""
    info = await broker.consumer_info(SIGNAL_STREAM_NAME, spec.durable_name)
    actual = info.config
    expected = spec.consumer_config
    fields = (
        "durable_name",
        "deliver_subject",
        "filter_subject",
        "deliver_policy",
        "ack_policy",
        "replay_policy",
        "max_ack_pending",
    )
    if any(getattr(actual, field, None) != getattr(expected, field) for field in fields):
        raise ExternalSourceTransportError("existing signal consumer differs from deployment")
    return await broker.subscribe_bind(
        stream=SIGNAL_STREAM_NAME,
        config=expected,
        consumer=spec.durable_name,
        manual_ack=True,
    )


class SourcePublicationWorker:
    """Publish a leased outbox row and wait for the exact stream's PubAck."""

    def __init__(self, outbox: ExternalSourceOutbox, jetstream: Any, *, tenant_id: str) -> None:
        _safe_key(tenant_id)
        self.outbox = outbox
        self.jetstream = jetstream
        self.tenant_id = tenant_id

    async def publish_once(self, *, at: datetime) -> bool:
        claim = await asyncio.to_thread(
            self.outbox.claim_next,
            self.tenant_id,
            at=at,
            lease_for=timedelta(seconds=10),
        )
        if claim is None:
            return False
        ack = await self.jetstream.publish(
            signal_subject(claim.tenant_id, claim.source_id),
            claim.payload,
            timeout=2.0,
            stream=SIGNAL_STREAM_NAME,
            headers={"Nats-Msg-Id": claim.publish_id},
        )
        if (
            getattr(ack, "stream", None) != SIGNAL_STREAM_NAME
            or type(getattr(ack, "seq", None)) is not int
            or ack.seq < 1
        ):
            raise ExternalSourceTransportError("signal PubAck differs from expected stream")
        marked = await asyncio.to_thread(
            self.outbox.mark_published,
            claim,
            broker_sequence=ack.seq,
            at=at,
        )
        if not marked:
            raise ExternalSourceTransportError("signal PubAck no longer owns the outbox claim")
        return True
