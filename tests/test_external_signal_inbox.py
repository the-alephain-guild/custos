"""Durability and ordering tests for an untrusted external signal inbox."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from custos.core.external_signal_contract import make_event_id, parse_custos_v1_alert
from custos.core.external_signal_inbox import ExternalSignalInbox, ExternalSignalInboxError

INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)


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
    receipt = ExternalSignalInbox(path).accept(message, received_at=NOW)
    assert receipt.status == "accepted"
    assert receipt.event_id == message.event_id

    recovered = ExternalSignalInbox(path).pending(INSTANCE_ID, now=NOW)
    assert [item.event_id for item in recovered] == [message.event_id]
    assert recovered[0].action == "ENTER_LONG"
    assert b"demo-secret-never-persist" not in path.read_bytes()


def test_exact_replay_is_idempotent_but_same_id_changed_amount_conflicts(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    first = alert()
    assert inbox.accept(first, received_at=NOW).status == "accepted"
    assert inbox.accept(first, received_at=NOW).status == "duplicate"
    with pytest.raises(ExternalSignalInboxError, match="conflict"):
        inbox.accept(alert(amount="12"), received_at=NOW)
    assert len(inbox.pending(INSTANCE_ID, now=NOW)) == 1


def test_risk_exit_supersedes_unconsumed_entry_at_same_bar(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    entry = alert()
    risk = alert(action="EXIT_LONG", risk_action="FLATTEN")
    inbox.accept(entry, received_at=NOW)
    inbox.accept(risk, received_at=NOW)
    assert [item.event_id for item in inbox.pending(INSTANCE_ID, now=NOW)] == [risk.event_id]
    with pytest.raises(ExternalSignalInboxError, match="risk"):
        inbox.accept(alert(action="ENTER_SHORT"), received_at=NOW)


def test_expired_pending_entry_does_not_reopen_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    message = alert()
    ExternalSignalInbox(path).accept(message, received_at=NOW)
    assert ExternalSignalInbox(path).pending(INSTANCE_ID, now=NOW + timedelta(seconds=61)) == []


def test_delivery_receipt_is_distinct_from_acceptance(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite")
    message = alert()
    inbox.accept(message, received_at=NOW)
    assert inbox.mark_delivered(message.event_id, delivered_at=NOW) is True
    assert inbox.pending(INSTANCE_ID, now=NOW) == []
    assert inbox.mark_delivered(message.event_id, delivered_at=NOW) is False


def test_bounded_inbox_refuses_new_event_without_losing_existing_one(tmp_path: Path) -> None:
    inbox = ExternalSignalInbox(tmp_path / "inbox.sqlite", max_pending=1)
    first = alert()
    inbox.accept(first, received_at=NOW)
    with pytest.raises(ExternalSignalInboxError, match="full"):
        inbox.accept(alert(action="ENTER_SHORT"), received_at=NOW)
    assert [item.event_id for item in inbox.pending(INSTANCE_ID, now=NOW)] == [first.event_id]


def test_inbox_refuses_symlink_and_permissive_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"not a database")
    link = tmp_path / "inbox-link.sqlite"
    link.symlink_to(target)
    with pytest.raises(ExternalSignalInboxError):
        ExternalSignalInbox(link)
    with pytest.raises(ExternalSignalInboxError):
        ExternalSignalInbox(target)
