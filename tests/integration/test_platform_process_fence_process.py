"""Independent PIDs contend on real OS locks while every opening gate stays closed."""

from __future__ import annotations

import json
import selectors
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from custos.core.platform_process_fence import PlatformProcessRegistry

pytest.importorskip("nautilus_trader")

WORKER = Path(__file__).with_name("platform_process_fence_worker.py")


def response(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(15):
            process.kill()
            _, error = process.communicate(timeout=5)
            pytest.fail(f"worker failed to reach its real ownership barrier: {error}")
    value = process.stdout.readline()
    if not value:
        _, error = process.communicate(timeout=5)
        pytest.fail(f"worker exited before its ownership result: {error}")
    return json.loads(value)


def start(binding, database, **options):
    request = {
        "authority_root": str(binding.authority_root),
        "registry_id": str(binding.registry_id),
        "host_scope_id": str(binding.host_scope_id),
        "database": str(database),
        **options,
    }
    return subprocess.Popen(
        [sys.executable, "-I", "-B", str(WORKER), json.dumps(request)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def release(process):
    process.stdin.write("release\n")
    process.stdin.flush()
    assert response(process)["status"] == "released"
    _, error = process.communicate(timeout=10)
    assert process.returncode == 0, error


def terminate(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


def test_same_instance_is_exclusive_across_real_pids_and_distinct_outboxes(tmp_path):
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    first = start(registry.binding, tmp_path / "first.sqlite3")
    second = None
    try:
        held = response(first)
        assert held["status"] == "claimed_closed"
        second = start(
            registry.binding,
            tmp_path / "second.sqlite3",
            tenant_id="other_tenant",
            runner_id=str(uuid4()),
        )
        refused = response(second)
        assert held["pid"] != refused["pid"]
        assert refused["status"] == "refused"
        assert refused["reason_code"] == "platform_account_occupied"
        assert second.wait(timeout=10) == 0
        release(first)
    finally:
        terminate(first)
        if second is not None:
            terminate(second)
        registry.close()


def test_different_sandbox_instances_with_equal_native_account_labels_can_coexist(tmp_path):
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    first = start(registry.binding, tmp_path / "first.sqlite3", instance_id=str(uuid4()))
    second = None
    try:
        held = response(first)
        assert held["status"] == "claimed_closed"
        second = start(registry.binding, tmp_path / "second.sqlite3", instance_id=str(uuid4()))
        other = response(second)
        assert other["status"] == "claimed_closed"
        assert held["pid"] != other["pid"]
        with registry._transaction(write=False) as connection:
            bindings = [
                json.loads(row[0])
                for row in connection.execute("SELECT adapter_binding FROM platform_process_claims")
            ]
        assert len(bindings) == 2
        assert bindings[0]["account_id"] == bindings[1]["account_id"]
        release(first)
        second.stdin.write("assert\n")
        second.stdin.flush()
        assert response(second)["status"] == "current"
        release(second)
    finally:
        terminate(first)
        if second is not None:
            terminate(second)
        registry.close()


@pytest.mark.parametrize("shared", ["scope_id", "wallet_address", "sodex_account_id", None])
def test_real_processes_use_independent_sodex_aliases_not_one_combined_account_tuple(
    tmp_path, shared
):
    from nautilus_trader.adapters.sodex import SodexExecClientConfig

    if not hasattr(SodexExecClientConfig, "wallet_address") or not hasattr(
        SodexExecClientConfig, "account_id"
    ):
        pytest.skip("installed native ABI has no public SoDEX account identity getters")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    identities = {
        "trading_mode": "testnet",
        "connector": "sodex",
        "scope_id": str(uuid4()),
        "wallet_address": "0x" + "11" * 20,
        "sodex_account_id": 100,
        "instance_id": str(uuid4()),
    }
    contender = {
        **identities,
        "scope_id": str(uuid4()),
        "wallet_address": "0x" + "22" * 20,
        "sodex_account_id": 200,
        "instance_id": str(uuid4()),
        "tenant_id": "other_tenant",
        "runner_id": str(uuid4()),
    }
    if shared is not None:
        contender[shared] = identities[shared]
    first = start(registry.binding, tmp_path / "first.sqlite3", **identities)
    second = None
    try:
        held = response(first)
        assert held["status"] == "claimed_closed"
        second = start(registry.binding, tmp_path / "second.sqlite3", **contender)
        result = response(second)
        assert held["pid"] != result["pid"]
        if shared is None:
            assert result["status"] == "claimed_closed"
            release(first)
            second.stdin.write("assert\n")
            second.stdin.flush()
            assert response(second)["status"] == "current"
            release(second)
        else:
            assert result["status"] == "refused"
            assert result["reason_code"] == "platform_account_occupied"
            assert second.wait(timeout=10) == 0
            release(first)
    finally:
        terminate(first)
        if second is not None:
            terminate(second)
        registry.close()


def test_failed_partial_sodex_claim_releases_only_its_own_locks_across_actual_pids(tmp_path):
    from nautilus_trader.adapters.sodex import SodexExecClientConfig

    from custos.core.platform_process_identity import PlatformAccountAlias

    if not hasattr(SodexExecClientConfig, "wallet_address") or not hasattr(
        SodexExecClientConfig, "account_id"
    ):
        pytest.skip("installed native ABI has no public SoDEX account identity getters")
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    holder = {
        "trading_mode": "testnet",
        "connector": "sodex",
        "scope_id": str(uuid4()),
        "wallet_address": "0x" + "11" * 20,
        "sodex_account_id": 100,
        "instance_id": str(uuid4()),
    }
    contender = {
        **holder,
        "scope_id": str(uuid4()),
        "wallet_address": "0x" + "22" * 20,
        "instance_id": str(uuid4()),
        "runner_id": str(uuid4()),
        "tenant_id": "other_tenant",
    }
    third_identity = {
        **contender,
        "wallet_address": "0x" + "33" * 20,
        "sodex_account_id": 300,
        "instance_id": str(uuid4()),
        "runner_id": str(uuid4()),
    }
    probe_identity = {
        **holder,
        "scope_id": str(uuid4()),
        "sodex_account_id": 400,
        "instance_id": str(uuid4()),
        "runner_id": str(uuid4()),
    }
    first = start(registry.binding, tmp_path / "first.sqlite3", **holder)
    others = []
    try:
        held = response(first)
        assert held["status"] == "claimed_closed"
        partial_alias = PlatformAccountAlias("testnet", "credential_scope", contender["scope_id"])
        partial_path = registry.binding.authority_root / "locks" / (partial_alias.digest + ".lock")
        assert not partial_path.exists()
        failed = start(
            registry.binding, tmp_path / "partial.sqlite3", hold_refused=True, **contender
        )
        others.append(failed)
        rejected = response(failed)
        assert rejected["pid"] != held["pid"]
        assert rejected["reason_code"] == "platform_account_occupied"
        assert failed.poll() is None
        assert partial_path.is_file()
        # The contender obtained its unique credential-scope lock before hitting
        # the holder's venue-account lock. A third PID must be able to take it now.
        third = start(registry.binding, tmp_path / "third.sqlite3", **third_identity)
        others.append(third)
        acquired = response(third)
        assert acquired["status"] == "claimed_closed"
        assert acquired["pid"] != held["pid"]
        assert failed.poll() is None
        probe = start(registry.binding, tmp_path / "probe.sqlite3", **probe_identity)
        others.append(probe)
        still_held = response(probe)
        assert still_held["pid"] not in {held["pid"], acquired["pid"]}
        assert still_held["reason_code"] == "platform_account_occupied"
        assert probe.wait(timeout=10) == 0
        failed.stdin.write("release\n")
        failed.stdin.flush()
        assert response(failed)["status"] == "refusal_complete"
        assert failed.wait(timeout=10) == 0
        release(first)
        third.stdin.write("assert\n")
        third.stdin.flush()
        assert response(third)["status"] == "current"
        release(third)
    finally:
        terminate(first)
        for process in others:
            terminate(process)
        registry.close()


@pytest.mark.parametrize("phase", ["pending", "mirror", "finalize", None])
def test_sigkill_at_each_committed_boundary_reclaims_a_new_closed_owner(tmp_path, phase):
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    database = tmp_path / "runner.sqlite3"
    first = start(registry.binding, database, pause_after=phase)
    second = None
    try:
        before = response(first)
        assert before["status"] == (phase or "claimed_closed")
        with registry._transaction(write=False) as connection:
            old = connection.execute(
                "SELECT owner_id,process_epoch FROM platform_process_claims"
            ).fetchone()
        first.kill()
        assert first.wait(timeout=10) < 0
        second = start(registry.binding, database)
        held = response(second)
        assert held["status"] == "claimed_closed"
        assert held["pid"] != before["pid"]
        assert held["owner_id"] != old["owner_id"]
        assert held["epoch"] > old["process_epoch"]
        with registry._transaction(write=False) as connection:
            abandoned = connection.execute(
                "SELECT phase,reason FROM platform_process_claims WHERE owner_id=?",
                (old["owner_id"],),
            ).fetchone()
            assert tuple(abandoned) == ("recovery_required", "abandoned_claim")
        release(second)
    finally:
        terminate(first)
        if second is not None:
            terminate(second)
        registry.close()


def test_fork_child_cannot_use_or_unlock_parent_and_exec_does_not_inherit_ownership(tmp_path):
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    first = start(registry.binding, tmp_path / "first.sqlite3")
    contenders = []
    try:
        held = response(first)
        assert held["status"] == "claimed_closed"
        first.stdin.write("fork\n")
        first.stdin.flush()
        child = response(first)
        assert child["status"] == "fork_refused"
        assert child["pid"] != held["pid"]
        assert child["reason_code"] == "platform_owner_stale"
        assert response(first)["status"] == "parent_current"
        contender = start(registry.binding, tmp_path / "contender.sqlite3")
        contenders.append(contender)
        assert response(contender)["reason_code"] == "platform_account_occupied"
        assert contender.wait(timeout=10) == 0
        first.stdin.write("exec\n")
        first.stdin.flush()
        assert response(first)["status"] == "exec_ready"
        contender = start(registry.binding, tmp_path / "after-exec.sqlite3")
        contenders.append(contender)
        assert response(contender)["status"] == "claimed_closed"
        release(contender)
    finally:
        terminate(first)
        for contender in contenders:
            terminate(contender)
        registry.close()


def test_unmirrored_pending_rewind_has_no_anchor_and_only_returns_a_new_closed_owner(tmp_path):
    import asyncio

    from tests.test_platform_process_fence import copy_sqlite, signed_sandbox_account

    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    database = tmp_path / "runner.sqlite3"
    store, _ = asyncio.run(signed_sandbox_account(database))
    registry.register_store(store)
    registry_database = registry.binding.authority_root / "registry.sqlite3"
    backup = tmp_path / "before-pending.sqlite3"
    copy_sqlite(registry_database, backup)
    first = start(registry.binding, database, pause_after="pending")
    second = None
    try:
        assert response(first)["status"] == "pending"
        with registry._transaction(write=False) as connection:
            pending = connection.execute(
                "SELECT owner_id,process_epoch FROM platform_process_claims"
            ).fetchone()
        first.kill()
        assert first.wait(timeout=10) < 0
        inode = registry_database.stat().st_ino
        copy_sqlite(backup, registry_database)
        assert registry_database.stat().st_ino == inode
        # No independent store ever mirrored this pending allocation. Reusing its
        # lost ordinal is indistinguishable from never having allocated it.
        second = start(registry.binding, database)
        held = response(second)
        assert held["status"] == "claimed_closed"
        assert held["owner_id"] != pending["owner_id"]
        assert held["epoch"] == pending["process_epoch"]
        release(second)
    finally:
        terminate(first)
        if second is not None:
            terminate(second)
        registry.close()
