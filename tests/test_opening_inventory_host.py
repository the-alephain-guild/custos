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
    options = getattr(request, "param", "sodex_perpetual")
    connector = (
        options.get("connector", "sodex_perpetual") if isinstance(options, dict) else options
    )
    lifecycle = options.get("lifecycle", "running") if isinstance(options, dict) else "running"
    if connector == "sodex_perpetual":
        spec = _sodex_spec(
            "opening-inventory", trading_mode="testnet", strategy_id=str(UUID(int=9))
        )
    else:
        from tests.test_nt_trading_node_host import _fact_spec

        spec = _fact_spec("opening-unsupported", connector, trading_mode="testnet")
    runtime = await closed_platform_runtime(
        tmp_path / "runner.sqlite3", spec, lifecycle_state=lifecycle
    )
    capability = _FactCapabilityReceipt()
    capability.runner_id = runtime.verified.command.runner_id
    from custos.core.runner_fact import runner_fact_capability_shape

    capability.capability_manifest = {"runner_fact_contracts": runner_fact_capability_shape()[1]}
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


async def test_signed_host_inventory_can_enter_the_real_risk_production_loop(
    signed_host, monkeypatch
):
    from decimal import Decimal

    from custos.core.runner_fact_producer import RunnerFactProductionLoop
    from tests.test_runner_fact_production_loop import _CapturingEmitter

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    monkeypatch.setattr(
        subject.host, "runner_fact_risk_snapshot", AsyncMock(return_value=(Decimal("1234.5"), ()))
    )
    monkeypatch.setattr(subject.host, "get_engine_status", AsyncMock(return_value=None))
    monkeypatch.setattr(subject.host, "deployment_ready", AsyncMock(return_value=True))
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=subject.host,
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    await loop._emit_observability(deployment)
    initial = next(f for f in emitter.emissions[0][1] if f["kind"] == "equity_snapshot")
    assert initial["account_inventory"]["status"] == "unavailable"
    import asyncio

    await asyncio.gather(*(entry[1] for entry in subject.host._opening_fact_reads.values()))
    await loop._emit_observability(deployment)
    assert len(emitter.emissions) == 2
    fact = next(f for f in emitter.emissions[1][1] if f["kind"] == "equity_snapshot")
    assert fact["amount"] == "1234.5"
    assert fact["trading_gate"] == "awaiting_release"
    inventory = fact["account_inventory"]
    assert inventory["scope"]["source"]["account_id"] == 4242
    assert inventory["inventory_complete"] is False
    assert inventory["equity_binding"]["status"] == "unproven"


@pytest.mark.parametrize(
    "signed_host", [{"lifecycle": "running"}, {"lifecycle": "paused"}], indirect=True
)
async def test_inventory_failure_keeps_current_original_risk_facts(signed_host, monkeypatch):
    from decimal import Decimal

    from custos.core.runner_fact_producer import RunnerFactProductionLoop
    from tests.test_runner_fact_production_loop import _CapturingEmitter

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    monkeypatch.setattr(
        subject.host, "runner_fact_risk_snapshot", AsyncMock(return_value=(Decimal("1234.5"), ()))
    )
    monkeypatch.setattr(subject.host, "get_engine_status", AsyncMock(return_value=None))
    monkeypatch.setattr(subject.host, "deployment_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        subject.host,
        "runner_fact_opening_snapshot",
        AsyncMock(side_effect=RuntimeError("inventory unavailable")),
    )
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=subject.host,
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    await loop._emit_observability(deployment)
    facts = emitter.emissions[0][1]
    equity = next((f for f in facts if f["kind"] == "equity_snapshot"), None)
    assert equity is not None
    assert equity["amount"] == "1234.5"
    assert equity["account_inventory"]["scope"]["connector"] == subject.spec["connector"]
    assert equity["account_inventory"]["status"] == "unavailable"
    assert any(f["kind"] == "position_snapshot" for f in facts)


async def test_inventory_failure_cannot_fallback_after_durable_authority_retires(
    signed_host, monkeypatch, tmp_path
):
    from decimal import Decimal

    from custos.core.runner_fact_producer import RunnerFactProductionLoop
    from tests.test_runner_fact_production_loop import _CapturingEmitter

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    monkeypatch.setattr(
        subject.host, "runner_fact_risk_snapshot", AsyncMock(return_value=(Decimal("1234.5"), ()))
    )
    monkeypatch.setattr(subject.host, "get_engine_status", AsyncMock(return_value=None))
    monkeypatch.setattr(subject.host, "deployment_ready", AsyncMock(return_value=True))

    async def retire(_instance):
        newer = await closed_platform_runtime(
            tmp_path / "newer.sqlite3", {**subject.spec, "generation": 2}, lifecycle_state="stopped"
        )
        await subject.runtime.store.record_desired_command(
            command=newer.verified.command,
            command_fingerprint=newer.verified.command_fingerprint,
            verification_receipt=newer.verified.verification_receipt,
        )
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(subject.host, "runner_fact_opening_snapshot", retire)
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=subject.host,
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    await loop._emit_observability(deployment)
    assert emitter.emissions == []


async def test_unfinished_inventory_never_blocks_risk_ticks_or_duplicates_reads(
    signed_host, monkeypatch
):
    import asyncio
    from decimal import Decimal

    from custos.core.runner_fact_producer import RunnerFactProductionLoop
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable
    from tests.test_runner_fact_production_loop import _CapturingEmitter

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    monkeypatch.setattr(
        subject.host, "runner_fact_risk_snapshot", AsyncMock(return_value=(Decimal("1234.5"), ()))
    )
    monkeypatch.setattr(subject.host, "get_engine_status", AsyncMock(return_value=None))
    monkeypatch.setattr(subject.host, "deployment_ready", AsyncMock(return_value=True))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_read(authority):
        calls.append(authority)
        started.set()
        await release.wait()
        return InventoryUnavailable(authority, subject.spec["connector"], "test_unavailable")

    monkeypatch.setattr(subject.host, "read_opening_account_inventory", slow_read)
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=subject.host,
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    first = asyncio.create_task(loop._emit_observability(deployment))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(asyncio.shield(first), timeout=0.5)
        for _ in range(3):
            await asyncio.wait_for(loop._emit_observability(deployment), timeout=0.5)
        assert len(calls) == 1
        assert len(emitter.emissions) == 4
        assert all(
            next(f for f in facts if f["kind"] == "equity_snapshot")["amount"] == "1234.5"
            for _, facts in emitter.emissions
        )
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        pending = getattr(subject.host, "_opening_fact_reads", {}).values()
        await asyncio.gather(*(entry[1] for entry in pending), return_exceptions=True)


async def test_observability_rechecks_desired_inside_the_signing_transaction(signed_host, tmp_path):
    from datetime import UTC, datetime

    from custos.core.opening_inventory_contract import unavailable_opening_inventory
    from custos.core.runner_fact import (
        RunnerFactEmitter,
        RunnerStateAuthorityError,
        equity_snapshot,
    )
    from tests.test_runner_fact_store import _identity

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    token = await subject.host.runner_fact_observation_token(deployment.authority)
    fact = equity_snapshot(
        event_id=UUID(int=900),
        amount="1234.5",
        currency=deployment.currency,
        observed_at=datetime.now(UTC),
        trading_gate="awaiting_release",
        account_inventory=unavailable_opening_inventory(
            deployment.authority, subject.spec["connector"]
        ),
    )
    newer = await closed_platform_runtime(
        tmp_path / "newer.sqlite3", {**subject.spec, "generation": 2}, lifecycle_state="stopped"
    )
    await subject.runtime.store.record_desired_command(
        command=newer.verified.command,
        command_fingerprint=newer.verified.command_fingerprint,
        verification_receipt=newer.verified.verification_receipt,
    )
    emitter = RunnerFactEmitter(subject.runtime.outbox, _identity(), lambda: None)
    with pytest.raises(RunnerStateAuthorityError):
        await emitter.emit_observability(deployment.authority, [fact], token.command_fingerprint)
    assert await subject.runtime.outbox.pending() == []


@pytest.mark.parametrize(
    "signed_host", [{"lifecycle": "running"}, {"lifecycle": "paused"}], indirect=True
)
async def test_current_platform_observation_signs_under_the_same_desired_fence(signed_host):
    import json
    from datetime import UTC, datetime

    from custos.core.opening_inventory_contract import unavailable_opening_inventory
    from custos.core.runner_fact import RunnerFactEmitter, equity_snapshot
    from tests.test_runner_fact_store import _identity

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    token = await subject.host.runner_fact_observation_token(deployment.authority)
    assert token.source_kind == "platform"
    fact = equity_snapshot(
        event_id=UUID(int=901),
        amount="1234.5",
        currency=deployment.currency,
        observed_at=datetime.now(UTC),
        trading_gate="awaiting_release",
        account_inventory=unavailable_opening_inventory(deployment.authority, deployment.connector),
    )
    emitter = RunnerFactEmitter(subject.runtime.outbox, _identity(), lambda: None)
    await emitter.emit_observability(deployment.authority, [fact], token.command_fingerprint)
    pending = await subject.runtime.outbox.pending()
    assert json.loads(pending[0].payload)["facts"][0]["amount"] == "1234.5"


def test_platform_observation_cannot_downgrade_a_missing_fingerprint_to_simulation():
    from custos.core.runner_fact_producer import (
        RunnerFactObservationRetired,
        RunnerFactObservationToken,
    )

    with pytest.raises(RunnerFactObservationRetired):
        RunnerFactObservationToken(None, 1, 0)


async def test_one_pending_venue_census_does_not_block_another_deployments_risk(
    signed_host, monkeypatch, tmp_path
):
    import asyncio
    from dataclasses import replace
    from decimal import Decimal

    from custos.core.execution_reservation_store import PlatformOpeningGate
    from custos.core.opening_inventory_contract import unavailable_opening_inventory
    from custos.core.runner_fact_producer import (
        RunnerFactObservationToken,
        RunnerFactProductionLoop,
    )
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable
    from tests.test_runner_fact_production_loop import _CapturingEmitter

    subject = signed_host
    first = subject.host.runner_fact_deployments()[0]
    second_spec = _sodex_spec(
        "second-risk-observer", trading_mode="testnet", strategy_id=str(UUID(int=19))
    )
    second_runtime = await closed_platform_runtime(tmp_path / "second.sqlite3", second_spec)
    second_authority = replace(
        first.authority,
        deployment_instance_id=UUID(second_spec["deployment_instance_id"]),
        deployment_spec_id=UUID(second_spec["deployment_spec_id"]),
        deployment_spec_digest=second_spec["deployment_spec_digest"],
        strategy_id=UUID(int=19),
    )
    second = replace(
        first,
        authority=second_authority,
        deployment_instance_id=str(second_authority.deployment_instance_id),
        deployment_spec_id=str(second_authority.deployment_spec_id),
        deployment_spec_digest=second_authority.deployment_spec_digest,
    )
    second_gate = PlatformOpeningGate(
        second_runtime.store,
        EngineLifecycleAuthority.from_verified_command(second_runtime.verified),
    )
    requested = []
    started, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def census(authority):
        started.set()
        await release.wait()
        return InventoryUnavailable(authority, subject.spec["connector"], "test_unavailable")

    monkeypatch.setattr(subject.host, "read_opening_account_inventory", census)

    class SchedulingHost:
        # A protocol scheduling fixture, not two native nodes in one runner process.
        def runner_fact_deployments(self):
            return first, second

        async def runner_fact_observation_token(self, authority):
            if authority == first.authority:
                return await subject.host.runner_fact_observation_token(authority)
            desired = await second_gate.current_observation_desired()
            return RunnerFactObservationToken(desired.command_fingerprint, id(second_runtime), 0)

        async def runner_fact_opening_snapshot(self, instance):
            if instance == first.deployment_instance_id:
                return await subject.host.runner_fact_opening_snapshot(instance)
            return "awaiting_release", unavailable_opening_inventory(
                second_authority, second.connector
            )

        async def runner_fact_risk_snapshot(self, instance, _currency):
            requested.append(instance)
            return Decimal("1234.5"), ()

        async def deployment_ready(self, _instance):
            return True

        async def runner_fact_capital_snapshot(self, *_args):
            raise RuntimeError("capital observation not supplied by scheduling fixture")

    class Emitter(_CapturingEmitter):
        async def emit(self, authority, facts):
            await super().emit(authority, facts)
            if len(self.emissions) == 2:
                stop.set()

    emitter = Emitter()
    loop = RunnerFactProductionLoop(
        host=SchedulingHost(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    run = asyncio.create_task(loop.run_observability(stop))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(asyncio.shield(run), timeout=1)
        assert requested == [first.deployment_instance_id, second.deployment_instance_id]
        assert len(emitter.emissions) == 2
        assert not release.is_set()
        assert len(subject.host._opening_fact_reads) == 1
    finally:
        release.set()
        stop.set()
        await asyncio.gather(run, return_exceptions=True)
        await asyncio.gather(
            *(entry[1] for entry in subject.host._opening_fact_reads.values()),
            return_exceptions=True,
        )


async def test_large_inventory_never_suppresses_an_otherwise_signable_risk_batch(signed_host):
    import json
    from datetime import UTC, datetime

    from custos.core.runner_fact import RunnerFactEmitter, equity_snapshot, position_snapshot
    from tests.test_opening_inventory_contract import _large_inventory
    from tests.test_runner_fact_store import _identity

    subject = signed_host
    deployment = subject.host.runner_fact_deployments()[0]
    authority = deployment.authority
    _, inventory = _large_inventory(authority)
    # Rebind the parser-only large fixture to this real signed venue instance.
    inventory["scope"].update(
        trading_mode=authority.trading_mode,
        connector=deployment.connector,
        source={
            "kind": "sodex_venue_account",
            "network": "testnet",
            "main_wallet": "0x" + "a" * 40,
            "account_id": 4242,
            "account_type": "perps",
        },
    )
    inventory["completeness"] = dict.fromkeys(inventory["completeness"])
    inventory["capability_binding"].update(
        capture_api_revision="sodex_public_get_v1",
        model_revision="sodex_public_account_v1",
        mapping_revision=None,
        runtime_artifact_digest=None,
    )
    inventory["observations"]["identity"]["rows"] = [
        {"kind": "sodex_account", "main_wallet": "0x" + "a" * 40, "account_id": 4242}
    ]
    for read in inventory["observations"].values():
        read.update(capture_token=None, block_height=42, pagination="unknown")
    observed_at = datetime.now(UTC)
    equity = equity_snapshot(
        event_id=UUID(int=902),
        amount="1234.5",
        currency=deployment.currency,
        observed_at=observed_at,
        trading_gate="awaiting_release",
        account_inventory=inventory,
    )
    positions = position_snapshot(
        event_id=UUID(int=903),
        observed_at=observed_at,
        positions=[
            {
                "instrument": "X" * 120 + str(index).zfill(8),
                "quantity": "1",
                "mark_price": "1",
                "currency": deployment.currency,
            }
            for index in range(2500)
        ],
    )
    token = await subject.host.runner_fact_observation_token(authority)
    emitter = RunnerFactEmitter(subject.runtime.outbox, _identity(), lambda: None)
    await emitter.emit_observability(authority, [equity, positions], token.command_fingerprint)
    pending = await subject.runtime.outbox.pending()
    batch = json.loads(pending[0].payload)
    assert batch["source_seq_start"] == 1
    assert batch["source_seq_end"] == 2
    assert batch["facts"][0]["amount"] == "1234.5"
    assert batch["facts"][0]["account_inventory"]["status"] == "unavailable"
    assert batch["facts"][0]["account_inventory"]["reasons"] == ["wire_budget_exceeded"]
    assert len(batch["facts"][1]["positions"]) == 2500
