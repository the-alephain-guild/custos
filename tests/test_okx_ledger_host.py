"""Host source epochs fence ledger ownership independently of native admission."""

import asyncio
from types import SimpleNamespace

from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.ledger_http import decode_exact_venue_response
from tests.test_okx_ledger_binding import ScopeVault
from tests.test_okx_ledger_group import ledger_fixture


class Strategy:
    def __init__(self):
        self.events = []

    def on_order_event(self, event):
        self.events.append(event)

    def on_position_event(self, event):
        self.events.append(event)


async def host_ledger_fixture(path):
    fixture = await ledger_fixture(path)
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    fixture.host = NtTradingNodeHost()
    fixture.resolver = VaultRunnerCredentialResolverV1(
        ScopeVault(scope.scope_id, scope.scope_digest)
    )
    fixture.instance = str(fixture.engine_authority.deployment_instance_id)
    resolved = await fixture.host.replace_okx_ledger_owner(
        fixture.store, fixture.engine_authority, fixture.resolver
    )
    fixture.owner = resolved.owner
    fixture.slot = fixture.host._okx_ledger_slots[fixture.instance]
    fixture.account_binding = fixture.owner.observe_account(
        decode_exact_venue_response(b'{"code":"0","data":[{"uid":"42","mainUid":"42"}]}')
    )
    return fixture


async def test_host_replacement_retires_original_owner_and_retains_new_epoch(tmp_path):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    original = fixture.owner
    replacement = await fixture.host.replace_okx_ledger_owner(
        fixture.store, fixture.engine_authority, fixture.resolver
    )
    assert original.retired
    assert fixture.host._okx_ledger_slots[fixture.instance] is not fixture.slot
    assert fixture.host._okx_ledger_slots[fixture.instance].current is replacement.owner
    replacement.owner.assert_current()


async def test_original_stop_completion_preserves_source_registered_after_its_request(
    tmp_path, monkeypatch
):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_once = fixture.host._stop_at_boundary_once

    async def held_shutdown(instance):
        entered.set()
        await release.wait()
        return await original_once(instance)

    monkeypatch.setattr(fixture.host, "_stop_at_boundary_once", held_shutdown)
    stopping = asyncio.create_task(fixture.host.stop(fixture.instance))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        replacement = await fixture.host.replace_okx_ledger_owner(
            fixture.store, fixture.engine_authority, fixture.resolver
        )
        current_slot = fixture.host._okx_ledger_slots[fixture.instance]
        release.set()
        await asyncio.wait_for(stopping, 3)
        assert fixture.host._okx_ledger_slots[fixture.instance] is current_slot
        assert current_slot.current is replacement.owner
        replacement.owner.assert_current()
    finally:
        release.set()
        await asyncio.wait_for(stopping, 3)


async def test_old_host_forwarder_and_run_finally_preserve_replacement_source(tmp_path):
    fixture = await host_ledger_fixture(tmp_path / "runner.sqlite3")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_async():
        entered.set()
        await release.wait()
        return "original run ended"

    def fail_sink(_instance, _event):
        raise RuntimeError("injected old source sink failure")

    strategy = Strategy()
    fixture.host.add_observation_sinks(order=fail_sink, position=lambda *_: None)
    fixture.host._attach_runtime_bridges(
        fixture.instance, strategy, object(), None, okx_ledger_slot=fixture.slot
    )
    original_run = asyncio.create_task(
        fixture.host._run_node_for_opening(
            fixture.instance, SimpleNamespace(run_async=run_async), fixture.slot
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        replacement = await fixture.host.replace_okx_ledger_owner(
            fixture.store, fixture.engine_authority, fixture.resolver
        )
        current_slot = fixture.host._okx_ledger_slots[fixture.instance]
        strategy.on_order_event("late original event")
        assert strategy.events == ["late original event"]
        assert fixture.instance not in fixture.host._event_forwarding_failures
        release.set()
        assert await asyncio.wait_for(original_run, 3) == "original run ended"
        assert fixture.owner.retired
        assert fixture.host._okx_ledger_slots[fixture.instance] is current_slot
        assert current_slot.current is replacement.owner
        replacement.owner.assert_current()
    finally:
        release.set()
        if not original_run.done():
            await asyncio.wait_for(original_run, 3)
