"""Durable, deployment-scoped deliveries derived from source observations."""

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
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from custos_toolkit.signals.types import Signal, SignalDirection

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.external_source_delivery import (
    PlannedTargetDelivery,
    make_target_delivery_id,
)


class ExternalTargetInboxError(ValueError):
    """A target delivery cannot cross authority, order, or recovery fences."""


@dataclass(frozen=True, slots=True)
class TargetIntakeReceipt:
    delivery_id: str
    status: str


@dataclass(frozen=True, slots=True)
class PendingFlatRequest:
    delivery_id: str
    connector: str
    pair: str
    handoff_at: datetime


@dataclass(frozen=True, slots=True)
class StoredTargetDelivery:
    delivery_id: str
    source_event_id: str
    target: str
    risk_action: str | None
    pair: str
    connector: str
    emitted_at_ns: int
    max_lag: int

    def to_signal(self) -> Signal | None:
        """Recreate only the bounded queue intent, never a flatness claim."""
        if self.target == "FLAT":
            return None
        if self.target not in {"LONG", "SHORT"}:
            raise ExternalTargetInboxError("stored target is unsupported")
        direction = (
            SignalDirection.ENTER_LONG if self.target == "LONG" else SignalDirection.ENTER_SHORT
        )
        return Signal(
            direction=direction,
            pair=self.pair,
            price=Decimal("0"),
            timestamp=self.emitted_at_ns,
            max_lag=self.max_lag,
            amount=None,
            order_type="market",
            metadata={
                "source_event_id": self.source_event_id,
                "target_delivery_id": self.delivery_id,
            },
        )


def _epoch_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ExternalTargetInboxError("target event time must be UTC")
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def _require_non_live(authority: EngineLifecycleAuthority) -> None:
    if authority.trading_mode not in {"sandbox", "testnet"}:
        raise ExternalTargetInboxError("external target delivery is outside non-live scope")


class ExternalTargetInbox:
    """Persist each deployment's handoff separately from source PubAck and fills."""

    def __init__(self, path: Path, *, max_pending: int = 1000) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.path = Path(path)
        self.max_pending = max_pending
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise ExternalTargetInboxError("target inbox path is unsafe") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ExternalTargetInboxError("target inbox file is unsafe")
        finally:
            os.close(descriptor)
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_target_delivery (
                    delivery_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    deployment_instance_id TEXT NOT NULL,
                    deployment_spec_id TEXT NOT NULL,
                    deployment_spec_digest TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    pair TEXT NOT NULL,
                    connector TEXT NOT NULL,
                    bar_close_ns INTEGER NOT NULL,
                    emitted_at_ns INTEGER NOT NULL,
                    max_lag INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    risk_action TEXT,
                    expires_at_ns INTEGER NOT NULL,
                    received_at_ns INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('accepted', 'dispatching', 'queued',
                                   'flat_requested', 'flat_confirmed',
                                   'uncertain', 'expired', 'superseded')
                    ),
                    handoff_at_ns INTEGER,
                    recovered_at_ns INTEGER,
                    resolved_at_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS external_target_pending
                    ON external_target_delivery(deployment_instance_id, status, bar_close_ns);
                CREATE TABLE IF NOT EXISTS external_target_flat_fence (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    deployment_instance_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    bar_close_ns INTEGER NOT NULL,
                    flat_delivery_id TEXT NOT NULL,
                    confirmed_at_ns INTEGER,
                    PRIMARY KEY (tenant_id, source_id, deployment_instance_id, pair)
                );
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

    def accept(
        self, delivery: PlannedTargetDelivery, *, received_at: datetime
    ) -> TargetIntakeReceipt:
        authority = delivery.authority
        _require_non_live(authority)
        if delivery.target not in {"LONG", "SHORT", "FLAT"}:
            raise ExternalTargetInboxError("target is unsupported")
        if delivery.risk_action is not None and (
            delivery.risk_action != "FLATTEN" or delivery.target != "FLAT"
        ):
            raise ExternalTargetInboxError("risk action requires a flat target")
        if delivery.delivery_id != make_target_delivery_id(
            tenant_id=delivery.tenant_id,
            source_event_id=delivery.source_event_id,
            authority=authority,
            connector=delivery.connector,
            pair=delivery.pair,
        ):
            raise ExternalTargetInboxError("target delivery identity differs from scope")
        now_ns = _epoch_ns(received_at)
        expiry_ns = _epoch_ns(delivery.expires_at)
        if now_ns > expiry_ns:
            raise ExternalTargetInboxError("target delivery is expired")
        bar_ns = _epoch_ns(delivery.bar_close_time)
        emitted_ns = _epoch_ns(delivery.emitted_at)
        if type(delivery.max_lag) is not int or not 1 <= delivery.max_lag <= 3600:
            raise ExternalTargetInboxError("target maxLag is invalid")
        if expiry_ns != emitted_ns + delivery.max_lag * 1_000_000_000:
            raise ExternalTargetInboxError("target expiry differs from source timestamp")
        if delivery.target == "FLAT":
            if delivery.signal is not None:
                raise ExternalTargetInboxError("flat target must not carry a queue signal")
        else:
            signal = delivery.signal
            expected_direction = (
                SignalDirection.ENTER_LONG
                if delivery.target == "LONG"
                else SignalDirection.ENTER_SHORT
            )
            if not isinstance(signal, Signal):
                raise ExternalTargetInboxError("entry target requires a typed queue signal")
            if signal.amount is not None:
                raise ExternalTargetInboxError("source amount override is not admitted")
            if (
                signal.direction != expected_direction
                or signal.pair != delivery.pair
                or signal.price != 0
                or signal.timestamp != emitted_ns
                or signal.max_lag != delivery.max_lag
                or signal.order_type != "market"
                or signal.investment_type is not None
                or signal.order_price_offset is not None
                or signal.signal_token is not None
                or signal.metadata.get("source_event_id") != delivery.source_event_id
                or signal.metadata.get("target_delivery_id") != delivery.delivery_id
            ):
                raise ExternalTargetInboxError("queue signal differs from target delivery")
        payload = {
            "deliveryId": delivery.delivery_id,
            "sourceEventId": delivery.source_event_id,
            "tenantId": delivery.tenant_id,
            "sourceId": delivery.source_id,
            "instanceId": str(authority.deployment_instance_id),
            "specId": str(authority.deployment_spec_id),
            "specDigest": authority.deployment_spec_digest,
            "generation": authority.generation,
            "pair": delivery.pair,
            "connector": delivery.connector,
            "barCloseNs": bar_ns,
            "emittedAtNs": emitted_ns,
            "maxLag": delivery.max_lag,
            "target": delivery.target,
            "riskAction": delivery.risk_action,
            "expiresAtNs": expiry_ns,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        instance = str(authority.deployment_instance_id)
        stream = (delivery.tenant_id, delivery.source_id, instance, delivery.pair)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_digest FROM external_target_delivery WHERE delivery_id = ?",
                (delivery.delivery_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise ExternalTargetInboxError("target delivery replay conflict")
                return TargetIntakeReceipt(delivery.delivery_id, "duplicate")
            registered_source = connection.execute(
                """SELECT tenant_id, source_id FROM external_target_delivery
                   WHERE deployment_instance_id = ? LIMIT 1""",
                (instance,),
            ).fetchone()
            if registered_source is not None and (
                registered_source["tenant_id"] != delivery.tenant_id
                or registered_source["source_id"] != delivery.source_id
            ):
                raise ExternalTargetInboxError("deployment is bound to another signal source")
            if delivery.target in {"LONG", "SHORT"}:
                uncertain = connection.execute(
                    """SELECT 1 FROM external_target_delivery
                       WHERE deployment_instance_id = ?
                         AND status IN ('dispatching', 'uncertain')
                         AND resolved_at_ns IS NULL LIMIT 1""",
                    (instance,),
                ).fetchone()
                if uncertain is not None:
                    raise ExternalTargetInboxError(
                        "unresolved or uncertain target delivery blocks entry"
                    )
                fence = connection.execute(
                    """SELECT bar_close_ns, confirmed_at_ns FROM external_target_flat_fence
                       WHERE tenant_id = ? AND source_id = ?
                         AND deployment_instance_id = ? AND pair = ?""",
                    stream,
                ).fetchone()
                if fence is not None:
                    if bar_ns <= fence["bar_close_ns"]:
                        raise ExternalTargetInboxError("entry precedes the latest flat")
                    if fence["confirmed_at_ns"] is None:
                        raise ExternalTargetInboxError("entry awaits flat confirmation")
            if delivery.target == "FLAT" and delivery.risk_action is None:
                prior_flat = connection.execute(
                    """SELECT confirmed_at_ns FROM external_target_flat_fence
                       WHERE tenant_id = ? AND source_id = ?
                         AND deployment_instance_id = ? AND pair = ?""",
                    stream,
                ).fetchone()
                if prior_flat is not None and prior_flat["confirmed_at_ns"] is None:
                    raise ExternalTargetInboxError(
                        "normal flat awaits prior risk flatten confirmation"
                    )
            head = connection.execute(
                """SELECT MAX(bar_close_ns) AS bar_close_ns FROM external_target_delivery
                   WHERE tenant_id = ? AND source_id = ?
                     AND deployment_instance_id = ? AND pair = ?""",
                stream,
            ).fetchone()
            if head["bar_close_ns"] is not None and bar_ns < head["bar_close_ns"]:
                raise ExternalTargetInboxError("target delivery arrived out of order")
            connection.execute(
                """UPDATE external_target_delivery SET status = 'expired'
                   WHERE deployment_instance_id = ? AND status = 'accepted'
                     AND expires_at_ns < ?""",
                (instance, now_ns),
            )
            pending = connection.execute(
                """SELECT COUNT(*) FROM external_target_delivery
                   WHERE deployment_instance_id = ? AND status = 'accepted'""",
                (instance,),
            ).fetchone()[0]
            if pending >= self.max_pending and delivery.target != "FLAT":
                raise ExternalTargetInboxError("target inbox is full")
            if delivery.target == "FLAT":
                connection.execute(
                    """UPDATE external_target_delivery SET status = 'superseded'
                       WHERE tenant_id = ? AND source_id = ?
                         AND deployment_instance_id = ? AND pair = ?
                         AND status = 'accepted'
                         AND bar_close_ns <= ?""",
                    (*stream, bar_ns),
                )
                connection.execute(
                    """INSERT INTO external_target_flat_fence
                       (tenant_id, source_id, deployment_instance_id, pair,
                        bar_close_ns, flat_delivery_id, confirmed_at_ns)
                       VALUES (?, ?, ?, ?, ?, ?, NULL)
                       ON CONFLICT(tenant_id, source_id, deployment_instance_id, pair)
                       DO UPDATE SET bar_close_ns = excluded.bar_close_ns,
                                     flat_delivery_id = excluded.flat_delivery_id,
                                     confirmed_at_ns = NULL""",
                    (*stream, bar_ns, delivery.delivery_id),
                )
            connection.execute(
                """INSERT INTO external_target_delivery
                   (delivery_id, tenant_id, source_id, source_event_id,
                    deployment_instance_id, deployment_spec_id, deployment_spec_digest,
                    generation, pair, connector, bar_close_ns, target, risk_action,
                    emitted_at_ns, max_lag, expires_at_ns, received_at_ns,
                    payload_digest, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')""",
                (
                    delivery.delivery_id,
                    delivery.tenant_id,
                    delivery.source_id,
                    delivery.source_event_id,
                    instance,
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                    delivery.pair,
                    delivery.connector,
                    bar_ns,
                    delivery.target,
                    delivery.risk_action,
                    emitted_ns,
                    delivery.max_lag,
                    expiry_ns,
                    now_ns,
                    digest,
                ),
            )
        return TargetIntakeReceipt(delivery.delivery_id, "accepted")

    def pending(
        self, authority: EngineLifecycleAuthority, *, at: datetime
    ) -> list[StoredTargetDelivery]:
        _require_non_live(authority)
        now_ns = _epoch_ns(at)
        instance = str(authority.deployment_instance_id)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE external_target_delivery SET status = 'expired'
                   WHERE deployment_instance_id = ? AND status = 'accepted'
                     AND expires_at_ns < ?""",
                (instance, now_ns),
            )
            rows = connection.execute(
                """SELECT event.delivery_id, event.source_event_id, event.target,
                          event.risk_action, event.pair, event.connector,
                          event.emitted_at_ns, event.max_lag
                   FROM external_target_delivery AS event
                   WHERE event.deployment_instance_id = ? AND event.deployment_spec_id = ?
                     AND event.deployment_spec_digest = ? AND event.generation = ?
                     AND event.status = 'accepted'
                     AND (event.target = 'FLAT' OR (
                       NOT EXISTS (
                         SELECT 1 FROM external_target_delivery AS unknown
                         WHERE unknown.deployment_instance_id = event.deployment_instance_id
                           AND unknown.status IN ('dispatching', 'uncertain')
                           AND unknown.resolved_at_ns IS NULL
                       ) AND NOT EXISTS (
                         SELECT 1 FROM external_target_flat_fence AS fence
                         WHERE fence.tenant_id = event.tenant_id
                           AND fence.source_id = event.source_id
                           AND fence.deployment_instance_id = event.deployment_instance_id
                           AND fence.pair = event.pair
                           AND fence.confirmed_at_ns IS NULL
                       )
                     ))
                   ORDER BY event.bar_close_ns, event.target != 'FLAT',
                            event.received_at_ns, event.delivery_id""",
                (
                    instance,
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            ).fetchall()
        return [StoredTargetDelivery(**dict(row)) for row in rows]

    def begin_delivery(
        self, delivery_id: str, *, authority: EngineLifecycleAuthority, at: datetime
    ) -> bool:
        _require_non_live(authority)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT 1 FROM external_target_delivery AS event
                   WHERE event.delivery_id = ? AND event.deployment_instance_id = ?
                     AND event.deployment_spec_id = ? AND event.deployment_spec_digest = ?
                     AND event.generation = ? AND event.status = 'accepted'
                     AND event.expires_at_ns >= ?
                     AND (event.target = 'FLAT' OR (
                       NOT EXISTS (
                         SELECT 1 FROM external_target_delivery AS unknown
                         WHERE unknown.deployment_instance_id = event.deployment_instance_id
                           AND unknown.status IN ('dispatching', 'uncertain')
                           AND unknown.resolved_at_ns IS NULL
                       ) AND NOT EXISTS (
                         SELECT 1 FROM external_target_flat_fence AS fence
                         WHERE fence.tenant_id = event.tenant_id
                           AND fence.source_id = event.source_id
                           AND fence.deployment_instance_id = event.deployment_instance_id
                           AND fence.pair = event.pair
                           AND fence.confirmed_at_ns IS NULL
                       )
                     ))""",
                (
                    delivery_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                    _epoch_ns(at),
                ),
            ).fetchone()
            if row is None:
                return False
            result = connection.execute(
                """UPDATE external_target_delivery SET status = 'dispatching'
                   WHERE delivery_id = ? AND status = 'accepted'""",
                (delivery_id,),
            )
            return result.rowcount == 1

    def mark_handoff(
        self, delivery_id: str, *, authority: EngineLifecycleAuthority, at: datetime
    ) -> bool:
        _require_non_live(authority)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE external_target_delivery
                   SET status = CASE WHEN target = 'FLAT' THEN 'flat_requested' ELSE 'queued' END,
                       handoff_at_ns = ?
                   WHERE delivery_id = ? AND deployment_instance_id = ?
                     AND deployment_spec_id = ? AND deployment_spec_digest = ?
                     AND generation = ? AND status = 'dispatching'""",
                (
                    _epoch_ns(at),
                    delivery_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            )
            return result.rowcount == 1

    def recover_uncertain(self, deployment_instance_id: UUID, *, at: datetime) -> int:
        recovered_ns = _epoch_ns(at)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE external_target_delivery
                   SET status = 'uncertain', recovered_at_ns = ?
                   WHERE deployment_instance_id = ?
                     AND status IN ('dispatching', 'queued', 'flat_requested')""",
                (recovered_ns, str(deployment_instance_id)),
            )
            return result.rowcount

    def pending_flat_request(
        self, delivery_id: str, *, authority: EngineLifecycleAuthority
    ) -> PendingFlatRequest | None:
        """Return the exact active flat fence and its persisted host handoff time."""
        _require_non_live(authority)
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT event.connector, event.pair, event.handoff_at_ns
                   FROM external_target_delivery AS event
                   JOIN external_target_flat_fence AS fence
                     ON fence.flat_delivery_id = event.delivery_id
                    AND fence.deployment_instance_id = event.deployment_instance_id
                    AND fence.pair = event.pair
                    AND fence.source_id = event.source_id
                    AND fence.tenant_id = event.tenant_id
                   WHERE event.delivery_id = ? AND event.deployment_instance_id = ?
                     AND event.deployment_spec_id = ? AND event.deployment_spec_digest = ?
                     AND event.generation = ? AND event.target = 'FLAT'
                     AND event.status IN ('flat_requested', 'uncertain')
                     AND event.handoff_at_ns IS NOT NULL
                     AND fence.confirmed_at_ns IS NULL""",
                (
                    delivery_id,
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            ).fetchone()
        if row is None:
            return None
        return PendingFlatRequest(
            delivery_id=delivery_id,
            connector=row["connector"],
            pair=row["pair"],
            handoff_at=datetime(1970, 1, 1, tzinfo=UTC)
            + timedelta(microseconds=row["handoff_at_ns"] // 1_000),
        )

    def pending_flat_requests(
        self, authority: EngineLifecycleAuthority, *, source_id: str
    ) -> list[PendingFlatRequest]:
        """List only this subscriber's handed-off, unresolved flat fences."""
        _require_non_live(authority)
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT event.delivery_id, event.connector, event.pair, event.handoff_at_ns
                   FROM external_target_delivery AS event
                   JOIN external_target_flat_fence AS fence
                     ON fence.flat_delivery_id = event.delivery_id
                    AND fence.deployment_instance_id = event.deployment_instance_id
                    AND fence.pair = event.pair
                    AND fence.source_id = event.source_id
                    AND fence.tenant_id = event.tenant_id
                   WHERE event.deployment_instance_id = ? AND event.deployment_spec_id = ?
                     AND event.deployment_spec_digest = ? AND event.generation = ?
                     AND event.source_id = ? AND event.target = 'FLAT'
                     AND event.status IN ('flat_requested', 'uncertain')
                     AND event.handoff_at_ns IS NOT NULL
                     AND fence.confirmed_at_ns IS NULL
                   ORDER BY event.handoff_at_ns, event.delivery_id""",
                (
                    str(authority.deployment_instance_id),
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                    source_id,
                ),
            ).fetchall()
        origin = datetime(1970, 1, 1, tzinfo=UTC)
        return [
            PendingFlatRequest(
                delivery_id=row["delivery_id"],
                connector=row["connector"],
                pair=row["pair"],
                handoff_at=origin + timedelta(microseconds=row["handoff_at_ns"] // 1_000),
            )
            for row in rows
        ]

    def confirm_flat(
        self, delivery_id: str, *, authority: EngineLifecycleAuthority, at: datetime
    ) -> bool:
        """Persist a caller-proven flat account; this method does not inspect a venue."""
        _require_non_live(authority)
        confirmed_ns = _epoch_ns(at)
        instance = str(authority.deployment_instance_id)
        with self._transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE external_target_flat_fence SET confirmed_at_ns = ?
                   WHERE flat_delivery_id = ? AND confirmed_at_ns IS NULL
                     AND deployment_instance_id = ? AND EXISTS (
                       SELECT 1 FROM external_target_delivery
                       WHERE delivery_id = ? AND deployment_instance_id = ?
                         AND deployment_spec_id = ? AND deployment_spec_digest = ?
                         AND generation = ? AND target = 'FLAT'
                         AND status IN ('flat_requested', 'uncertain')
                   )""",
                (
                    confirmed_ns,
                    delivery_id,
                    instance,
                    delivery_id,
                    instance,
                    str(authority.deployment_spec_id),
                    authority.deployment_spec_digest,
                    authority.generation,
                ),
            )
            if result.rowcount != 1:
                return False
            connection.execute(
                """UPDATE external_target_delivery SET status = 'flat_confirmed'
                   WHERE delivery_id = ?""",
                (delivery_id,),
            )
            connection.execute(
                """UPDATE external_target_delivery SET resolved_at_ns = ?
                   WHERE deployment_instance_id = ? AND status = 'uncertain'
                     AND resolved_at_ns IS NULL""",
                (confirmed_ns, instance),
            )
            return True
