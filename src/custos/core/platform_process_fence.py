"""Persistent, closed platform account ownership on a registered local filesystem."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sqlite3
import stat
import sys
import threading
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

try:
    import fcntl
except ImportError:
    fcntl = None

from custos.core.platform_process_identity import (
    PlatformAccountAliases,
    PlatformAuthorityBinding,
    PlatformFenceError,
    canonical_platform_bytes,
)

_DESCRIPTORS: set[int] = set()
_DESCRIPTOR_GUARD = threading.RLock()
_MAX_EPOCH = 2**63 - 1
_REGISTRY_DDL = """
CREATE TABLE platform_registry_meta(registry_id TEXT NOT NULL,host_scope_id TEXT NOT NULL,schema_version INTEGER NOT NULL);
CREATE TABLE platform_registry_files(name TEXT PRIMARY KEY,device INTEGER NOT NULL,inode INTEGER NOT NULL);
CREATE TABLE platform_store_bindings(store_id TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE,
 device INTEGER NOT NULL,inode INTEGER NOT NULL,mutation_seq INTEGER NOT NULL);
CREATE TABLE platform_process_claims(owner_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,
 process_epoch INTEGER NOT NULL,aliases BLOB NOT NULL,context BLOB NOT NULL,
 adapter_binding BLOB NOT NULL,phase TEXT NOT NULL CHECK(phase IN ('claim_pending','claimed_closed','recovery_required','retired')),
 reason TEXT);
CREATE TABLE platform_alias_heads(digest TEXT PRIMARY KEY,alias BLOB NOT NULL,owner_id TEXT NOT NULL,
 process_epoch INTEGER NOT NULL,device INTEGER NOT NULL,inode INTEGER NOT NULL);
"""


def _registry_guards() -> dict[str, str]:
    guards = {}
    for table in (
        "platform_registry_meta",
        "platform_registry_files",
        "platform_store_bindings",
        "platform_process_claims",
        "platform_alias_heads",
    ):
        name = table + "_no_delete"
        guards[name] = (
            f"CREATE TRIGGER {name} BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'platform history is retained'); END"
        )
        name = table + "_no_replace"
        key = {
            "platform_store_bindings": "store_id",
            "platform_process_claims": "owner_id",
            "platform_alias_heads": "digest",
            "platform_registry_files": "name",
        }.get(table)
        condition = "1" if key is None else f"{key}=NEW.{key}"
        guards[name] = (
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition}) BEGIN SELECT RAISE(ABORT,'platform history is retained'); END"
        )
    updates = {
        "platform_registry_meta": "1",
        "platform_registry_files": "1",
        "platform_store_bindings": "NEW.store_id<>OLD.store_id OR NEW.path<>OLD.path OR NEW.device<>OLD.device OR NEW.inode<>OLD.inode OR typeof(NEW.mutation_seq)<>'integer' OR NEW.mutation_seq<OLD.mutation_seq",
        "platform_alias_heads": "NEW.digest<>OLD.digest OR NEW.alias<>OLD.alias OR NEW.device<>OLD.device OR NEW.inode<>OLD.inode OR typeof(NEW.process_epoch)<>'integer' OR NEW.process_epoch<=OLD.process_epoch",
        "platform_process_claims": "NEW.owner_id<>OLD.owner_id OR NEW.store_id<>OLD.store_id OR NEW.process_epoch<>OLD.process_epoch OR NEW.aliases<>OLD.aliases OR NEW.context<>OLD.context OR NEW.adapter_binding<>OLD.adapter_binding OR NOT ((OLD.phase='claim_pending' AND NEW.phase='claimed_closed' AND NEW.reason IS NULL) OR (OLD.phase IN ('claim_pending','claimed_closed') AND NEW.phase='recovery_required' AND NEW.reason='abandoned_claim') OR (OLD.phase='claimed_closed' AND NEW.phase='retired' AND NEW.reason IS NULL))",
    }
    for table, condition in updates.items():
        name = table + "_guard_update"
        guards[name] = (
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'platform history is immutable'); END"
        )
    return guards


def _validate_registry_schema(connection):
    expected = {}
    for statement in _REGISTRY_DDL.split(";"):
        if statement.strip():
            expected[statement.split()[2].split("(")[0]] = " ".join(statement.split())
    expected.update(
        {name: " ".join(statement.split()) for name, statement in _registry_guards().items()}
    )
    actual = {
        row[0]: " ".join(row[1].split())
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger') AND name NOT LIKE 'sqlite_%'"
        )
    }
    if actual != expected:
        raise PlatformFenceError("platform_registry_schema_differs")


def _fork_child() -> None:
    for descriptor in _DESCRIPTORS:
        os.close(descriptor)
    _DESCRIPTORS.clear()
    _DESCRIPTOR_GUARD.release()


_HAS_REQUIRED_OS_PRIMITIVES = fcntl is not None and all(
    hasattr(os, name) for name in ("register_at_fork", "O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")
)
if _HAS_REQUIRED_OS_PRIMITIVES:
    os.register_at_fork(
        before=_DESCRIPTOR_GUARD.acquire,
        after_in_parent=_DESCRIPTOR_GUARD.release,
        after_in_child=_fork_child,
    )


def _open(path, flags, *, dir_fd=None, mode=0o600):
    with _DESCRIPTOR_GUARD:
        descriptor = os.open(
            path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, mode, dir_fd=dir_fd
        )
        _DESCRIPTORS.add(descriptor)
        return descriptor


def _close(descriptor: int) -> None:
    with _DESCRIPTOR_GUARD:
        os.close(descriptor)
        _DESCRIPTORS.discard(descriptor)


def _identity(value) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _require_private(path: Path, *, directory: bool) -> None:
    _require_parent_chain(path)
    try:
        value = path.lstat()
    except OSError as error:
        raise PlatformFenceError("platform_registry_storage_unavailable") from error
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077:
        raise PlatformFenceError("platform_registry_storage_unsafe")


def _require_parent_chain(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise PlatformFenceError("platform_registry_storage_unsafe")
    for parent in path.parents:
        value = parent.lstat()
        trusted_owner = value.st_uid in {0, os.geteuid()}
        sticky_root = value.st_uid == 0 and value.st_mode & stat.S_ISVTX
        if (
            not stat.S_ISDIR(value.st_mode)
            or not trusted_owner
            or (value.st_mode & 0o022 and not sticky_root)
        ):
            raise PlatformFenceError("platform_registry_storage_unsafe")


def _require_local_filesystem(path: Path) -> None:
    if not _HAS_REQUIRED_OS_PRIMITIVES:
        raise PlatformFenceError("unsupported_storage")
    if sys.platform == "darwin":
        # Darwin's statfs64 prefix is fixed by sys/mount.h; the tail contains mount names.
        class StatFS(ctypes.Structure):
            _fields_ = [
                ("bsize", ctypes.c_uint32),
                ("iosize", ctypes.c_int32),
                ("counts", ctypes.c_uint64 * 5),
                ("fsid", ctypes.c_int32 * 2),
                ("owner", ctypes.c_uint32),
                ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("subtype", ctypes.c_uint32),
                ("name", ctypes.c_char * 16),
                ("tail", ctypes.c_byte * 2080),
            ]

        function = ctypes.CDLL(None, use_errno=True).statfs64
        function.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFS)]
        function.restype = ctypes.c_int
        result = StatFS()
        if function(os.fsencode(path), ctypes.byref(result)) != 0:
            raise PlatformFenceError("unsupported_storage")
        supported = result.flags & 0x1000 and result.name in {b"apfs", b"hfs"}
    elif sys.platform == "linux":
        matches = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            mount = left.split()[4]
            for encoded, plain in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                mount = mount.replace(encoded, plain)
            if path.is_relative_to(mount):
                matches.append((len(mount), right.split()[0]))
        supported = bool(matches) and max(matches)[1] in {
            "ext2",
            "ext3",
            "ext4",
            "xfs",
            "btrfs",
            "tmpfs",
            "overlay",
            "zfs",
        }
    else:
        supported = False
    if not supported:
        raise PlatformFenceError("unsupported_storage")


class PlatformProcessRegistry:
    """A machine-bound registry; holding a lease never authorizes an order."""

    @property
    def binding(self) -> PlatformAuthorityBinding:
        return self._binding

    @classmethod
    def initialize(cls, root: Path, *, host_scope_id: UUID):
        root = root.absolute()
        _require_parent_chain(root)
        if root.exists():
            _require_private(root, directory=True)
            if any(root.iterdir()):
                raise PlatformFenceError("platform_registry_already_exists")
        else:
            root.mkdir(mode=0o700)
        _require_local_filesystem(root)
        registry_id = uuid4()
        binding = PlatformAuthorityBinding(root, registry_id, host_scope_id)
        (root / "locks").mkdir(mode=0o700)
        descriptor = _open(root / "registry.sqlite3", os.O_CREAT | os.O_EXCL | os.O_RDWR)
        database_identity = _identity(os.fstat(descriptor))
        os.fsync(descriptor)
        _close(descriptor)
        connection = sqlite3.connect(root / "registry.sqlite3")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_REGISTRY_DDL)
            for statement in _registry_guards().values():
                connection.execute(statement)
            connection.execute(
                "INSERT INTO platform_registry_meta VALUES(?,?,1)",
                (str(registry_id), str(host_scope_id)),
            )
            connection.commit()
        finally:
            connection.close()
        marker = canonical_platform_bytes(
            {
                "registry_id": str(registry_id),
                "host_scope_id": str(host_scope_id),
                "schema_version": 1,
                "root": _identity(root.stat()),
                "locks": _identity((root / "locks").stat()),
                "database": database_identity,
            }
        )
        descriptor = _open(root / "registry.identity", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(marker)
                stream.flush()
                os.fsync(stream.fileno())
            marker_identity = _identity(os.fstat(descriptor))
        finally:
            _close(descriptor)
        with sqlite3.connect(root / "registry.sqlite3") as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "INSERT INTO platform_registry_files VALUES('registry.identity',?,?)",
                marker_identity,
            )
        descriptor = _open(root, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(descriptor)
        _close(descriptor)
        return cls.open_registered(binding)

    @classmethod
    def open_machine_authority(cls) -> PlatformProcessRegistry | None:
        """Open the registry installed by the host administrator."""
        if os.name != "posix":
            return None
        etc = Path("/private/etc" if sys.platform == "darwin" else "/etc")
        return cls._open_machine_authority(etc / "custos" / "platform-authority.toml", 0)

    @classmethod
    def _open_machine_authority(
        cls, path: Path, administrator_uid: int
    ) -> PlatformProcessRegistry | None:
        try:
            named = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise PlatformFenceError("platform_registry_storage_unavailable") from error

        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_uid != administrator_uid
            or named.st_mode & 0o022
        ):
            raise PlatformFenceError("platform_registry_storage_unsafe")
        try:
            _require_parent_chain(path)
            descriptor = _open(path, os.O_RDONLY)
        except OSError as error:
            raise PlatformFenceError("platform_registry_storage_unavailable") from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != administrator_uid
                or opened.st_mode & 0o022
                or _identity(path.lstat()) != _identity(opened)
            ):
                raise PlatformFenceError("platform_registry_storage_unsafe")
            content = os.pread(descriptor, 4097, 0)
        except OSError as error:
            raise PlatformFenceError("platform_registry_storage_unavailable") from error
        finally:
            _close(descriptor)
        try:
            if len(content) > 4096:
                raise ValueError("platform authority configuration exceeds its size bound")
            document = tomllib.loads(content.decode())
            if document.keys() != {
                "schema_version",
                "authority_root",
                "registry_id",
                "host_scope_id",
            }:
                raise ValueError("platform authority configuration fields differ")
            root = Path(document["authority_root"])
            registry_id = UUID(document["registry_id"])
            host_scope_id = UUID(document["host_scope_id"])
            if (
                str(root) != document["authority_root"]
                or root.anchor != "/"
                or str(registry_id) != document["registry_id"]
                or str(host_scope_id) != document["host_scope_id"]
            ):
                raise ValueError("platform authority configuration is not canonical")
            binding = PlatformAuthorityBinding(
                root, registry_id, host_scope_id, document["schema_version"]
            )
        except (UnicodeError, ValueError, TypeError, AttributeError) as error:
            raise PlatformFenceError("platform_registry_binding_invalid") from error
        return cls.open_registered(binding)

    @classmethod
    def open_registered(cls, binding: PlatformAuthorityBinding):
        if not isinstance(binding, PlatformAuthorityBinding):
            raise PlatformFenceError("platform_registry_binding_invalid")
        registry = cls.__new__(cls)
        registry._binding = binding
        registry._pid = os.getpid()
        registry._mutex = threading.RLock()
        registry._leases = {}
        registry._fds = {}
        try:
            _require_local_filesystem(binding.authority_root)
            for name, directory in (
                (".", True),
                ("locks", True),
                ("registry.identity", False),
                ("registry.sqlite3", False),
            ):
                path = binding.authority_root / name
                _require_private(path, directory=directory)
                registry._fds[name] = _open(
                    path, os.O_RDONLY | (os.O_DIRECTORY if directory else 0)
                )
            descriptor = registry._fds["registry.identity"]
            marker_bytes = os.pread(descriptor, 4097, 0)
            marker = json.loads(marker_bytes)
            if (
                len(marker_bytes) > 4096
                or canonical_platform_bytes(marker) != marker_bytes
                or marker.get("registry_id") != str(binding.registry_id)
                or marker.get("host_scope_id") != str(binding.host_scope_id)
                or marker.get("schema_version") != 1
            ):
                raise PlatformFenceError("platform_registry_binding_invalid")
            registry._marker_bytes = marker_bytes
            registry._marker = marker
            registry._require_current()
            with registry._transaction(write=False) as connection:
                rows = connection.execute("SELECT * FROM platform_registry_meta").fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (
                    str(binding.registry_id),
                    str(binding.host_scope_id),
                    1,
                ):
                    raise PlatformFenceError("platform_registry_binding_invalid")
                marker_identity = connection.execute(
                    "SELECT device,inode FROM platform_registry_files WHERE name='registry.identity'"
                ).fetchone()
                if marker_identity is None or tuple(marker_identity) != _identity(
                    os.fstat(descriptor)
                ):
                    raise PlatformFenceError("platform_registry_replaced")
            return registry
        except BaseException as error:
            for descriptor in registry._fds.values():
                _close(descriptor)
            if isinstance(error, (OSError, ValueError, TypeError, KeyError)):
                raise PlatformFenceError("platform_registry_storage_unavailable") from error
            raise

    def _require_current(self) -> None:
        if os.getpid() != self._pid or not self._fds:
            raise PlatformFenceError("platform_registry_process_stale")
        root = self.binding.authority_root
        for name, directory in (
            (".", True),
            ("locks", True),
            ("registry.identity", False),
            ("registry.sqlite3", False),
        ):
            path = root / name
            _require_private(path, directory=directory)
            if _identity(path.lstat()) != _identity(os.fstat(self._fds[name])):
                raise PlatformFenceError("platform_registry_replaced")
        if os.pread(self._fds["registry.identity"], 4097, 0) != self._marker_bytes:
            raise PlatformFenceError("platform_registry_replaced")
        for name, key in ((".", "root"), ("locks", "locks"), ("registry.sqlite3", "database")):
            if tuple(self._marker[key]) != _identity(os.fstat(self._fds[name])):
                raise PlatformFenceError("platform_registry_replaced")

    @contextmanager
    def _transaction(self, *, write=True):
        self._require_current()
        connection = sqlite3.connect(
            (self.binding.authority_root / "registry.sqlite3").as_uri() + "?mode=rw",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            _validate_registry_schema(connection)
            yield connection
            if write:
                connection.commit()
            self._require_current()
        except sqlite3.Error as error:
            raise PlatformFenceError("platform_registry_unavailable") from error
        finally:
            connection.close()

    def register_store(self, store) -> None:
        self._require_store(store)
        snapshot = store.platform_storage_snapshot()
        with self._mutex, self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM platform_store_bindings WHERE store_id=? OR path=?",
                (str(snapshot.store_id), str(snapshot.path)),
            ).fetchone()
            if row is not None:
                self._check_storage(row, snapshot)
            else:
                connection.execute(
                    "INSERT INTO platform_store_bindings VALUES(?,?,?,?,?)",
                    (
                        str(snapshot.store_id),
                        str(snapshot.path),
                        *snapshot.file_identity,
                        snapshot.mutation_seq,
                    ),
                )

    @staticmethod
    def _require_store(store):
        from custos.core.runner_fact import RunnerStateStore

        if not isinstance(store, RunnerStateStore):
            raise PlatformFenceError("platform_storage_identity_invalid")

    @staticmethod
    def _check_storage(row, snapshot) -> None:
        if (
            row["store_id"] != str(snapshot.store_id)
            or row["path"] != str(snapshot.path)
            or (row["device"], row["inode"]) != snapshot.file_identity
        ):
            raise PlatformFenceError("platform_storage_binding_differs")
        if type(row["mutation_seq"]) is not int or snapshot.mutation_seq < row["mutation_seq"]:
            raise PlatformFenceError("platform_storage_continuity_regressed")

    def _check_snapshot(self, connection, snapshot) -> None:
        row = connection.execute(
            "SELECT * FROM platform_store_bindings WHERE store_id=?",
            (str(snapshot.storage.store_id),),
        ).fetchone()
        if row is None:
            raise PlatformFenceError("platform_storage_unregistered")
        self._check_storage(row, snapshot.storage)
        for mirror in snapshot.mirrors:
            claim = connection.execute(
                "SELECT * FROM platform_process_claims WHERE owner_id=?", (str(mirror.owner_id),)
            ).fetchone()
            if (
                mirror.registry_id != self.binding.registry_id
                or claim is None
                or claim["store_id"] != str(snapshot.storage.store_id)
                or claim["process_epoch"] != mirror.process_epoch
                or bytes(claim["aliases"]) != mirror.aliases_bytes
                or bytes(claim["context"]) != mirror.context_bytes
                or bytes(claim["adapter_binding"]) != mirror.adapter_binding
            ):
                raise PlatformFenceError("platform_registry_continuity_regressed")
            for alias in json.loads(mirror.aliases_bytes):
                from custos.core.platform_process_identity import PlatformAccountAlias

                digest = PlatformAccountAlias(**alias).digest
                head = connection.execute(
                    "SELECT process_epoch FROM platform_alias_heads WHERE digest=?", (digest,)
                ).fetchone()
                if head is None or head[0] < mirror.process_epoch:
                    raise PlatformFenceError("platform_registry_continuity_regressed")

    def _lock_aliases(self, account):
        acquired = []
        try:
            for alias in account.aliases:
                with self._transaction(write=False) as connection:
                    head = connection.execute(
                        "SELECT * FROM platform_alias_heads WHERE digest=?", (alias.digest,)
                    ).fetchone()
                name = alias.digest + ".lock"
                flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if head is None else 0)
                try:
                    descriptor = _open(name, flags, dir_fd=self._fds["locks"])
                except FileExistsError:
                    descriptor = _open(name, os.O_RDWR, dir_fd=self._fds["locks"])
                acquired.append((alias, descriptor))
                value = os.fstat(descriptor)
                _require_private(self.binding.authority_root / "locks" / name, directory=False)
                if head is not None and _identity(value) != (head["device"], head["inode"]):
                    raise PlatformFenceError("platform_alias_lock_replaced")
                if _identity(value) != _identity(
                    os.stat(name, dir_fd=self._fds["locks"], follow_symlinks=False)
                ):
                    raise PlatformFenceError("platform_alias_lock_replaced")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise PlatformFenceError("platform_account_occupied") from error
                os.fsync(descriptor)
            os.fsync(self._fds["locks"])
            return acquired
        except BaseException as error:
            for _, descriptor in reversed(acquired):
                _close(descriptor)
            if isinstance(error, OSError):
                raise PlatformFenceError("platform_alias_lock_unavailable") from error
            raise

    def claim_closed(self, store, account: PlatformAccountAliases):
        self._require_current()
        self._require_store(store)
        if type(account) is not PlatformAccountAliases:
            raise PlatformFenceError("platform_account_identity_invalid")
        with self._mutex:
            locks = self._lock_aliases(account)
            try:
                snapshot = store.platform_process_snapshot(account.context.authority)
                if snapshot.context.binding_bytes != account.context.binding_bytes:
                    raise PlatformFenceError("platform_desired_stale")
                owner = uuid4()
                with self._transaction() as connection:
                    self._check_snapshot(connection, snapshot)
                    epochs = [
                        row[0]
                        for alias in account.aliases
                        for row in connection.execute(
                            "SELECT process_epoch FROM platform_alias_heads WHERE digest=?",
                            (alias.digest,),
                        )
                    ]
                    epoch = max(epochs, default=0) + 1
                    if epoch > _MAX_EPOCH:
                        raise PlatformFenceError("platform_epoch_exhausted")
                    # All intersecting OS locks are held; a previous unretired owner
                    # is abandoned, never silently promoted into a recovered engine.
                    for alias in account.aliases:
                        connection.execute(
                            "UPDATE platform_process_claims SET phase='recovery_required',reason='abandoned_claim' "
                            "WHERE owner_id IN (SELECT owner_id FROM platform_alias_heads WHERE digest=?) "
                            "AND phase IN ('claim_pending','claimed_closed')",
                            (alias.digest,),
                        )
                    connection.execute(
                        "INSERT INTO platform_process_claims VALUES(?,?,?,?,?,?,?,NULL)",
                        (
                            str(owner),
                            str(snapshot.storage.store_id),
                            epoch,
                            account.aliases_bytes,
                            account.context.binding_bytes,
                            account.adapter_binding,
                            "claim_pending",
                        ),
                    )
                    for alias, descriptor in locks:
                        updated = connection.execute(
                            "UPDATE platform_alias_heads SET owner_id=?,process_epoch=? WHERE digest=?",
                            (str(owner), epoch, alias.digest),
                        )
                        if updated.rowcount == 0:
                            connection.execute(
                                "INSERT INTO platform_alias_heads VALUES(?,?,?,?,?,?)",
                                (
                                    alias.digest,
                                    canonical_platform_bytes(
                                        {
                                            "trading_mode": alias.trading_mode,
                                            "kind": alias.kind,
                                            "value": alias.value,
                                        }
                                    ),
                                    str(owner),
                                    epoch,
                                    *_identity(os.fstat(descriptor)),
                                ),
                            )
                store._record_platform_process_binding(
                    account, self.binding.registry_id, owner, epoch
                )
                snapshot = store.platform_process_snapshot(account.context.authority)
                with self._transaction() as connection:
                    self._check_snapshot(connection, snapshot)
                    if snapshot.current_owner_id != owner:
                        raise PlatformFenceError("platform_owner_stale")
                    connection.execute(
                        "UPDATE platform_process_claims SET phase='claimed_closed' WHERE owner_id=?",
                        (str(owner),),
                    )
                    connection.execute(
                        "UPDATE platform_store_bindings SET mutation_seq=MAX(mutation_seq,?) WHERE store_id=?",
                        (snapshot.storage.mutation_seq, str(snapshot.storage.store_id)),
                    )
                lease = PlatformProcessLease(
                    self, store, account, owner, epoch, locks, snapshot.storage.mutation_seq
                )
                lease.assert_current()
                self._leases[owner] = lease
                return lease
            except BaseException:
                for _, descriptor in reversed(locks):
                    _close(descriptor)
                raise

    async def claim_closed_async(self, store, account: PlatformAccountAliases):
        """Wait for claim cleanup before propagating cancellation to the caller."""
        task = asyncio.create_task(asyncio.to_thread(self.claim_closed, store, account))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            async def complete(protected):
                while not protected.done():
                    try:
                        await asyncio.shield(protected)
                    except asyncio.CancelledError:
                        continue
                return protected.result()

            lease = await complete(task)
            cleanup = asyncio.create_task(asyncio.to_thread(lease.close))
            await complete(cleanup)
            raise

    def close(self) -> None:
        self._require_current()
        with self._mutex:
            if self._leases:
                raise PlatformFenceError("platform_owner_still_held")
            for descriptor in self._fds.values():
                _close(descriptor)
            self._fds.clear()


class PlatformProcessLease:
    """A held closed process claim, without any opening-release permission."""

    @property
    def phase(self) -> str:
        return "claimed_closed"

    @property
    def authority(self):
        return self._account.context.authority

    @property
    def owner_id(self) -> UUID:
        return self._owner_id

    @property
    def process_epoch(self) -> int:
        return self._process_epoch

    def __init__(self, registry, store, account, owner, epoch, locks, sequence):
        self._registry, self._store, self._account = registry, store, account
        self._owner_id, self._process_epoch = owner, epoch
        self._locks, self._sequence = locks, sequence
        self._pid, self._closed = os.getpid(), False
        self._state_lock = threading.RLock()

    def __enter__(self):
        self.assert_current()
        return self

    def __exit__(self, *_error):
        self.close()

    def _require_open(self):
        if os.getpid() != self._pid or self._closed:
            raise PlatformFenceError("platform_owner_stale")

    def assert_current(self) -> None:
        snapshot = self._current_snapshot()
        with self._registry._transaction(write=False) as connection:
            self._assert_snapshot(connection, snapshot)
        self._remember(snapshot)

    @contextmanager
    def dispatch_control(self, outbox_connection: sqlite3.Connection):
        """Hold the process lease while the opening writer admits a control action."""
        with self._state_lock:
            self._require_open()
            self._registry._require_current()
            snapshot = self._store.platform_process_snapshot_on_control(
                outbox_connection, self.authority
            )
            self._require_snapshot_identity(snapshot)
            with self._registry._transaction(write=False) as connection:
                self._assert_snapshot(connection, snapshot)
            self._remember(snapshot)
            yield

    def _remember(self, snapshot) -> None:
        with self._state_lock:
            self._require_open()
            if snapshot.storage.mutation_seq < self._sequence:
                raise PlatformFenceError("platform_storage_continuity_regressed")
            self._sequence = snapshot.storage.mutation_seq

    def _current_snapshot(self):
        self._require_open()
        self._registry._require_current()
        snapshot = self._store.platform_process_snapshot(self._account.context.authority)
        self._require_snapshot_identity(snapshot)
        return snapshot

    def _require_snapshot_identity(self, snapshot) -> None:
        with self._state_lock:
            self._require_open()
            if (
                snapshot.current_owner_id != self.owner_id
                or snapshot.context.binding_bytes != self._account.context.binding_bytes
                or snapshot.storage.mutation_seq < self._sequence
            ):
                raise PlatformFenceError("platform_owner_stale")

    def _assert_snapshot(self, connection, snapshot) -> None:
        with self._state_lock:
            self._require_open()
            if snapshot.storage.mutation_seq < self._sequence:
                raise PlatformFenceError("platform_storage_continuity_regressed")
            self._assert_registry_snapshot(connection, snapshot)

    def _assert_registry_snapshot(self, connection, snapshot) -> None:
        self._registry._check_snapshot(connection, snapshot)
        claim = connection.execute(
            "SELECT * FROM platform_process_claims WHERE owner_id=?", (str(self.owner_id),)
        ).fetchone()
        if (
            claim is None
            or claim["phase"] != "claimed_closed"
            or claim["process_epoch"] != self.process_epoch
        ):
            raise PlatformFenceError("platform_owner_stale")
        for alias, descriptor in self._locks:
            path = self._registry.binding.authority_root / "locks" / (alias.digest + ".lock")
            _require_private(path, directory=False)
            head = connection.execute(
                "SELECT * FROM platform_alias_heads WHERE digest=?", (alias.digest,)
            ).fetchone()
            if (
                head is None
                or head["owner_id"] != str(self.owner_id)
                or head["process_epoch"] != self.process_epoch
                or _identity(path.lstat()) != _identity(os.fstat(descriptor))
                or _identity(os.fstat(descriptor)) != (head["device"], head["inode"])
            ):
                raise PlatformFenceError("platform_alias_lock_replaced")

    def checkpoint(self) -> None:
        # Finish the outbox read before taking the registry writer; never hold two writers.
        snapshot = self._current_snapshot()
        with self._registry._transaction() as connection:
            self._assert_snapshot(connection, snapshot)
            connection.execute(
                "UPDATE platform_store_bindings SET mutation_seq=MAX(mutation_seq,?) WHERE store_id=?",
                (snapshot.storage.mutation_seq, str(snapshot.storage.store_id)),
            )
        self._remember(snapshot)

    def retire_closed(self) -> None:
        with self._state_lock, self._registry._transaction() as connection:
            self._require_open()
            row = connection.execute(
                "SELECT process_epoch,phase FROM platform_process_claims WHERE owner_id=?",
                (str(self.owner_id),),
            ).fetchone()
            if row is None or row[0] != self.process_epoch or row[1] != "claimed_closed":
                raise PlatformFenceError("platform_owner_stale")
            for alias, _ in self._locks:
                head = connection.execute(
                    "SELECT owner_id,process_epoch FROM platform_alias_heads WHERE digest=?",
                    (alias.digest,),
                ).fetchone()
                if head is None or tuple(head) != (str(self.owner_id), self.process_epoch):
                    raise PlatformFenceError("platform_owner_stale")
            connection.execute(
                "UPDATE platform_process_claims SET phase='retired' WHERE owner_id=?",
                (str(self.owner_id),),
            )

    def close(self) -> None:
        if os.getpid() == self._pid and self._closed:
            return
        self._require_open()
        with self._registry._mutex:
            if self._closed:
                return
            with self._state_lock:
                self.retire_closed()
                for _, descriptor in reversed(self._locks):
                    _close(descriptor)
                self._closed = True
            self._registry._leases.pop(self.owner_id, None)
