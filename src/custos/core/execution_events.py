"""Durable execution observations without platform business identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from custos.core.local_runtime_state import RuntimeLease, RuntimeStoreIdentity
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable


class ExecutionRefusalRecorder(Protocol):
    def record_local_refusal(
        self, *, client_order_id: str, instrument_id: str, side: str, reason_code: str
    ) -> None: ...


def _token(value: str, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 512:
        raise RuntimeAuthorityError(f"{name} must be a bounded string")
    if (not value and not empty) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RuntimeAuthorityError(f"{name} must not contain control characters")


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
                    kind TEXT NOT NULL CHECK(kind='order_refused'),
                    payload TEXT NOT NULL,
                    source_time_ns INTEGER,
                    result TEXT NOT NULL CHECK(result='refused'),
                    UNIQUE(run_id,event_id)
                );
                CREATE TABLE execution_attempt (
                    sequence INTEGER NOT NULL REFERENCES execution_event(sequence),
                    process_generation INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    recorded_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(sequence,process_generation,owner_id)
                );
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
        lease.assert_current()
        _token(event_id, "event_id")
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
        run_document = json.dumps(
            asdict(identity), default=str, sort_keys=True, separators=(",", ":")
        )
        payload = json.dumps(asdict(refusal), sort_keys=True, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO execution_run VALUES (?,?)",
                (str(identity.run_id), run_document),
            )
            if (
                connection.execute(
                    "SELECT identity FROM execution_run WHERE run_id=?", (str(identity.run_id),)
                ).fetchone()[0]
                != run_document
            ):
                raise RuntimeAuthorityError("execution run identity conflicts with durable history")
            row = connection.execute(
                "SELECT * FROM execution_event WHERE run_id=? AND event_id=?",
                (str(identity.run_id), event_id),
            ).fetchone()
            if row is not None and row["payload"] != payload:
                raise RuntimeAuthorityError(
                    "execution event identity conflicts with durable history"
                )
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO execution_event(run_id,event_id,kind,payload,source_time_ns,result) VALUES (?,?,'order_refused',?,NULL,'refused')",
                    (str(identity.run_id), event_id, payload),
                )
                sequence = cursor.lastrowid
            else:
                sequence = row["sequence"]
            connection.execute(
                "INSERT OR IGNORE INTO execution_attempt VALUES (?,?,?,?)",
                (sequence, fence.process_generation, str(fence.owner_id), time.time_ns()),
            )
        lease.assert_current()
        return RecordedExecutionEvent(sequence, event_id, identity, refusal)

    def events(self, run_id: UUID) -> tuple[RecordedExecutionEvent, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT event.*,run.identity FROM execution_event AS event JOIN execution_run AS run USING(run_id) WHERE run_id=? ORDER BY sequence",
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
