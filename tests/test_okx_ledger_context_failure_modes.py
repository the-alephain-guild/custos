"""A ledger writer must validate its own database and current signed desired."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from custos.core.platform_process_identity import PlatformFenceError
from custos.core.runner_fact import RunnerFactOutbox, RunnerStateAuthorityError
from tests.test_okx_ledger_binding import running_store


async def test_foreign_clone_with_the_same_store_uuid_is_not_this_outbox_database(tmp_path):
    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    own = outbox.platform_storage_snapshot()
    clone = tmp_path / "clone.sqlite3"
    with (
        closing(sqlite3.connect(outbox.path)) as source,
        closing(sqlite3.connect(clone)) as destination,
    ):
        source.backup(destination)
    foreign = RunnerFactOutbox(clone)
    with foreign._platform_storage_read(write=True) as (connection, opened):
        assert opened.store_id == own.store_id
        baseline = store._platform_process_snapshot_on(connection, opened, authority)
        assert baseline.context.command_fingerprint == verified.command_fingerprint
        with pytest.raises(RunnerStateAuthorityError, match="database"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=own, authority=authority
            )


async def test_same_database_but_a_different_outbox_object_is_refused(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    other = RunnerFactOutbox(outbox.path)
    with other._platform_storage_read(write=True) as (connection, opened):
        with pytest.raises(RunnerStateAuthorityError, match="outbox"):
            store.require_ledger_context_on(
                connection, outbox=other, opened_storage=opened, authority=authority
            )


async def test_wrong_authority_is_checked_on_the_actual_writer_connection(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    with outbox._platform_storage_read(write=True) as (connection, opened):
        with pytest.raises(PlatformFenceError, match="desired_stale"):
            store.require_ledger_context_on(
                connection,
                outbox=outbox,
                opened_storage=opened,
                authority=replace(authority, generation=99),
            )


async def test_uncommitted_desired_change_is_visible_to_the_guard_not_a_second_snapshot(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    with outbox._platform_storage_read(write=True) as (connection, opened):
        connection.execute(
            "UPDATE desired_deployments SET command_fingerprint=? WHERE deployment_instance_id=?",
            ("f" * 64, str(authority.deployment_instance_id)),
        )
        with pytest.raises(PlatformFenceError, match="desired_invalid"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )
        connection.rollback()


async def test_invalid_receipt_is_rejected_on_the_same_transaction(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    with outbox._platform_storage_read(write=True) as (connection, opened):
        connection.execute("UPDATE desired_deployments SET verification_receipt=?", ("{}",))
        with pytest.raises(PlatformFenceError, match="desired_invalid"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )
        connection.rollback()


async def test_temporary_shadow_table_is_not_a_durable_source(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    with outbox._platform_storage_read(write=True) as (connection, opened):
        connection.execute(
            "CREATE TEMP TABLE desired_deployments AS SELECT * FROM main.desired_deployments"
        )
        with pytest.raises(RunnerStateAuthorityError, match="temporary schema"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )


async def test_attached_database_is_not_part_of_the_controlled_writer(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    extra = tmp_path / "extra.sqlite3"
    with closing(sqlite3.connect(extra)):
        pass
    with outbox._platform_storage_read(write=True) as (connection, opened):
        connection.execute("ATTACH DATABASE ? AS foreign_data", (str(extra),))
        with pytest.raises(RunnerStateAuthorityError, match="attached database"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )


async def test_open_connection_cannot_be_relabelled_after_store_file_replacement(tmp_path):
    import os

    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    replacement = tmp_path / "replacement.sqlite3"
    with (
        closing(sqlite3.connect(outbox.path)) as source,
        closing(sqlite3.connect(replacement)) as destination,
    ):
        source.backup(destination)
    with closing(outbox._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        opened = outbox._platform_storage_snapshot_on(connection, outbox.path.lstat())
        os.replace(replacement, outbox.path)
        with pytest.raises(PlatformFenceError, match="storage_replaced"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )
        connection.rollback()


async def test_connection_without_an_active_transaction_is_refused(tmp_path):
    outbox, store, authority, _ = await running_store(tmp_path / "runner.sqlite3")
    opened = outbox.platform_storage_snapshot()
    with closing(outbox._connect()) as connection:
        assert not connection.in_transaction
        with pytest.raises(RunnerStateAuthorityError, match="controlled active connection"):
            store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )
