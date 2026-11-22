"""Ledger context reads use the actual group transaction, without opening TLS."""

import sqlite3
from uuid import uuid4

import pytest

from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.core.okx_ledger_binding import OkxLedgerOwnerSlot
from custos.core.platform_process_identity import PlatformFenceError
from tests.test_okx_ledger_binding import ScopeVault, running_store
from tests.test_runner_fact_store import _runner_authority


async def test_actual_connection_context_works_without_tls_or_any_second_connection(
    tmp_path, monkeypatch
):
    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    with outbox._platform_storage_read(write=True) as (connection, opened):
        assert outbox._opening_control.connection is None
        with pytest.raises(PlatformFenceError, match="identity_invalid"):
            store.platform_process_snapshot_on_control(connection, authority)

        def must_not_open_snapshot(*_args, **_kwargs):
            raise AssertionError("ledger helper opened a second snapshot or borrowed TLS")

        with monkeypatch.context() as patch:
            patch.setattr(store, "platform_process_snapshot", must_not_open_snapshot)
            patch.setattr(store, "platform_process_snapshot_on_control", must_not_open_snapshot)
            patch.setattr(outbox, "platform_storage_snapshot", must_not_open_snapshot)
            patch.setattr(outbox, "_connect", must_not_open_snapshot)
            patch.setattr(sqlite3, "connect", must_not_open_snapshot)
            snapshot = store.require_ledger_context_on(
                connection, outbox=outbox, opened_storage=opened, authority=authority
            )
        assert snapshot.context.command_fingerprint == verified.command_fingerprint
        assert snapshot.storage.store_id == opened.store_id
        assert snapshot.storage.mutation_seq == opened.mutation_seq
        assert connection.in_transaction and outbox._opening_control.connection is None


async def test_provisional_watermark_is_returned_without_poisoning_an_owner_after_rollback(
    tmp_path,
):
    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    owner = (
        await OkxLedgerOwnerSlot().replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    before = outbox.platform_storage_snapshot()
    with outbox._platform_storage_read(write=True) as (connection, opened):
        outbox._enqueue_in_transaction(
            connection,
            _runner_authority(verified),
            store._identity,
            [
                {
                    "kind": "heartbeat",
                    "event_id": str(uuid4()),
                    "status": "online",
                    "observed_at": "2026-11-22T00:00:00Z",
                }
            ],
        )
        provisional = store.require_ledger_context_on(
            connection, outbox=outbox, opened_storage=opened, authority=authority
        )
        assert provisional.storage.mutation_seq > before.mutation_seq
        connection.rollback()
    after = outbox.platform_storage_snapshot()
    assert after.mutation_seq == before.mutation_seq
    owner.assert_current()
    assert not owner.retired
    with outbox._platform_storage_read(write=True) as (connection, opened):
        retried = store.require_ledger_context_on(
            connection, outbox=outbox, opened_storage=opened, authority=authority
        )
        assert retried.context.command_fingerprint == verified.command_fingerprint


async def test_same_transaction_sees_legitimate_unrelated_metadata_progress(tmp_path):
    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    with outbox._platform_storage_read(write=True) as (connection, opened):
        connection.execute("UPDATE desired_deployments SET desired_status=desired_status")
        current = store.require_ledger_context_on(
            connection, outbox=outbox, opened_storage=opened, authority=authority
        )
        assert current.storage.mutation_seq > opened.mutation_seq
        assert current.context.command_fingerprint == verified.command_fingerprint
        connection.rollback()
