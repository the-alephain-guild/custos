"""Durable, token-free inbox for already parsed external strategy alerts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_signal_contract import CustosV1Alert


class ExternalSignalInboxError(ValueError):
    """An event cannot be accepted or recovered without changing its meaning."""


@dataclass(frozen=True, slots=True)
class IntakeReceipt:
    event_id: str
    status: str


@dataclass(frozen=True, slots=True)
class StoredExternalSignal:
    event_id: str
    source_id: str
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str
    generation: int
    instrument: str
    bar_close_time: datetime
    action: str
    risk_action: str | None
    payload: dict[str, object]


def _epoch_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExternalSignalInboxError("event time must include a timezone")
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def _sanitized(alert: CustosV1Alert) -> dict[str, object]:
    trade = alert.trade
    return {
        "schemaVersion": 1,
        "eventId": alert.event_id,
        "sourceId": alert.source_id,
        "strategyRevision": alert.strategy_revision,
        "deploymentInstanceId": str(alert.deployment_instance_id),
        "instrument": trade.instrument,
        "timeframe": alert.timeframe,
        "barCloseTime": alert.bar_close_time.isoformat(),
        "timestamp": trade.timestamp.isoformat(),
        "maxLag": trade.max_lag,
        "action": trade.action,
        "orderType": trade.order_type,
        "orderPriceOffset": (
            str(trade.order_price_offset) if trade.order_price_offset is not None else None
        ),
        "investmentType": trade.investment_type,
        "amount": str(trade.amount) if trade.amount is not None else None,
        "requestedLeverage": alert.requested_leverage,
        "riskAction": alert.risk_action,
    }


class ExternalSignalInbox:
    """SQLite inbox with idempotent acceptance and a per-stream risk fence.

    The inbox is not an authorization boundary. The caller must authenticate
    the sender and parse against a verified deployment before calling accept.
    """

    def __init__(self, path: Path, *, max_pending: int = 1000) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.path = Path(path)
        self.max_pending = max_pending
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise ExternalSignalInboxError("external signal inbox path is unsafe") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ExternalSignalInboxError("external signal inbox file is not private")
        finally:
            os.close(descriptor)
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_signal_inbox (
                    event_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    deployment_instance_id TEXT NOT NULL,
                    deployment_spec_id TEXT NOT NULL DEFAULT '',
                    deployment_spec_digest TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    strategy_revision TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    bar_close_ns INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    risk_action TEXT,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    expires_at_ns INTEGER NOT NULL,
                    received_at_ns INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('accepted', 'delivered', 'superseded', 'expired')
                    ),
                    delivered_at_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS external_signal_pending
                    ON external_signal_inbox(deployment_instance_id, status, bar_close_ns);
                CREATE TABLE IF NOT EXISTS external_signal_risk_fence (
                    source_id TEXT NOT NULL,
                    deployment_instance_id TEXT NOT NULL,
                    strategy_revision TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    bar_close_ns INTEGER NOT NULL,
                    risk_event_id TEXT,
                    flat_confirmed_at_ns INTEGER,
                    PRIMARY KEY (source_id, deployment_instance_id,
                                 strategy_revision, instrument)
                );
                CREATE TABLE IF NOT EXISTS external_signal_delivery_attempt (
                    event_id TEXT PRIMARY KEY,
                    deployment_instance_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('dispatching', 'queued', 'uncertain', 'resolved')
                    ),
                    started_at_ns INTEGER NOT NULL,
                    queued_at_ns INTEGER,
                    recovered_at_ns INTEGER,
                    resolved_at_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS external_signal_attempt_state
                    ON external_signal_delivery_attempt(deployment_instance_id, state);
                """
            )
            legacy_attempts = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'table' AND name = 'external_signal_entry_delivery'"""
            ).fetchone()
            if legacy_attempts is not None:
                connection.execute(
                    """INSERT INTO external_signal_delivery_attempt
                       (event_id, deployment_instance_id, state, started_at_ns,
                        queued_at_ns, recovered_at_ns, resolved_at_ns)
                       SELECT event_id, deployment_instance_id, state, started_at_ns,
                              queued_at_ns, recovered_at_ns, resolved_at_ns
                       FROM external_signal_entry_delivery"""
                )
                connection.execute("DROP TABLE external_signal_entry_delivery")
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(external_signal_risk_fence)")
            }
            if "risk_event_id" not in columns:
                connection.execute(
                    "ALTER TABLE external_signal_risk_fence ADD COLUMN risk_event_id TEXT"
                )
            if "flat_confirmed_at_ns" not in columns:
                connection.execute(
                    "ALTER TABLE external_signal_risk_fence ADD COLUMN flat_confirmed_at_ns INTEGER"
                )
            inbox_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(external_signal_inbox)")
            }
            if "deployment_spec_digest" not in inbox_columns:
                connection.execute(
                    "ALTER TABLE external_signal_inbox ADD COLUMN deployment_spec_digest TEXT "
                    "NOT NULL DEFAULT ''"
                )
            if "deployment_spec_id" not in inbox_columns:
                connection.execute(
                    "ALTER TABLE external_signal_inbox ADD COLUMN deployment_spec_id TEXT "
                    "NOT NULL DEFAULT ''"
                )
            if "generation" not in inbox_columns:
                connection.execute(
                    "ALTER TABLE external_signal_inbox ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def accept(
        self,
        alert: CustosV1Alert,
        *,
        authority: EngineLifecycleAuthority,
        received_at: datetime,
    ) -> IntakeReceipt:
        if authority.trading_mode not in {"sandbox", "testnet"}:
            raise ExternalSignalInboxError("external signal is outside non-live runtime scope")
        if alert.deployment_instance_id != authority.deployment_instance_id:
            raise ExternalSignalInboxError("external signal instance differs from runtime")
        payload = _sanitized(alert)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        expires_ns = _epoch_ns(alert.trade.timestamp) + alert.trade.max_lag * 1_000_000_000
        received_ns = _epoch_ns(received_at)
        if received_ns > expires_ns:
            raise ExternalSignalInboxError("external signal is expired")

        stream = (
            alert.source_id,
            str(alert.deployment_instance_id),
            alert.strategy_revision,
            alert.trade.instrument,
        )
        bar_ns = _epoch_ns(alert.bar_close_time)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT payload_digest, deployment_spec_id, deployment_spec_digest, generation
                   FROM external_signal_inbox WHERE event_id = ?""",
                (alert.event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_digest"] != digest
                    or existing["deployment_spec_id"] != str(authority.deployment_spec_id)
                    or existing["deployment_spec_digest"] != authority.deployment_spec_digest
                    or existing["generation"] != authority.generation
                ):
                    raise ExternalSignalInboxError("external signal replay conflict")
                return IntakeReceipt(alert.event_id, "duplicate")

            if alert.trade.action.startswith("ENTER"):
                uncertain = connection.execute(
                    """SELECT 1 FROM external_signal_delivery_attempt
                       WHERE deployment_instance_id = ? AND state = 'uncertain' LIMIT 1""",
                    (str(alert.deployment_instance_id),),
                ).fetchone()
                if uncertain is not None:
                    raise ExternalSignalInboxError("unresolved external entry blocks new entry")

            fence = connection.execute(
                """SELECT bar_close_ns, flat_confirmed_at_ns
                   FROM external_signal_risk_fence
                   WHERE source_id = ? AND deployment_instance_id = ?
                     AND strategy_revision = ? AND instrument = ?""",
                stream,
            ).fetchone()
            if alert.trade.action.startswith("ENTER") and fence is not None:
                if bar_ns <= fence["bar_close_ns"]:
                    raise ExternalSignalInboxError("entry precedes the latest risk flatten")
                if fence["flat_confirmed_at_ns"] is None:
                    raise ExternalSignalInboxError("entry awaits flat confirmation")

            head = connection.execute(
                """SELECT MAX(bar_close_ns) AS bar_close_ns FROM external_signal_inbox
                   WHERE source_id = ? AND deployment_instance_id = ?
                     AND strategy_revision = ? AND instrument = ?""",
                stream,
            ).fetchone()
            if head["bar_close_ns"] is not None and bar_ns < head["bar_close_ns"]:
                raise ExternalSignalInboxError("external signal arrived out of order")

            if alert.risk_action == "FLATTEN":
                connection.execute(
                    """UPDATE external_signal_inbox SET status = 'superseded'
                       WHERE source_id = ? AND deployment_instance_id = ?
                         AND strategy_revision = ? AND instrument = ?
                         AND status = 'accepted' AND action LIKE 'ENTER_%'
                         AND bar_close_ns <= ?""",
                    (*stream, bar_ns),
                )
                connection.execute(
                    """INSERT INTO external_signal_risk_fence
                       (source_id, deployment_instance_id, strategy_revision,
                        instrument, bar_close_ns, risk_event_id, flat_confirmed_at_ns)
                       VALUES (?, ?, ?, ?, ?, ?, NULL)
                       ON CONFLICT(source_id, deployment_instance_id,
                                   strategy_revision, instrument)
                       DO UPDATE SET bar_close_ns = excluded.bar_close_ns,
                                     risk_event_id = excluded.risk_event_id,
                                     flat_confirmed_at_ns = NULL""",
                    (*stream, bar_ns, alert.event_id),
                )

            pending = connection.execute(
                """SELECT COUNT(*) FROM external_signal_inbox
                   WHERE deployment_instance_id = ? AND status = 'accepted'
                     AND deployment_spec_id = ?
                     AND deployment_spec_digest = ? AND generation = ?""",
                (
                    str(alert.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            ).fetchone()[0]
            if pending >= self.max_pending:
                raise ExternalSignalInboxError("external signal inbox is full")

            connection.execute(
                """INSERT INTO external_signal_inbox
                   (event_id, source_id, deployment_instance_id,
                    deployment_spec_id, deployment_spec_digest, generation, strategy_revision,
                    instrument, bar_close_ns,
                    action, risk_action, payload_json, payload_digest,
                    expires_at_ns, received_at_ns, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')""",
                (
                    alert.event_id,
                    alert.source_id,
                    str(alert.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                    alert.strategy_revision,
                    alert.trade.instrument,
                    bar_ns,
                    alert.trade.action,
                    alert.risk_action,
                    payload_json,
                    digest,
                    expires_ns,
                    received_ns,
                ),
            )
        return IntakeReceipt(alert.event_id, "accepted")

    def pending(
        self, authority: EngineLifecycleAuthority, *, now: datetime
    ) -> list[StoredExternalSignal]:
        if authority.trading_mode not in {"sandbox", "testnet"}:
            raise ExternalSignalInboxError("external signal is outside non-live runtime scope")
        deployment_instance_id = authority.deployment_instance_id
        now_ns = _epoch_ns(now)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE external_signal_inbox SET status = 'expired'
                   WHERE deployment_instance_id = ? AND status = 'accepted'
                     AND expires_at_ns < ?""",
                (str(deployment_instance_id), now_ns),
            )
            rows = connection.execute(
                """SELECT event.* FROM external_signal_inbox AS event
                   LEFT JOIN external_signal_delivery_attempt AS delivery
                     ON delivery.event_id = event.event_id
                   WHERE event.deployment_instance_id = ? AND event.status = 'accepted'
                     AND event.deployment_spec_id = ?
                     AND event.deployment_spec_digest = ? AND event.generation = ?
                     AND delivery.event_id IS NULL
                   ORDER BY event.bar_close_ns, event.risk_action IS NULL,
                            event.received_at_ns, event.event_id""",
                (
                    str(deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            ).fetchall()
        return [
            StoredExternalSignal(
                event_id=row["event_id"],
                source_id=row["source_id"],
                deployment_instance_id=UUID(row["deployment_instance_id"]),
                deployment_spec_id=UUID(row["deployment_spec_id"]),
                deployment_spec_digest=row["deployment_spec_digest"],
                generation=row["generation"],
                instrument=row["instrument"],
                bar_close_time=datetime.fromtimestamp(row["bar_close_ns"] / 1_000_000_000, UTC),
                action=row["action"],
                risk_action=row["risk_action"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def begin_signal_delivery(
        self,
        event_id: str,
        *,
        authority: EngineLifecycleAuthority,
        started_at: datetime,
    ) -> bool:
        """Persist a normal signal attempt before touching the strategy queue."""
        if authority.trading_mode not in {"sandbox", "testnet"}:
            raise ExternalSignalInboxError("external signal is outside non-live runtime scope")
        started_ns = _epoch_ns(started_at)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT 1 FROM external_signal_inbox
                   WHERE event_id = ? AND status = 'accepted' AND risk_action IS NULL
                     AND deployment_instance_id = ? AND deployment_spec_id = ?
                     AND deployment_spec_digest = ? AND generation = ?
                     AND expires_at_ns >= ?""",
                (
                    event_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                    started_ns,
                ),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """INSERT OR IGNORE INTO external_signal_delivery_attempt
                   (event_id, deployment_instance_id, state, started_at_ns)
                   VALUES (?, ?, 'dispatching', ?)""",
                (event_id, str(authority.deployment_instance_id), started_ns),
            )
            return cursor.rowcount == 1

    def recover_uncertain_deliveries(self, deployment_instance_id: UUID, *, at: datetime) -> int:
        """Quarantine normal signals with unknown outcomes after restart."""
        instance = str(deployment_instance_id)
        recovered_ns = _epoch_ns(at)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT event_id FROM external_signal_delivery_attempt
                   WHERE deployment_instance_id = ? AND state IN ('dispatching', 'queued')""",
                (instance,),
            ).fetchall()
            if not rows:
                return 0
            event_ids = [row["event_id"] for row in rows]
            connection.executemany(
                """UPDATE external_signal_delivery_attempt
                   SET state = 'uncertain', recovered_at_ns = ? WHERE event_id = ?""",
                [(recovered_ns, event_id) for event_id in event_ids],
            )
            connection.executemany(
                """UPDATE external_signal_inbox SET status = 'superseded'
                   WHERE event_id = ? AND status = 'accepted'""",
                [(event_id,) for event_id in event_ids],
            )
            return len(event_ids)

    def uncertain_signal_ids(self, deployment_instance_id: UUID) -> list[str]:
        """Name signals requiring account and open-order reconciliation."""
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT event_id FROM external_signal_delivery_attempt
                   WHERE deployment_instance_id = ? AND state = 'uncertain'
                   ORDER BY started_at_ns, event_id""",
                (str(deployment_instance_id),),
            ).fetchall()
        return [row["event_id"] for row in rows]

    def mark_delivered(
        self,
        event_id: str,
        *,
        authority: EngineLifecycleAuthority,
        delivered_at: datetime,
    ) -> bool:
        if authority.trading_mode not in {"sandbox", "testnet"}:
            raise ExternalSignalInboxError("external signal is outside non-live runtime scope")
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT action, risk_action FROM external_signal_inbox
                   WHERE event_id = ? AND deployment_instance_id = ?
                     AND deployment_spec_id = ?
                     AND deployment_spec_digest = ? AND generation = ?""",
                (
                    event_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            ).fetchone()
            if row is None:
                return False
            normal_signal = row["risk_action"] is None
            if normal_signal:
                attempt = connection.execute(
                    """SELECT state FROM external_signal_delivery_attempt
                       WHERE event_id = ? AND deployment_instance_id = ?""",
                    (event_id, str(authority.deployment_instance_id)),
                ).fetchone()
                if attempt is None or attempt["state"] != "dispatching":
                    return False
            cursor = connection.execute(
                """UPDATE external_signal_inbox
                   SET status = 'delivered', delivered_at_ns = ?
                   WHERE event_id = ? AND status = 'accepted'
                     AND deployment_instance_id = ?
                     AND deployment_spec_id = ?
                     AND deployment_spec_digest = ? AND generation = ?""",
                (
                    _epoch_ns(delivered_at),
                    event_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if normal_signal:
                connection.execute(
                    """UPDATE external_signal_delivery_attempt
                       SET state = 'queued', queued_at_ns = ? WHERE event_id = ?""",
                    (_epoch_ns(delivered_at), event_id),
                )
            return True

    def confirm_flat(
        self,
        risk_event_id: str,
        *,
        authority: EngineLifecycleAuthority,
        confirmed_at: datetime,
    ) -> bool:
        """Release the entry fence only for the latest delivered risk event.

        The caller must first independently establish that the venue position and
        risk-increasing orders are clear. This method does not inspect the venue.
        """
        if authority.trading_mode not in {"sandbox", "testnet"}:
            raise ExternalSignalInboxError("external signal is outside non-live runtime scope")
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE external_signal_risk_fence
                   SET flat_confirmed_at_ns = ?
                   WHERE risk_event_id = ? AND flat_confirmed_at_ns IS NULL
                     AND EXISTS (
                       SELECT 1 FROM external_signal_inbox
                       WHERE event_id = ? AND risk_action = 'FLATTEN'
                         AND status = 'delivered'
                         AND deployment_instance_id = ?
                         AND deployment_spec_id = ?
                         AND deployment_spec_digest = ? AND generation = ?
                     )""",
                (
                    _epoch_ns(confirmed_at),
                    risk_event_id,
                    risk_event_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """UPDATE external_signal_delivery_attempt
                   SET state = 'resolved', resolved_at_ns = ?
                   WHERE deployment_instance_id = ? AND state = 'uncertain'""",
                (_epoch_ns(confirmed_at), str(authority.deployment_instance_id)),
            )
            return True
