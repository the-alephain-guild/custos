"""A reconciliation period that crosses a calendar month boundary also closes the
month for settlement. The built-in daemon is the only automatic producer of the
close Crucible's month-end completeness gate waits for."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from custos.core import runner_fact_producer as producer

_spec = importlib.util.spec_from_file_location(
    "loop_fixtures", Path(__file__).with_name("test_runner_fact_production_loop.py")
)
assert _spec and _spec.loader
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


def _deployment(started: datetime):
    instance_id = uuid4()
    authority = SimpleNamespace(
        stream_key=f"default:testnet:runner:{instance_id}",
        deployment_spec_id=uuid4(),
        trading_mode="testnet",
        generation=1,
    )
    return SimpleNamespace(
        authority=authority,
        deployment_instance_id=str(instance_id),
        reconciliation_available=True,
        reconciliation_coverage_started_at=started,
    )


async def _run_periods(start: datetime, ticks: list[datetime], monkeypatch):
    deployment = _deployment(start)
    clock = [ticks[0]]

    class Host(_fixtures._CoverageHost):
        def runner_fact_deployments(self):
            return (deployment,)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    emitter = _fixtures._CapturingEmitter()
    loop = producer.RunnerFactProductionLoop(
        host=Host(),
        emitter=emitter,
        snapshot_interval_secs=1,
        period_secs=60,
        period_retry_secs=1,
    )
    stop = asyncio.Event()
    remaining = list(ticks[1:])

    async def advance(stop_event, seconds):
        if remaining:
            clock[0] = remaining.pop(0)
        else:
            stop_event.set()

    loop._wait = advance
    monkeypatch.setattr(producer, "datetime", Clock)
    await loop.run_periods(stop)
    return [fact for _, batch in emitter.emissions for fact in batch], emitter


def test_a_period_crossing_the_month_boundary_closes_the_month_once(monkeypatch) -> None:
    start = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)
    facts, emitter = asyncio.run(
        _run_periods(
            start,
            [
                start + timedelta(seconds=20),
                start + timedelta(seconds=61),
                start + timedelta(seconds=121),
            ],
            monkeypatch,
        )
    )
    closes = [fact for fact in facts if fact["kind"] == "period_closed"]
    assert len(closes) == 1, [fact["kind"] for fact in facts]
    assert closes[0]["period"] == "2026-08"
    assert closes[0]["closed_at"] == "2026-09-01T00:00:00Z"
    # The close terminates its own batch: Crucible refuses a period close that
    # is not the last fact of the batch it arrives in.
    batches = [
        batch for _, batch in emitter.emissions if any(f["kind"] == "period_closed" for f in batch)
    ]
    assert len(batches) == 1 and batches[0][-1]["kind"] == "period_closed"
    # The reconciliation close of the crossing period precedes the month close.
    kinds = [fact["kind"] for fact in facts]
    assert kinds.index("reconciliation_period_closed") < kinds.index("period_closed")


def test_a_period_inside_one_month_closes_no_month(monkeypatch) -> None:
    start = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    facts, _ = asyncio.run(
        _run_periods(
            start, [start + timedelta(seconds=20), start + timedelta(seconds=61)], monkeypatch
        )
    )
    assert "reconciliation_period_closed" in [fact["kind"] for fact in facts]
    assert not [fact for fact in facts if fact["kind"] == "period_closed"]


def test_the_month_close_identity_is_stable_for_its_stream_and_period() -> None:
    authority = SimpleNamespace(stream_key="default:testnet:runner:one")
    first = producer._scoped_event_id(authority, "settlement_period", "2026-08")
    again = producer._scoped_event_id(authority, "settlement_period", "2026-08")
    other = producer._scoped_event_id(authority, "settlement_period", "2026-09")
    assert first == again and first != other
