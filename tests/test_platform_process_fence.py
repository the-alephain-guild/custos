"""Real platform ownership remains closed and bound to durable runner storage."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from contextlib import contextmanager
from uuid import uuid4

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.platform_process_identity import PlatformFenceError, platform_counter_triggers
from custos.core.runner_fact import RunnerFactOutbox
from tests.test_runner_fact_store import _runner_fact_store, _verified_command


def test_new_runner_store_has_one_identity_that_survives_reopening(tmp_path):
    database = tmp_path / "runner.sqlite3"
    first = RunnerFactOutbox(database).platform_storage_snapshot()
    reopened = RunnerFactOutbox(database).platform_storage_snapshot()

    assert first.store_id.int != 0
    assert reopened.store_id == first.store_id
    assert reopened.path == database.resolve()
    assert reopened.mutation_seq >= first.mutation_seq
    assert reopened.file_identity == first.file_identity


async def test_actual_signed_desired_mutation_advances_the_store_counter_and_rollback_does_not(
    tmp_path,
):
    outbox, store = _runner_fact_store(tmp_path / "runner.sqlite3")
    before = outbox.platform_storage_snapshot()
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    committed = outbox.platform_storage_snapshot()
    assert committed.mutation_seq > before.mutation_seq
    with sqlite3.connect(outbox.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE desired_deployments SET desired_status='applied'")
        connection.rollback()
    assert outbox.platform_storage_snapshot().mutation_seq == committed.mutation_seq


def test_old_nonempty_database_does_not_acquire_a_new_identity_and_history_is_readable(tmp_path):
    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE retained_history (value TEXT NOT NULL)")
        connection.execute("INSERT INTO retained_history VALUES('historical evidence')")
    outbox = RunnerFactOutbox(database)
    with pytest.raises(PlatformFenceError, match="identity_missing"):
        outbox.platform_storage_snapshot()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM retained_history").fetchall() == [
            ("historical evidence",)
        ]
        assert connection.execute("SELECT count(*) FROM runner_store_identity").fetchone() == (0,)


def test_store_uuid_cannot_be_changed_and_missing_counter_is_not_silently_reinstalled(tmp_path):
    database = tmp_path / "runner.sqlite3"
    outbox = RunnerFactOutbox(database)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE runner_store_identity SET store_id=?", (str(uuid4()),))
        connection.execute("DROP TRIGGER platform_mutation_desired_deployments_insert")
    with pytest.raises(PlatformFenceError, match="schema_differs"):
        outbox.platform_storage_snapshot()
    with pytest.raises(PlatformFenceError, match="schema_differs"):
        RunnerFactOutbox(database)


async def test_counter_overflow_rolls_back_real_signed_desired_and_opening_gate(tmp_path):
    outbox, store = _runner_fact_store(tmp_path / "runner.sqlite3")
    # Corrupt only the isolated counter fixture; the real desired and gate writes keep all guards.
    with sqlite3.connect(outbox.path) as connection:
        connection.execute("DROP TRIGGER platform_store_identity_immutable")
        connection.execute("UPDATE runner_store_identity SET mutation_seq=?", (2**63 - 1,))
        connection.execute(platform_counter_triggers()["platform_store_identity_immutable"])
    _, _, verified = _verified_command()
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_desired_command(
            command=verified.command,
            command_fingerprint=verified.command_fingerprint,
            verification_receipt=verified.verification_receipt,
        )
    with sqlite3.connect(outbox.path) as connection:
        assert connection.execute("SELECT count(*) FROM desired_deployments").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM runner_opening_gate").fetchone() == (0,)


def test_replace_cannot_reinitialize_a_store_identity_without_recursive_triggers(tmp_path):
    outbox = RunnerFactOutbox(tmp_path / "runner.sqlite3")
    original = outbox.platform_storage_snapshot()
    with sqlite3.connect(outbox.path) as connection:
        assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT OR REPLACE INTO runner_store_identity VALUES(1,?,1,0)", (str(uuid4()),)
            )
    assert outbox.platform_storage_snapshot().store_id == original.store_id


@pytest.mark.parametrize(
    "statement",
    [
        "REPLACE INTO runner_store_identity VALUES(1,'{new_id}',1,0)",
        "INSERT INTO runner_store_identity VALUES(1,'{new_id}',1,0) "
        "ON CONFLICT(singleton) DO UPDATE SET store_id=excluded.store_id,mutation_seq=0",
        "UPDATE runner_store_identity SET store_id='{new_id}',mutation_seq=mutation_seq+1",
        "UPDATE runner_store_identity SET schema_revision=2,mutation_seq=mutation_seq+1",
        "DELETE FROM runner_store_identity",
    ],
)
def test_store_identity_replacement_upsert_and_mutation_are_rejected(tmp_path, statement):
    outbox = RunnerFactOutbox(tmp_path / "runner.sqlite3")
    original = outbox.platform_storage_snapshot()
    with sqlite3.connect(outbox.path) as connection:
        assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement.format(new_id=uuid4()))
    assert outbox.platform_storage_snapshot() == original


async def signed_sandbox_account(database, *, instance=None):
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

    _, store = _runner_fact_store(database)

    def mutate(event):
        if instance is not None:
            event["event_id"] = str(uuid4())
            event["aggregate_id"] = str(instance)
            event["payload"]["deployment_instance_id"] = str(instance)
            event["event_type"] = ".".join(
                (event["event_type"].split(".")[0], event["payload"]["runner_id"], str(instance))
            )

    _, _, verified = _verified_command(mutate_event=mutate)
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    authority = EngineLifecycleAuthority.from_verified_command(verified)
    context = store.platform_process_snapshot(authority).context
    config = build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
    return store, resolve_platform_account_identity(context, config)


async def test_closed_claim_is_mirrored_and_normal_retirement_allows_a_new_epoch(tmp_path):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    first = registry.claim_closed(store, account)
    first.assert_current()
    assert first.phase == "claimed_closed"
    owner, epoch = first.owner_id, first.process_epoch
    with pytest.raises(PlatformFenceError, match="platform_account_occupied"):
        registry.claim_closed(store, account)
    assert store.load_opening_gate_state_sync(account.context.authority) == "awaiting_release"
    first.close()
    with pytest.raises(PlatformFenceError):
        first.assert_current()
    second = registry.claim_closed(store, account)
    assert second.owner_id != owner
    assert second.process_epoch > epoch
    second.assert_current()
    second.close()
    registry.close()


async def test_caller_cannot_construct_an_adapter_proof_from_its_own_dictionary(tmp_path):
    from custos.core.platform_process_fence import PlatformProcessRegistry
    from custos.core.platform_process_identity import PlatformAccountAliases

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    forged = None
    try:
        with pytest.raises(PlatformFenceError, match="platform_adapter_identity_differs"):
            forged_account = PlatformAccountAliases(account.context, account.aliases, b"{}")
            forged = registry.claim_closed(store, forged_account)
    finally:
        if forged is not None:
            forged.close()
        registry.close()


async def test_independent_checkpoint_cannot_overwrite_a_newer_committed_highwater(tmp_path):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    database = tmp_path / "runner.sqlite3"
    store, first_account = await signed_sandbox_account(database, instance=uuid4())
    _, second_account = await signed_sandbox_account(database, instance=uuid4())
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    first = registry.claim_closed(store, first_account)
    second = registry.claim_closed(store, second_account)
    original = registry._transaction
    waiting, resume = threading.Event(), threading.Event()
    errors = []

    @contextmanager
    def transaction(*, write=True):
        if write and threading.current_thread().name == "old-checkpoint":
            waiting.set()
            if not resume.wait(10):
                raise RuntimeError("checkpoint barrier timed out")
        with original(write=write) as connection:
            yield connection

    registry._transaction = transaction

    def checkpoint():
        try:
            first.checkpoint()
        except PlatformFenceError as error:
            errors.append(error.reason_code)

    worker = threading.Thread(target=checkpoint, name="old-checkpoint")
    worker.start()
    try:
        assert waiting.wait(10)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE runner_state_schema SET schema_version=schema_version")
        second.checkpoint()
        with original(write=False) as connection:
            newer = connection.execute(
                "SELECT mutation_seq FROM platform_store_bindings"
            ).fetchone()[0]
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive()
    with original(write=False) as connection:
        actual = connection.execute("SELECT mutation_seq FROM platform_store_bindings").fetchone()[
            0
        ]
    first.close()
    second.close()
    registry.close()
    assert actual >= newer
    assert errors in ([], ["platform_storage_continuity_regressed"])


async def test_an_older_concurrent_assert_cannot_lower_the_live_memory_anchor(tmp_path):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    database = tmp_path / "runner.sqlite3"
    store, account = await signed_sandbox_account(database)
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    original = registry._transaction
    waiting, resume = threading.Event(), threading.Event()
    errors = []

    @contextmanager
    def transaction(*, write=True):
        if not write and threading.current_thread().name == "older-assert":
            waiting.set()
            if not resume.wait(10):
                raise RuntimeError("assert barrier timed out")
        with original(write=write) as connection:
            yield connection

    registry._transaction = transaction

    def observe():
        try:
            lease.assert_current()
        except PlatformFenceError as error:
            errors.append(error.reason_code)

    worker = threading.Thread(target=observe, name="older-assert")
    worker.start()
    try:
        assert waiting.wait(10)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE runner_state_schema SET schema_version=schema_version")
        lease.assert_current()
        observed = lease._sequence
    finally:
        resume.set()
        worker.join(10)
    try:
        assert not worker.is_alive()
        assert lease._sequence >= observed
        assert errors in ([], ["platform_storage_continuity_regressed"])
    finally:
        lease.close()
        registry.close()


@pytest.mark.parametrize("operation", ["assert_current", "checkpoint"])
async def test_concurrent_close_invalidates_an_observation_before_it_touches_closed_fds(
    tmp_path, operation
):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    original = registry._transaction
    waiting, resume = threading.Event(), threading.Event()
    errors = []

    @contextmanager
    def transaction(*, write=True):
        if threading.current_thread().name == "concurrent-observer":
            waiting.set()
            if not resume.wait(10):
                raise RuntimeError("close barrier timed out")
        with original(write=write) as connection:
            yield connection

    registry._transaction = transaction

    def observe():
        try:
            getattr(lease, operation)()
        except PlatformFenceError as error:
            errors.append(error.reason_code)

    worker = threading.Thread(target=observe, name="concurrent-observer")
    worker.start()
    try:
        assert waiting.wait(10)
        lease.close()
        lease.close()
    finally:
        resume.set()
        worker.join(10)
    try:
        assert not worker.is_alive()
        assert errors == ["platform_owner_stale"]
        with original(write=False) as connection:
            assert (
                connection.execute("SELECT phase FROM platform_process_claims").fetchone()[0]
                == "retired"
            )
    finally:
        registry.close()


def test_registry_initialization_rejects_a_symlink_parent_without_creating_state(tmp_path):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(PlatformFenceError):
        PlatformProcessRegistry.initialize(link / "authority", host_scope_id=uuid4())
    assert not (actual / "authority").exists()


async def test_register_store_rejects_a_caller_supplied_snapshot_proxy(tmp_path):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, _ = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    snapshot = store.platform_storage_snapshot()

    class Proxy:
        def platform_storage_snapshot(self):
            return snapshot

    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    try:
        with pytest.raises(PlatformFenceError):
            registry.register_store(Proxy())
    finally:
        registry.close()


async def test_async_claim_cancellation_closes_actual_owner_before_returning(tmp_path):
    import asyncio

    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    original = store._record_platform_process_binding
    entered, resume = threading.Event(), threading.Event()

    def record(*args):
        entered.set()
        if not resume.wait(10):
            raise RuntimeError("claim barrier timed out")
        return original(*args)

    store._record_platform_process_binding = record
    try:
        task = asyncio.create_task(registry.claim_closed_async(store, account))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        store._record_platform_process_binding = original
        lease = registry.claim_closed(store, account)
        lease.assert_current()
        lease.close()
    finally:
        resume.set()
        registry.close()


@pytest.mark.parametrize("entry", ["registry.identity", "registry.sqlite3", "locks", "alias"])
@pytest.mark.parametrize("mutation", ["missing", "replacement", "symlink", "permissions"])
async def test_registered_path_damage_rejects_current_owner_and_open_without_repair(
    tmp_path, entry, mutation
):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    if entry == "alias":
        path = registry.binding.authority_root / "locks" / (account.aliases[0].digest + ".lock")
    else:
        path = registry.binding.authority_root / entry
    original_mode = path.stat().st_mode & 0o777
    saved = path.with_name(path.name + ".saved")
    if mutation == "permissions":
        path.chmod(0o777 if path.is_dir() else 0o666)
    else:
        path.rename(saved)
        if mutation == "symlink":
            path.symlink_to(saved, target_is_directory=saved.is_dir())
        elif mutation == "replacement":
            if saved.is_dir():
                shutil.copytree(saved, path)
            else:
                shutil.copy2(saved, path)
    try:
        with pytest.raises(PlatformFenceError):
            lease.assert_current()
        with pytest.raises(PlatformFenceError):
            registry.claim_closed(store, account)
        if entry != "alias":
            with pytest.raises(PlatformFenceError):
                PlatformProcessRegistry.open_registered(registry.binding)
        if mutation == "missing":
            assert not path.exists()
    finally:
        if mutation == "permissions":
            path.chmod(original_mode)
        else:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
            saved.rename(path)
        lease.close()
        registry.close()


def copy_sqlite(source, destination):
    from contextlib import closing

    with (
        closing(sqlite3.connect(source)) as original,
        closing(sqlite3.connect(destination)) as target,
    ):
        original.backup(target)


@pytest.mark.parametrize("rewind", ["outbox", "registry"])
async def test_single_store_rewind_behind_a_real_other_store_anchor_is_rejected(tmp_path, rewind):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    database = tmp_path / "runner.sqlite3"
    store, account = await signed_sandbox_account(database)
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    registry_database = registry.binding.authority_root / "registry.sqlite3"
    source = database if rewind == "outbox" else registry_database
    backup = tmp_path / "before.sqlite3"
    copy_sqlite(source, backup)
    lease = registry.claim_closed(store, account)
    lease.checkpoint()
    lease.close()
    inode = source.stat().st_ino
    copy_sqlite(backup, source)
    assert source.stat().st_ino == inode
    try:
        with pytest.raises(PlatformFenceError, match="continuity_regressed"):
            registry.claim_closed(store, account)
    finally:
        registry.close()


async def test_finalize_rechecks_highwater_after_its_outbox_read_and_never_returns_stale_lease(
    tmp_path,
):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    database = tmp_path / "runner.sqlite3"
    store, existing_account = await signed_sandbox_account(database, instance=uuid4())
    _, new_account = await signed_sandbox_account(database, instance=uuid4())
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    existing = registry.claim_closed(store, existing_account)
    original = registry._transaction
    waiting, resume = threading.Event(), threading.Event()
    results, errors = [], []
    writes = 0

    @contextmanager
    def transaction(*, write=True):
        nonlocal writes
        if write and threading.current_thread().name == "finalize-claim":
            writes += 1
            if writes == 2:
                waiting.set()
                if not resume.wait(10):
                    raise RuntimeError("finalize barrier timed out")
        with original(write=write) as connection:
            yield connection

    registry._transaction = transaction

    def claim():
        try:
            results.append(registry.claim_closed(store, new_account))
        except PlatformFenceError as error:
            errors.append(error.reason_code)

    worker = threading.Thread(target=claim, name="finalize-claim")
    worker.start()
    try:
        assert waiting.wait(10)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE runner_state_schema SET schema_version=schema_version")
        existing.checkpoint()
        with original(write=False) as connection:
            highwater = connection.execute(
                "SELECT mutation_seq FROM platform_store_bindings"
            ).fetchone()[0]
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive()
    assert results == []
    assert errors == ["platform_storage_continuity_regressed"]
    with original(write=False) as connection:
        assert (
            connection.execute("SELECT mutation_seq FROM platform_store_bindings").fetchone()[0]
            == highwater
        )
    recovered = registry.claim_closed(store, new_account)
    recovered.close()
    existing.close()
    registry.close()


@pytest.mark.parametrize("lifecycle", ["paused", "stopped", "running"])
async def test_real_newer_signed_desired_invalidates_an_existing_closed_owner(tmp_path, lifecycle):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)

    def mutate(event):
        event["event_id"] = str(uuid4())
        event["aggregate_version"] = 2
        event["payload"].update(generation=2, lifecycle_state=lifecycle)

    _, _, newer = _verified_command(mutate_event=mutate)
    await store.record_desired_command(
        command=newer.command,
        command_fingerprint=newer.command_fingerprint,
        verification_receipt=newer.verification_receipt,
    )
    with pytest.raises(PlatformFenceError):
        lease.assert_current()
    with pytest.raises(PlatformFenceError):
        lease.checkpoint()
    lease.close()
    registry.close()


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM platform_process_claims",
        "UPDATE platform_process_claims SET process_epoch=process_epoch+1",
        "REPLACE INTO platform_process_claims SELECT * FROM platform_process_claims",
        "DELETE FROM platform_alias_heads",
        "UPDATE platform_alias_heads SET process_epoch=0",
        "REPLACE INTO platform_alias_heads SELECT * FROM platform_alias_heads",
        "UPDATE platform_store_bindings SET mutation_seq=0",
        "DELETE FROM platform_store_bindings",
        "UPDATE platform_registry_meta SET registry_id='changed'",
        "DELETE FROM platform_registry_files",
    ],
)
async def test_registry_history_and_checkpoint_cannot_be_rewritten_by_normal_dml(
    tmp_path, statement
):
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    lease.close()
    try:
        with sqlite3.connect(registry.binding.authority_root / "registry.sqlite3") as connection:
            assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
    finally:
        registry.close()


async def test_repeated_cancellation_waits_for_actual_close_before_it_propagates(tmp_path):
    import asyncio

    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    original = registry.claim_closed
    entered, claim_go = threading.Event(), threading.Event()
    closing, close_go, closed = threading.Event(), threading.Event(), threading.Event()

    def claim(*args):
        lease = original(*args)
        original_close = lease.close

        def close():
            closing.set()
            if not close_go.wait(10):
                raise RuntimeError("close barrier timed out")
            original_close()
            closed.set()

        lease.close = close
        entered.set()
        if not claim_go.wait(10):
            raise RuntimeError("claim barrier timed out")
        return lease

    registry.claim_closed = claim
    task = asyncio.create_task(registry.claim_closed_async(store, account))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        claim_go.set()
        assert await asyncio.to_thread(closing.wait, 10)
        task.cancel()
        # An event-loop turn after the second cancellation must not finish the API.
        turn = asyncio.Event()
        asyncio.get_running_loop().call_soon(turn.set)
        await turn.wait()
        returned_early = task.done()
        close_go.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert not returned_early
        assert not registry._leases
    finally:
        claim_go.set()
        close_go.set()
        await asyncio.to_thread(closed.wait, 10)
        registry.close()


@pytest.mark.parametrize("locked", ["outbox", "registry"])
async def test_real_sqlite_writer_contention_fails_closed_and_releases_partial_resources(
    tmp_path, locked
):
    import asyncio

    from custos.core.platform_process_fence import PlatformProcessRegistry

    database = tmp_path / "runner.sqlite3"
    store, account = await signed_sandbox_account(database)
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    target = (
        database if locked == "outbox" else registry.binding.authority_root / "registry.sqlite3"
    )
    connection = sqlite3.connect(target)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(PlatformFenceError, match="unavailable"):
            await asyncio.to_thread(registry.claim_closed, store, account)
        assert not registry._leases
        connection.rollback()
        lease = registry.claim_closed(store, account)
        lease.assert_current()
        lease.close()
    finally:
        connection.close()
        registry.close()


@pytest.mark.parametrize("missing", ["fcntl", "register_at_fork"])
def test_missing_os_primitives_have_a_typed_unsupported_result(tmp_path, missing):
    import subprocess
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import asyncio,os,sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0,sys.argv[1])
if sys.argv[3]=='fcntl':
    sys.modules['fcntl']=None
else:
    del os.register_at_fork
from custos.core.platform_process_fence import PlatformProcessRegistry
from custos.core.platform_process_identity import PlatformFenceError
try:
    PlatformProcessRegistry.initialize(Path(sys.argv[2]),host_scope_id=uuid4())
except PlatformFenceError as error:
    assert error.reason_code=='unsupported_storage'
else:
    raise AssertionError('missing OS primitive was accepted')
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            str(source),
            str(tmp_path / "unsupported"),
            missing,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "scenario", ["exact", "missing", "world_readable", "wrong_registry", "symlink"]
)
def test_daemon_machine_binding_requires_the_private_exact_registration(
    tmp_path, monkeypatch, scenario
):
    import pwd
    from types import SimpleNamespace

    from custos.core.platform_process_fence import PlatformProcessRegistry

    home = tmp_path / "machine"
    home.mkdir()
    machine_dir = home / ".arx"
    machine_dir.mkdir(mode=0o700)
    other = tmp_path / "environment-home"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    config = machine_dir / "platform-process-authority.toml"

    registration = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    binding = registration.binding
    registration.close()
    if scenario == "missing":
        assert PlatformProcessRegistry.open_machine_authority() is None
        return

    source = config if scenario != "symlink" else machine_dir / "linked.toml"
    registry_id = uuid4() if scenario == "wrong_registry" else binding.registry_id
    source.write_text(
        f'schema_version=1\nauthority_root="{binding.authority_root}"\n'
        f'registry_id="{registry_id}"\nhost_scope_id="{binding.host_scope_id}"\n',
        encoding="utf-8",
    )
    source.chmod(0o644 if scenario == "world_readable" else 0o600)
    if scenario == "symlink":
        config.symlink_to(source)
    if scenario == "exact":
        loaded = PlatformProcessRegistry.open_machine_authority()
        assert loaded is not None
        assert loaded.binding == binding
        loaded.close()
    else:
        reason = "binding_invalid" if scenario == "wrong_registry" else "storage_unsafe"
        with pytest.raises(PlatformFenceError, match=reason):
            PlatformProcessRegistry.open_machine_authority()


async def test_opening_control_reuses_the_writer_and_rejects_retired_process(tmp_path, monkeypatch):
    from custos.core.execution_reservation_store import PlatformOpeningGate, RunnerOpeningGateClosed
    from custos.core.platform_process_fence import PlatformProcessRegistry

    store, account = await signed_sandbox_account(tmp_path / "runner.sqlite3")
    authority = account.context.authority
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    gate = PlatformOpeningGate(store, authority)
    gate.bind_process_lease(lease)
    seen = []
    snapshot_on_control = store.platform_process_snapshot_on_control

    def capture_writer(connection, expected_authority):
        seen.append(connection)
        return snapshot_on_control(connection, expected_authority)

    monkeypatch.setattr(store, "platform_process_snapshot_on_control", capture_writer)
    try:
        with gate.control_action():
            with store.opening_control_action(authority) as writer:
                with gate.control_action():
                    assert seen == [writer, writer]
        with pytest.raises(RunnerOpeningGateClosed, match="awaiting_opening_release"):
            gate.assert_released()
        lease.close()
        with pytest.raises(PlatformFenceError, match="platform_owner_stale"):
            with gate.control_action():
                pytest.fail("retired owner admitted a control action")
    finally:
        lease.close()
        registry.close()
