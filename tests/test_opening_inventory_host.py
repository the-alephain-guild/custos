from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus import host as host_module
from custos.engines.nautilus import venue_sodex
from custos.engines.nautilus.ledger_http import ReadOnlyVenueHttp, decode_exact_venue_response
from tests.engines.nautilus.test_runner_safety_execution_boundary import _GatedStrategy
from tests.fixtures.fake_live_node import FakeLiveNodeType
from tests.fixtures.platform_opening import closed_platform_runtime
from tests.test_nt_trading_node_host import (
    _Artifact,
    _credential,
    _FactCapabilityReceipt,
    _FactEmitterStub,
    _sodex_spec,
    _StrategyDouble,
)


@pytest.fixture
async def signed_host(tmp_path, monkeypatch, request):
    monkeypatch.setattr(host_module, "LiveNode", FakeLiveNodeType)
    monkeypatch.setattr(venue_sodex, "validate_account_configuration", AsyncMock())
    connector = getattr(request, "param", "sodex_perpetual")
    if connector == "sodex_perpetual":
        spec = _sodex_spec(
            "opening-inventory", trading_mode="testnet", strategy_id=str(UUID(int=9))
        )
    else:
        from tests.test_nt_trading_node_host import _fact_spec

        spec = _fact_spec("opening-unsupported", connector, trading_mode="testnet")
    runtime = await closed_platform_runtime(tmp_path / "runner.sqlite3", spec)
    capability = _FactCapabilityReceipt()
    capability.runner_id = runtime.verified.command.runner_id
    host = host_module.NtTradingNodeHost(
        tenant_id="tenant-a",
        runner_id=str(runtime.verified.command.runner_id),
        runner_fact_emitter=_FactEmitterStub(),
        capability_receipt=capability,
        runner_safety_boundary_factory=runtime.factory,
    )

    class Strategy(_StrategyDouble, _GatedStrategy):
        pass

    strategy = Strategy(spec)
    strategy.submitted = []
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    authority = EngineLifecycleAuthority.from_verified_command(runtime.verified)
    requested = []

    def get(_http, path, params):
        suffix = path.rsplit("/", 1)[1]
        requested.append((path, params))
        data = (
            {"user": "0x" + "a" * 40, "aid": 4242}
            if suffix == "state"
            else {
                "blockHeight": 42,
                "blockTime": 1793760000000,
                suffix: [],
            }
        )
        return decode_exact_venue_response(json.dumps({"code": "0", "data": data}).encode())

    monkeypatch.setattr(ReadOnlyVenueHttp, "get_exact", get)
    try:
        yield SimpleNamespace(
            host=host, spec=spec, runtime=runtime, authority=authority, requested=requested
        )
    finally:
        await host.stop(spec["deployment_instance_id"])


async def test_host_reads_the_real_collector_without_writing_gate_or_facts(signed_host):
    subject = signed_host
    before = subject.runtime.store.load_opening_gate_state_sync(subject.authority)
    result = await subject.host.read_opening_account_inventory(subject.authority)

    assert result.scope.authority == subject.authority
    assert result.scope.wallet_address == "0x" + "a" * 40
    assert result.scope.account_id == 4242
    assert result.account_identity_matches is True
    assert result.inventory_complete is False
    assert len(subject.requested) == 4
    assert subject.runtime.store.load_opening_gate_state_sync(subject.authority) == before
    assert await subject.runtime.outbox.pending() == []


@pytest.mark.parametrize("lifecycle", ["running", "paused", "stopped"])
async def test_network_read_does_not_hold_a_write_lock_and_new_desired_retires_the_result(
    signed_host, monkeypatch, tmp_path, lifecycle
):
    import asyncio
    from threading import Event

    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    subject = signed_host
    started, resume = Event(), Event()
    original = ReadOnlyVenueHttp.get_exact

    def delayed(http, path, params):
        if path.endswith("/balances"):
            started.set()
            assert resume.wait(timeout=3)
        return original(http, path, params)

    monkeypatch.setattr(ReadOnlyVenueHttp, "get_exact", delayed)
    task = asyncio.create_task(subject.host.read_opening_account_inventory(subject.authority))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        spec = {**subject.spec, "generation": 2}
        newer = await closed_platform_runtime(
            tmp_path / "newer.sqlite3", spec, lifecycle_state=lifecycle
        )
        await asyncio.wait_for(
            subject.runtime.store.record_desired_command(
                command=newer.verified.command,
                command_fingerprint=newer.verified.command_fingerprint,
                verification_receipt=newer.verified.verification_receipt,
            ),
            timeout=1,
        )
    finally:
        resume.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert isinstance(result, InventoryUnavailable)
    assert result.inventory_complete is False
    assert await subject.runtime.outbox.pending() == []


async def test_local_stop_during_network_read_discards_provider_result(signed_host, monkeypatch):
    import asyncio
    from threading import Event

    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    subject = signed_host
    started, resume = Event(), Event()
    original = ReadOnlyVenueHttp.get_exact

    def delayed(http, path, params):
        started.set()
        assert resume.wait(timeout=3)
        return original(http, path, params)

    monkeypatch.setattr(ReadOnlyVenueHttp, "get_exact", delayed)
    task = asyncio.create_task(subject.host.read_opening_account_inventory(subject.authority))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await subject.host.stop(subject.spec["deployment_instance_id"])
    finally:
        resume.set()
    assert isinstance(await task, InventoryUnavailable)


@pytest.mark.parametrize(
    "field",
    [
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
        "trading_mode",
    ],
)
async def test_other_source_authority_is_rejected_before_any_read(signed_host, field):
    from dataclasses import replace

    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    values = {
        "deployment_instance_id": UUID(int=31),
        "deployment_spec_id": UUID(int=32),
        "deployment_spec_digest": "b" * 64,
        "generation": 2,
        "trading_mode": "live",
    }
    result = await signed_host.host.read_opening_account_inventory(
        replace(signed_host.authority, **{field: values[field]})
    )
    assert isinstance(result, InventoryUnavailable)
    assert signed_host.requested == []


async def test_inventory_from_another_wallet_is_not_reassigned_to_the_current_host(
    signed_host, monkeypatch
):
    from dataclasses import replace

    from custos.engines.nautilus.opening_inventory import InventoryUnavailable
    from custos.engines.nautilus.sodex_opening_inventory import SodexOpeningInventoryCollector

    original = SodexOpeningInventoryCollector.collect

    async def foreign(collector):
        result = await original(collector)
        return replace(result, scope=replace(result.scope, wallet_address="0x" + "b" * 40))

    monkeypatch.setattr(SodexOpeningInventoryCollector, "collect", foreign)
    assert isinstance(
        await signed_host.host.read_opening_account_inventory(signed_host.authority),
        InventoryUnavailable,
    )


async def test_failed_stop_attempt_also_retires_an_inflight_account_read(signed_host, monkeypatch):
    import asyncio
    from threading import Event

    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    subject = signed_host
    started, resume = Event(), Event()
    original = ReadOnlyVenueHttp.get_exact

    def delayed(http, path, params):
        started.set()
        assert resume.wait(timeout=3)
        return original(http, path, params)

    async def failed_shutdown(*_args):
        raise OSError("fixture cancellation failure")

    monkeypatch.setattr(ReadOnlyVenueHttp, "get_exact", delayed)
    task = asyncio.create_task(subject.host.read_opening_account_inventory(subject.authority))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        with monkeypatch.context() as stop_patch:
            stop_patch.setattr(subject.host, "_apply_shutdown_policy", failed_shutdown)
            with pytest.raises(OSError, match="fixture cancellation failure"):
                await subject.host.stop(subject.spec["deployment_instance_id"])
    finally:
        resume.set()
    assert isinstance(await task, InventoryUnavailable)


@pytest.mark.parametrize("signed_host", ["binance_perpetual"], indirect=True)
async def test_unsupported_venue_is_explicit_and_does_not_use_the_nt_cache(signed_host):
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    result = await signed_host.host.read_opening_account_inventory(signed_host.authority)
    assert isinstance(result, InventoryUnavailable)
    assert result.connector == "binance_perpetual"
    assert result.reason == "connector_inventory_unsupported"
    assert result.inventory_complete is False
    assert signed_host.requested == []


@pytest.mark.parametrize(
    ("field", "value"), [("_tenant_id", "foreign-tenant"), ("_runner_id", str(UUID(int=99)))]
)
async def test_changed_machine_identity_cannot_read_another_source(
    signed_host, monkeypatch, field, value
):
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    monkeypatch.setattr(signed_host.host, field, value)
    result = await signed_host.host.read_opening_account_inventory(signed_host.authority)
    assert isinstance(result, InventoryUnavailable)
    assert result.reason == "opening_source_identity_mismatch"
    assert signed_host.requested == []
