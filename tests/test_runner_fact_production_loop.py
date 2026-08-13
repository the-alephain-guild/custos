from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from custos.core.runner_fact_producer import (
    RunnerCapitalBasisSnapshot,
    RunnerFactProductionLoop,
)


class _RejectingEmitter:
    async def emit(self, authority, facts):
        del authority, facts
        raise AssertionError("reconciliation cadence must not forge a settlement period close")


async def test_unavailable_reconciliation_does_not_emit_settlement_close() -> None:
    loop = RunnerFactProductionLoop(
        host=object(),
        emitter=_RejectingEmitter(),
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    deployment = SimpleNamespace(
        authority=SimpleNamespace(
            deployment_spec_id=uuid4(),
            trading_mode="sandbox",
        ),
        deployment_instance_id=str(uuid4()),
        reconciliation_available=False,
    )
    closed_at = datetime.now(UTC)

    assert await loop._close_reconciliation_period(
        deployment,
        closed_at - timedelta(seconds=60),
        closed_at,
    )


class _CapturingEmitter:
    def __init__(self) -> None:
        self.emissions = []

    async def emit(self, authority, facts):
        json.dumps(facts, allow_nan=False, separators=(",", ":"), sort_keys=True)
        self.emissions.append((authority, tuple(facts)))


class _CapitalBasisHost:
    def __init__(self, deployment) -> None:
        self.deployment = deployment

    async def runner_fact_risk_snapshot(self, deployment_instance_id, currency):
        assert deployment_instance_id == self.deployment.deployment_instance_id
        assert currency == "USDT"
        return 4470, ()

    async def runner_fact_capital_snapshot(self, deployment_instance_id, currency):
        assert deployment_instance_id == self.deployment.deployment_instance_id
        assert currency == "USDT"
        return RunnerCapitalBasisSnapshot(
            currency="USDT",
            venue_available="4463.27",
            strategy_sizing_basis="4463.27",
            configured_initial_capital="10000",
            capital_mode="compound",
            reserved_notional="7",
            open_exposure="443",
            total_exposure="450",
            max_total_notional="1000",
            within_policy=True,
        )


async def test_observability_emits_signed_capital_basis_without_log_inference() -> None:
    authority = SimpleNamespace(
        stream_key="default:testnet:runner:instance",
        tenant_id="default",
        trading_mode="testnet",
        runner_id=uuid4(),
        deployment_instance_id=uuid4(),
        deployment_spec_id=uuid4(),
        deployment_spec_digest="a" * 64,
        generation=1,
    )
    deployment = SimpleNamespace(
        authority=authority,
        deployment_instance_id=str(authority.deployment_instance_id),
        currency="USDT",
    )
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=_CapitalBasisHost(deployment),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )

    await loop._emit_observability(deployment)

    assert len(emitter.emissions) == 1
    facts = emitter.emissions[0][1]
    capital = next(fact for fact in facts if fact["kind"] == "RunnerRuntimeLogFact.v1")
    assert isinstance(capital["event_id"], str)
    assert isinstance(capital["correlation_id"], str)
    assert capital["message"] == "runner_capital_basis_observed"
    assert capital["component"] == "custos.capital_basis"
    assert capital["structured_fields"] == {
        "schema_version": 1,
        "currency": "USDT",
        "venue_equity": "4470",
        "venue_available": "4463.27",
        "strategy_sizing_basis": "4463.27",
        "configured_initial_capital": "10000",
        "capital_mode": "compound",
        "reserved_notional": "7",
        "open_exposure": "443",
        "total_exposure": "450",
        "max_total_notional": "1000",
        "within_policy": True,
    }
