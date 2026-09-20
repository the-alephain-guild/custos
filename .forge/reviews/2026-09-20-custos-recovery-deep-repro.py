"""Local evidence for additional recovery/accounting findings; no venue requests."""

from __future__ import annotations

import ast

# ruff: noqa: E402 -- standalone review probe imports repository-local fixtures.
import asyncio
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_command_intake import CommandDeliveryPolicy, CommandIntakeCoordinator
from custos.core.runner_command_runtime import (
    RunnerCommandRuntimeCoordinator,
    RunnerCommandRuntimeStatus,
)
from custos.core.runner_fact import execution_fill
from custos.core.runner_fact_producer import RunnerFactProductionLoop
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics
from tests import test_order_reservation as reservations
from tests import test_runner_command_runtime as commands
from tests import test_runner_fact_production_loop as periods
from tests import test_runner_fact_store as durable
from tests.engines.nautilus import test_runner_safety_execution_boundary as gates


def load_previous_probe():
    spec = importlib.util.spec_from_file_location(
        "audit_native_fixtures", ROOT / ".forge/reviews/2026-09-20-custos-deep-followup-repro.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def node_exit_is_not_recovered():
    with tempfile.TemporaryDirectory() as directory:
        _, store = durable._runner_fact_store(Path(directory) / "node.db")
        raw, subject, verified = durable._verified_command()
        await store.record_desired_command(
            command=verified.command,
            command_fingerprint=verified.command_fingerprint,
            verification_receipt=verified.verification_receipt,
        )
        await store.commit_applied_and_enqueue_lifecycle(
            delivery_id="applied",
            verified=verified,
            engine_handle="old-node",
            observed_status="ready",
        )
        instance = str(verified.command.deployment_instance_id)
        host = NtTradingNodeHost()
        authority = EngineLifecycleAuthority.from_verified_command(verified)
        host._lifecycle_authorities[instance] = authority

        async def failed_run():
            raise RuntimeError("fixture node loop ended")

        task = asyncio.create_task(failed_run())
        host._active_nodes[instance] = SimpleNamespace(task=task)
        host._runner_fact_contexts[instance] = (
            SimpleNamespace(deployment_instance_id=instance),
            None,
        )
        await asyncio.gather(task, return_exceptions=True)
        host._on_node_task_done(instance, task)
        assert host.runner_fact_deployments() == ()
        state = await store.load_engine_lifecycle_state(verified)
        assert state.observed_status == "ready"
        delivery = commands._Delivery(
            delivered_count=2, delivery_id="redelivery", subject=subject, data=raw
        )
        intake = CommandIntakeCoordinator(
            authenticator=durable._command_authenticator(),
            durability=store,
            policy=CommandDeliveryPolicy(),
        )
        result = await intake.process(delivery)
        assert result.status.value == "idempotent_terminal_replay" and delivery.events == ["ack"]
        calls = []
        for path in (ROOT / "src/custos").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "supervise_once"
                ):
                    calls.append(str(path))
        assert calls == []
        print(
            "RD-1: failed node leaves active observations; durable status remains ready; redelivery is ACK-only; lifecycle restart supervisor has no caller"
        )


async def heartbeat_failure_overrides_applied():
    with tempfile.TemporaryDirectory() as directory:
        _, store = durable._runner_fact_store(Path(directory) / "heartbeat.db")
        raw, subject, verified = durable._verified_command()
        signal = asyncio.Event()
        running = False

        class Delivery(commands._Delivery):
            async def in_progress(self):
                self.events.append("in_progress_failed")
                signal.set()
                raise ConnectionError("fixture ACK lease transport unavailable")

        delivery = Delivery(delivered_count=5, subject=subject, data=raw)
        policy = CommandDeliveryPolicy(in_progress_interval_seconds=0.01)
        intake = CommandIntakeCoordinator(
            authenticator=durable._command_authenticator(), durability=store, policy=policy
        )
        runtime = RunnerCommandRuntimeCoordinator(
            intake=intake,
            durability=store,
            release_resolver=object(),
            artifact_runtime=None,
            entry_point_loader=object(),
            credential_resolver=object(),
            engine_lifecycle=object(),
            delivery_policy=policy,
        )

        async def applied_operation(delivery_id, command):
            nonlocal running
            await asyncio.wait_for(signal.wait(), timeout=2)
            running = True
            await store.commit_applied_and_enqueue_lifecycle(
                delivery_id=delivery_id,
                verified=command,
                engine_handle="running-node",
                observed_status="ready",
            )
            return None, SimpleNamespace(activation_id="fixture"), SimpleNamespace()

        runtime._resolve_activate_apply = applied_operation
        result = await runtime.process(delivery)
        state = await store.load_engine_lifecycle_state(verified)
        assert result.status is RunnerCommandRuntimeStatus.TERMINAL_REJECTED
        assert running and state.observed_status == "quarantined" and delivery.events[-1] == "term"
        with store._outbox._connect() as db:
            outcomes = [
                row[0]
                for row in db.execute(
                    "SELECT outcome FROM command_outcomes ORDER BY recorded_at_ns"
                )
            ]
        assert outcomes == ["applied", "retry_exhausted"]
        print(
            "RD-2: ACK heartbeat fails, operation commits applied, then coordinator records retry_exhausted/quarantined while execution remains running"
        )


async def executed_fill_is_rolled_back():
    from nautilus_trader.model import OrderSide

    original = load_previous_probe()
    instrument, _, fill = original.native_position()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fills.db"
        store = reservations._store(path)
        store.reserve_order_notional_sync(
            event_id="reserve",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="order-1",
            policy_id=reservations.POLICY_ID,
            requested_notional=Decimal("90"),
        )
        cache = SimpleNamespace(
            instrument=lambda _: instrument, order=lambda _: SimpleNamespace(is_reduce_only=False)
        )
        breaker = gates._breaker()
        boundary = RunnerReservationBoundary(
            store=store,
            deployment_instance_id=reservations.INSTANCE_A,
            policy_id=reservations.POLICY_ID,
            fallback_breaker=breaker,
            semantics=NautilusCachedOrderSemantics(cache),
        )
        boundary.on_order_event(fill(OrderSide.BUY, "120.00", "1.000", "0", 1))
        row = store.load_order_reservation_sync(reservations.INSTANCE_A, "order-1")
        assert breaker.frozen and row.filled_quantity == 0 and row.reserved_notional == 90
        reopened = reservations._store(path)
        reopened.reserve_order_notional_sync(
            event_id="after-restart",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="additional",
            policy_id=reservations.POLICY_ID,
            requested_notional=Decimal("50"),
        )
        exposure = await reopened.load_runner_exposure(reservations.POLICY_ID)
        assert exposure.total_exposure == 140 and exposure.max_total_notional == 150
        assert Decimal("120") + Decimal("50") > exposure.max_total_notional
        print(
            "RD-3: executed fill 120 breaches per-order 100 and is not booked; after reopening, ledger allows another 50 with recorded total 140 though fill+reservation=170 > cap150"
        )


async def generation_period_mixes_scopes():
    import custos.core.runner_fact_producer as producer

    start = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    clock = [start + timedelta(seconds=10)]
    first = replace(durable._authority(), trading_mode="testnet")
    second = replace(first, generation=2)
    old_fill = execution_fill(
        event_id="80000000-0000-4000-8000-000000000001",
        venue="BINANCE",
        venue_trade_id="old-trade",
        venue_order_id="old-order",
        instrument="BTCUSDT-PERP.BINANCE",
        side="buy",
        quantity="1",
        price="100",
        fee="0",
        currency="USDT",
        occurred_at=start + timedelta(seconds=10),
    )
    external = {
        key: old_fill[key]
        for key in (
            "venue_trade_id",
            "venue_order_id",
            "instrument",
            "side",
            "quantity",
            "price",
            "fee",
            "currency",
            "occurred_at",
        )
    }

    def deployment(authority, at):
        return SimpleNamespace(
            authority=authority,
            deployment_instance_id=str(authority.deployment_instance_id),
            reconciliation_available=True,
            valuation_checkpoint_available=True,
            currency="USDT",
            reconciliation_coverage_started_at=at,
        )

    class Host(periods._ValuationHost):
        current = deployment(first, start)

        def runner_fact_deployments(self):
            return (self.current,)

        async def runner_fact_venue_ledger(self, *args):
            evidence = await super().runner_fact_venue_ledger(*args)
            return replace(evidence, fills=(external,))

    host = Host()
    emitter = periods._CapturingEmitter()
    stop = asyncio.Event()
    loop = RunnerFactProductionLoop(
        host=host, emitter=emitter, snapshot_interval_secs=1, period_secs=60, period_retry_secs=1
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    waits = 0

    async def step(stop_event, seconds):
        nonlocal waits
        waits += 1
        if waits == 1:
            host.current = deployment(second, start + timedelta(seconds=30))
            clock[0] = start + timedelta(seconds=61)
        else:
            stop_event.set()

    loop._wait = step
    real_datetime = producer.datetime
    try:
        producer.datetime = Clock
        await loop.run_periods(stop)
    finally:
        producer.datetime = real_datetime
    assert host.requests[0][1] == start
    assert all(authority.generation == 2 for authority, _ in emitter.emissions)
    assert any(f.get("fills") for _, facts in emitter.emissions for f in facts)
    source = (
        ROOT.parent / "crucible-rust/crates/store/src/runner_fact_reconciliation_projector.rs"
    ).read_text()
    query = re.search(
        r'let ledger_rows = sqlx::query\(\s*"(SELECT source_seq, payload, occurred_at.*?ORDER BY source_seq, fact_event_id)"',
        source,
        re.S,
    ).group(1)
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE TABLE runner_fact_reconciliation_ledger_events (tenant_id TEXT, trading_mode TEXT, runner_id TEXT, deployment_instance_id TEXT, deployment_spec_id TEXT, deployment_spec_digest TEXT, generation INTEGER, source_seq INTEGER, payload TEXT, occurred_at TEXT, fact_event_id TEXT)"
        )
        scope = (
            first.tenant_id,
            first.trading_mode,
            str(first.runner_id),
            str(first.deployment_instance_id),
            str(first.deployment_spec_id),
            first.deployment_spec_digest,
        )
        db.execute(
            "INSERT INTO runner_fact_reconciliation_ledger_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                *scope,
                1,
                1,
                json.dumps({**old_fill, "seq": 1}),
                old_fill["occurred_at"],
                old_fill["event_id"],
            ),
        )
        assert (
            len(
                db.execute(
                    query, {str(i): value for i, value in enumerate((*scope, 1, 100), 1)}
                ).fetchall()
            )
            == 1
        )
        assert (
            db.execute(
                query, {str(i): value for i, value in enumerate((*scope, 2, 100), 1)}
            ).fetchall()
            == []
        )
    finally:
        db.close()
    print(
        "RD-4: generation2 period includes old venue trade from 12:00:10; exact consumer generation filter removes its internal generation1 row (SQL exercised in SQLite, not PG)"
    )


async def plain_exit_is_refused_but_spent():
    from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

    from tests.toolkit import test_close_reduce_only_fallback as close_fixtures

    with tempfile.TemporaryDirectory() as directory:
        store = reservations._store(Path(directory) / "exit.db")
        store.reserve_order_notional_sync(
            event_id="entry",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="entry",
            policy_id=reservations.POLICY_ID,
            requested_notional=Decimal("100"),
        )
        store.record_order_fill_sync(
            event_id="entry-fill",
            deployment_instance_id=reservations.INSTANCE_A,
            client_order_id="entry",
            fill_notional=Decimal("100"),
            fill_quantity=Decimal("1"),
            position_id="position-existing",
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="sell",
        )
        boundary = RunnerReservationBoundary(
            store=store,
            deployment_instance_id=reservations.INSTANCE_A,
            policy_id=reservations.POLICY_ID,
            fallback_breaker=gates._breaker(),
            semantics=gates._Semantics(),
        )
        strategy = close_fixtures._make_strategy(1000)
        strategy._position.quantity = Decimal("1")
        calls = []
        context = close_fixtures._make_ctx(calls)

        def create(**values):
            calls.append(values)
            return gates._order("close-1", notional="100", reduce_only=values["reduce_only"])

        context.execution_manager.create_exit_order = create
        context.order_tracker.record_reduce_only_refusal()
        refusals = []
        gate = gates._gate(boundary, refusals)
        downstream = strategy.submit_order
        strategy.submit_order = lambda order: gate.submit_order(downstream, order)
        coordinator = SignalExecutionCoordinator(strategy)
        coordinator.execute_exit_for_pair(context, close_fixtures._exit_signal(), object())
        assert calls[0]["reduce_only"] is False and strategy.submitted == []
        assert context.order_tracker.plain_close_submitted
        assert refusals[0].reason_code == "custos_runner_notional_policy_rejected"
        strategy.now_ns += 10_000_000_000
        coordinator.execute_exit_for_pair(context, close_fixtures._exit_signal(), object())
        assert len(calls) == 1 and strategy.submitted == [] and not strategy._position.is_closed
        print(
            "RD-5: plain close of existing exposure100 is charged as new100 and rejected under cap150; tracker spends sole attempt though no order was submitted"
        )


async def main():
    await node_exit_is_not_recovered()
    await heartbeat_failure_overrides_applied()
    await executed_fill_is_rolled_back()
    await generation_period_mixes_scopes()
    await plain_exit_is_refused_but_spent()


if __name__ == "__main__":
    asyncio.run(main())
