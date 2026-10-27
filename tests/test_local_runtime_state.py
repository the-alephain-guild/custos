import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from custos.core.local_runtime_state import (
    BoundRuntimeStore,
    RuntimeAuthorityRegistry,
    RuntimeStorageBindings,
    RuntimeStoreIdentity,
)
from custos.core.runtime_authority import (
    RuntimeAccountOccupied,
    RuntimeAuthorityError,
    RuntimeFenceStale,
    RuntimeStorageUnavailable,
)
from tests.test_runtime_authority import account


def probe(path: Path) -> RuntimeStoreIdentity:
    identity, sequence = path.read_text().split(":")
    return RuntimeStoreIdentity(UUID(identity), int(sequence))


def stores(root: Path) -> RuntimeStorageBindings:
    root.mkdir()
    state_id, journal_id = uuid4(), uuid4()
    state, journal = root / "state.identity", root / "journal.identity"
    state.write_text(f"{state_id}:4")
    journal.write_text(f"{journal_id}:9")
    return RuntimeStorageBindings(
        BoundRuntimeStore(state, state_id), BoundRuntimeStore(journal, journal_id)
    )


def test_control_ownership_does_not_depend_on_readable_risk_storage(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    with registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as lease:
        bindings.state.path.unlink()
        with pytest.raises(RuntimeStorageUnavailable):
            lease.assert_current()
        lease.assert_control_current()


def test_the_control_only_check_still_rejects_retirement_and_closed_owners(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    with registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=stores(tmp_path / "run"),
    ) as lease:
        lease.assert_control_current()
        lease.retire()
        with pytest.raises(RuntimeFenceStale):
            lease.assert_control_current()
    with pytest.raises(RuntimeFenceStale):
        lease.assert_control_current()


def test_control_actions_allow_reentrant_checks_and_release_after_exceptions(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    with registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=stores(tmp_path / "run"),
    ) as lease:
        with pytest.raises(RuntimeError, match="operation failed"):
            with lease.control_action():
                lease.assert_current()
                with lease.control_action():
                    lease.assert_control_current()
                raise RuntimeError("operation failed")
        completed = threading.Event()
        worker = threading.Thread(target=lambda: (lease.close(), completed.set()))
        worker.start()
        worker.join(3)
        assert completed.is_set()


@pytest.mark.filterwarnings("ignore:This process .*multi-threaded:DeprecationWarning")
def test_a_fork_copy_is_rejected_before_acquiring_an_inherited_action_lock(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    with registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=stores(tmp_path / "run"),
    ) as lease:
        entered, release = threading.Event(), threading.Event()

        def hold():
            with lease.control_action():
                entered.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        assert entered.wait(3)
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(reader)
            try:
                with lease.control_action():
                    os.write(writer, b"unsafe")
            except RuntimeFenceStale:
                os.write(writer, b"fenced")
            finally:
                os._exit(0)
        os.close(writer)
        ready = []
        try:
            ready, _, _ = select.select([reader], [], [], 3)
            assert ready, "fork copy waited on an inherited action lock"
            assert os.read(reader, 16) == b"fenced"
        finally:
            release.set()
            worker.join(3)
            assert not worker.is_alive()
            if not ready:
                os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            os.close(reader)


def test_a_reopened_registry_preserves_authorization_and_advances_process_generation(tmp_path):
    root = tmp_path / "control"
    bindings = stores(tmp_path / "run")
    registry = RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as first:
        first.assert_current()
        assert first.fence.authorization_epoch == 1
        assert first.fence.process_generation == 1
    reopened = RuntimeAuthorityRegistry.open(root, identity_probe=probe)
    with reopened.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=1,
        stores=bindings,
    ) as recovered:
        assert recovered.fence.authorization_epoch == first.fence.authorization_epoch
        assert recovered.fence.process_generation == first.fence.process_generation + 1
        assert recovered.fence.owner_id != first.fence.owner_id
        assert reopened.read(account()).phase == "recovery_required"


def test_another_registry_cannot_take_the_wallet_while_its_process_owner_holds_it(tmp_path):
    root = tmp_path / "control"
    bindings = stores(tmp_path / "run")
    registry = RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    other = RuntimeAuthorityRegistry.open(root, identity_probe=probe)
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as held:
        with pytest.raises(RuntimeAccountOccupied):
            with other.claim(
                account(),
                run_id=run_id,
                permit_digest="a" * 64,
                expected_authorization_epoch=1,
                stores=bindings,
            ):
                pass
        held.assert_current()


def test_store_progress_advances_the_durable_floor_and_a_rollback_is_rejected(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as held:
        bindings.journal.path.write_text(f"{bindings.journal.store_id}:10")
        held.assert_current()
    bindings.journal.path.write_text(f"{bindings.journal.store_id}:9")
    with pytest.raises(RuntimeStorageUnavailable):
        registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
        )


def test_retirement_does_not_authorize_a_platform_takeover(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    with registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as held:
        held.retire()
    with pytest.raises(RuntimeAuthorityError):
        with registry.claim(
            account(),
            run_id=uuid4(),
            permit_digest="b" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
            control_source="platform",
        ):
            pass


@pytest.mark.parametrize("expected_epoch", [False, 0.0, -1])
def test_invalid_authorization_epochs_cannot_create_ownership(tmp_path, expected_epoch):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    with pytest.raises(RuntimeAuthorityError):
        with registry.claim(
            account(),
            run_id=uuid4(),
            permit_digest="a" * 64,
            expected_authorization_epoch=expected_epoch,
            stores=bindings,
        ):
            pass
    assert registry.read(account()) is None


def test_missing_or_empty_registry_is_not_reinitialized_on_open(tmp_path):
    root = tmp_path / "control"
    RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    database = root / "registry.sqlite3"
    database.unlink()
    with pytest.raises(RuntimeStorageUnavailable):
        RuntimeAuthorityRegistry.open(root, identity_probe=probe)
    assert not database.exists()
    database.touch()
    with pytest.raises(RuntimeStorageUnavailable):
        RuntimeAuthorityRegistry.open(root, identity_probe=probe)
    assert database.stat().st_size == 0


def test_recovery_cannot_switch_to_another_directory_even_with_copied_store_ids(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    original = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=original,
    ):
        pass
    replacement = stores(tmp_path / "other-run")
    replacement.state.path.write_text(original.state.path.read_text())
    replacement.journal.path.write_text(original.journal.path.read_text())
    replacement = RuntimeStorageBindings(
        BoundRuntimeStore(replacement.state.path, original.state.store_id),
        BoundRuntimeStore(replacement.journal.path, original.journal.store_id),
    )
    with pytest.raises(RuntimeStorageUnavailable):
        registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=replacement,
        )
    assert registry.read(account()).process_generation == 1


def test_missing_journal_does_not_create_an_empty_replacement(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ):
        pass
    bindings.journal.path.unlink()
    with pytest.raises(RuntimeStorageUnavailable):
        registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
        )
    assert not bindings.journal.path.exists()


def test_a_store_cannot_be_both_runtime_state_and_adapter_journal(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    with pytest.raises(RuntimeStorageUnavailable):
        with registry.claim(
            account(),
            run_id=uuid4(),
            permit_digest="a" * 64,
            expected_authorization_epoch=0,
            stores=RuntimeStorageBindings(bindings.state, bindings.state),
        ):
            pass


def test_an_old_lease_cannot_act_or_release_a_replacement_run(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    old = registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    )
    old.retire()
    old.close()
    with registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="b" * 64,
        expected_authorization_epoch=1,
        stores=bindings,
    ) as replacement:
        assert replacement.fence.authorization_epoch == 2
        assert replacement.fence.process_generation == 2
        with pytest.raises(RuntimeFenceStale):
            old.assert_current()
        old.close()
        replacement.assert_current()
        assert registry.read(account()).owner_id == replacement.fence.owner_id


def test_retired_run_cannot_reuse_its_permit_but_another_account_is_independent(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as held:
        held.retire()
    with pytest.raises(RuntimeAuthorityError):
        registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
        )
    other = account(wallet_address="0x2222222222222222222222222222222222222222")
    with registry.claim(
        other,
        run_id=uuid4(),
        permit_digest="b" * 64,
        expected_authorization_epoch=0,
        stores=stores(tmp_path / "other"),
    ) as other_owner:
        other_owner.assert_current()
        assert other_owner.fence.account_scope != held.fence.account_scope


def test_replaced_store_uuid_is_rejected_without_advancing_the_owner(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ):
        pass
    bindings.journal.path.write_text(f"{uuid4()}:10")
    with pytest.raises(RuntimeStorageUnavailable):
        registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
        )
    assert registry.read(account()).process_generation == 1
    bindings.journal.path.write_text(f"{bindings.journal.store_id}:9")
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=1,
        stores=bindings,
    ) as recovered:
        recovered.assert_current()


def test_a_different_valid_registry_database_cannot_replace_the_registered_one(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    other = RuntimeAuthorityRegistry.initialize(tmp_path / "other", identity_probe=probe)
    shutil.copyfile(other.path, registry.path)
    with pytest.raises(RuntimeStorageUnavailable):
        RuntimeAuthorityRegistry.open(registry.root, identity_probe=probe)


def test_registered_databases_and_markers_cannot_be_symlinks(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    original = registry.path.read_bytes()
    saved = tmp_path / "saved.sqlite3"
    saved.write_bytes(original)
    registry.path.unlink()
    registry.path.symlink_to(saved)
    with pytest.raises(RuntimeStorageUnavailable):
        RuntimeAuthorityRegistry.open(registry.root, identity_probe=probe)


def test_a_control_directory_readable_by_other_users_is_refused(tmp_path):
    root = tmp_path / "control"
    root.mkdir(mode=0o755)
    with pytest.raises(RuntimeStorageUnavailable):
        RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)


@pytest.mark.parametrize("sequence", [True, -1, 1.0])
def test_store_checkpoint_must_be_a_nonnegative_integer(sequence):
    with pytest.raises(RuntimeStorageUnavailable):
        RuntimeStoreIdentity(uuid4(), sequence)


CHILD = """
import json, sys
from pathlib import Path
from uuid import UUID
from custos.core.local_runtime_state import RuntimeAuthorityRegistry, RuntimeStorageBindings, BoundRuntimeStore
from custos.core.runtime_authority import RuntimeAccountOccupied, RuntimeStorageUnavailable
from tests.test_local_runtime_state import probe
from tests.test_runtime_authority import account
root, run_id, state_path, state_id, journal_path, journal_id, expected = sys.argv[1:]
registry = RuntimeAuthorityRegistry.open(Path(root), identity_probe=probe)
bindings = RuntimeStorageBindings(BoundRuntimeStore(Path(state_path), UUID(state_id)), BoundRuntimeStore(Path(journal_path), UUID(journal_id)))
try:
    lease = registry.claim(account(), run_id=UUID(run_id), permit_digest='a'*64, expected_authorization_epoch=int(expected), stores=bindings)
except RuntimeAccountOccupied:
    print('occupied', flush=True)
    sys.exit(7)
except RuntimeStorageUnavailable:
    print('storage-unavailable', flush=True)
    sys.exit(8)
with lease:
    print(json.dumps({'authorization_epoch': lease.fence.authorization_epoch, 'process_generation': lease.fence.process_generation}), flush=True)
    sys.stdin.read()
"""


def child_command(root, run_id, bindings, expected):
    return [
        sys.executable,
        "-c",
        CHILD,
        str(root),
        str(run_id),
        str(bindings.state.path),
        str(bindings.state.store_id),
        str(bindings.journal.path),
        str(bindings.journal.store_id),
        str(expected),
    ]


def test_an_independent_process_is_refused_while_the_account_lock_is_held(tmp_path):
    root = tmp_path / "control"
    registry = RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as held:
        result = subprocess.run(
            child_command(root, run_id, bindings, 1),
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 7, result.stderr
        assert result.stdout.strip() == "occupied"
        held.assert_current()


def test_a_killed_owner_releases_only_the_process_lock_not_the_registered_identity(tmp_path):
    root = tmp_path / "control"
    registry = RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    child = subprocess.Popen(
        child_command(root, run_id, bindings, 0),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0], "owner did not acknowledge acquisition"
        first = json.loads(child.stdout.readline())
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode < 0
        with registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
        ) as recovered:
            assert recovered.fence.authorization_epoch == first["authorization_epoch"]
            assert recovered.fence.process_generation == first["process_generation"] + 1
            recovered.assert_current()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("recreate", [False, True])
def test_replacing_a_held_lock_file_does_not_let_another_process_steal_the_owner(
    tmp_path, recreate
):
    root = tmp_path / "control"
    registry = RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as held:
        lock_path = root / f"{account().scope_digest}.lock"
        lock_path.unlink()
        if recreate:
            lock_path.touch(mode=0o600)
        result = subprocess.run(
            child_command(root, run_id, bindings, 1),
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 8, result.stderr
        assert registry.read(account()).owner_id == held.fence.owner_id
        with pytest.raises(RuntimeStorageUnavailable):
            held.assert_current()


FORK_OWNER = """
import json, os, signal, sys
from pathlib import Path
from uuid import UUID
from custos.core.local_runtime_state import RuntimeAuthorityRegistry, RuntimeStorageBindings, BoundRuntimeStore
from tests.test_local_runtime_state import probe
from tests.test_runtime_authority import account
root, run_id, state_path, state_id, journal_path, journal_id, expected = sys.argv[1:]
registry = RuntimeAuthorityRegistry.open(Path(root), identity_probe=probe)
bindings = RuntimeStorageBindings(BoundRuntimeStore(Path(state_path), UUID(state_id)), BoundRuntimeStore(Path(journal_path), UUID(journal_id)))
with registry.claim(account(), run_id=UUID(run_id), permit_digest='a'*64, expected_authorization_epoch=int(expected), stores=bindings):
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        os.write(write_fd, b'ready')
        os.close(write_fd)
        signal.pause()
        os._exit(0)
    os.close(write_fd)
    assert os.read(read_fd, 5) == b'ready'
    os.close(read_fd)
    print(json.dumps({'child_pid': child_pid}), flush=True)
    sys.stdin.read()
"""


def test_a_forked_child_does_not_keep_or_unlock_its_parent_account_lock(tmp_path):
    root = tmp_path / "control"
    registry = RuntimeAuthorityRegistry.initialize(root, identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    command = child_command(root, run_id, bindings, 0)
    command[2] = FORK_OWNER
    parent = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    descendant_pid = None
    try:
        assert select.select([parent.stdout], [], [], 10)[0], "fork owner did not become ready"
        descendant_pid = json.loads(parent.stdout.readline())["child_pid"]
        with pytest.raises(RuntimeAccountOccupied):
            registry.claim(
                account(),
                run_id=run_id,
                permit_digest="a" * 64,
                expected_authorization_epoch=1,
                stores=bindings,
            )
        parent.kill()
        parent.wait(timeout=10)
        with registry.claim(
            account(),
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=1,
            stores=bindings,
        ) as recovered:
            recovered.assert_current()
    finally:
        if parent.poll() is None:
            parent.kill()
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        parent.communicate(timeout=10)
