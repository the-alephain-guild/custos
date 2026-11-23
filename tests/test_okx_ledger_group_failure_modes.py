"""Ledger retirement never waits for an owner while its Store writer is held."""

import asyncio
import json
import threading

import pytest

from custos.core.okx_ledger_binding import OkxLedgerOwnerRetired
from tests.test_okx_ledger_group import ledger_fixture


async def test_control_callback_invalidates_original_owner_without_waiting_for_its_lock(tmp_path):
    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    held = threading.Event()
    release = threading.Event()

    def hold_owner():
        with fixture.owner._lock:
            held.set()
            assert release.wait(3)

    worker = asyncio.create_task(asyncio.to_thread(hold_owner))
    assert await asyncio.to_thread(held.wait, 3)
    try:
        with fixture.store.opening_control_action(fixture.engine_authority):
            fixture.owner.retire_from_callback("failed")
            assert fixture.owner._invalidated.is_set()
            release.set()
    finally:
        release.set()
        await worker
    with pytest.raises(OkxLedgerOwnerRetired):
        fixture.owner.assert_current()


async def test_second_batch_failure_rolls_back_sequence_and_owner_floor_then_same_owner_retries(
    tmp_path, monkeypatch
):
    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    before = fixture.outbox.platform_storage_snapshot()
    original = fixture.outbox._enqueue_in_transaction
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second batch failure")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fixture.outbox, "_enqueue_in_transaction", fail_second)
        with pytest.raises(RuntimeError, match="injected second"):
            await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    assert calls == 2
    assert await fixture.outbox.pending() == []
    assert fixture.outbox.platform_storage_snapshot().mutation_seq == before.mutation_seq
    assert fixture.owner._minimum_seq == before.mutation_seq
    fixture.owner.assert_current()
    await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    assert [
        json.loads(item.payload)["source_seq_start"] for item in await fixture.outbox.pending()
    ] == [1, 2, 3]


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other"),
        ("trading_mode", "live"),
        ("runner_id", "90000000-0000-4000-8000-000000000001"),
        ("deployment_instance_id", "90000000-0000-4000-8000-000000000002"),
        ("deployment_spec_id", "90000000-0000-4000-8000-000000000003"),
        ("deployment_spec_digest", "f" * 64),
        ("generation", 2),
        ("strategy_id", "90000000-0000-4000-8000-000000000004"),
        ("capability_version_id", "90000000-0000-4000-8000-000000000005"),
        ("capability_version", 2),
        ("capability_manifest_digest", "f" * 64),
    ],
)
async def test_writer_rejects_each_changed_authority_field_before_allocating_sequence(
    tmp_path, field, value
):
    from dataclasses import replace
    from uuid import UUID

    from custos.core.okx_ledger_binding import OkxLedgerBindingError

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    if field.endswith("_id") and field != "tenant_id":
        value = UUID(value)
    wrong = replace(fixture.authority, **{field: value})
    with pytest.raises(OkxLedgerBindingError):
        await fixture.emitter.emit_ledger_group(wrong, capture)
    assert await fixture.outbox.pending() == []


@pytest.mark.parametrize("other_instance", [False, True])
async def test_real_control_writer_failure_invalidates_group_waiting_with_owner_held(
    tmp_path, monkeypatch, other_instance
):
    from contextlib import contextmanager
    from uuid import uuid4

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder
    from tests.test_runner_fact_store import _verified_command

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    control_authority = fixture.engine_authority
    if other_instance:

        def change(event):
            event["event_id"] = str(uuid4())
            event["payload"]["deployment_instance_id"] = str(uuid4())
            event["aggregate_id"] = event["payload"]["deployment_instance_id"]
            event["event_type"] = (
                f"DeploymentInstanceDesiredStateChanged.{event['payload']['runner_id']}.{event['payload']['deployment_instance_id']}"
            )

        _, _, verified = _verified_command(mutate_event=change)
        await fixture.store.record_desired_command(
            command=verified.command,
            command_fingerprint=verified.command_fingerprint,
            verification_receipt=verified.verification_receipt,
        )
        control_authority = EngineLifecycleAuthority.from_verified_command(verified)
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    writer_held = threading.Event()
    group_waiting = threading.Event()
    callback_done = threading.Event()
    handler_called = threading.Event()
    original = fixture.outbox._platform_storage_read

    @contextmanager
    def observed_writer(*, write=False):
        if write:
            group_waiting.set()
        with original(write=write) as value:
            yield value

    monkeypatch.setattr(fixture.outbox, "_platform_storage_read", observed_writer)

    class Strategy:
        def on_order_event(self, event):
            handler_called.set()

        def on_position_event(self, event):
            pass

    def fail_sink(_event):
        raise RuntimeError("injected forwarding sink failure")

    def control_writer():
        strategy = Strategy()
        bridge = StrategyEventForwarder(
            deployment_instance_id=str(control_authority.deployment_instance_id),
            on_sink_failure=lambda *_: fixture.owner.retire_from_callback("failed"),
        )
        bridge.add_order_sink("fixture", fail_sink)
        bridge.install(strategy)
        with fixture.store.opening_control_action(control_authority):
            writer_held.set()
            assert group_waiting.wait(3)
            strategy.on_order_event(object())
            assert handler_called.is_set()
            callback_done.set()

    writer = asyncio.create_task(asyncio.to_thread(control_writer))
    assert await asyncio.to_thread(writer_held.wait, 3)
    try:
        with pytest.raises(OkxLedgerOwnerRetired):
            await asyncio.wait_for(fixture.emitter.emit_ledger_group(fixture.authority, capture), 4)
        assert callback_done.is_set()
    finally:
        group_waiting.set()
        await asyncio.wait_for(writer, 4)
    assert (
        fixture.store.platform_process_snapshot(
            fixture.engine_authority
        ).context.command_fingerprint
        == fixture.verified.command_fingerprint
    )
    assert await fixture.outbox.pending() == []


async def test_ordinary_retirement_waits_for_committing_group_then_rejects_old_capture(
    tmp_path, monkeypatch
):
    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    final_check = threading.Event()
    retirement_started = threading.Event()
    retirement_done = threading.Event()
    original = fixture.store.require_ledger_context_on
    checks = 0

    def checked(*args, **kwargs):
        nonlocal checks
        value = original(*args, **kwargs)
        checks += 1
        if checks == 2:
            final_check.set()
            assert retirement_started.wait(3)
            assert not retirement_done.is_set()
        return value

    monkeypatch.setattr(fixture.store, "require_ledger_context_on", checked)

    def retire():
        assert final_check.wait(3)
        retirement_started.set()
        fixture.owner.retire("stopped")
        retirement_done.set()

    retiring = asyncio.create_task(asyncio.to_thread(retire))
    ids = await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    await asyncio.wait_for(retiring, 4)
    assert len(ids) == 3 and retirement_done.is_set()
    assert len(await fixture.outbox.pending()) == 3
    with pytest.raises(OkxLedgerOwnerRetired):
        await fixture.emitter.emit_ledger_group(fixture.authority, capture)


async def test_final_guard_failure_rolls_back_provisional_floor_and_group_then_retries(
    tmp_path, monkeypatch
):
    from custos.core.okx_ledger_binding import OkxLedgerBindingError, OkxLedgerObservationOwner

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    before = fixture.outbox.platform_storage_snapshot()
    original = OkxLedgerObservationOwner.require_capture_on
    checks = 0

    def reject_final(owner, authority, frozen, snapshot):
        nonlocal checks
        original(owner, authority, frozen, snapshot)
        checks += 1
        if checks == 2:
            assert snapshot.storage.mutation_seq > before.mutation_seq
            assert owner._minimum_seq == before.mutation_seq
            raise OkxLedgerBindingError("injected final guard refusal")

    with monkeypatch.context() as patch:
        patch.setattr(OkxLedgerObservationOwner, "require_capture_on", reject_final)
        with pytest.raises(OkxLedgerBindingError, match="final guard"):
            await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    assert fixture.outbox.platform_storage_snapshot().mutation_seq == before.mutation_seq
    assert fixture.owner._minimum_seq == before.mutation_seq
    assert await fixture.outbox.pending() == []
    await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    assert len(await fixture.outbox.pending()) == 3
    assert fixture.owner._minimum_seq == fixture.outbox.platform_storage_snapshot().mutation_seq


async def test_signed_pause_rejects_old_capture_even_without_explicit_owner_retirement(tmp_path):
    from custos.core.platform_process_identity import PlatformFenceError
    from tests.test_okx_ledger_binding import signed_okx_command

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    paused = signed_okx_command(generation=2, state="paused")
    await fixture.store.record_desired_command(
        command=paused.command,
        command_fingerprint=paused.command_fingerprint,
        verification_receipt=paused.verification_receipt,
    )
    with pytest.raises(PlatformFenceError):
        await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    assert await fixture.outbox.pending() == []


@pytest.mark.parametrize(
    "field,value", [("uid", "43"), ("scope_digest", "e" * 64), ("owner_epoch", None)]
)
async def test_freeze_rejects_account_binding_from_another_uid_scope_or_epoch(
    tmp_path, field, value
):
    from dataclasses import replace
    from uuid import uuid4

    from custos.core.okx_ledger_binding import OkxLedgerBindingError

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    wrong = replace(fixture.account_binding, **{field: value or uuid4()})
    with pytest.raises(OkxLedgerBindingError):
        fixture.owner.freeze_group(
            fixture.authority, wrong, fixture.raw.content_digest, fixture.batches
        )
    assert await fixture.outbox.pending() == []


@pytest.mark.parametrize("finish", ["commit", "rollback"])
async def test_ended_control_transaction_uses_ordinary_retirement_until_group_commit(
    tmp_path, monkeypatch, finish
):
    from custos.core.okx_ledger_binding import OkxLedgerObservationOwner

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    control_ended = threading.Event()
    final_checked = threading.Event()
    callback_started = threading.Event()
    callback_returned = threading.Event()
    original = OkxLedgerObservationOwner.require_capture_on
    checks = 0

    def held_final_check(owner, authority, frozen, snapshot):
        nonlocal checks
        original(owner, authority, frozen, snapshot)
        checks += 1
        if checks == 2:
            final_checked.set()
            assert callback_started.wait(3)
            assert not callback_returned.wait(0.1), (
                "no writer: retirement must wait for group COMMIT"
            )

    monkeypatch.setattr(OkxLedgerObservationOwner, "require_capture_on", held_final_check)

    def control_callback():
        with fixture.store.opening_control_action(fixture.engine_authority) as connection:
            getattr(connection, finish)()
            assert not connection.in_transaction
            control_ended.set()
            assert final_checked.wait(3)
            callback_started.set()
            fixture.owner.retire_from_callback("failed")
            callback_returned.set()

    control = asyncio.create_task(asyncio.to_thread(control_callback))
    assert await asyncio.to_thread(control_ended.wait, 3)
    try:
        ids = await asyncio.wait_for(
            fixture.emitter.emit_ledger_group(fixture.authority, capture), 4
        )
        assert len(ids) == 3
    finally:
        final_checked.set()
        await asyncio.wait_for(control, 4)
    assert callback_returned.is_set() and fixture.owner.retired
    assert len(await fixture.outbox.pending()) == 3


async def test_closed_control_connection_does_not_turn_callback_into_a_deferred_writer(tmp_path):
    import sqlite3

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        with fixture.store.opening_control_action(fixture.engine_authority) as connection:
            connection.close()
            fixture.owner.retire_from_callback("failed")
            assert fixture.owner._retired == "failed"
    assert fixture.owner.retired
