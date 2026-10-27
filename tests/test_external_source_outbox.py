"""Durable source publication stays separate from subscriber execution."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custos.core.external_source_outbox import ExternalSourceOutbox, ExternalSourceOutboxError
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal

NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
BAR_CLOSE = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def event(*, target: str = "SHORT", max_lag: int = 60, tenant_id: str = "tenant-a"):
    payload = {
        "schemaVersion": 1,
        "sourceId": "synthetic-supertrend",
        "strategyRevision": "v0.5",
        "symbol": "ETH-USDT",
        "timeframe": "30",
        "barCloseTime": "2026-10-27T12:00:00Z",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": max_lag,
        "target": target,
        "signalToken": "synthetic-only-credential-never-store",
        "eventId": make_source_event_id(
            source_id="synthetic-supertrend",
            strategy_revision="v0.5",
            symbol="ETH-USDT",
            timeframe="30",
            bar_close_time=BAR_CLOSE,
            target=target,
            risk_action=None,
        ),
    }
    return parse_external_source_signal(
        payload,
        tenant_id=tenant_id,
        expected_source_id="synthetic-supertrend",
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=NOW,
    )


def test_accept_is_durable_idempotent_and_does_not_store_token(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite"
    outbox = ExternalSourceOutbox(path)
    source = event()
    assert outbox.accept(source, received_at=NOW).status == "accepted"
    assert ExternalSourceOutbox(path).accept(source, received_at=NOW).status == "duplicate"
    with pytest.raises(ExternalSourceOutboxError, match="conflict"):
        outbox.accept(event(max_lag=120), received_at=NOW)
    assert b"synthetic-only-credential" not in path.read_bytes()


def test_broker_ack_is_required_and_stale_claim_cannot_ack(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite"
    outbox = ExternalSourceOutbox(path)
    outbox.accept(event(), received_at=NOW)
    first = outbox.claim_next("tenant-a", at=NOW, lease_for=timedelta(seconds=5))
    assert first is not None
    assert isinstance(first.payload, bytes)
    assert json.loads(first.payload)["target"] == "SHORT"
    assert b"signalToken" not in first.payload
    assert outbox.claim_next("tenant-a", at=NOW + timedelta(seconds=2)) is None
    with pytest.raises(ExternalSourceOutboxError, match="broker sequence"):
        outbox.mark_published(first, broker_sequence=0, at=NOW)

    restarted = ExternalSourceOutbox(path)
    second = restarted.claim_next("tenant-a", at=NOW + timedelta(seconds=6))
    assert second is not None
    assert second.claim_id != first.claim_id
    assert (
        restarted.mark_published(first, broker_sequence=11, at=NOW + timedelta(seconds=6)) is False
    )
    assert restarted.mark_published(second, broker_sequence=12, at=NOW + timedelta(seconds=6))
    assert restarted.claim_next("tenant-a", at=NOW + timedelta(seconds=7)) is None


def test_two_tenants_have_independent_outbox_rows(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    first = event(tenant_id="tenant-a")
    second = event(tenant_id="tenant-b")
    assert first.event_id == second.event_id
    outbox.accept(first, received_at=NOW)
    outbox.accept(second, received_at=NOW)
    a = outbox.claim_next("tenant-a", at=NOW)
    b = outbox.claim_next("tenant-b", at=NOW)
    assert a is not None and b is not None
    assert a.tenant_id == "tenant-a" and b.tenant_id == "tenant-b"
    assert a.publish_id != b.publish_id


def test_expired_source_is_not_published_and_capacity_is_bounded(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite", max_pending=1)
    outbox.accept(event(), received_at=NOW)
    with pytest.raises(ExternalSourceOutboxError, match="full"):
        outbox.accept(event(target="LONG"), received_at=NOW)
    assert outbox.claim_next("tenant-a", at=NOW + timedelta(seconds=61)) is None
    assert outbox.accept(event(target="LONG"), received_at=NOW).status == "accepted"


def test_expired_backlog_frees_capacity_on_new_acceptance(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite", max_pending=1)
    outbox.accept(event(), received_at=NOW)
    next_bar = BAR_CLOSE + timedelta(minutes=30)
    later = replace(
        event(target="LONG"),
        bar_close_time=next_bar,
        timestamp=next_bar,
        event_id=make_source_event_id(
            source_id="synthetic-supertrend",
            strategy_revision="v0.5",
            symbol="ETH-USDT",
            timeframe="30",
            bar_close_time=next_bar,
            target="LONG",
            risk_action=None,
        ),
    )
    assert outbox.accept(later, received_at=NOW + timedelta(minutes=30)).status == "accepted"


def test_fabricated_identity_and_unsafe_database_are_rejected(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    with pytest.raises(ExternalSourceOutboxError, match="identity"):
        outbox.accept(replace(event(), event_id="forged"), received_at=NOW)
    link = tmp_path / "linked.sqlite"
    link.symlink_to(outbox.path)
    with pytest.raises(ExternalSourceOutboxError, match="unsafe"):
        ExternalSourceOutbox(link)
