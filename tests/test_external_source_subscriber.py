"""Broker acknowledgement follows one deployment's durable target acceptance."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal
from custos.core.external_source_transport import signal_subject, subscription_spec
from custos.engines.nautilus.external_source_delivery import (
    ExternalSourceDeliveryError,
    SourceSubscriptionBinding,
)
from custos.engines.nautilus.external_source_subscriber import ExternalSourceSubscriber
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
BAR = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
FIRST = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000001"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000001"),
    deployment_spec_digest="a" * 64,
    generation=1,
    trading_mode="testnet",
)
SECOND = EngineLifecycleAuthority(
    deployment_instance_id=UUID("10000000-0000-4000-8000-000000000002"),
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000002"),
    deployment_spec_digest="b" * 64,
    generation=1,
    trading_mode="testnet",
)


class Message:
    def __init__(self, data: bytes, subject: str) -> None:
        self.data = data
        self.subject = subject
        self.acked = 0
        self.termed = 0

    async def ack(self) -> None:
        self.acked += 1

    async def term(self) -> None:
        self.termed += 1


def published_bytes(tmp_path: Path) -> bytes:
    source_id = "synthetic-supertrend"
    payload = {
        "schemaVersion": 1,
        "sourceId": source_id,
        "strategyRevision": "v0.5",
        "symbol": "ETH-USDT",
        "timeframe": "30",
        "barCloseTime": "2026-10-27T12:00:00Z",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": 60,
        "target": "SHORT",
        "eventId": make_source_event_id(
            source_id=source_id,
            strategy_revision="v0.5",
            symbol="ETH-USDT",
            timeframe="30",
            bar_close_time=BAR,
            target="SHORT",
            risk_action=None,
        ),
    }
    event = parse_external_source_signal(
        payload,
        tenant_id="tenant-a",
        expected_source_id=source_id,
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=NOW,
    )
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    outbox.accept(event, received_at=NOW)
    claim = outbox.claim_next("tenant-a", at=NOW)
    assert claim is not None
    return claim.payload


def subscriber(
    tmp_path: Path,
    authority: EngineLifecycleAuthority,
    *,
    connector: str,
    scope_provider=None,
) -> tuple[ExternalSourceSubscriber, ExternalTargetInbox]:
    source_id = "synthetic-supertrend"
    spec = subscription_spec(
        tenant_id="tenant-a",
        runner_id=UUID("30000000-0000-4000-8000-000000000001"),
        authority=authority,
        source_id=source_id,
    )
    binding = SourceSubscriptionBinding(
        tenant_id="tenant-a",
        source_id=source_id,
        source_symbol="ETH-USDT",
        target_pair="ETH-USDT",
        authority=authority,
    )
    scope = ExternalSourceRuntimeScope(
        authority=authority,
        connector=connector,
        allowed_pairs=frozenset({"ETH-USDT"}),
        authorized_leverage=3,
    )
    inbox = ExternalTargetInbox(tmp_path / f"target-{authority.deployment_instance_id}.sqlite")
    return ExternalSourceSubscriber(
        spec,
        binding,
        inbox,
        scope_provider=scope_provider or (lambda: scope),
    ), inbox


@pytest.mark.asyncio
async def test_one_published_event_is_acked_after_two_independent_durable_acceptances(
    tmp_path: Path,
) -> None:
    payload = published_bytes(tmp_path)
    first, first_inbox = subscriber(tmp_path, FIRST, connector="okx_perpetual")
    second, second_inbox = subscriber(tmp_path, SECOND, connector="binance_perpetual")
    subject = signal_subject("tenant-a", "synthetic-supertrend")
    first_msg, second_msg = Message(payload, subject), Message(payload, subject)

    assert (await first.handle_message(first_msg, at=NOW)).status == "accepted"
    assert (await second.handle_message(second_msg, at=NOW)).status == "accepted"
    assert first_msg.acked == second_msg.acked == 1
    assert first_msg.termed == second_msg.termed == 0
    first_pending = first_inbox.pending(FIRST, at=NOW)
    second_pending = second_inbox.pending(SECOND, at=NOW)
    assert first_pending[0].source_event_id == second_pending[0].source_event_id
    assert first_pending[0].delivery_id != second_pending[0].delivery_id

    duplicate = Message(payload, subject)
    assert (await first.handle_message(duplicate, at=NOW)).status == "duplicate"
    assert duplicate.acked == 1
    assert len(first_inbox.pending(FIRST, at=NOW)) == 1


@pytest.mark.asyncio
async def test_wrong_subject_or_tenant_is_terminal_without_target_receipt(tmp_path: Path) -> None:
    payload = published_bytes(tmp_path)
    consumer, inbox = subscriber(tmp_path, FIRST, connector="okx_perpetual")
    wrong_subject = Message(payload, signal_subject("tenant-b", "synthetic-supertrend"))
    assert await consumer.handle_message(wrong_subject, at=NOW) is None
    assert wrong_subject.termed == 1 and wrong_subject.acked == 0

    changed = json.loads(payload)
    changed["tenantId"] = "tenant-b"
    wrong_tenant = Message(
        json.dumps(changed).encode(), signal_subject("tenant-a", "synthetic-supertrend")
    )
    assert await consumer.handle_message(wrong_tenant, at=NOW) is None
    assert wrong_tenant.termed == 1 and wrong_tenant.acked == 0
    assert inbox.pending(FIRST, at=NOW) == []


@pytest.mark.asyncio
async def test_store_failure_and_runtime_generation_drift_do_not_ack(
    tmp_path: Path, monkeypatch
) -> None:
    payload = published_bytes(tmp_path)
    consumer, inbox = subscriber(tmp_path, FIRST, connector="okx_perpetual")
    subject = signal_subject("tenant-a", "synthetic-supertrend")

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(inbox, "accept", unavailable)
    message = Message(payload, subject)
    with pytest.raises(sqlite3.OperationalError):
        await consumer.handle_message(message, at=NOW)
    assert message.acked == message.termed == 0

    drifted = ExternalSourceRuntimeScope(
        authority=replace(FIRST, generation=2),
        connector="okx_perpetual",
        allowed_pairs=frozenset({"ETH-USDT"}),
        authorized_leverage=3,
    )
    separate = tmp_path / "drifted"
    separate.mkdir()
    changed, _ = subscriber(
        separate, FIRST, connector="okx_perpetual", scope_provider=lambda: drifted
    )
    other = Message(payload, subject)
    with pytest.raises(ExternalSourceDeliveryError, match="authority"):
        await changed.handle_message(other, at=NOW)
    assert other.acked == other.termed == 0
