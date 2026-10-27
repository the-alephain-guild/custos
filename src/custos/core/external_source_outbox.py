"""Private durable outbox for target-neutral source events awaiting broker PubAck."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from custos.core.external_source_signal import ExternalSourceSignal, make_source_event_id


class ExternalSourceOutboxError(ValueError):
    """A source event cannot be safely stored or published."""


@dataclass(frozen=True, slots=True)
class SourceIntakeReceipt:
    event_id: str
    status: str


@dataclass(frozen=True, slots=True)
class SourcePublicationClaim:
    tenant_id: str
    event_id: str
    source_id: str
    publish_id: str
    claim_id: str
    payload: bytes


def _epoch_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ExternalSourceOutboxError("source outbox time must be UTC")
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def _wire(signal: ExternalSourceSignal) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "tenantId": signal.tenant_id,
        "sourceId": signal.source_id,
        "strategyRevision": signal.strategy_revision,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "barCloseTime": signal.bar_close_time.isoformat().replace("+00:00", "Z"),
        "timestamp": signal.timestamp.isoformat().replace("+00:00", "Z"),
        "maxLag": signal.max_lag,
        "target": signal.target,
        "riskAction": signal.risk_action,
        "eventId": signal.event_id,
    }


class ExternalSourceOutbox:
    """Persist one source fact, then lease it until an independent PubAck arrives."""

    def __init__(self, path: Path, *, max_pending: int = 1000) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.path = Path(path)
        self.max_pending = max_pending
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise ExternalSourceOutboxError("source outbox path is unsafe") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ExternalSourceOutboxError("source outbox file is unsafe")
        finally:
            os.close(descriptor)
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_source_outbox (
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    bar_close_ns INTEGER NOT NULL,
                    risk_action TEXT,
                    expires_at_ns INTEGER NOT NULL,
                    received_at_ns INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'claimed', 'published', 'expired')
                    ),
                    claim_id TEXT,
                    lease_until_ns INTEGER,
                    published_at_ns INTEGER,
                    broker_sequence INTEGER,
                    PRIMARY KEY (tenant_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS external_source_outbox_pending
                    ON external_source_outbox(tenant_id, status, bar_close_ns);
                """
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

    def accept(self, signal: ExternalSourceSignal, *, received_at: datetime) -> SourceIntakeReceipt:
        identity = make_source_event_id(
            source_id=signal.source_id,
            strategy_revision=signal.strategy_revision,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            bar_close_time=signal.bar_close_time,
            target=signal.target,
            risk_action=signal.risk_action,
        )
        if identity != signal.event_id:
            raise ExternalSourceOutboxError("source event identity differs from fields")
        received_ns = _epoch_ns(received_at)
        expires_ns = _epoch_ns(signal.expires_at)
        if received_ns > expires_ns:
            raise ExternalSourceOutboxError("source event is expired")
        payload_json = json.dumps(_wire(signal), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT payload_digest FROM external_source_outbox
                   WHERE tenant_id = ? AND event_id = ?""",
                (signal.tenant_id, signal.event_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise ExternalSourceOutboxError("source event replay conflict")
                return SourceIntakeReceipt(signal.event_id, "duplicate")
            connection.execute(
                """UPDATE external_source_outbox SET status = 'expired'
                   WHERE status IN ('pending', 'claimed') AND expires_at_ns < ?""",
                (received_ns,),
            )
            pending = connection.execute(
                """SELECT COUNT(*) FROM external_source_outbox
                   WHERE status IN ('pending', 'claimed')"""
            ).fetchone()[0]
            if pending >= self.max_pending:
                raise ExternalSourceOutboxError("source outbox is full")
            connection.execute(
                """INSERT INTO external_source_outbox
                   (tenant_id, event_id, source_id, payload_json, payload_digest,
                    bar_close_ns, risk_action, expires_at_ns, received_at_ns, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    signal.tenant_id,
                    signal.event_id,
                    signal.source_id,
                    payload_json,
                    digest,
                    _epoch_ns(signal.bar_close_time),
                    signal.risk_action,
                    expires_ns,
                    received_ns,
                ),
            )
        return SourceIntakeReceipt(signal.event_id, "accepted")

    def claim_next(
        self,
        tenant_id: str,
        *,
        at: datetime,
        lease_for: timedelta = timedelta(seconds=10),
    ) -> SourcePublicationClaim | None:
        if lease_for <= timedelta(0) or lease_for > timedelta(minutes=5):
            raise ExternalSourceOutboxError("source publication lease is invalid")
        now_ns = _epoch_ns(at)
        lease_until_ns = _epoch_ns(at + lease_for)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE external_source_outbox SET status = 'expired'
                   WHERE status IN ('pending', 'claimed') AND expires_at_ns < ?""",
                (now_ns,),
            )
            row = connection.execute(
                """SELECT * FROM external_source_outbox
                   WHERE tenant_id = ? AND expires_at_ns >= ?
                     AND (status = 'pending'
                          OR (status = 'claimed' AND lease_until_ns < ?))
                   ORDER BY bar_close_ns, risk_action IS NULL, received_at_ns, event_id
                   LIMIT 1""",
                (tenant_id, now_ns, now_ns),
            ).fetchone()
            if row is None:
                return None
            claim_id = str(uuid4())
            connection.execute(
                """UPDATE external_source_outbox
                   SET status = 'claimed', claim_id = ?, lease_until_ns = ?
                   WHERE tenant_id = ? AND event_id = ?""",
                (claim_id, lease_until_ns, tenant_id, row["event_id"]),
            )
            event_id = str(row["event_id"])
            publish_id = hashlib.sha256(f"{tenant_id}\0{event_id}".encode()).hexdigest()
            return SourcePublicationClaim(
                tenant_id=tenant_id,
                event_id=event_id,
                source_id=str(row["source_id"]),
                publish_id=publish_id,
                claim_id=claim_id,
                payload=str(row["payload_json"]).encode(),
            )

    def mark_published(
        self, claim: SourcePublicationClaim, *, broker_sequence: int, at: datetime
    ) -> bool:
        if type(broker_sequence) is not int or broker_sequence < 1:
            raise ExternalSourceOutboxError("broker sequence is invalid")
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE external_source_outbox
                   SET status = 'published', published_at_ns = ?, broker_sequence = ?
                   WHERE tenant_id = ? AND event_id = ?
                     AND status = 'claimed' AND claim_id = ?""",
                (
                    _epoch_ns(at),
                    broker_sequence,
                    claim.tenant_id,
                    claim.event_id,
                    claim.claim_id,
                ),
            )
            return result.rowcount == 1
