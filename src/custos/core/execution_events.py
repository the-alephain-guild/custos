"""Durable execution observations without platform business identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from custos.core.execution_risk_events import RiskMutationRecord, _token
from custos.core.local_runtime_state import RuntimeLease, RuntimeStoreIdentity
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable


class ExecutionRefusalRecorder(Protocol):
    def record_local_refusal(
        self, *, client_order_id: str, instrument_id: str, side: str, reason_code: str
    ) -> None: ...


@dataclass(frozen=True)
class ExecutionRunIdentity:
    run_id: UUID
    account_scope: str
    authorization_epoch: int
    configuration_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID) or not re.fullmatch(
            r"[0-9a-f]{64}", self.account_scope
        ):
            raise RuntimeAuthorityError("execution run identity is invalid")
        for value in (self.authorization_epoch, self.configuration_revision):
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise RuntimeAuthorityError("execution revisions must be positive integers")


@dataclass(frozen=True)
class OrderRefusalRecord:
    client_order_id: str
    instrument_id: str
    side: str
    reason_code: str

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _token(value, name, empty=name in {"client_order_id", "instrument_id", "side"})
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", self.reason_code):
            raise RuntimeAuthorityError("refusal reason must be a structured reason code")


@dataclass(frozen=True)
class RecordedExecutionEvent:
    sequence: int
    event_id: str
    identity: ExecutionRunIdentity
    refusal: OrderRefusalRecord


@dataclass(frozen=True)
class RecordedRiskEvent:
    sequence: int
    event_id: str
    identity: ExecutionRunIdentity
    risk: RiskMutationRecord


class ExecutionEventStore:
    def __init__(self, path: Path, store_id: UUID) -> None:
        self.path = path.resolve()
        self.store_id = store_id

    @classmethod
    def initialize(cls, path: Path) -> ExecutionEventStore:
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        store_id = uuid4()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE execution_meta (
                    schema_version INTEGER NOT NULL CHECK(schema_version=1),
                    store_id TEXT NOT NULL
                );
                CREATE TABLE execution_run (
                    run_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL
                );
                CREATE TABLE execution_event (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES execution_run(run_id),
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('order_refused','risk_mutation')),
                    payload TEXT NOT NULL,
                    source_time_ns INTEGER,
                    result TEXT NOT NULL CHECK(result IN ('refused','applied')),
                    UNIQUE(run_id,event_id)
                );
                CREATE TABLE execution_attempt (
                    sequence INTEGER NOT NULL REFERENCES execution_event(sequence),
                    process_generation INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    recorded_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(sequence,process_generation,owner_id)
                );
                CREATE TABLE local_risk_meta (
                    schema_version INTEGER NOT NULL CHECK(schema_version=2),
                    account_scope TEXT,
                    native_account_id TEXT,
                    client_id TEXT
                );
                INSERT INTO local_risk_meta VALUES (2,NULL,NULL,NULL);
                CREATE TABLE local_account_risk (
                    account_scope TEXT PRIMARY KEY,
                    reason_code TEXT,
                    source_event_id TEXT
                );
                CREATE TABLE local_order_reservation (
                    account_scope TEXT NOT NULL REFERENCES local_account_risk(account_scope),
                    client_order_id TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES execution_run(run_id),
                    policy_id TEXT NOT NULL,
                    reserved_notional TEXT NOT NULL,
                    filled_exposure TEXT NOT NULL,
                    filled_quantity TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','partially_filled','filled','released','closed')),
                    position_id TEXT,
                    instrument_id TEXT,
                    side TEXT,
                    PRIMARY KEY(account_scope,client_order_id)
                );
                CREATE TABLE local_venue_fill (
                    account_scope TEXT NOT NULL REFERENCES local_account_risk(account_scope),
                    fill_identity TEXT NOT NULL,
                    original_run_id TEXT REFERENCES execution_run(run_id),
                    action TEXT NOT NULL CHECK(action IN ('fill','reduce')),
                    request_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    audit_sequence INTEGER NOT NULL REFERENCES execution_event(sequence),
                    PRIMARY KEY(account_scope,fill_identity)
                );
                CREATE TABLE local_reduction_claim (
                    account_scope TEXT NOT NULL REFERENCES local_account_risk(account_scope),
                    closing_client_order_id TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES execution_run(run_id),
                    authorization_epoch INTEGER NOT NULL,
                    process_generation INTEGER NOT NULL,
                    permit_digest TEXT NOT NULL,
                    native_account_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity TEXT NOT NULL,
                    params_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','dispatching','unknown','not_sent','filled')),
                    PRIMARY KEY(account_scope,closing_client_order_id)
                );
                CREATE UNIQUE INDEX local_active_position_close
                    ON local_reduction_claim(account_scope,position_id)
                    WHERE state IN ('prepared','dispatching','unknown');
                """
            )
            connection.execute("INSERT INTO execution_meta VALUES (1,?)", (str(store_id),))
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls.open(path)

    @classmethod
    def open(cls, path: Path) -> ExecutionEventStore:
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise RuntimeStorageUnavailable("execution database is missing or replaced")
        try:
            with closing(
                sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)
            ) as connection:
                rows = connection.execute("SELECT * FROM execution_meta").fetchall()
                if len(rows) != 1 or rows[0][0] != 1:
                    raise RuntimeStorageUnavailable("execution metadata is invalid")
                store_id = UUID(rows[0][1])
        except (sqlite3.Error, ValueError) as error:
            raise RuntimeStorageUnavailable("execution database is not initialized") from error
        return cls(path, store_id)

    @contextmanager
    def _transaction(self):
        if not self.path.is_file() or self.path.is_symlink():
            raise RuntimeStorageUnavailable("execution database is missing or replaced")
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw", uri=True, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM execution_meta").fetchall()
            if (
                len(rows) != 1
                or rows[0]["schema_version"] != 1
                or rows[0]["store_id"] != str(self.store_id)
            ):
                raise RuntimeStorageUnavailable("execution store identity differs")
            yield connection
            connection.commit()
        except Exception as error:
            connection.rollback()
            if isinstance(error, sqlite3.Error):
                raise RuntimeStorageUnavailable("execution database transaction failed") from error
            raise
        finally:
            connection.close()

    def identity(self) -> RuntimeStoreIdentity:
        with self._transaction() as connection:
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM execution_event"
            ).fetchone()[0]
            return RuntimeStoreIdentity(self.store_id, sequence)

    def append_refusal(
        self,
        *,
        lease: RuntimeLease,
        identity: ExecutionRunIdentity,
        event_id: str,
        refusal: OrderRefusalRecord,
    ) -> RecordedExecutionEvent:
        _token(event_id, "event_id")
        payload = json.dumps(asdict(refusal), sort_keys=True, separators=(",", ":"))
        with self._runtime_transaction(lease=lease, identity=identity) as connection:
            sequence = self._append_record(
                connection,
                lease=lease,
                identity=identity,
                event_id=event_id,
                kind="order_refused",
                payload=payload,
                result="refused",
            )
        return RecordedExecutionEvent(sequence, event_id, identity, refusal)

    @contextmanager
    def _runtime_transaction(
        self, *, lease: RuntimeLease, identity: ExecutionRunIdentity
    ) -> Iterator[sqlite3.Connection]:
        with lease.control_action():
            lease.assert_current()
            self._require_runtime_binding(lease, identity)
            with self._transaction() as connection:
                self._bind_run(connection, identity)
                yield connection
            # Never hold the risk connection while the registry probes this store.
            lease.assert_current()

    def _require_runtime_binding(self, lease: RuntimeLease, identity: ExecutionRunIdentity) -> None:
        fence = lease.fence
        if (
            identity.run_id != fence.run_id
            or identity.account_scope != fence.account_scope
            or identity.authorization_epoch != fence.authorization_epoch
        ):
            raise RuntimeAuthorityError("execution identity differs from current authority")
        # The account guard's continuity checkpoint must cover this exact database.
        if (
            lease.state_store.path.resolve() != self.path
            or lease.state_store.store_id != self.store_id
        ):
            raise RuntimeStorageUnavailable("execution database is outside registered storage")

    @staticmethod
    def _bind_run(connection: sqlite3.Connection, identity: ExecutionRunIdentity) -> None:
        document = json.dumps(asdict(identity), default=str, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT OR IGNORE INTO execution_run VALUES (?,?)", (str(identity.run_id), document)
        )
        if (
            connection.execute(
                "SELECT identity FROM execution_run WHERE run_id=?", (str(identity.run_id),)
            ).fetchone()[0]
            != document
        ):
            raise RuntimeAuthorityError("execution run identity conflicts with durable history")

    @staticmethod
    def _append_record(
        connection: sqlite3.Connection,
        *,
        lease: RuntimeLease,
        identity: ExecutionRunIdentity,
        event_id: str,
        kind: str,
        payload: str,
        result: str,
    ) -> int:
        _token(event_id, "event_id")
        row = connection.execute(
            "SELECT * FROM execution_event WHERE run_id=? AND event_id=?",
            (str(identity.run_id), event_id),
        ).fetchone()
        if row is not None and (
            row["payload"] != payload or row["kind"] != kind or row["result"] != result
        ):
            raise RuntimeAuthorityError("execution event identity conflicts with durable history")
        if row is None:
            cursor = connection.execute(
                "INSERT INTO execution_event(run_id,event_id,kind,payload,source_time_ns,result) VALUES (?,?,?,?,NULL,?)",
                (str(identity.run_id), event_id, kind, payload, result),
            )
            sequence = cursor.lastrowid
            if sequence is None:
                raise RuntimeStorageUnavailable("execution event has no durable sequence")
        else:
            sequence = row["sequence"]
        fence = lease.fence
        connection.execute(
            "INSERT OR IGNORE INTO execution_attempt VALUES (?,?,?,?)",
            (sequence, fence.process_generation, str(fence.owner_id), time.time_ns()),
        )
        return sequence

    def _append_risk(
        self,
        connection: sqlite3.Connection,
        *,
        lease: RuntimeLease,
        identity: ExecutionRunIdentity,
        event_id: str,
        risk: RiskMutationRecord,
    ) -> int:
        return self._append_record(
            connection,
            lease=lease,
            identity=identity,
            event_id=event_id,
            kind="risk_mutation",
            payload=risk.payload(),
            result="refused" if risk.outcome.get("error_code") else "applied",
        )

    @staticmethod
    def _find_risk(
        connection: sqlite3.Connection, *, run_id: UUID, event_id: str
    ) -> RiskMutationRecord | None:
        row = connection.execute(
            "SELECT kind,payload FROM execution_event WHERE run_id=? AND event_id=?",
            (str(run_id), event_id),
        ).fetchone()
        if row is None:
            return None
        if row["kind"] != "risk_mutation":
            raise RuntimeAuthorityError("risk event identity belongs to another event kind")
        return RiskMutationRecord(**json.loads(row["payload"]))

    def events(self, run_id: UUID) -> tuple[RecordedExecutionEvent, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT event.*,run.identity FROM execution_event AS event JOIN execution_run AS run USING(run_id) WHERE run_id=? AND kind='order_refused' ORDER BY sequence",
                (str(run_id),),
            ).fetchall()
            events = []
            for row in rows:
                fields = json.loads(row["identity"])
                fields["run_id"] = UUID(fields["run_id"])
                events.append(
                    RecordedExecutionEvent(
                        row["sequence"],
                        row["event_id"],
                        ExecutionRunIdentity(**fields),
                        OrderRefusalRecord(**json.loads(row["payload"])),
                    )
                )
            return tuple(events)

    def risk_events(self, run_id: UUID) -> tuple[RecordedRiskEvent, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT event.*,run.identity FROM execution_event AS event JOIN execution_run AS run USING(run_id) WHERE run_id=? AND kind='risk_mutation' ORDER BY sequence",
                (str(run_id),),
            ).fetchall()
            records = []
            for row in rows:
                fields = json.loads(row["identity"])
                fields["run_id"] = UUID(fields["run_id"])
                records.append(
                    RecordedRiskEvent(
                        row["sequence"],
                        row["event_id"],
                        ExecutionRunIdentity(**fields),
                        RiskMutationRecord(**json.loads(row["payload"])),
                    )
                )
            return tuple(records)


class LocalExecutionRecorder:
    def __init__(
        self, store: ExecutionEventStore, lease: RuntimeLease, *, configuration_revision: int
    ) -> None:
        self.store = store
        self.lease = lease
        self.identity = ExecutionRunIdentity(
            lease.fence.run_id,
            lease.fence.account_scope,
            lease.fence.authorization_epoch,
            configuration_revision,
        )

    def record_local_refusal(
        self, *, client_order_id: str, instrument_id: str, side: str, reason_code: str
    ) -> None:
        refusal = OrderRefusalRecord(client_order_id, instrument_id, side, reason_code)
        encoded = json.dumps(asdict(refusal), sort_keys=True, separators=(",", ":")).encode()
        self.store.append_refusal(
            lease=self.lease,
            identity=self.identity,
            event_id="order-refusal:" + hashlib.sha256(encoded).hexdigest(),
            refusal=refusal,
        )
