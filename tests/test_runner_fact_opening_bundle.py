from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.core.runner_fact import position_snapshot


@pytest.mark.parametrize(
    "observed_at",
    ["2026-11-04T00:00:00.123456789Z", "2026-11-04T00:00:00.000000001Z"],
)
def test_position_snapshot_preserves_the_native_capture_nanosecond(observed_at):
    fact = position_snapshot(event_id=UUID(int=4), positions=(), observed_at=observed_at)

    assert fact["observed_at"] == observed_at


def test_native_cash_sample_requires_registered_capture_and_retains_its_identity():
    from dataclasses import replace

    from custos.core.opening_observation import AdmittedNativeCashSample
    from custos.core.runner_fact import RunnerFactContractError
    from tests.test_opening_inventory_contract import _cash_candidate
    from tests.test_runner_fact_outbox import _authority

    authority = replace(_authority(), trading_mode="sandbox")
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = {
        **read(None),
        "status": "not_applicable",
        "reason": "model_not_applicable",
    }
    manifest = {
        "runner_fact_contracts": {
            "risk": {
                "opening_inventory": {
                    "schema_revision": 1,
                    "source_models": ["unavailable", "sandbox-account-state-v1"],
                    "capture_apis": ["execution-account-capture-v1"],
                    "mapping_revisions": ["native_currency_codes_v1"],
                    "runtime_artifact_digests": ["e" * 64],
                    "complete_models": [],
                }
            }
        }
    }
    with pytest.raises(RunnerFactContractError, match="registered"):
        AdmittedNativeCashSample.from_inventory(inventory, authority, "USDT", manifest)
    manifest["runner_fact_contracts"]["risk"]["opening_inventory"]["complete_models"] = [
        "sandbox-account-state-v1"
    ]

    sample = AdmittedNativeCashSample.from_inventory(inventory, authority, "USDT", manifest)

    assert str(sample.equity) == "1"
    assert sample.observed_at == inventory["equity_binding"]["observed_at"]
    assert sample.sample_id == inventory["equity_binding"]["capture_token"]
    assert sample.positions == ()


def _future_native_bundle(independent_available=True, authority=None):
    from custos.core.opening_inventory_contract import native_capture_token
    from custos.core.opening_observation import (
        AdmittedNativeCashSample,
        IndependentRiskSample,
        OpeningObservationBundle,
        RiskSampleUnavailable,
    )
    from custos.core.runner_fact_producer import RunnerFactObservationToken
    from custos.core.utc_time import render_utc_nanos
    from tests.test_opening_inventory_contract import _cash_candidate
    from tests.test_runner_fact_outbox import _authority

    authority = authority or replace(_authority(), trading_mode="sandbox")
    inventory, read = _cash_candidate(authority)
    inventory["native_capture"]["observed_at_ns"] += 123456789
    at = render_utc_nanos(inventory["native_capture"]["observed_at_ns"])
    token = native_capture_token(inventory["scope"]["source"], inventory["native_capture"])
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = {
        **read(None),
        "status": "not_applicable",
        "reason": "model_not_applicable",
    }
    for observation in inventory["observations"].values():
        observation.update(source_time=at, capture_token=token)
    inventory["equity_binding"].update(observed_at=at, capture_token=token)
    # Explicit future semantic fixture, not an installed artifact receipt.
    manifest = {
        "runner_fact_contracts": {
            "risk": {
                "opening_inventory": {
                    "schema_revision": 1,
                    "source_models": ["unavailable", "sandbox-account-state-v1"],
                    "capture_apis": ["execution-account-capture-v1"],
                    "mapping_revisions": ["native_currency_codes_v1"],
                    "runtime_artifact_digests": ["e" * 64],
                    "complete_models": ["sandbox-account-state-v1"],
                }
            }
        }
    }
    independent = (
        IndependentRiskSample(
            Decimal("4470"),
            "USDT",
            ({"instrument": "BTCUSDT", "quantity": "1", "mark_price": "100", "currency": "USDT"},),
            "2026-11-04T11:00:00.000000001Z",
            "risk-1",
            "portfolio",
        )
        if independent_available
        else RiskSampleUnavailable("2026-11-04T11:00:00.000000001Z", "portfolio_unavailable")
    )
    return OpeningObservationBundle(
        authority,
        RunnerFactObservationToken("f" * 64, 1, 1),
        independent,
        "awaiting_release",
        inventory,
        AdmittedNativeCashSample.from_inventory(inventory, authority, "USDT", manifest),
        "USDT",
    )


@pytest.mark.parametrize("independent_available", [True, False])
async def test_production_loop_uses_native_equity_time_and_complete_fallback_plan(
    independent_available,
):
    from custos.core.runner_fact_producer import RunnerFactProductionLoop

    bundle = _future_native_bundle(independent_available)
    deployment = SimpleNamespace(
        authority=bundle.authority,
        deployment_instance_id=str(bundle.authority.deployment_instance_id),
        deployment_spec_id=str(bundle.authority.deployment_spec_id),
        currency="USDT",
        connector="binance",
    )

    class Host:
        async def runner_fact_observation_token(self, authority):
            return bundle.token

        async def deployment_ready(self, instance):
            return True

        async def runner_fact_opening_bundle(self, instance, currency):
            return bundle

        async def runner_fact_risk_snapshot(self, *_args):
            raise AssertionError("the typed bundle is the only combined observation input")

        async def runner_fact_capital_snapshot(self, *_args):
            raise RuntimeError("capital sample unavailable")

    class Emitter:
        plans = []

        async def emit_observability(self, authority, plan, fingerprint):
            assert authority == bundle.authority
            assert fingerprint == bundle.token.command_fingerprint
            self.plans.append(plan)

    emitter = Emitter()
    loop = RunnerFactProductionLoop(
        host=Host(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )

    await loop._emit_observability(deployment)

    plan = emitter.plans[0]
    equity, positions, beat = plan.primary[:3]
    assert equity["amount"] == "1"
    assert equity["observed_at"] == "2026-11-04T00:00:00.123456789Z"
    assert positions["positions"] == []
    assert positions["observed_at"] == equity["observed_at"]
    assert beat["observed_at"] != equity["observed_at"]
    if independent_available:
        fallback_equity, fallback_positions = plan.fallback[:2]
        assert fallback_equity["amount"] == "4470"
        assert fallback_equity["observed_at"] == bundle.independent.observed_at
        assert fallback_positions["observed_at"] == bundle.independent.observed_at
        assert fallback_positions["positions"][0]["quantity"] == "1"
        assert fallback_equity["account_inventory"]["status"] == "unavailable"
    else:
        assert plan.fallback is None


@pytest.mark.parametrize("fallback_available", [True, False])
async def test_sqlite_budget_fallback_restores_the_entire_independent_risk_sample(
    tmp_path, monkeypatch, fallback_available
):
    import json
    from copy import deepcopy

    from custos.core import runner_fact as wire
    from custos.core.opening_inventory_contract import native_capture_token
    from custos.core.opening_observation import ObservabilityFactPlan
    from custos.core.runner_fact import RunnerFactEmitter, equity_snapshot
    from tests.fixtures.platform_opening import closed_platform_runtime
    from tests.test_nt_trading_node_host import _fact_spec
    from tests.test_runner_fact_store import _identity, _runner_authority

    spec = _fact_spec(
        "native-budget",
        "binance",
        trading_mode="sandbox",
        strategy_id="11000000-0000-4000-8000-000000000011",
    )
    runtime = await closed_platform_runtime(tmp_path / "facts.sqlite3", spec)
    authority = _runner_authority(runtime.verified)
    bundle = _future_native_bundle(authority=authority)
    inventory = deepcopy(bundle.inventory)
    source = inventory["scope"]["source"]
    configuration = json.loads(source["configuration_json"])
    configuration.setdefault("client_config", {})["leverages"] = {
        "BTC" + str(index).zfill(80) + ".BINANCE": "1" for index in range(64)
    }
    source["configuration_json"] = json.dumps(configuration)
    from hashlib import sha256

    source["configuration_sha256"] = sha256(source["configuration_json"].encode()).hexdigest()
    capture_token = native_capture_token(source, inventory["native_capture"])
    for read in inventory["observations"].values():
        read["capture_token"] = capture_token
    inventory["observations"]["identity"]["rows"][0]["configuration_sha256"] = source[
        "configuration_sha256"
    ]
    inventory["equity_binding"]["capture_token"] = capture_token
    primary = (
        equity_snapshot(
            event_id=UUID(int=901),
            amount="1",
            currency="USDT",
            observed_at=bundle.native.observed_at,
            trading_gate="awaiting_release",
            account_inventory=inventory,
        ),
        position_snapshot(
            event_id=UUID(int=902), positions=(), observed_at=bundle.native.observed_at
        ),
    )
    from custos.core.opening_inventory_contract import unavailable_opening_inventory

    fallback = (
        equity_snapshot(
            event_id=UUID(int=903),
            amount="4470",
            currency="USDT",
            observed_at=bundle.independent.observed_at,
            trading_gate="awaiting_release",
            account_inventory=unavailable_opening_inventory(
                authority, "binance", "wire_budget_exceeded"
            ),
        ),
        position_snapshot(
            event_id=UUID(int=904),
            positions=bundle.independent.positions,
            observed_at=bundle.independent.observed_at,
        ),
    )
    monkeypatch.setattr(wire, "MAX_BATCH_BYTES", 9000)
    emitter = RunnerFactEmitter(runtime.outbox, _identity(), lambda: None)
    plan = ObservabilityFactPlan.from_facts(primary, fallback if fallback_available else None)
    plan.primary[0]["amount"] = "99"
    if plan.fallback is not None:
        plan.fallback[0]["amount"] = "99"
    if not fallback_available:
        with pytest.raises(wire.RunnerFactPayloadTooLarge):
            await emitter.emit_observability(authority, plan, runtime.verified.command_fingerprint)
        assert await runtime.outbox.pending() == []
        # The failed attempt must leave neither consumed event IDs nor stream sequence gaps.
        await emitter.emit_observability(
            authority,
            ObservabilityFactPlan.from_facts(fallback, None),
            runtime.verified.command_fingerprint,
        )
    else:
        await emitter.emit_observability(authority, plan, runtime.verified.command_fingerprint)

    batches = await runtime.outbox.pending()
    assert len(batches) == 1
    signed = json.loads(batches[0].payload)
    equity, positions = signed["facts"]
    assert (signed["source_seq_start"], signed["source_seq_end"]) == (1, 2)
    assert equity["amount"] == "4470"
    assert equity["observed_at"] == bundle.independent.observed_at
    assert positions["positions"][0]["quantity"] == "1"
    assert positions["observed_at"] == bundle.independent.observed_at
    assert equity["event_id"] == str(UUID(int=903))
    assert equity["account_inventory"]["reasons"] == ["wire_budget_exceeded"]


async def test_production_bundle_and_fact_plan_cannot_be_changed_through_nested_aliases():
    from custos.core.opening_inventory_contract import require_opening_inventory_support
    from custos.core.opening_observation import ObservabilityFactPlan
    from custos.core.runner_fact_producer import RunnerFactProductionLoop
    from tests.test_native_opening_inventory import _manifest

    bundle = _future_native_bundle()
    deployment = SimpleNamespace(
        authority=bundle.authority,
        deployment_instance_id=str(bundle.authority.deployment_instance_id),
        deployment_spec_id=str(bundle.authority.deployment_spec_id),
        currency="USDT",
        connector="binance",
    )

    class Host:
        async def runner_fact_observation_token(self, authority):
            return bundle.token

        async def deployment_ready(self, instance):
            return True

        async def runner_fact_opening_bundle(self, instance, currency):
            return bundle

        async def runner_fact_capital_snapshot(self, *_args):
            bundle.inventory["capability_binding"]["runtime_artifact_digest"] = "f" * 64
            bundle.independent.positions[0]["quantity"] = "99"
            raise RuntimeError("capital unavailable after a hostile alias mutation")

    class Emitter:
        plans = []

        async def emit_observability(self, authority, plan, fingerprint):
            self.plans.append(plan)

    emitter = Emitter()
    loop = RunnerFactProductionLoop(
        host=Host(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    await loop._emit_observability(deployment)
    plan = emitter.plans[0]
    require_opening_inventory_support(plan.primary[0]["account_inventory"], _manifest(True))
    assert plan.fallback[1]["positions"][0]["quantity"] == "1"
    primary, fallback = plan.primary, plan.fallback
    primary[0]["amount"] = "99"
    fallback[0]["amount"] = "99"
    assert plan.primary[0]["amount"] == "1"
    assert plan.fallback[0]["amount"] == "4470"
    external = [{"kind": "heartbeat", "nested": {"value": 1}}]
    snapshot = ObservabilityFactPlan.from_facts(external, external)
    external[0]["nested"]["value"] = 2
    assert snapshot.primary[0]["nested"]["value"] == 1


def test_bundle_refuses_inventory_different_from_its_admitted_native_sample():
    from custos.core.opening_observation import OpeningObservationBundle
    from custos.core.runner_fact import RunnerFactContractError

    original = _future_native_bundle()
    inventory = original.inventory
    inventory["capability_binding"]["runtime_artifact_digest"] = "f" * 64
    with pytest.raises(RunnerFactContractError, match="differs"):
        OpeningObservationBundle(
            original.authority,
            original.token,
            original.independent,
            original.trading_gate,
            inventory,
            original.native,
            original.currency,
        )


@pytest.mark.parametrize(
    "failure",
    [
        "status",
        "readiness",
        "retired_status",
        "retired_readiness",
        "cancelled_status",
        "cancelled_readiness",
    ],
)
async def test_auxiliary_health_failure_does_not_suppress_native_risk_but_retirement_does(failure):
    from custos.core.runner_fact_producer import (
        RunnerFactObservationRetired,
        RunnerFactProductionLoop,
    )

    bundle = _future_native_bundle(False)
    deployment = SimpleNamespace(
        authority=bundle.authority,
        deployment_instance_id=str(bundle.authority.deployment_instance_id),
        deployment_spec_id=str(bundle.authority.deployment_spec_id),
        currency="USDT",
        connector="binance",
    )

    class Host:
        bundle_calls = 0

        def fail(self):
            import asyncio

            if failure.startswith("cancelled"):
                raise asyncio.CancelledError()
            if failure.startswith("retired"):
                raise RunnerFactObservationRetired("source was retired")
            raise RuntimeError("independent portfolio failed")

        async def runner_fact_observation_token(self, authority):
            return bundle.token

        async def get_engine_status(self, instance):
            if failure.endswith("status"):
                self.fail()
            return SimpleNamespace(reliable=True)

        async def deployment_ready(self, instance):
            if failure.endswith("readiness"):
                self.fail()
            return True

        async def runner_fact_opening_bundle(self, instance, currency):
            self.bundle_calls += 1
            return bundle

    class Emitter:
        plans = []

        async def emit_observability(self, authority, plan, fingerprint):
            self.plans.append(plan)

    host, emitter = Host(), Emitter()
    loop = RunnerFactProductionLoop(
        host=host, emitter=emitter, snapshot_interval_secs=1, period_secs=60, period_retry_secs=1
    )
    if failure.startswith("cancelled"):
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            await loop._emit_observability(deployment)
        assert host.bundle_calls == 0
        assert emitter.plans == []
        return
    await loop._emit_observability(deployment)
    if failure.startswith("retired"):
        assert host.bundle_calls == 0
        assert emitter.plans == []
    else:
        assert host.bundle_calls == 1
        equity, positions, beat = emitter.plans[0].primary
        assert equity["amount"] == "1"
        assert positions["observed_at"] == equity["observed_at"] == bundle.native.observed_at
        assert beat["status"] == "degraded"
        assert emitter.plans[0].fallback is None
