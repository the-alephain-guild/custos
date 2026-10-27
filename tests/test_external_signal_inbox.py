"""Durability and ordering tests for an untrusted external signal inbox."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_signal_contract import make_event_id, parse_custos_v1_alert
from custos.core.external_signal_inbox import ExternalSignalInbox, ExternalSignalInboxError

INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
AUTHORITY = EngineLifecycleAuthority(
    deployment_instance_id=INSTANCE_ID,
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000001"),
    deployment_spec_digest="a" * 64,
    generation=1,
    trading_mode="testnet",
)


def alert(
    *,
    action: str = "ENTER_LONG",
    risk_action: str | None = None,
    bar_close_time: datetime | None = None,
    amount: str = "10.25",
):
    bar_close_time = bar_close_time or datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    payload = {
        "action": action,
        "instrument": "ETH-USDT-SWAP",
        "signalToken": "demo-secret-never-persist",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": "60",
        "investmentType": ("margin" if action.startswith("ENTER") else "percentage_position"),
        "amount": amount if action.startswith("ENTER") else "100",
        "schemaVersion": 1,
        "sourceId": "independent-demo",
        "strategyRevision": "supertrend-v0.5",
        "deploymentInstanceId": str(INSTANCE_ID),
        "timeframe": "30",
        "barCloseTime": bar_close_time.isoformat().replace("+00:00", "Z"),
        "requestedLeverage": 3,
    }
    if risk_action is not None:
        payload["riskAction"] = risk_action
    payload["eventId"] = make_event_id(
        source_id="independent-demo",
        strategy_revision="supertrend-v0.5",
        instrument="ETH-USDT-SWAP",
        timeframe="30",
        bar_close_time=bar_close_time,
        action=action,
        risk_action=risk_action,
    )
    return parse_custos_v1_alert(
        payload,
        now=NOW,
        allowed_instruments={"ETH-USDT-SWAP"},
        expected_deployment_instance_id=INSTANCE_ID,
        authorized_leverage=3,
    )


def test_successful_receipt_survives_restart_without_token(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    message = alert()
    receipt = ExternalSignalInbox(path).accept(message, authority=AUTHORITY, received_at=NOW)
    assert receipt.status == "accepted"
    assert receipt.event_id == message.event_id

    recovered = ExternalSignalInbox(path).pending(AUTHORITY, now=NOW)
    assert [item.event_id for item in recovered] == [message.event_id]
    assert recovered[0].action == "ENTER_LONG"
    assert b"demo-secret-never-persist" not in path.read_bytes()


def test_exact_replay_is_idempotent_but_same_id_changed_amount_conflicts(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    first = alert()
    assert inbox.accept(first, authority=AUTHORITY, received_at=NOW).status == "accepted"
    assert inbox.accept(first, authority=AUTHORITY, received_at=NOW).status == "duplicate"
    with pytest.raises(ExternalSignalInboxError, match="conflict"):
        inbox.accept(alert(amount="12"), authority=AUTHORITY, received_at=NOW)
    assert len(inbox.pending(AUTHORITY, now=NOW)) == 1


def test_risk_exit_supersedes_unconsumed_entry_at_same_bar(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    entry = alert()
    risk = alert(action="EXIT_LONG", risk_action="FLATTEN")
    inbox.accept(entry, authority=AUTHORITY, received_at=NOW)
    inbox.accept(risk, authority=AUTHORITY, received_at=NOW)
    assert [item.event_id for item in inbox.pending(AUTHORITY, now=NOW)] == [risk.event_id]
    with pytest.raises(ExternalSignalInboxError, match="risk"):
        inbox.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW)


def test_newer_entry_waits_for_confirmed_flat_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    risk = alert(
        action="EXIT_LONG",
        risk_action="FLATTEN",
        bar_close_time=datetime(2026, 10, 27, 11, 30, tzinfo=UTC),
    )
    newer_entry = alert(action="ENTER_SHORT")
    inbox = ExternalSignalInbox(path)
    inbox.accept(risk, authority=AUTHORITY, received_at=NOW)
    with pytest.raises(ExternalSignalInboxError, match="flat confirmation"):
        inbox.accept(newer_entry, authority=AUTHORITY, received_at=NOW)
    assert inbox.mark_delivered(risk.event_id, authority=AUTHORITY, delivered_at=NOW)

    recovered = ExternalSignalInbox(path)
    with pytest.raises(ExternalSignalInboxError, match="flat confirmation"):
        recovered.accept(newer_entry, authority=AUTHORITY, received_at=NOW)
    assert recovered.confirm_flat(risk.event_id, authority=AUTHORITY, confirmed_at=NOW) is True
    assert recovered.confirm_flat(risk.event_id, authority=AUTHORITY, confirmed_at=NOW) is False
    assert (
        ExternalSignalInbox(path).accept(newer_entry, authority=AUTHORITY, received_at=NOW).status
        == "accepted"
    )


def test_flat_confirmation_requires_latest_delivered_risk_event(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    earlier = alert(
        action="EXIT_LONG",
        risk_action="FLATTEN",
        bar_close_time=datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
    )
    later = alert(
        action="EXIT_LONG",
        risk_action="FLATTEN",
        bar_close_time=datetime(2026, 10, 27, 11, 30, tzinfo=UTC),
    )
    inbox.accept(earlier, authority=AUTHORITY, received_at=NOW)
    assert inbox.confirm_flat(earlier.event_id, authority=AUTHORITY, confirmed_at=NOW) is False
    assert inbox.mark_delivered(earlier.event_id, authority=AUTHORITY, delivered_at=NOW)
    inbox.accept(later, authority=AUTHORITY, received_at=NOW)
    assert inbox.confirm_flat(earlier.event_id, authority=AUTHORITY, confirmed_at=NOW) is False
    with pytest.raises(ExternalSignalInboxError, match="flat confirmation"):
        inbox.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW)
    assert inbox.mark_delivered(later.event_id, authority=AUTHORITY, delivered_at=NOW)
    assert inbox.confirm_flat(later.event_id, authority=AUTHORITY, confirmed_at=NOW) is True
    assert (
        inbox.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW).status
        == "accepted"
    )


def test_expired_pending_entry_does_not_reopen_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    message = alert()
    ExternalSignalInbox(path).accept(message, authority=AUTHORITY, received_at=NOW)
    assert ExternalSignalInbox(path).pending(AUTHORITY, now=NOW + timedelta(seconds=61)) == []


def test_delivery_receipt_is_distinct_from_acceptance(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    message = alert()
    inbox.accept(message, authority=AUTHORITY, received_at=NOW)
    assert inbox.mark_delivered(message.event_id, authority=AUTHORITY, delivered_at=NOW) is False
    assert inbox.begin_signal_delivery(message.event_id, authority=AUTHORITY, started_at=NOW)
    assert inbox.mark_delivered(message.event_id, authority=AUTHORITY, delivered_at=NOW) is True
    assert inbox.pending(AUTHORITY, now=NOW) == []
    assert inbox.mark_delivered(message.event_id, authority=AUTHORITY, delivered_at=NOW) is False


def test_ordinary_exit_has_durable_attempt_and_uncertain_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    inbox = ExternalSignalInbox(path)
    exit_event = alert(
        action="EXIT_LONG",
        bar_close_time=datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
    )
    inbox.accept(exit_event, authority=AUTHORITY, received_at=NOW)
    assert inbox.mark_delivered(exit_event.event_id, authority=AUTHORITY, delivered_at=NOW) is False
    assert inbox.begin_signal_delivery(exit_event.event_id, authority=AUTHORITY, started_at=NOW)
    assert inbox.mark_delivered(exit_event.event_id, authority=AUTHORITY, delivered_at=NOW)

    restarted = ExternalSignalInbox(path)
    assert restarted.recover_uncertain_deliveries(INSTANCE_ID, at=NOW) == 1
    assert restarted.uncertain_signal_ids(INSTANCE_ID) == [exit_event.event_id]
    with pytest.raises(ExternalSignalInboxError, match="unresolved external entry"):
        restarted.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW)


def test_recovered_queued_entry_blocks_new_entries_until_flat_confirmed(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    inbox = ExternalSignalInbox(path)
    first = alert(bar_close_time=datetime(2026, 10, 27, 11, 0, tzinfo=UTC))
    inbox.accept(first, authority=AUTHORITY, received_at=NOW)
    assert inbox.begin_signal_delivery(first.event_id, authority=AUTHORITY, started_at=NOW)
    assert inbox.begin_signal_delivery(first.event_id, authority=AUTHORITY, started_at=NOW) is False
    assert inbox.mark_delivered(first.event_id, authority=AUTHORITY, delivered_at=NOW)

    restarted = ExternalSignalInbox(path)
    assert restarted.recover_uncertain_deliveries(INSTANCE_ID, at=NOW) == 1
    assert restarted.recover_uncertain_deliveries(INSTANCE_ID, at=NOW) == 0
    assert restarted.uncertain_signal_ids(INSTANCE_ID) == [first.event_id]
    assert restarted.pending(AUTHORITY, now=NOW) == []
    with pytest.raises(ExternalSignalInboxError, match="unresolved external entry"):
        restarted.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW)

    risk = alert(
        action="EXIT_LONG",
        risk_action="FLATTEN",
        bar_close_time=datetime(2026, 10, 27, 11, 30, tzinfo=UTC),
    )
    restarted.accept(risk, authority=AUTHORITY, received_at=NOW)
    assert restarted.mark_delivered(risk.event_id, authority=AUTHORITY, delivered_at=NOW)
    assert restarted.confirm_flat(risk.event_id, authority=AUTHORITY, confirmed_at=NOW)
    assert restarted.uncertain_signal_ids(INSTANCE_ID) == []
    assert (
        restarted.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW).status
        == "accepted"
    )


def test_claimed_but_unqueued_entry_is_not_replayed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    inbox = ExternalSignalInbox(path)
    first = alert()
    inbox.accept(first, authority=AUTHORITY, received_at=NOW)
    assert inbox.begin_signal_delivery(first.event_id, authority=AUTHORITY, started_at=NOW)
    assert inbox.pending(AUTHORITY, now=NOW) == []

    restarted = ExternalSignalInbox(path)
    assert restarted.recover_uncertain_deliveries(INSTANCE_ID, at=NOW) == 1
    assert restarted.uncertain_signal_ids(INSTANCE_ID) == [first.event_id]
    assert restarted.mark_delivered(first.event_id, authority=AUTHORITY, delivered_at=NOW) is False
    assert restarted.pending(AUTHORITY, now=NOW) == []


def test_risk_during_entry_dispatch_never_acknowledges_that_entry(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    inbox = ExternalSignalInbox(path)
    entry = alert(bar_close_time=datetime(2026, 10, 27, 11, 0, tzinfo=UTC))
    risk = alert(
        action="EXIT_LONG",
        risk_action="FLATTEN",
        bar_close_time=datetime(2026, 10, 27, 11, 30, tzinfo=UTC),
    )
    inbox.accept(entry, authority=AUTHORITY, received_at=NOW)
    assert inbox.begin_signal_delivery(entry.event_id, authority=AUTHORITY, started_at=NOW)
    inbox.accept(risk, authority=AUTHORITY, received_at=NOW)

    assert inbox.mark_delivered(entry.event_id, authority=AUTHORITY, delivered_at=NOW) is False
    restarted = ExternalSignalInbox(path)
    assert restarted.recover_uncertain_deliveries(INSTANCE_ID, at=NOW) == 1
    assert restarted.uncertain_signal_ids(INSTANCE_ID) == [entry.event_id]
    assert restarted.mark_delivered(risk.event_id, authority=AUTHORITY, delivered_at=NOW)
    assert restarted.confirm_flat(risk.event_id, authority=AUTHORITY, confirmed_at=NOW)
    assert restarted.uncertain_signal_ids(INSTANCE_ID) == []


def test_bounded_inbox_refuses_new_event_without_losing_existing_one(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite", max_pending=1)
    first = alert()
    inbox.accept(first, authority=AUTHORITY, received_at=NOW)
    with pytest.raises(ExternalSignalInboxError, match="full"):
        inbox.accept(alert(action="ENTER_SHORT"), authority=AUTHORITY, received_at=NOW)
    assert [item.event_id for item in inbox.pending(AUTHORITY, now=NOW)] == [first.event_id]


def test_inbox_refuses_symlink_and_permissive_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"not a database")
    link = tmp_path / "inbox-link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ExternalSignalInboxError):
        ExternalSignalInbox(link)
    with pytest.raises(ExternalSignalInboxError):
        ExternalSignalInbox(target)


def test_existing_risk_fence_migrates_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE external_signal_risk_fence (
                source_id TEXT NOT NULL,
                deployment_instance_id TEXT NOT NULL,
                strategy_revision TEXT NOT NULL,
                instrument TEXT NOT NULL,
                bar_close_ns INTEGER NOT NULL,
                PRIMARY KEY (source_id, deployment_instance_id, strategy_revision, instrument)
            )"""
        )
        connection.execute(
            "INSERT INTO external_signal_risk_fence VALUES (?, ?, ?, ?, ?)",
            ("independent-demo", str(INSTANCE_ID), "supertrend-v0.5", "ETH-USDT-SWAP", 1),
        )
    path.chmod(0o600)
    inbox = ExternalSignalInbox(path)
    with pytest.raises(ExternalSignalInboxError, match="flat confirmation"):
        inbox.accept(alert(), authority=AUTHORITY, received_at=NOW)


def test_previous_entry_attempt_table_migrates_without_losing_uncertainty(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE external_signal_entry_delivery (
                event_id TEXT PRIMARY KEY,
                deployment_instance_id TEXT NOT NULL,
                state TEXT NOT NULL,
                started_at_ns INTEGER NOT NULL,
                queued_at_ns INTEGER,
                recovered_at_ns INTEGER,
                resolved_at_ns INTEGER
            )"""
        )
        connection.execute(
            """INSERT INTO external_signal_entry_delivery
               (event_id, deployment_instance_id, state, started_at_ns)
               VALUES (?, ?, 'uncertain', 1)""",
            ("prior-entry", str(INSTANCE_ID)),
        )
    path.chmod(0o600)

    inbox = ExternalSignalInbox(path)

    assert inbox.uncertain_signal_ids(INSTANCE_ID) == ["prior-entry"]


def test_pending_is_bound_to_exact_runtime_generation_and_digest(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    message = alert()
    inbox.accept(message, authority=AUTHORITY, received_at=NOW)
    changed_generation = EngineLifecycleAuthority(
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=AUTHORITY.deployment_spec_id,
        deployment_spec_digest=AUTHORITY.deployment_spec_digest,
        generation=2,
        trading_mode="testnet",
    )
    changed_digest = EngineLifecycleAuthority(
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=AUTHORITY.deployment_spec_id,
        deployment_spec_digest="b" * 64,
        generation=1,
        trading_mode="testnet",
    )
    changed_spec = EngineLifecycleAuthority(
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=UUID("20000000-0000-4000-8000-000000000002"),
        deployment_spec_digest=AUTHORITY.deployment_spec_digest,
        generation=1,
        trading_mode="testnet",
    )
    assert [item.event_id for item in inbox.pending(AUTHORITY, now=NOW)] == [message.event_id]
    assert inbox.pending(changed_generation, now=NOW) == []
    assert inbox.pending(changed_digest, now=NOW) == []
    assert inbox.pending(changed_spec, now=NOW) == []


def test_older_generation_pending_does_not_fill_current_generation_capacity(
    tmp_path: Path,
) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite", max_pending=1)
    inbox.accept(alert(), authority=AUTHORITY, received_at=NOW)
    replacement = EngineLifecycleAuthority(
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=AUTHORITY.deployment_spec_id,
        deployment_spec_digest="b" * 64,
        generation=2,
        trading_mode="testnet",
    )
    current = alert(action="ENTER_SHORT")
    assert inbox.accept(current, authority=replacement, received_at=NOW).status == "accepted"
    assert [item.event_id for item in inbox.pending(replacement, now=NOW)] == [current.event_id]
    assert inbox.mark_delivered(alert().event_id, authority=replacement, delivered_at=NOW) is False
