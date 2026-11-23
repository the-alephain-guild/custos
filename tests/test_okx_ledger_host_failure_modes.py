"""Host lifecycle retires the original ledger resolution before it can publish."""

import asyncio
import threading

import pytest

from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.core.okx_ledger_binding import OkxLedgerOwnerRetired
from custos.engines.nautilus.host import NtTradingNodeHost
from tests.test_okx_ledger_binding import ScopeVault, running_store
from tests.test_okx_ledger_host import Strategy, host_ledger_fixture


@pytest.mark.parametrize("operation", ["stop", "stop_at_boundary", "close"])
async def test_host_stop_and_close_retire_resolution_without_an_active_node(tmp_path, operation):
    _, store, authority, verified = await running_store(tmp_path / "runner.sqlite3")
    scope = verified.command.to_runtime_spec().credential_scope
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    resolving = asyncio.Event()
    release = asyncio.Event()

    class PendingResolver:
        async def resolve(self, desired, credential_scope):
            resolving.set()
            await release.wait()
            return await resolver.resolve(desired, credential_scope)

    host = NtTradingNodeHost()
    replace_owner = host.replace_okx_ledger_owner
    pending = asyncio.create_task(replace_owner(store, authority, PendingResolver()))
    try:
        await asyncio.wait_for(resolving.wait(), 3)
        assert not host._active_nodes
        if operation == "close":
            await host.close()
        else:
            await getattr(host, operation)(str(authority.deployment_instance_id))
        release.set()
        with pytest.raises(OkxLedgerOwnerRetired):
            await asyncio.wait_for(pending, 3)
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)


@pytest.mark.parametrize("operation", ["stop", "stop_at_boundary", "close"])
async def test_each_stop_request_retires_new_pending_source_while_shutdown_is_single_flight(
    tmp_path, monkeypatch, operation
):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    stopping = asyncio.Event()
    release_stop = asyncio.Event()
    resolving = asyncio.Event()
    release_resolver = asyncio.Event()
    repeated_request = asyncio.Event()
    original_once = fixture.host._stop_at_boundary_once
    original_boundary = fixture.host.stop_at_boundary
    shutdown_calls = 0
    stop_requests = 0

    async def held_shutdown(instance):
        nonlocal shutdown_calls
        shutdown_calls += 1
        stopping.set()
        await release_stop.wait()
        return await original_once(instance)

    async def observed_boundary(instance):
        nonlocal stop_requests
        stop_requests += 1
        if stop_requests == 2:
            repeated_request.set()
        return await original_boundary(instance)

    class Resolver:
        async def resolve(self, desired, scope):
            resolving.set()
            await release_resolver.wait()
            return await fixture.resolver.resolve(desired, scope)

    monkeypatch.setattr(fixture.host, "_stop_at_boundary_once", held_shutdown)
    monkeypatch.setattr(fixture.host, "stop_at_boundary", observed_boundary)
    first = asyncio.create_task(fixture.host.stop(fixture.instance))
    tasks = [first]
    try:
        await asyncio.wait_for(stopping.wait(), 3)
        pending = asyncio.create_task(
            fixture.host.replace_okx_ledger_owner(
                fixture.store, fixture.engine_authority, Resolver()
            )
        )
        tasks.append(pending)
        await asyncio.wait_for(resolving.wait(), 3)
        assert not fixture.host._active_nodes
        repeated = asyncio.create_task(
            fixture.host.close()
            if operation == "close"
            else getattr(fixture.host, operation)(fixture.instance)
        )
        tasks.append(repeated)
        await asyncio.wait_for(repeated_request.wait(), 3)
        release_stop.set()
        await asyncio.wait_for(asyncio.gather(first, repeated), 3)
        assert shutdown_calls == 1
        release_resolver.set()
        with pytest.raises(OkxLedgerOwnerRetired):
            await asyncio.wait_for(pending, 3)
        assert fixture.instance not in fixture.host._okx_ledger_slots
    finally:
        release_stop.set()
        release_resolver.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


@pytest.mark.parametrize("pending_replacement", [False, True])
async def test_original_callback_cannot_clear_a_slots_new_owner_or_pending_epoch(
    tmp_path, pending_replacement
):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    entered = asyncio.Event()
    release = asyncio.Event()

    class Resolver:
        async def resolve(self, desired, scope):
            entered.set()
            await release.wait()
            return await fixture.resolver.resolve(desired, scope)

    replacement = asyncio.create_task(
        fixture.slot.replace(fixture.store, fixture.engine_authority, Resolver())
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        epoch = fixture.slot._resolution_epoch
        assert epoch is not None
        if not pending_replacement:
            release.set()
            new_owner = (await asyncio.wait_for(replacement, 3)).owner
        with fixture.store.opening_control_action(fixture.engine_authority):
            fixture.slot.retire_from_callback(fixture.owner, "failed")
        if pending_replacement:
            assert fixture.slot._resolution_epoch == epoch
            release.set()
            new_owner = (await asyncio.wait_for(replacement, 3)).owner
        assert fixture.slot.current is new_owner
        new_owner.assert_current()
    finally:
        release.set()
        if not replacement.done():
            await asyncio.wait_for(asyncio.gather(replacement, return_exceptions=True), 3)


async def test_real_host_forwarder_invalidates_waiting_group_inside_control_writer(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from custos.core import runner_fact

    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    original_connect = runner_fact.sqlite3.connect

    def bounded_connect(*args, **kwargs):
        # A broken callback lock order must release its worker after SQLite's timeout.
        return original_connect(*args, **(kwargs | {"timeout": 0.5}))

    monkeypatch.setattr(runner_fact.sqlite3, "connect", bounded_connect)

    def group_counts():
        connection = fixture.outbox._connect()
        try:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "runner_fact_stream",
                    "runner_fact_atomic_group",
                    "runner_fact_seen_event",
                    "runner_fact_outbox",
                )
            )
        finally:
            connection.close()

    before_rows = group_counts()
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    before = fixture.outbox.platform_storage_snapshot()
    writer_held = threading.Event()
    group_waiting = threading.Event()
    callback_returned = threading.Event()
    invalidated_before_release = []
    strategy = Strategy()

    def fail_sink(_instance, _event):
        raise RuntimeError("injected Host observation sink failure")

    fixture.host.add_observation_sinks(order=fail_sink, position=lambda *_: None)
    fixture.host._attach_runtime_bridges(
        fixture.instance, strategy, object(), None, okx_ledger_slot=fixture.slot
    )
    original_slot_lock = fixture.slot._lock

    class OutsideControlWriterLock:
        def __enter__(self):
            connection = fixture.outbox._opening_control.connection
            assert connection is None or not connection.in_transaction, (
                "Host callback must not take its slot inside the control writer"
            )
            return original_slot_lock.__enter__()

        def __exit__(self, *args):
            return original_slot_lock.__exit__(*args)

    monkeypatch.setattr(fixture.slot, "_lock", OutsideControlWriterLock())
    original = fixture.outbox._platform_storage_read

    @contextmanager
    def observed_writer(*, write=False):
        if write:
            group_waiting.set()
        with original(write=write) as value:
            yield value

    monkeypatch.setattr(fixture.outbox, "_platform_storage_read", observed_writer)

    def control_callback():
        with fixture.store.opening_control_action(fixture.engine_authority):
            writer_held.set()
            assert group_waiting.wait(3)
            strategy.on_order_event("real Host callback")
            assert strategy.events == ["real Host callback"]
            invalidated_before_release.append(fixture.owner._invalidated.is_set())
            callback_returned.set()

    writer = asyncio.create_task(asyncio.to_thread(control_callback))
    assert await asyncio.to_thread(writer_held.wait, 3)
    try:
        with pytest.raises(OkxLedgerOwnerRetired):
            await asyncio.wait_for(fixture.emitter.emit_ledger_group(fixture.authority, capture), 4)
        assert callback_returned.is_set()
        assert invalidated_before_release == [True]
    finally:
        group_waiting.set()
        await asyncio.wait_for(writer, 4)
    assert fixture.instance in fixture.host._event_forwarding_failures
    assert await fixture.outbox.pending() == []
    assert fixture.outbox.platform_storage_snapshot().mutation_seq == before.mutation_seq
    assert group_counts() == before_rows
    assert fixture.owner.retired


async def test_real_host_stop_waits_for_group_commit_then_rejects_original_capture(
    tmp_path, monkeypatch
):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    final_checked = threading.Event()
    stop_started = threading.Event()
    stop_done = threading.Event()
    original = fixture.store.require_ledger_context_on
    checks = 0

    def final_guard(*args, **kwargs):
        nonlocal checks
        current = original(*args, **kwargs)
        checks += 1
        if checks == 2:
            final_checked.set()
            assert stop_started.wait(3)
            assert not stop_done.wait(0.1), "Host stop must wait for the held owner COMMIT"
        return current

    monkeypatch.setattr(fixture.store, "require_ledger_context_on", final_guard)

    def ordinary_stop():
        assert final_checked.wait(3)
        stop_started.set()
        asyncio.run(fixture.host.stop(fixture.instance))
        stop_done.set()

    stopping = asyncio.create_task(asyncio.to_thread(ordinary_stop))
    try:
        ids = await asyncio.wait_for(
            fixture.emitter.emit_ledger_group(fixture.authority, capture), 4
        )
        assert len(ids) == 3
    finally:
        final_checked.set()
        stop_started.set()
        await asyncio.wait_for(stopping, 4)
    assert stop_done.is_set() and fixture.owner.retired
    assert fixture.instance not in fixture.host._okx_ledger_slots
    assert len(await fixture.outbox.pending()) == 3
    with pytest.raises(OkxLedgerOwnerRetired):
        await fixture.emitter.emit_ledger_group(fixture.authority, capture)


@pytest.mark.parametrize("finish", ["cancel", "fail", "replace"])
async def test_pending_resolution_exit_preserves_only_its_own_host_slot(tmp_path, finish):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    entered = asyncio.Event()
    release = asyncio.Event()

    class Resolver:
        async def resolve(self, desired, scope):
            entered.set()
            await release.wait()
            if finish == "fail":
                raise RuntimeError("injected resolution failure")
            return await fixture.resolver.resolve(desired, scope)

    pending = asyncio.create_task(
        fixture.host.replace_okx_ledger_owner(fixture.store, fixture.engine_authority, Resolver())
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if finish == "replace":
            new = await fixture.host.replace_okx_ledger_owner(
                fixture.store, fixture.engine_authority, fixture.resolver
            )
            new_slot = fixture.host._okx_ledger_slots[fixture.instance]
            release.set()
            with pytest.raises(OkxLedgerOwnerRetired):
                await asyncio.wait_for(pending, 3)
            assert fixture.host._okx_ledger_slots[fixture.instance] is new_slot
            assert new_slot.current is new.owner
            new.owner.assert_current()
        else:
            if finish == "cancel":
                pending.cancel()
                expected = asyncio.CancelledError
            else:
                release.set()
                expected = RuntimeError
            with pytest.raises(expected):
                await asyncio.wait_for(pending, 3)
            assert fixture.instance not in fixture.host._okx_ledger_slots
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)


async def test_real_host_deploy_startup_failure_retires_its_captured_source(tmp_path, monkeypatch):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")

    def unavailable():
        raise RuntimeError("injected unavailable native engine")

    monkeypatch.setattr(fixture.host, "_ensure_nt_available", unavailable)
    with pytest.raises(RuntimeError, match="unavailable native"):
        await fixture.host.deploy(
            fixture.verified.command.to_runtime_spec().model_dump(mode="python"), {}, object()
        )
    assert fixture.owner.retired
    assert fixture.instance not in fixture.host._okx_ledger_slots
