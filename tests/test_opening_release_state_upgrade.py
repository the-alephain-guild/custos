"""The pinned actual predecessor survives an atomic in-place kernel upgrade."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from custos.core.platform_process_fence import PlatformProcessRegistry
from custos.core.platform_process_identity import PlatformAuthorityBinding, PlatformFenceError
from custos.core.runner_fact import RunnerFactOutbox
from tests.test_runner_fact_store import _runner_fact_store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def predecessor_code(tmp_path_factory):
    root = tmp_path_factory.mktemp("opening-predecessor-source")
    archive = subprocess.run(
        ["git", "archive", "bbfa18ea41d5603641b10c7129f21ff9b28b1d50"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
    return root


@pytest.fixture
def predecessor(tmp_path, predecessor_code):
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(predecessor_code), str(predecessor_code / "src")]),
    }
    subprocess.run(
        [sys.executable, str(ROOT / "tests/fixtures/create_pre_opening_state.py"), str(tmp_path)],
        cwd=predecessor_code,
        env=environment,
        check=True,
        capture_output=True,
    )
    return tmp_path, json.loads((tmp_path / "expected.json").read_text())


def test_real_predecessor_keeps_store_stream_signed_bytes_and_registry_binding(predecessor):
    directory, before = predecessor
    database = directory / "runner.sqlite3"
    outbox = RunnerFactOutbox(database)
    after = outbox.platform_storage_snapshot()
    assert str(after.store_id) == before["store_id"]
    assert after.mutation_seq == before["mutation_seq"]
    with outbox._connect() as connection:
        assert [
            base64.b64encode(row[0]).decode()
            for row in connection.execute(
                "SELECT payload FROM runner_fact_outbox ORDER BY source_seq_start"
            )
        ] == before["outbox"]
        assert [
            list(row) for row in connection.execute("SELECT * FROM runner_fact_stream")
        ] == before["stream"]
        assert [
            list(row) for row in connection.execute("SELECT * FROM platform_process_heads")
        ] == before["bindings"]
    assert RunnerFactOutbox(database).platform_storage_snapshot() == after
    _, store = _runner_fact_store(database)
    registry = PlatformProcessRegistry.open_registered(
        PlatformAuthorityBinding(
            directory / "authority", UUID(before["registry_id"]), UUID(before["host_scope_id"])
        )
    )
    try:
        registry.register_store(store)
    finally:
        registry.close()


@pytest.mark.parametrize(
    "corruption", ["guard", "unknown", "partial", "column", "uuid", "counter", "header"]
)
def test_unknown_partial_or_changed_predecessor_is_not_adopted(predecessor, corruption):
    directory, before = predecessor
    database = directory / "runner.sqlite3"
    with sqlite3.connect(database) as connection:
        if corruption == "guard":
            connection.execute("DROP TRIGGER platform_mutation_desired_deployments_insert")
        elif corruption == "unknown":
            connection.execute("CREATE TABLE foreign_state(value TEXT)")
        elif corruption == "partial":
            connection.execute("CREATE TABLE runner_opening_permissions(value TEXT)")
        elif corruption == "column":
            connection.execute("ALTER TABLE runner_fact_stream ADD COLUMN foreign_field TEXT")
        elif corruption == "header":
            connection.execute("DELETE FROM runner_state_schema")
        else:
            guard = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='platform_store_identity_immutable'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER platform_store_identity_immutable")
            connection.execute("PRAGMA ignore_check_constraints=ON")
            if corruption == "uuid":
                connection.execute("UPDATE runner_store_identity SET store_id='not-a-uuid'")
            else:
                connection.execute("UPDATE runner_store_identity SET mutation_seq=-1")
            connection.execute(guard)
    with pytest.raises(PlatformFenceError, match="schema_differs|identity_invalid"):
        RunnerFactOutbox(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT store_id FROM runner_store_identity").fetchone()[0] == (
            "not-a-uuid" if corruption == "uuid" else before["store_id"]
        )
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='runner_opening_acknowledgements'"
            ).fetchone()[0]
            == 0
        )


def test_failed_upgrade_rolls_back_ddl_and_retries_the_same_registered_store(
    predecessor, monkeypatch
):
    directory, before = predecessor
    database = directory / "runner.sqlite3"
    connect = RunnerFactOutbox._connect

    def fail_second_table(self):
        connection = connect(self)
        connection.set_authorizer(
            lambda action, name, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TABLE and name == "runner_opening_permissions"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(RunnerFactOutbox, "_connect", fail_second_table)
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            RunnerFactOutbox(database)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'runner_opening_%' AND name NOT IN ('runner_opening_gate','runner_opening_release_inbox','runner_opening_release_one_pending')"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT mutation_seq FROM runner_store_identity").fetchone()[0]
            == before["mutation_seq"]
        )
    assert (
        str(RunnerFactOutbox(database).platform_storage_snapshot().store_id) == before["store_id"]
    )


async def test_legacy_runner_recovers_its_signed_desired_without_an_opening_capability(predecessor):
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.runner_fact import runner_fact_capability_shape
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

    directory, before = predecessor
    _, store = _runner_fact_store(directory / "runner.sqlite3")
    desired = await store.load_durable_desired_command(UUID(before["instance_id"]))
    authority = EngineLifecycleAuthority.from_verified_command(desired)
    assert store.load_opening_gate_state_sync(authority) == "awaiting_release"
    projectors, contracts = runner_fact_capability_shape()
    assert (
        "RunnerOpeningReleaseFact.v1" not in projectors
        and "opening_release" not in contracts["risk"]
    )
    context = store.platform_process_snapshot(authority).context
    account = resolve_platform_account_identity(
        context, build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
    )
    registry = PlatformProcessRegistry.open_registered(
        PlatformAuthorityBinding(
            directory / "authority", UUID(before["registry_id"]), UUID(before["host_scope_id"])
        )
    )
    lease = None
    try:
        lease = registry.claim_closed(store, account)
        lease.assert_current()
        assert store.load_opening_gate_state_sync(authority, lease) == "awaiting_release"
    finally:
        if lease is not None:
            lease.close()
        registry.close()
