from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from custos.core.runner_fact_producer import (
    RunnerCapitalBasisSnapshot,
    RunnerFactProductionLoop,
    VenueLedgerEvidence,
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

    async def emit_group(self, authority, batches):
        for facts in batches:
            await self.emit(authority, facts)


class _CoverageHost:
    def __init__(self) -> None:
        self.requests = []

    async def runner_fact_venue_ledger(self, deployment_instance_id, coverage_from, closed_at):
        self.requests.append((deployment_instance_id, coverage_from, closed_at))
        return VenueLedgerEvidence(
            venue="BINANCE",
            source="venue_api",
            watermark="watermark-1",
            coverage_from=coverage_from,
            observed_through=closed_at,
            completeness={
                "balances_complete": True,
                "positions_complete": True,
                "fills_complete": True,
                "fees_complete": True,
            },
            balances=(
                {
                    "asset": "USDT",
                    "currency": "USDT",
                    "total": "100",
                    "available": "100",
                },
            ),
            positions=(),
            fills=(),
            fees=(),
        )


async def test_first_reconciliation_period_can_cover_from_signed_command_time() -> None:
    host = _CoverageHost()
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=host,
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    started_at = datetime(2026, 8, 13, 22, 26, tzinfo=UTC)
    coverage_from = datetime(2026, 8, 13, 22, 25, 21, tzinfo=UTC)
    closed_at = started_at + timedelta(seconds=60)
    authority = SimpleNamespace(
        stream_key="default:testnet:runner:instance",
        deployment_spec_id=uuid4(),
        trading_mode="testnet",
    )
    deployment = SimpleNamespace(
        authority=authority,
        deployment_instance_id=str(uuid4()),
        reconciliation_available=True,
    )

    assert await loop._close_reconciliation_period(
        deployment,
        started_at,
        closed_at,
        coverage_from=coverage_from,
    )

    assert host.requests == [(deployment.deployment_instance_id, coverage_from, closed_at)]
    close = emitter.emissions[-1][1][0]
    assert close["period_started_at"] == "2026-08-13T22:26:00Z"
    manifest = emitter.emissions[0][1][0]
    assert manifest["coverage_from"] == "2026-08-13T22:25:21Z"


class _ValuationHost(_CoverageHost):
    async def runner_fact_venue_ledger(self, deployment_instance_id, coverage_from, closed_at):
        evidence = await super().runner_fact_venue_ledger(
            deployment_instance_id, coverage_from, closed_at
        )
        return VenueLedgerEvidence(
            **{
                field: getattr(evidence, field)
                for field in (
                    "venue",
                    "source",
                    "watermark",
                    "coverage_from",
                    "observed_through",
                    "completeness",
                    "balances",
                    "positions",
                    "fills",
                    "fees",
                )
            },
            valuation_collection_started_at=closed_at - timedelta(seconds=1),
            venue_wallet_balances={"USDT": "1000"},
            valuation_positions=(
                {
                    "instrument": "BTCUSDT-PERP.BINANCE",
                    "currency": "USDT",
                    "quantity": "2",
                    "avg_entry_price": "95",
                    "mark_price": "101",
                },
            ),
        )

    async def runner_fact_valuation_snapshot(self, deployment_instance_id, currency):
        assert currency == "USDT"
        return (
            "1010",
            (
                {
                    "instrument": "BTCUSDT-PERP.BINANCE",
                    "currency": "USDT",
                    "quantity": "2",
                    "avg_entry_price": "95",
                    "mark_price": "100",
                },
            ),
        )


async def test_period_close_emits_one_owner_signed_common_valuation_fact() -> None:
    host = _ValuationHost()
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=host,
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    started_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    authority = SimpleNamespace(
        stream_key="default:testnet:runner:instance",
        deployment_spec_id=uuid4(),
        trading_mode="testnet",
    )
    deployment = SimpleNamespace(
        authority=authority,
        deployment_instance_id=str(uuid4()),
        currency="USDT",
        reconciliation_available=True,
        valuation_checkpoint_available=True,
    )

    assert await loop._close_reconciliation_period(
        deployment, started_at, started_at + timedelta(seconds=60)
    )

    checkpoint = next(
        facts[0]
        for _, facts in emitter.emissions
        if facts[0]["kind"] == "RunnerValuationCheckpointFact.v1"
    )
    assert checkpoint["venue_wallet_balance"] == "1000"
    assert checkpoint["positions"][0]["internal_mark_price"] == "100"
    assert checkpoint["positions"][0]["common_mark_price"] == "101"
    assert checkpoint["venue_snapshot_id"] == emitter.emissions[0][1][0]["snapshot_id"]


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


async def test_observability_reports_degraded_when_host_audit_state_is_unreliable() -> None:
    authority = SimpleNamespace(
        stream_key="default:testnet:runner:degraded",
        deployment_spec_id=uuid4(),
    )
    deployment = SimpleNamespace(
        authority=authority,
        deployment_instance_id=str(uuid4()),
        currency="USDT",
    )

    class DegradedHost(_CapitalBasisHost):
        async def get_engine_status(self, deployment_instance_id):
            assert deployment_instance_id == deployment.deployment_instance_id
            return SimpleNamespace(
                reliable=False,
                unreliable_reason="runner_event_forwarding_failed:fixture",
            )

    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=DegradedHost(deployment),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )

    await loop._emit_observability(deployment)

    heartbeat = next(fact for fact in emitter.emissions[0][1] if fact["kind"] == "heartbeat")
    assert heartbeat["status"] == "degraded"


async def test_valuation_failure_publishes_no_partial_period() -> None:
    class UnpricedHost(_ValuationHost):
        async def runner_fact_valuation_snapshot(self, *args):
            raise ValueError("mark unavailable")

    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=UnpricedHost(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    deployment = SimpleNamespace(
        authority=SimpleNamespace(stream_key="test", deployment_spec_id=uuid4()),
        deployment_instance_id=str(uuid4()),
        currency="USDT",
        reconciliation_available=True,
        valuation_checkpoint_available=True,
    )
    start = datetime(2026, 9, 20, tzinfo=UTC)
    assert not await loop._close_reconciliation_period(
        deployment, start, start + timedelta(seconds=60)
    )
    assert emitter.emissions == []


async def test_required_checkpoint_missing_data_cannot_close_period():
    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=_CoverageHost(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    deployment = SimpleNamespace(
        authority=SimpleNamespace(stream_key="test", deployment_spec_id=uuid4()),
        deployment_instance_id=str(uuid4()),
        currency="USDT",
        reconciliation_available=True,
        valuation_checkpoint_available=True,
    )
    start = datetime(2026, 9, 20, tzinfo=UTC)
    assert not await loop._close_reconciliation_period(
        deployment, start, start + timedelta(seconds=60)
    )
    assert emitter.emissions == []


async def test_cash_checkpoint_compares_account_inventory_without_strategy_cost():
    from dataclasses import replace

    class CashHost(_CoverageHost):
        async def runner_fact_venue_ledger(self, *args):
            evidence = await super().runner_fact_venue_ledger(*args)
            return replace(
                evidence,
                valuation_collection_started_at=evidence.observed_through,
                venue_wallet_balances={"USDT": "100"},
                valuation_positions=(),
                cash_inventory=(
                    {"asset": "BTC", "quantity": "1", "mark_price": "100"},
                    {"asset": "USDT", "quantity": "100", "mark_price": "1"},
                ),
            )

        async def runner_fact_cash_snapshot(self, *args):
            return "190", (
                {"asset": "BTC", "quantity": "1", "mark_price": "90"},
                {"asset": "USDT", "quantity": "100", "mark_price": "1"},
            )

        async def runner_fact_valuation_snapshot(self, *args):
            raise AssertionError("cash inventory must not use derivative cost basis")

    emitter = _CapturingEmitter()
    loop = RunnerFactProductionLoop(
        host=CashHost(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    deployment = SimpleNamespace(
        authority=SimpleNamespace(stream_key="cash", deployment_spec_id=uuid4()),
        deployment_instance_id=str(uuid4()),
        currency="USDT",
        reconciliation_available=True,
        valuation_checkpoint_available=True,
    )
    start = datetime(2026, 9, 20, tzinfo=UTC)
    assert await loop._close_reconciliation_period(deployment, start, start + timedelta(seconds=60))
    fact = next(
        facts[0]
        for _, facts in emitter.emissions
        if facts[0]["kind"] == "RunnerValuationCheckpointFact.v1"
    )
    assert fact["positions"] == []
    assert fact["cash_inventory"][0] == {
        "asset": "BTC",
        "internal_quantity": "1",
        "venue_quantity": "1",
        "internal_mark_price": "90",
        "common_mark_price": "100",
    }


def test_cash_checkpoint_fixture_is_reproducible():
    from pathlib import Path

    from custos.core.runner_fact import valuation_checkpoint

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/runner_cash_inventory_checkpoint_v1.json").read_text()
    )
    arguments = {
        key: value
        for key, value in fixture.items()
        if key not in {"kind", "seq", "checkpoint_digest"}
    }
    assert {**valuation_checkpoint(**arguments), "seq": 1} == fixture
