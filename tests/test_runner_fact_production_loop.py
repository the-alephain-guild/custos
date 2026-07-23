from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from custos.core.runner_fact_producer import RunnerFactProductionLoop


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
