"""Retired ledger owners cannot be revived by delayed parsing or stale storage."""

import asyncio
import threading

import pytest

from custos.core import okx_ledger_binding
from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from tests.test_okx_ledger_binding import ScopeVault, running_store


@pytest.mark.parametrize("reason", ["run_exited", "failed"])
async def test_retiring_the_slot_invalidates_a_pending_resolution_even_while_desired_runs(
    tmp_path, reason
):
    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    entered, release = threading.Event(), threading.Event()

    class BlockingVault(ScopeVault):
        def decrypt(self, reference):
            entered.set()
            assert release.wait(2), "release the test-owned credential parser"
            return super().decrypt(reference)

    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    first = await slot.replace(
        store,
        authority,
        VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
    )
    pending = asyncio.create_task(
        slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(BlockingVault(scope.scope_id, scope.scope_digest)),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert first.owner.retired
        slot.retire(reason)
        release.set()
        with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
            await pending
        with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
            _ = slot.current
    finally:
        release.set()
        if not pending.done():
            await pending


async def test_cancelled_resolution_does_not_publish_or_override_the_next_resolution(tmp_path):
    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingVault(ScopeVault):
        def decrypt(self, reference):
            entered.set()
            try:
                assert release.wait(2), "release the test-owned credential parser"
                return super().decrypt(reference)
            finally:
                finished.set()

    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    first = await slot.replace(store, authority, resolver)
    pending = asyncio.create_task(
        slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(BlockingVault(scope.scope_id, scope.scope_digest)),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert first.owner.retired
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
            _ = slot.current
        newer = await slot.replace(store, authority, resolver)
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert slot.current is newer.owner and not newer.owner.retired
        newer.owner.assert_current()
    finally:
        release.set()
        if not pending.done():
            await pending


async def test_late_older_resolution_cannot_replace_a_new_same_scope_owner(tmp_path):
    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    entered, release = threading.Event(), threading.Event()

    class BlockingVault(ScopeVault):
        def decrypt(self, reference):
            entered.set()
            assert release.wait(2), "release the test-owned credential parser"
            return super().decrypt(reference)

    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    old = asyncio.create_task(
        slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(BlockingVault(scope.scope_id, scope.scope_digest)),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        newer = await slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
        release.set()
        with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired, match="superseded"):
            await old
        assert slot.current is newer.owner
    finally:
        release.set()
        if not old.done():
            await old


async def test_uid_drift_retires_the_owner_and_the_original_uid_cannot_revive_it(tmp_path):
    from custos.engines.nautilus.ledger_http import decode_exact_venue_response

    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    owner = (
        await okx_ledger_binding.OkxLedgerOwnerSlot().replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    first = decode_exact_venue_response(b'{"code":"0","data":[{"uid":"42","mainUid":"42"}]}')
    owner.observe_account(first)
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        owner.observe_account(
            decode_exact_venue_response(b'{"code":"0","data":[{"uid":"43","mainUid":"43"}]}')
        )
    assert owner.retired
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        owner.observe_account(first)


async def test_pause_retires_the_old_token_even_after_a_new_running_generation(tmp_path):
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from tests.test_okx_ledger_binding import signed_okx_command

    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    old = (
        await slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    paused = signed_okx_command(generation=2, state="paused")
    await store.record_desired_command(
        command=paused.command,
        command_fingerprint=paused.command_fingerprint,
        verification_receipt=paused.verification_receipt,
    )
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        old.assert_current()
    resumed = signed_okx_command(generation=3)
    await store.record_desired_command(
        command=resumed.command,
        command_fingerprint=resumed.command_fingerprint,
        verification_receipt=resumed.verification_receipt,
    )
    new = (
        await slot.replace(
            store,
            EngineLifecycleAuthority.from_verified_command(resumed),
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    new.assert_current()
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        old.assert_current()


@pytest.mark.parametrize("fault", ["wrong_digest", "wrong_scope_id", "resolution_failure"])
async def test_failed_new_resolution_does_not_restore_the_old_owner(tmp_path, fault):
    from uuid import uuid4

    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    old = (
        await slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner

    class FaultVault(ScopeVault):
        def decrypt(self, reference):
            if fault == "resolution_failure":
                raise OSError("fixture resolution failure")
            value = super().decrypt(reference)
            if fault == "wrong_digest":
                value["scope_digest"] = "b" * 64
            else:
                value["scope_id"] = str(uuid4())
            return value

    from custos.core.runner_command_runtime import (
        RunnerCredentialResolutionRejected,
        RunnerCredentialResolutionUnavailable,
    )

    expected = {
        "wrong_digest": (RunnerCredentialResolutionRejected, "signed scope digest"),
        "wrong_scope_id": (
            okx_ledger_binding.OkxLedgerBindingError,
            "resolved credential metadata differs",
        ),
        "resolution_failure": (RunnerCredentialResolutionUnavailable, "vault is unavailable"),
    }[fault]
    with pytest.raises(expected[0], match=expected[1]):
        await slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(FaultVault(scope.scope_id, scope.scope_digest)),
        )
    assert old.retired
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        _ = slot.current


async def test_real_store_watermark_rewind_with_unchanged_desired_and_file_identity_is_refused(
    tmp_path,
):
    import sqlite3
    from contextlib import closing
    from uuid import uuid4

    from tests.test_runner_fact_store import _runner_authority

    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    owner = (
        await okx_ledger_binding.OkxLedgerOwnerSlot().replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    backup = tmp_path / "before.sqlite3"
    with (
        closing(sqlite3.connect(outbox.path)) as source,
        closing(sqlite3.connect(backup)) as destination,
    ):
        source.backup(destination)
    before = outbox.platform_storage_snapshot()
    await outbox.enqueue(
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
    owner.assert_current()
    advanced = outbox.platform_storage_snapshot()
    assert advanced.mutation_seq > before.mutation_seq
    with (
        closing(sqlite3.connect(backup)) as source,
        closing(sqlite3.connect(outbox.path)) as destination,
    ):
        source.backup(destination)
    rewound = outbox.platform_storage_snapshot()
    assert rewound.file_identity == before.file_identity and rewound.store_id == before.store_id
    assert rewound.mutation_seq < advanced.mutation_seq
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired) as failure:
        owner.assert_current()
    assert "regressed" in str(failure.value.__cause__)


async def test_replacing_an_isolated_store_file_retires_the_old_owner(tmp_path):
    import os
    import sqlite3
    from contextlib import closing

    outbox, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    owner = (
        await okx_ledger_binding.OkxLedgerOwnerSlot().replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
        )
    ).owner
    before = outbox.platform_storage_snapshot()
    replacement = tmp_path / "replacement.sqlite3"
    with (
        closing(sqlite3.connect(outbox.path)) as source,
        closing(sqlite3.connect(replacement)) as destination,
    ):
        source.backup(destination)
    os.replace(replacement, outbox.path)
    assert outbox.path.stat().st_ino != before.file_identity[1]
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        owner.assert_current()
    assert owner.retired


async def test_unrecorded_signed_command_is_not_a_durable_source_authority(tmp_path):
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.platform_process_identity import PlatformFenceError
    from tests.test_okx_ledger_binding import signed_okx_command
    from tests.test_runner_fact_store import _runner_fact_store

    _, store = _runner_fact_store(tmp_path / "empty.sqlite3")
    verified = signed_okx_command()
    scope = verified.command.to_runtime_spec().credential_scope
    vault = ScopeVault(scope.scope_id, scope.scope_digest)
    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    with pytest.raises(PlatformFenceError, match="desired"):
        await slot.replace(
            store,
            EngineLifecycleAuthority.from_verified_command(verified),
            VaultRunnerCredentialResolverV1(vault),
        )
    assert vault.calls == []
    with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
        _ = slot.current


async def test_arbitrary_store_proxy_cannot_supply_a_context_or_scope(tmp_path):
    _, _, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    vault = ScopeVault(scope.scope_id, scope.scope_digest)
    with pytest.raises(okx_ledger_binding.OkxLedgerBindingError, match="actual RunnerStateStore"):
        await okx_ledger_binding.OkxLedgerOwnerSlot().replace(
            object(), authority, VaultRunnerCredentialResolverV1(vault)
        )
    assert vault.calls == []


async def test_signed_scope_generation_change_during_resolution_refuses_old_publication(tmp_path):
    from custos.core.platform_process_identity import PlatformFenceError
    from tests.test_okx_ledger_binding import signed_okx_command

    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    entered, release = threading.Event(), threading.Event()

    class BlockingVault(ScopeVault):
        def decrypt(self, reference):
            entered.set()
            assert release.wait(2), "release the test-owned credential parser"
            return super().decrypt(reference)

    slot = okx_ledger_binding.OkxLedgerOwnerSlot()
    pending = asyncio.create_task(
        slot.replace(
            store,
            authority,
            VaultRunnerCredentialResolverV1(BlockingVault(scope.scope_id, scope.scope_digest)),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        new = signed_okx_command(generation=2, scope_digest="b" * 64)
        await store.record_desired_command(
            command=new.command,
            command_fingerprint=new.command_fingerprint,
            verification_receipt=new.verification_receipt,
        )
        release.set()
        with pytest.raises(PlatformFenceError, match="stale"):
            await pending
        with pytest.raises(okx_ledger_binding.OkxLedgerOwnerRetired):
            _ = slot.current
    finally:
        release.set()
        if not pending.done():
            await pending
