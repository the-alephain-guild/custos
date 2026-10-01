"""A signed stop seals the instance stream with a terminal valuation.

These tests drive the real SQLite state store and outbox: the stop outcome, the
terminal fact, the owed month close and the stream seal are one transaction, and
nothing about them may depend on in-process state that a restart would lose.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
from custos.core.engine_lifecycle import EngineLifecycleConfig, EngineLifecycleSupervisor
from custos.core.engine_protocol import EngineStopBoundary, StopBoundaryValuation
from custos.core.runner_fact import (
    TERMINAL_VALUATION_KIND,
    RunnerFactContractError,
    RunnerFactEmitter,
    RunnerFactIdentity,
    RunnerFactOutbox,
    RunnerStateDurabilityError,
    RunnerStateStore,
    TerminalValuationSettings,
    equity_snapshot,
    runner_fact_event_id,
)
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactProductionLoop
from custos.engines.nautilus.venues import venue_for_connector
from tests.test_runner_fact_store import RUNNER_ID, _runner_authority, _verified_command

STOP_REQUESTED = datetime(2026, 10, 14, 10, 5, 0, tzinfo=UTC)
LIFECYCLE_LEASE_NS = 60_000_000_000


def _command(generation: int, lifecycle_state: str):
    def mutate(event: dict[str, Any]) -> None:
        event["payload"]["generation"] = generation
        event["payload"]["lifecycle_state"] = lifecycle_state
        event["aggregate_version"] = generation

    return _verified_command(mutate_event=mutate)[2]


def _identity() -> RunnerFactIdentity:
    return RunnerFactIdentity(
        Ed25519PrivateKey.from_private_bytes(bytes(range(65, 97))), "runner-state-key"
    )


def _store(database: Path, *, terminal_valuation: bool = True) -> tuple[RunnerFactOutbox, Any]:
    outbox = RunnerFactOutbox(database)
    store = RunnerStateStore(
        outbox=outbox,
        identity=_identity(),
        tenant_id="acme",
        runner_id=RUNNER_ID,
        authority_resolver=_runner_authority,
        terminal_valuation=(
            TerminalValuationSettings(venue_for_connector=venue_for_connector)
            if terminal_valuation
            else None
        ),
    )
    return outbox, store


async def _apply(store, verified, *, delivery_id: str) -> None:
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    await store.record_in_progress_lease(
        delivery_id=delivery_id,
        verified=verified,
        lease_until_ns=time.time_ns() + LIFECYCLE_LEASE_NS,
    )
    await store.commit_applied_and_enqueue_lifecycle(
        delivery_id=delivery_id,
        verified=verified,
        engine_handle="node" if verified.command.lifecycle_state == "running" else None,
        observed_status=str(verified.command.lifecycle_state),
    )


async def _prepare_stop(store, verified, *, delivery_id: str = "stop-delivery") -> None:
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    await store.record_in_progress_lease(
        delivery_id=delivery_id,
        verified=verified,
        lease_until_ns=time.time_ns() + LIFECYCLE_LEASE_NS,
    )


def _equity(outbox: RunnerFactOutbox, verified, amount: str, observed_at: datetime) -> str:
    authority = _runner_authority(verified)
    event_id = str(runner_fact_event_id(authority.stream_key, "equity", observed_at.isoformat()))
    outbox.enqueue_sync(
        authority,
        _identity(),
        (
            equity_snapshot(
                event_id=event_id,
                amount=amount,
                currency="USDT",
                observed_at=observed_at,
            ),
        ),
    )
    return event_id


def _valuation(**overrides: Any) -> StopBoundaryValuation:
    values: dict[str, Any] = {
        "currency": "USDT",
        "equity": Decimal("10060.5"),
        "open_positions": (),
        "observed_at": STOP_REQUESTED + timedelta(seconds=2),
        "marks_oldest_at": None,
    }
    values.update(overrides)
    return StopBoundaryValuation(**values)


def _boundary(**overrides: Any) -> EngineStopBoundary:
    values: dict[str, Any] = {
        "stop_requested_at": STOP_REQUESTED,
        "node_was_running": True,
        "stopped_gracefully": True,
        "run_task_failed": False,
        "reaped": True,
        "stop_effective_at": STOP_REQUESTED + timedelta(seconds=2, milliseconds=5),
        "valuation": _valuation(),
        "valuation_failure": None,
    }
    values.update(overrides)
    return EngineStopBoundary(**values)


def _batches(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT payload FROM runner_fact_outbox ORDER BY source_seq_start"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _terminal(database: Path) -> dict[str, Any]:
    terminals = [
        fact
        for batch in _batches(database)
        for fact in batch["facts"]
        if fact["kind"] == TERMINAL_VALUATION_KIND
    ]
    assert len(terminals) == 1, terminals
    return terminals[0]


async def _running_then_stop(tmp_path: Path, *, with_equity: bool = True):
    database = tmp_path / "runner-state.sqlite3"
    outbox, store = _store(database)
    running = _command(1, "running")
    await _apply(store, running, delivery_id="run-delivery")
    equity_event = None
    if with_equity:
        equity_event = _equity(outbox, running, "10050", STOP_REQUESTED - timedelta(seconds=30))
    stop = _command(2, "stopped")
    await _prepare_stop(store, stop)
    return database, outbox, store, running, stop, equity_event


@pytest.mark.asyncio
async def test_a_confirmed_stop_puts_lifecycle_and_terminal_in_one_batch(tmp_path: Path) -> None:
    database, _, store, running, stop, equity_event = await _running_then_stop(tmp_path)

    result = await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery",
        verified=stop,
        boundary=_boundary(),
        causes=frozenset(),
    )

    assert result.committed is True
    batch = _batches(database)[-1]
    assert [fact["kind"] for fact in batch["facts"]] == [
        "RunnerDeploymentLifecycleFact.v1",
        TERMINAL_VALUATION_KIND,
    ]
    lifecycle, terminal = batch["facts"]
    assert (lifecycle["lifecycle_state"], lifecycle["outcome"]) == ("stopped", "applied")
    assert terminal["outcome"] == "confirmed" and terminal["reason_code"] is None
    assert terminal["generation"] == 2 and terminal["closes_generation"] == 1
    assert terminal["command_fingerprint"] == stop.command_fingerprint
    assert terminal["equity"] == {"amount": "10060.5", "currency": "USDT"}
    assert terminal["open_positions"] == []
    assert terminal["prior_equity"]["event_id"] == equity_event
    assert terminal["prior_equity"]["amount"] == "10050"
    assert terminal["prior_equity"]["seq"] < terminal["seq"]
    assert terminal["account_scope"] == {
        "venue": "BINANCE",
        "credential_scope_id": "91000000-0000-4000-8000-000000000011",
        "credential_scope_digest": "a" * 64,
        "sub_account": None,
        "wallet_type": None,
    }
    # The golden command is a sandbox deployment without a shutdown policy.
    assert terminal["valuation_source"] == "simulated_account"
    assert terminal["position_policy"] == "preserve"
    assert terminal["period"] == "2026-10"
    state = await store.load_engine_lifecycle_state(stop)
    assert state.applied_generation == 2 and state.observed_status == "stopped"


@pytest.mark.parametrize(
    ("causes", "boundary", "reason"),
    [
        ({"stop_timeout"}, {"valuation": None, "stopped_gracefully": False}, "stop_timeout"),
        ({"engine_task_failed"}, {"valuation": None}, "engine_task_failed"),
        (
            {"valuation_unreliable"},
            {"valuation": None, "valuation_failure": "valuation_unreliable"},
            "valuation_unreliable",
        ),
        # A mark 61 seconds older than the read is not the same boundary.
        (
            set(),
            {
                "valuation": _valuation(
                    marks_oldest_at=STOP_REQUESTED - timedelta(seconds=59),
                    open_positions=(
                        {
                            "instrument": "BTCUSDT-PERP.BINANCE",
                            "quantity": "0.01",
                            "mark_price": "61000",
                            "currency": "USDT",
                        },
                    ),
                )
            },
            "valuation_gap_exceeded",
        ),
        # Open positions with no price watermark cannot be confirmed.
        (
            set(),
            {
                "valuation": _valuation(
                    open_positions=(
                        {
                            "instrument": "BTCUSDT-PERP.BINANCE",
                            "quantity": "0.01",
                            "mark_price": "61000",
                            "currency": "USDT",
                        },
                    ),
                )
            },
            "valuation_unreliable",
        ),
        # More decimals than the consumer can hold: the fallback, and only the fallback.
        (
            set(),
            {"valuation": _valuation(equity=Decimal("1." + "1" * 29))},
            "valuation_out_of_contract",
        ),
        # Both a stale mark and an out-of-range amount: precedence picks the gap.
        (
            set(),
            {
                "valuation": _valuation(
                    equity=Decimal("1." + "1" * 29),
                    marks_oldest_at=STOP_REQUESTED - timedelta(seconds=59),
                    open_positions=(
                        {
                            "instrument": "BTCUSDT-PERP.BINANCE",
                            "quantity": "0.01",
                            "mark_price": "61000",
                            "currency": "USDT",
                        },
                    ),
                )
            },
            "valuation_gap_exceeded",
        ),
        # Several stop-side causes at once: the first by precedence is the reason.
        (
            {"engine_task_failed", "stop_timeout", "valuation_unreliable"},
            {"valuation": None},
            "stop_timeout",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_unconfirmed_stop_carries_the_one_reason_precedence_selects(
    tmp_path: Path, causes: set[str], boundary: dict[str, Any], reason: str
) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path)

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery",
        verified=stop,
        boundary=_boundary(**boundary),
        causes=frozenset(causes),
    )

    terminal = _terminal(database)
    assert terminal["outcome"] == "valuation_unconfirmed"
    assert terminal["reason_code"] == reason
    assert terminal["equity"] is None and terminal["open_positions"] is None
    assert terminal["valuation_observed_at"] is None and terminal["marks_oldest_at"] is None
    assert terminal["period"] is None
    assert terminal["closes_generation"] == 1


@pytest.mark.asyncio
async def test_a_stop_before_any_equity_snapshot_cites_no_prior_equity(tmp_path: Path) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path, with_equity=False)

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )

    terminal = _terminal(database)
    assert (terminal["outcome"], terminal["reason_code"]) == (
        "valuation_unconfirmed",
        "no_prior_signed_equity",
    )
    assert terminal["prior_equity"] is None


@pytest.mark.asyncio
async def test_the_sandbox_simulator_never_confirms_its_configured_equity(tmp_path: Path) -> None:
    """Its prior equity is the configured starting capital: cited, never confirmed."""
    database, _, store, _, stop, equity_event = await _running_then_stop(tmp_path)

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery",
        verified=stop,
        boundary=_boundary(valuation=None, valuation_failure="valuation_unavailable"),
        causes=frozenset({"valuation_unavailable"}),
    )

    terminal = _terminal(database)
    assert terminal["reason_code"] == "valuation_unavailable"
    assert terminal["prior_equity"]["event_id"] == equity_event


@pytest.mark.asyncio
async def test_the_sandbox_simulator_without_a_snapshot_still_reports_unavailable(
    tmp_path: Path,
) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path, with_equity=False)

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery",
        verified=stop,
        boundary=_boundary(valuation=None, valuation_failure="valuation_unavailable"),
        causes=frozenset({"valuation_unavailable"}),
    )

    terminal = _terminal(database)
    assert terminal["reason_code"] == "valuation_unavailable"
    assert terminal["prior_equity"] is None


@pytest.mark.asyncio
async def test_an_instance_that_never_ran_is_stopped_with_no_running_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _store(database)
    stop = _command(2, "stopped")
    await _prepare_stop(store, stop)

    result = await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery",
        verified=stop,
        boundary=_boundary(node_was_running=False, valuation=None),
        causes=frozenset({"engine_not_running_at_stop"}),
    )

    assert result.committed is True and result.outcome == "applied"
    terminal = _terminal(database)
    assert terminal["reason_code"] == "no_prior_running_generation"
    assert terminal["closes_generation"] is None


@pytest.mark.asyncio
async def test_a_stop_after_a_pause_closes_the_last_running_generation(tmp_path: Path) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _store(database)
    await _apply(store, _command(1, "running"), delivery_id="run")
    await _apply(store, _command(2, "paused"), delivery_id="pause")
    stop = _command(3, "stopped")
    await _prepare_stop(store, stop)

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery",
        verified=stop,
        boundary=_boundary(node_was_running=False, valuation=None),
        causes=frozenset({"engine_not_running_at_stop"}),
    )

    terminal = _terminal(database)
    assert terminal["closes_generation"] == 1
    assert terminal["reason_code"] == "engine_not_running_at_stop"


@pytest.mark.asyncio
async def test_applied_and_retry_exhausted_exclude_each_other_for_one_stop(
    tmp_path: Path,
) -> None:
    _, _, store, _, stop, _ = await _running_then_stop(tmp_path / "exhausted-first")
    await store.commit_verified_command_outcome_and_enqueue_fact(
        delivery_id="stop-delivery",
        verified=stop,
        outcome="retry_exhausted",
        reason_code="retry_exhausted:runtime_apply_failed",
        engine_handle=None,
        observed_status="quarantined",
        lifecycle_state="stopped",
    )
    with pytest.raises(RunnerStateDurabilityError, match="retry-exhausted"):
        await store.commit_stop_applied_and_enqueue_terminal(
            delivery_id="stop-redelivery",
            verified=stop,
            boundary=_boundary(),
            causes=frozenset(),
        )

    _, _, store, _, stop, _ = await _running_then_stop(tmp_path / "applied-first")
    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )
    with pytest.raises(RunnerStateDurabilityError, match="applied"):
        await store.commit_verified_command_outcome_and_enqueue_fact(
            delivery_id="stop-late",
            verified=stop,
            outcome="retry_exhausted",
            reason_code="retry_exhausted:late",
            engine_handle=None,
            observed_status="quarantined",
            lifecycle_state="stopped",
        )


@pytest.mark.asyncio
async def test_a_failed_terminal_commit_leaves_no_partial_stop(tmp_path: Path) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path)
    before = _batches(database)

    class Boom(RuntimeError):
        pass

    original = store._identity.sign_batch_payload
    calls = {"count": 0}

    def failing(payload: bytes) -> str:
        calls["count"] += 1
        raise Boom("signing failed")

    store._identity.sign_batch_payload = failing  # type: ignore[method-assign]
    with pytest.raises(Boom):
        await store.commit_stop_applied_and_enqueue_terminal(
            delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
        )
    store._identity.sign_batch_payload = original  # type: ignore[method-assign]

    assert _batches(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM runner_stop_terminal").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runner_fact_stream_seal").fetchone()[0] == 0
        outcomes = connection.execute(
            "SELECT count(*) FROM command_outcomes WHERE generation = 2"
        ).fetchone()[0]
    assert outcomes == 0


@pytest.mark.asyncio
async def test_the_stored_terminal_batch_is_resent_as_is_and_never_rebuilt(
    tmp_path: Path,
) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path)
    first = await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )
    stored = _batches(database)

    reopened_outbox, reopened = _store(database)
    pending = await reopened_outbox.pending()
    assert [json.loads(batch.payload) for batch in pending] == stored
    again = await reopened.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-redelivery",
        verified=stop,
        # Whatever a later attempt would say, the stored bytes are what was signed.
        boundary=_boundary(valuation=None),
        causes=frozenset({"stop_timeout"}),
    )

    assert again.committed is False
    assert again.lifecycle_batch_id == first.lifecycle_batch_id
    assert _batches(database) == stored


@pytest.mark.asyncio
async def test_the_stream_is_sealed_after_its_terminal_fact(tmp_path: Path) -> None:
    database, outbox, store, running, stop, _ = await _running_then_stop(tmp_path)
    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RunnerFactContractError, match="sealed"):
            _equity(outbox, running, "10070", STOP_REQUESTED + timedelta(seconds=5))
    assert "runner_fact_sealed_stream_refused" in [entry["event"] for entry in logs]

    archived = _command(3, "archived")
    await _apply(store, archived, delivery_id="archive")
    kinds = [fact["kind"] for fact in _batches(database)[-1]["facts"]]
    assert kinds == ["RunnerDeploymentLifecycleFact.v1"]
    assert _batches(database)[-1]["facts"][0]["lifecycle_state"] == "archived"


@pytest.mark.asyncio
async def test_the_production_loop_cannot_emit_into_a_sealed_stream(tmp_path: Path) -> None:
    database, outbox, store, running, stop, _ = await _running_then_stop(tmp_path)
    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )
    stored = _batches(database)
    authority = _runner_authority(running)
    deployment = RunnerFactDeployment(
        authority=authority,
        deployment_instance_id=str(authority.deployment_instance_id),
        deployment_spec_id=str(authority.deployment_spec_id),
        deployment_spec_digest=authority.deployment_spec_digest,
        venue="BINANCE",
        currency="USDT",
        reconciliation_available=False,
        strategy_version="1",
        timeframe="1m",
    )

    class Host:
        def runner_fact_deployments(self):
            return (deployment,)

        async def deployment_ready(self, deployment_instance_id):
            return True

        async def runner_fact_risk_snapshot(self, deployment_instance_id, currency):
            return Decimal("10070"), ()

        async def runner_fact_capital_snapshot(self, deployment_instance_id, currency):
            raise RuntimeError("no capital basis")

    loop = RunnerFactProductionLoop(
        host=Host(),
        emitter=RunnerFactEmitter(outbox, _identity(), lambda: None),
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    with structlog.testing.capture_logs() as logs:
        await loop._emit_observability(deployment)

    assert "runner_fact_observability_enqueue_failed" in [entry["event"] for entry in logs]
    assert _batches(database) == stored


@pytest.mark.asyncio
async def test_an_owed_month_close_is_enqueued_before_the_terminal(tmp_path: Path) -> None:
    database, outbox, store, running, stop, _ = await _running_then_stop(tmp_path)
    authority = _runner_authority(running)
    month_start = datetime(2026, 10, 1, tzinfo=UTC)
    await RunnerFactEmitter(outbox, _identity(), lambda: None).owe_month_close(
        authority, "2026-09", month_start
    )

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )

    facts = [(batch, fact) for batch in _batches(database) for fact in batch["facts"]]
    close_batch, close = next((b, f) for b, f in facts if f["kind"] == "period_closed")
    terminal = next(f for _, f in facts if f["kind"] == TERMINAL_VALUATION_KIND)
    assert close["period"] == "2026-09" and close["closed_at"] == "2026-10-01T00:00:00Z"
    assert close["seq"] < terminal["seq"]
    # The month belongs to the running generation's settlement scope.
    assert close_batch["generation"] == 1 and close_batch["facts"][-1] is not None
    assert close["event_id"] == str(
        runner_fact_event_id(authority.stream_key, "settlement_period", "2026-09")
    )
    with sqlite3.connect(database) as connection:
        owed = connection.execute("SELECT count(*) FROM runner_fact_owed_month_close").fetchone()
    assert owed[0] == 0


@pytest.mark.asyncio
async def test_a_runner_without_the_capability_flag_emits_no_terminal(tmp_path: Path) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _store(database, terminal_valuation=False)
    await _apply(store, _command(1, "running"), delivery_id="run")
    stop = _command(2, "stopped")
    await _prepare_stop(store, stop)

    await store.commit_stop_applied_and_enqueue_terminal(
        delivery_id="stop-delivery", verified=stop, boundary=_boundary(), causes=frozenset()
    )

    kinds = [fact["kind"] for batch in _batches(database) for fact in batch["facts"]]
    assert TERMINAL_VALUATION_KIND not in kinds
    assert kinds[-1] == "RunnerDeploymentLifecycleFact.v1"


class _BoundaryEngine:
    """An engine that reports its stop boundary and counts how often it was asked."""

    def __init__(self, boundaries: list[EngineStopBoundary]) -> None:
        self._boundaries = boundaries
        self.boundary_calls = 0

    async def stop_at_boundary(self, deployment_instance_id: str) -> EngineStopBoundary:
        self.boundary_calls += 1
        return self._boundaries.pop(0)

    async def stop(self, deployment_instance_id: str) -> None:
        raise AssertionError("the stop command must use the boundary-reporting stop")

    def supports_trading_mode(self, mode: str) -> bool:
        return True


def _supervisor(engine, store) -> EngineLifecycleSupervisor:
    return EngineLifecycleSupervisor(
        engine=engine,
        state_store=store,
        artifact_capability=ArtifactRuntimeCapabilityV1.production_ready(),
        config=EngineLifecycleConfig(readiness_timeout_secs=1.0),
    )


def _outcomes(database: Path, generation: int) -> list[tuple[str, str]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT outcome, reason_code FROM command_outcomes WHERE generation = ?",
            (generation,),
        ).fetchall()


@pytest.mark.asyncio
async def test_a_reap_timeout_acks_without_any_outcome_until_the_node_is_reaped(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-state.sqlite3"
    outbox, store = _store(database)
    running = _command(1, "running")
    await _apply(store, running, delivery_id="run")
    _equity(outbox, running, "10050", STOP_REQUESTED - timedelta(seconds=30))
    stop = _command(2, "stopped")
    await store.record_desired_command(
        command=stop.command,
        command_fingerprint=stop.command_fingerprint,
        verification_receipt=stop.verification_receipt,
    )
    reaped: asyncio.Future[datetime] = asyncio.get_running_loop().create_future()
    engine = _BoundaryEngine(
        [
            _boundary(
                stopped_gracefully=False,
                reaped=False,
                stop_effective_at=None,
                valuation=None,
                reap=reaped,
            )
        ]
    )
    supervisor = _supervisor(engine, store)

    await supervisor.apply_non_running(delivery_id="stop-delivery", verified=stop)

    assert _outcomes(database, 2) == []
    assert TERMINAL_VALUATION_KIND not in [
        fact["kind"] for batch in _batches(database) for fact in batch["facts"]
    ]
    assert (await store.load_engine_lifecycle_state(stop)).stop_reap_pending is True

    # A duplicate delivery while the node is being reaped does not stop it again.
    await supervisor.apply_non_running(delivery_id="stop-duplicate", verified=stop)
    assert engine.boundary_calls == 1

    reaped_at = STOP_REQUESTED + timedelta(seconds=41)
    reaped.set_result(reaped_at)
    await supervisor.reaps_settled()

    assert _outcomes(database, 2) == [("applied", "applied")]
    terminal = _terminal(database)
    assert terminal["reason_code"] == "stop_timeout"
    assert terminal["stop_effective_at"] == "2026-10-14T10:05:41Z"
    assert (await store.load_engine_lifecycle_state(stop)).stop_reap_pending is False


@pytest.mark.asyncio
async def test_a_restart_settles_a_pending_reap_as_a_process_exit(tmp_path: Path) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _store(database)
    await _apply(store, _command(1, "running"), delivery_id="run")
    stop = _command(2, "stopped")
    await store.record_desired_command(
        command=stop.command,
        command_fingerprint=stop.command_fingerprint,
        verification_receipt=stop.verification_receipt,
    )
    never: asyncio.Future[datetime] = asyncio.get_running_loop().create_future()
    await _supervisor(
        _BoundaryEngine(
            [
                _boundary(
                    stopped_gracefully=False,
                    reaped=False,
                    stop_effective_at=None,
                    valuation=None,
                    reap=never,
                )
            ]
        ),
        store,
    ).apply_non_running(delivery_id="stop-delivery", verified=stop)
    never.cancel()

    _, restarted_store = _store(database)
    restarted_engine = _BoundaryEngine([])
    await _supervisor(restarted_engine, restarted_store).recover_pending_stops()

    assert restarted_engine.boundary_calls == 0, "recovery commits; it never stops again"
    assert _outcomes(database, 2) == [("applied", "applied")]
    terminal = _terminal(database)
    assert terminal["reason_code"] == "process_exit_before_confirmation"
    assert terminal["stop_effective_at"] is None
    assert terminal["closes_generation"] == 1
    assert await restarted_store.list_recoverable_desired_command_identities() == ()
    assert await restarted_store.list_pending_stop_reaps() == ()


@pytest.mark.asyncio
async def test_a_stop_interrupted_by_a_process_exit_is_settled_on_redelivery(
    tmp_path: Path,
) -> None:
    """The previous process recorded its lease, stopped the node and died."""
    database = tmp_path / "runner-state.sqlite3"
    _, store = _store(database)
    await _apply(store, _command(1, "running"), delivery_id="run")
    stop = _command(2, "stopped")
    await _prepare_stop(store, stop, delivery_id="stop-before-exit")
    engine = _BoundaryEngine([_boundary(node_was_running=False, valuation=None)])

    await _supervisor(engine, store).apply_non_running(delivery_id="stop-redelivery", verified=stop)

    assert _terminal(database)["reason_code"] == "process_exit_before_confirmation"
    assert _terminal(database)["stop_effective_at"] is None


@pytest.mark.asyncio
async def test_a_failed_persist_is_retried_without_stopping_again(tmp_path: Path) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path)
    engine = _BoundaryEngine([_boundary()])
    supervisor = _supervisor(engine, store)
    original = store.commit_stop_applied_and_enqueue_terminal
    attempts = {"count": 0}

    async def flaky(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return await original(**kwargs)

    store.commit_stop_applied_and_enqueue_terminal = flaky  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        await supervisor.apply_non_running(delivery_id="stop-delivery", verified=stop)
    await supervisor.apply_non_running(delivery_id="stop-redelivery", verified=stop)

    assert engine.boundary_calls == 1
    terminal = _terminal(database)
    assert terminal["reason_code"] == "capture_persist_failed"
    assert _outcomes(database, 2) == [("applied", "applied")]


@pytest.mark.asyncio
async def test_a_confirmed_stop_through_the_supervisor(tmp_path: Path) -> None:
    database, _, store, _, stop, _ = await _running_then_stop(tmp_path)
    engine = _BoundaryEngine([_boundary()])

    await _supervisor(engine, store).apply_non_running(delivery_id="stop-delivery", verified=stop)

    assert _terminal(database)["outcome"] == "confirmed"
    assert _outcomes(database, 2) == [("applied", "applied")]


def test_boundary_causes_name_what_the_stop_could_not_establish() -> None:
    assert _boundary().stop_causes() == frozenset()
    assert _boundary(node_was_running=False, valuation=None).stop_causes() == {
        "engine_not_running_at_stop"
    }
    assert _boundary(stopped_gracefully=False, valuation=None).stop_causes() == {"stop_timeout"}
    assert _boundary(run_task_failed=True, valuation=None).stop_causes() == {"engine_task_failed"}
    assert _boundary(valuation=None).stop_causes() == {"valuation_unreliable"}
    assert _boundary(valuation=None, valuation_failure="valuation_unavailable").stop_causes() == {
        "valuation_unavailable"
    }
    with pytest.raises(ValueError):
        replace(_boundary(), reaped=False, reap=None)


def test_the_daemon_produces_terminal_facts_only_under_its_capability_flag() -> None:
    from types import SimpleNamespace

    from custos.cli._daemon import _terminal_valuation_settings

    declared = SimpleNamespace(
        capability_manifest={
            "runner_fact_contracts": {
                "settlement": {"schema_version": 1, "terminal_valuation": "v1"}
            }
        }
    )
    undeclared = SimpleNamespace(
        capability_manifest={"runner_fact_contracts": {"settlement": {"schema_version": 1}}}
    )

    settings = _terminal_valuation_settings(declared)
    assert settings is not None and settings.venue_for_connector("binance_perpetual") == "BINANCE"
    assert _terminal_valuation_settings(undeclared) is None
