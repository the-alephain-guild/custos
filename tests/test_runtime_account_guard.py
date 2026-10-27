"""Prepare storage under the canonical account lock before claiming execution ownership."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import threading
from uuid import uuid4

import pytest

from custos.core.local_runtime_state import RuntimeAuthorityRegistry
from custos.core.runtime_authority import (
    RuntimeAccountOccupied,
    RuntimeAuthorityError,
    RuntimeFenceStale,
    RuntimeStorageUnavailable,
)
from tests.test_local_runtime_state import probe, stores
from tests.test_runtime_authority import account


def registered(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    ) as lease:
        original = lease.fence
    return registry, bindings, run_id, original


def prepare(guard, original_run_id, **changes):
    fields = dict(run_id=original_run_id, permit_digest="a" * 64, expected_authorization_epoch=1)
    fields.update(changes)
    return guard.prepare_claim(**fields)


def test_guard_excludes_another_owner_without_creating_a_registry_owner(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    bindings = stores(tmp_path / "run")
    run_id = uuid4()
    with registry.account_guard(account()) as guard:
        checkpoint = guard.prepare_claim(
            run_id=run_id,
            permit_digest="a" * 64,
            expected_authorization_epoch=0,
        )
        assert checkpoint.state is None and checkpoint.journal is None
        assert checkpoint.authorization_epoch == 1
        assert registry.read(account()) is None
        with pytest.raises(RuntimeAccountOccupied):
            registry.claim(
                account(),
                run_id=run_id,
                permit_digest="a" * 64,
                expected_authorization_epoch=0,
                stores=bindings,
            )
        lease = guard.claim(checkpoint, stores=bindings)
    guard.close()
    try:
        lease.assert_current()
        assert lease.fence.authorization_epoch == 1
        assert lease.fence.process_generation == 1
        with pytest.raises(RuntimeFenceStale):
            guard.claim(checkpoint, stores=bindings)
    finally:
        lease.close()


def test_preflight_reads_state_and_registered_journal_floor_without_reopening_journal(tmp_path):
    registry, bindings, run_id, original = registered(tmp_path)
    calls = []

    def state_only(path):
        calls.append(path)
        assert path == bindings.state.path
        return probe(path)

    registry = RuntimeAuthorityRegistry.open(registry.root, identity_probe=state_only)
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        assert checkpoint.state.path == bindings.state.path
        assert checkpoint.state.store_id == bindings.state.store_id
        assert checkpoint.state.sequence == 4
        assert checkpoint.journal.path == bindings.journal.path
        assert checkpoint.journal.store_id == bindings.journal.store_id
        assert checkpoint.journal.sequence == 9
        assert calls == [bindings.state.path]
        lease = guard.claim(checkpoint, stores=bindings, identity_probe=probe)
    try:
        lease.assert_current()
        assert calls == [bindings.state.path]
        assert lease.fence.process_generation == original.process_generation + 1
    finally:
        lease.close()


@pytest.mark.parametrize("damage", ["epoch", "source", "run", "permit"])
def test_preflight_rejects_a_wrong_request_before_client_preparation(tmp_path, damage):
    registry, _bindings, run_id, original = registered(tmp_path)
    changes = {
        "epoch": {"expected_authorization_epoch": 0},
        "source": {"control_source": "platform"},
        "run": {"run_id": uuid4()},
        "permit": {"permit_digest": "b" * 64},
    }
    prepared_client = []
    with registry.account_guard(account()) as guard:
        with pytest.raises(RuntimeAuthorityError):
            prepare(guard, run_id, **changes[damage])
            prepared_client.append(True)
    assert not prepared_client
    assert registry.read(account()).process_generation == original.process_generation


def test_claim_cannot_overwrite_a_registration_changed_after_preflight(tmp_path):
    registry, bindings, run_id, _original = registered(tmp_path)
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        with sqlite3.connect(registry.path) as connection:
            connection.execute(
                "UPDATE account_authority SET permit_digest=? WHERE scope=?",
                ("b" * 64, account().scope_digest),
            )
        with pytest.raises(RuntimeFenceStale):
            guard.claim(checkpoint, stores=bindings)
    with sqlite3.connect(registry.path) as connection:
        assert (
            connection.execute("SELECT permit_digest FROM account_authority").fetchone()[0]
            == "b" * 64
        )


@pytest.mark.parametrize("role", ["state", "journal"])
def test_prepared_claim_rejects_a_regressed_store(tmp_path, role):
    registry, bindings, run_id, original = registered(tmp_path)
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        binding = getattr(bindings, role)
        binding.path.write_text(f"{binding.store_id}:1")
        with pytest.raises(RuntimeStorageUnavailable):
            guard.claim(checkpoint, stores=bindings)
    assert registry.read(account()).process_generation == original.process_generation


def test_a_preflight_failure_does_not_advance_owner_generation(tmp_path):
    registry, bindings, run_id, original = registered(tmp_path)
    with pytest.raises(RuntimeError, match="client preparation failed"):
        with registry.account_guard(account()) as guard:
            prepare(guard, run_id)
            raise RuntimeError("client preparation failed")
    assert registry.read(account()).process_generation == original.process_generation
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=1,
        stores=bindings,
    ) as recovered:
        assert recovered.fence.process_generation == original.process_generation + 1


def test_claim_preserves_the_state_floor_observed_during_preflight(tmp_path):
    registry, bindings, run_id, original = registered(tmp_path)
    bindings.state.path.write_text(f"{bindings.state.store_id}:7")
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        assert checkpoint.state.sequence == 7
        bindings.state.path.write_text(f"{bindings.state.store_id}:6")
        with pytest.raises(RuntimeStorageUnavailable):
            guard.claim(checkpoint, stores=bindings)
    assert registry.read(account()).process_generation == original.process_generation


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", str(uuid4())),
        ("phase", "retired"),
        ("process_generation", 5),
        ("authorization_epoch", 2),
    ],
)
def test_claim_compares_the_entire_preflight_registration(tmp_path, field, value):
    registry, bindings, run_id, _original = registered(tmp_path)
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        with sqlite3.connect(registry.path) as connection:
            connection.execute(f"UPDATE account_authority SET {field}=?", (value,))
        with pytest.raises(RuntimeFenceStale):
            guard.claim(checkpoint, stores=bindings)
    with sqlite3.connect(registry.path) as connection:
        assert connection.execute(f"SELECT {field} FROM account_authority").fetchone()[0] == value


def test_claim_does_not_replace_a_watermark_advanced_after_preflight(tmp_path):
    registry, bindings, run_id, original = registered(tmp_path)
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        with sqlite3.connect(registry.path) as connection:
            metadata = json.loads(
                connection.execute("SELECT stores FROM account_authority").fetchone()[0]
            )
            metadata["journal"]["sequence"] = 100
            changed = json.dumps(metadata, sort_keys=True)
            connection.execute("UPDATE account_authority SET stores=?", (changed,))
        with pytest.raises(RuntimeFenceStale):
            guard.claim(checkpoint, stores=bindings)
    assert registry.read(account()).process_generation == original.process_generation
    with sqlite3.connect(registry.path) as connection:
        assert connection.execute("SELECT stores FROM account_authority").fetchone()[0] == changed


@pytest.mark.parametrize("damage", ["missing", "uuid", "sequence"])
def test_bad_state_is_rejected_before_client_preparation(tmp_path, damage):
    registry, bindings, run_id, original = registered(tmp_path)
    if damage == "missing":
        bindings.state.path.unlink()
    elif damage == "uuid":
        bindings.state.path.write_text(f"{uuid4()}:4")
    else:
        bindings.state.path.write_text(f"{bindings.state.store_id}:3")
    with registry.account_guard(account()) as guard:
        with pytest.raises(RuntimeStorageUnavailable):
            prepare(guard, run_id)
    assert registry.read(account()).process_generation == original.process_generation


def test_retired_preflight_requires_a_new_run_and_permit_before_preparation(tmp_path):
    registry, bindings, run_id, _original = registered(tmp_path)
    with registry.claim(
        account(),
        run_id=run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=1,
        stores=bindings,
    ) as lease:
        lease.retire()
    with registry.account_guard(account()) as guard:
        with pytest.raises(RuntimeAuthorityError):
            prepare(guard, run_id)
        checkpoint = prepare(guard, uuid4(), permit_digest="b" * 64)
        assert checkpoint.authorization_epoch == 2
        recovered = guard.claim(checkpoint, stores=bindings)
    with recovered:
        assert recovered.fence.authorization_epoch == 2
        assert recovered.fence.process_generation == 3


def test_prepared_claim_is_not_transferable_between_accounts(tmp_path):
    from dataclasses import replace

    from custos.core.runtime_account_guard import PreparedRuntimeClaim

    registry, bindings, run_id, original = registered(tmp_path)
    other = account(wallet_address="0x2222222222222222222222222222222222222222")
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        assert isinstance(checkpoint, PreparedRuntimeClaim)
        with pytest.raises(RuntimeFenceStale):
            guard.claim(replace(checkpoint), stores=bindings)
        with registry.account_guard(other) as other_guard:
            with pytest.raises(RuntimeFenceStale):
                other_guard.claim(checkpoint, stores=bindings)
    assert registry.read(account()).process_generation == original.process_generation
    assert registry.read(other) is None


def test_two_leases_keep_their_own_probe_without_replacing_the_registry_probe(tmp_path):
    registry, bindings, run_id, _original = registered(tmp_path)
    other = account(wallet_address="0x2222222222222222222222222222222222222222")
    other_bindings = stores(tmp_path / "other")
    calls = []

    def active_probe(path):
        assert path in (bindings.state.path, bindings.journal.path)
        calls.append(path)
        return probe(path)

    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        first = guard.claim(checkpoint, stores=bindings, identity_probe=active_probe)
    with (
        first,
        registry.claim(
            other,
            run_id=uuid4(),
            permit_digest="b" * 64,
            expected_authorization_epoch=0,
            stores=other_bindings,
        ) as second,
    ):
        assert registry._probe is probe
        first.assert_current()
        second.assert_current()
    assert calls == [bindings.state.path, bindings.journal.path] * 2


@pytest.mark.parametrize("generation", [0, 2**63 - 1])
def test_preflight_rejects_invalid_or_exhausted_generation_before_client_preparation(
    tmp_path, generation
):
    registry, _bindings, run_id, _original = registered(tmp_path)
    with sqlite3.connect(registry.path) as connection:
        connection.execute("UPDATE account_authority SET process_generation=?", (generation,))
    with registry.account_guard(account()) as guard:
        with pytest.raises(RuntimeAuthorityError):
            prepare(guard, run_id)
    assert registry.read(account()).process_generation == generation


def test_a_parallel_close_waits_for_the_whole_preparation_and_cannot_close_the_lease(tmp_path):
    registry, bindings, run_id, _original = registered(tmp_path)
    closed = threading.Event()
    with registry.account_guard(account()) as guard:
        checkpoint = prepare(guard, run_id)
        worker = threading.Thread(target=lambda: (guard.close(), closed.set()))
        worker.start()
        assert not closed.wait(0.05)
        with pytest.raises(RuntimeAuthorityError):
            guard.close()
        lease = guard.claim(checkpoint, stores=bindings)
    try:
        worker.join(3)
        assert closed.is_set()
        lease.assert_current()
    finally:
        lease.close()


@pytest.mark.filterwarnings("ignore:This process .*multi-threaded:DeprecationWarning")
def test_a_fork_copy_neither_waits_for_the_guard_nor_closes_a_reused_descriptor(tmp_path):
    registry, _bindings, run_id, _original = registered(tmp_path)
    entered, release = threading.Event(), threading.Event()
    held, errors = [], []

    def hold():
        try:
            with registry.account_guard(account()) as guard:
                held.append(guard)
                entered.set()
                release.wait(10)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(3), "worker did not acquire preparation lock"
    guard = held[0]
    original_descriptor = guard._descriptor
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        replacement = os.open(tmp_path / "child-file", os.O_RDWR | os.O_CREAT, 0o600)
        if replacement != original_descriptor:
            os.dup2(replacement, original_descriptor)
            os.close(replacement)
        try:
            with pytest.raises(RuntimeFenceStale):
                prepare(guard, run_id)
            with pytest.raises(RuntimeFenceStale):
                with guard.preparation():
                    pass
            with pytest.raises(RuntimeFenceStale):
                guard.close()
            os.fstat(original_descriptor)
            os.write(writer, b"fenced")
        finally:
            os._exit(0)
    os.close(writer)
    ready = []
    try:
        ready, _, _ = select.select([reader], [], [], 3)
        assert ready, "fork copy waited for another thread's inherited guard"
        assert os.read(reader, 16) == b"fenced"
    finally:
        if not ready:
            os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        os.close(reader)
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
