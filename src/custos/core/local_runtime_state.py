"""Durable host-local account fences; this module never grants trading readiness."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from custos.core.runtime_authority import (
    RuntimeAccountIdentity,
    RuntimeAccountOccupied,
    RuntimeAuthorityError,
    RuntimeFence,
    RuntimeFenceStale,
    RuntimeStorageUnavailable,
)

_LOCK_DESCRIPTORS: set[int] = set()
_LOCK_DESCRIPTOR_GUARD = threading.RLock()


def _close_inherited_locks() -> None:
    # Closing a fork copy is safe; LOCK_UN would also unlock the parent's copy.
    for descriptor in tuple(_LOCK_DESCRIPTORS):
        try:
            os.close(descriptor)
        except OSError as error:
            # Another registered child hook may already have closed this descriptor.
            if error.errno != errno.EBADF:
                # Do not leave a child alive with an unknown inherited account lock.
                os._exit(70)
    _LOCK_DESCRIPTORS.clear()
    _LOCK_DESCRIPTOR_GUARD.release()


os.register_at_fork(
    before=_LOCK_DESCRIPTOR_GUARD.acquire,
    after_in_parent=_LOCK_DESCRIPTOR_GUARD.release,
    after_in_child=_close_inherited_locks,
)


def _release_lock(descriptor: int, *, unlock: bool = False) -> None:
    with _LOCK_DESCRIPTOR_GUARD:
        try:
            if unlock:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            try:
                os.close(descriptor)
            finally:
                _LOCK_DESCRIPTORS.discard(descriptor)


@dataclass(frozen=True)
class RuntimeStoreIdentity:
    store_id: UUID
    sequence: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.store_id, UUID)
            or type(self.sequence) is not int
            or self.sequence < 0
        ):
            raise RuntimeStorageUnavailable("runtime store identity or sequence is invalid")


@dataclass(frozen=True)
class BoundRuntimeStore:
    path: Path
    store_id: UUID


@dataclass(frozen=True)
class RuntimeStorageBindings:
    state: BoundRuntimeStore
    journal: BoundRuntimeStore


@dataclass(frozen=True)
class RuntimeAuthoritySnapshot:
    run_id: UUID
    control_source: str
    authorization_epoch: int
    process_generation: int
    owner_id: UUID | None
    phase: str


class RuntimeAuthorityRegistry:
    """Persist internal ownership, independently of permit and venue validation."""

    def __init__(self, root: Path, identity_probe: Callable[[Path], RuntimeStoreIdentity]) -> None:
        self.root = root.resolve()
        self.path = self.root / "registry.sqlite3"
        self._marker = self.root / "registry.identity"
        self._probe = identity_probe
        try:
            self.registry_id = UUID(self._marker.read_text().strip())
        except (OSError, ValueError) as error:
            raise RuntimeStorageUnavailable("authority registry marker is invalid") from error

    @classmethod
    def initialize(
        cls, root: Path, *, identity_probe: Callable[[Path], RuntimeStoreIdentity]
    ) -> RuntimeAuthorityRegistry:
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls._require_private_root(root)
        if any(root.iterdir()):
            raise RuntimeStorageUnavailable("control directory already contains state")
        registry_id = uuid4()
        marker = root / "registry.identity"
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(registry_id) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(root / "registry.sqlite3", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(root / "registry.sqlite3")
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE registry_meta (
                    schema_version INTEGER NOT NULL CHECK(schema_version=1),
                    registry_id TEXT NOT NULL
                );
                CREATE TABLE account_authority (
                    scope TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    control_source TEXT NOT NULL CHECK(control_source IN ('local','platform')),
                    permit_digest TEXT NOT NULL,
                    authorization_epoch INTEGER NOT NULL,
                    process_generation INTEGER NOT NULL,
                    owner_id TEXT,
                    phase TEXT NOT NULL CHECK(phase IN ('recovery_required','retired')),
                    stores TEXT NOT NULL,
                    lock_device TEXT NOT NULL,
                    lock_inode TEXT NOT NULL
                );
                """
            )
            connection.execute("INSERT INTO registry_meta VALUES (1,?)", (str(registry_id),))
            connection.commit()
        finally:
            connection.close()
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return cls.open(root, identity_probe=identity_probe)

    @staticmethod
    def _require_private_root(root: Path) -> None:
        metadata = root.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeStorageUnavailable("control directory must be private to its owner")

    @classmethod
    def open(
        cls, root: Path, *, identity_probe: Callable[[Path], RuntimeStoreIdentity]
    ) -> RuntimeAuthorityRegistry:
        root = Path(root)
        if (
            not (root / "registry.identity").is_file()
            or not (root / "registry.sqlite3").is_file()
            or (root / "registry.identity").is_symlink()
            or (root / "registry.sqlite3").is_symlink()
        ):
            raise RuntimeStorageUnavailable("registered authority storage is missing")
        cls._require_private_root(root)
        registry = cls(root, identity_probe)
        with registry._transaction():
            pass
        return registry

    @contextmanager
    def _transaction(self):
        self._require_private_root(self.root)
        if (
            not self.path.is_file()
            or not self._marker.is_file()
            or self.path.is_symlink()
            or self._marker.is_symlink()
        ):
            raise RuntimeStorageUnavailable("registered authority storage is missing")
        with self.path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise RuntimeStorageUnavailable("registered authority database is not initialized")
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw", uri=True, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM registry_meta").fetchall()
            if (
                len(row) != 1
                or row[0]["schema_version"] != 1
                or row[0]["registry_id"] != str(self.registry_id)
                or self._marker.read_text().strip() != str(self.registry_id)
            ):
                raise RuntimeStorageUnavailable("authority registry identity differs")
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise RuntimeStorageUnavailable("authority database cannot be verified") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _stores(self, bindings: RuntimeStorageBindings) -> str:
        if (
            bindings.state.store_id == bindings.journal.store_id
            or bindings.state.path.resolve() == bindings.journal.path.resolve()
        ):
            raise RuntimeStorageUnavailable("state and journal must be distinct registered stores")
        result = {}
        for role, store in (("state", bindings.state), ("journal", bindings.journal)):
            path = Path(store.path)
            if not path.is_file() or path.is_symlink():
                raise RuntimeStorageUnavailable("registered runtime storage is missing or unsafe")
            try:
                observed = self._probe(path.resolve())
            except (OSError, ValueError) as error:
                raise RuntimeStorageUnavailable("runtime store identity cannot be read") from error
            if (
                not isinstance(observed, RuntimeStoreIdentity)
                or observed.store_id != store.store_id
            ):
                raise RuntimeStorageUnavailable("runtime store identity differs")
            result[role] = {
                "path": str(path.resolve()),
                "store_id": str(store.store_id),
                "sequence": observed.sequence,
            }
        return json.dumps(result, sort_keys=True)

    @staticmethod
    def _require_continuity(observed: str, registered: str) -> None:
        current, previous = json.loads(observed), json.loads(registered)
        for role in ("state", "journal"):
            if (
                current[role]["path"] != previous[role]["path"]
                or current[role]["store_id"] != previous[role]["store_id"]
                or current[role]["sequence"] < previous[role]["sequence"]
            ):
                raise RuntimeStorageUnavailable("registered runtime storage continuity differs")

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> RuntimeAuthoritySnapshot:
        return RuntimeAuthoritySnapshot(
            run_id=UUID(row["run_id"]),
            control_source=row["control_source"],
            authorization_epoch=row["authorization_epoch"],
            process_generation=row["process_generation"],
            owner_id=UUID(row["owner_id"]) if row["owner_id"] else None,
            phase=row["phase"],
        )

    def read(self, account: RuntimeAccountIdentity) -> RuntimeAuthoritySnapshot | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM account_authority WHERE scope=?", (account.scope_digest,)
            ).fetchone()
            return self._snapshot(row) if row else None

    def claim(
        self,
        account: RuntimeAccountIdentity,
        *,
        run_id: UUID,
        permit_digest: str,
        expected_authorization_epoch: int,
        stores: RuntimeStorageBindings,
        control_source: Literal["local", "platform"] = "local",
    ) -> RuntimeLease:
        with self._transaction() as connection:
            registered = connection.execute(
                "SELECT lock_device,lock_inode FROM account_authority WHERE scope=?",
                (account.scope_digest,),
            ).fetchone()
        lock_path = self.root / f"{account.scope_digest}.lock"
        flags = os.O_RDWR | os.O_NOFOLLOW
        if registered is None:
            flags |= os.O_CREAT
        try:
            with _LOCK_DESCRIPTOR_GUARD:
                descriptor = os.open(lock_path, flags, 0o600)
                _LOCK_DESCRIPTORS.add(descriptor)
        except OSError as error:
            raise RuntimeStorageUnavailable("registered account lock is unavailable") from error
        try:
            actual = self._lock_identity(account.scope_digest, descriptor)
            if registered is not None and actual != (
                registered["lock_device"],
                registered["lock_inode"],
            ):
                raise RuntimeStorageUnavailable("registered account lock was replaced")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeAccountOccupied("another process holds the venue account") from error
            return self._claim_locked(
                account,
                run_id=run_id,
                permit_digest=permit_digest,
                expected_authorization_epoch=expected_authorization_epoch,
                stores=stores,
                control_source=control_source,
                lock_descriptor=descriptor,
            )
        except BaseException:
            _release_lock(descriptor)
            raise

    def _lock_identity(self, scope: str, descriptor: int) -> tuple[str, str]:
        try:
            held = os.fstat(descriptor)
            named = (self.root / f"{scope}.lock").lstat()
        except OSError as error:
            raise RuntimeStorageUnavailable("account lock identity cannot be read") from error
        identity = (str(held.st_dev), str(held.st_ino))
        if (
            not stat.S_ISREG(held.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or identity != (str(named.st_dev), str(named.st_ino))
        ):
            raise RuntimeStorageUnavailable("account lock path no longer names the held file")
        return identity

    def _claim_locked(
        self,
        account: RuntimeAccountIdentity,
        *,
        run_id: UUID,
        permit_digest: str,
        expected_authorization_epoch: int,
        stores: RuntimeStorageBindings,
        control_source: Literal["local", "platform"] = "local",
        lock_descriptor: int,
    ) -> RuntimeLease:
        if (
            not isinstance(run_id, UUID)
            or not isinstance(permit_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", permit_digest)
            or type(expected_authorization_epoch) is not int
            or expected_authorization_epoch < 0
            or control_source not in {"local", "platform"}
        ):
            raise RuntimeAuthorityError("runtime ownership request is invalid")
        metadata = self._stores(stores)
        owner_id = uuid4()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM account_authority WHERE scope=?", (account.scope_digest,)
            ).fetchone()
            lock_identity = self._lock_identity(account.scope_digest, lock_descriptor)
            if row is None:
                if expected_authorization_epoch != 0:
                    raise RuntimeFenceStale("initial authorization must expect epoch zero")
                authorization_epoch = generation = 1
            else:
                if lock_identity != (row["lock_device"], row["lock_inode"]):
                    raise RuntimeStorageUnavailable("registered account lock was replaced")
                if expected_authorization_epoch != row["authorization_epoch"]:
                    raise RuntimeFenceStale("authorization epoch differs")
                if control_source != row["control_source"]:
                    raise RuntimeAuthorityError("control-source change requires explicit handoff")
                if row["phase"] == "retired":
                    if str(run_id) == row["run_id"] or permit_digest == row["permit_digest"]:
                        raise RuntimeAuthorityError("retired runtime requires a new run and permit")
                    authorization_epoch = row["authorization_epoch"] + 1
                else:
                    if (
                        str(run_id) != row["run_id"]
                        or permit_digest != row["permit_digest"]
                        or control_source != row["control_source"]
                    ):
                        raise RuntimeAuthorityError("runtime recovery authorization differs")
                    authorization_epoch = row["authorization_epoch"]
                generation = row["process_generation"] + 1
                self._require_continuity(metadata, row["stores"])
            fence = RuntimeFence(
                account.scope_digest,
                run_id,
                control_source,
                authorization_epoch,
                generation,
                owner_id,
            )
            connection.execute(
                """
                INSERT INTO account_authority VALUES (?,?,?,?,?,?,?,'recovery_required',?,?,?)
                ON CONFLICT(scope) DO UPDATE SET
                    run_id=excluded.run_id,control_source=excluded.control_source,
                    permit_digest=excluded.permit_digest,
                    authorization_epoch=excluded.authorization_epoch,
                    process_generation=excluded.process_generation,owner_id=excluded.owner_id,
                    phase=excluded.phase,stores=excluded.stores,
                    lock_device=excluded.lock_device,lock_inode=excluded.lock_inode
                """,
                (
                    account.scope_digest,
                    str(run_id),
                    control_source,
                    permit_digest,
                    authorization_epoch,
                    generation,
                    str(owner_id),
                    metadata,
                    *lock_identity,
                ),
            )
        return RuntimeLease(self, fence, stores, lock_descriptor)

    def _current(self, connection: sqlite3.Connection, fence: RuntimeFence) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM account_authority WHERE scope=?", (fence.account_scope,)
        ).fetchone()
        if row is None or self._snapshot(row) != RuntimeAuthoritySnapshot(
            fence.run_id,
            fence.control_source,
            fence.authorization_epoch,
            fence.process_generation,
            fence.owner_id,
            row["phase"],
        ):
            raise RuntimeFenceStale("runtime process fence is stale")
        return row


class RuntimeLease:
    """An internal process owner, not a permit, flat-account proof or ready receipt."""

    def __init__(
        self,
        registry: RuntimeAuthorityRegistry,
        fence: RuntimeFence,
        stores: RuntimeStorageBindings,
        lock_descriptor: int,
    ) -> None:
        self._registry = registry
        self.fence = fence
        self._stores = stores
        self._closed = False
        self._pid = os.getpid()
        self._lock_descriptor = lock_descriptor

    def __enter__(self) -> RuntimeLease:
        return self

    @property
    def state_store(self) -> BoundRuntimeStore:
        return self._stores.state

    def __exit__(self, *_exc) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed or os.getpid() != self._pid:
            raise RuntimeFenceStale("runtime lease is closed or belongs to another process")

    def assert_current(self) -> None:
        self._require_open()
        with self._registry._transaction() as connection:
            row = self._registry._current(connection, self.fence)
            identity = self._registry._lock_identity(
                self.fence.account_scope, self._lock_descriptor
            )
            if identity != (row["lock_device"], row["lock_inode"]):
                raise RuntimeStorageUnavailable("registered account lock identity differs")
            if row["phase"] == "retired":
                raise RuntimeFenceStale("runtime has been retired")
            observed = self._registry._stores(self._stores)
            self._registry._require_continuity(observed, row["stores"])
            connection.execute(
                "UPDATE account_authority SET stores=? WHERE scope=?",
                (observed, self.fence.account_scope),
            )

    def retire(self) -> None:
        self.assert_current()
        with self._registry._transaction() as connection:
            self._registry._current(connection, self.fence)
            connection.execute(
                "UPDATE account_authority SET phase='retired' WHERE scope=?",
                (self.fence.account_scope,),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._require_open()
        try:
            with self._registry._transaction() as connection:
                self._registry._current(connection, self.fence)
                connection.execute(
                    "UPDATE account_authority SET owner_id=NULL WHERE scope=?",
                    (self.fence.account_scope,),
                )
        finally:
            self._closed = True
            _release_lock(self._lock_descriptor, unlock=True)
