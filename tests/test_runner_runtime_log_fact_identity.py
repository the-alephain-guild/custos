"""Runner runtime-log identities remain deterministic and stream scoped."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactEmitter,
)
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64
CORRELATION_ID = UUID("73000000-0000-4000-8000-000000000007")


class CapturingEmitter:
    def __init__(self) -> None:
        self.facts: list[dict[str, object]] = []

    async def emit(
        self,
        authority: RunnerFactAuthority,
        facts: Sequence[Mapping[str, Any]],
    ) -> None:
        del authority
        self.facts.extend(dict(fact) for fact in facts)
        return None

    def emit_sync(
        self,
        authority: RunnerFactAuthority,
        facts: Sequence[Mapping[str, Any]],
    ) -> None:
        del authority
        self.facts.extend(dict(fact) for fact in facts)
        return None


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="acme",
        trading_mode="sandbox",
        runner_id=UUID("10000000-0000-4000-8000-000000000001"),
        deployment_instance_id=UUID("20000000-0000-4000-8000-000000000002"),
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000003"),
        deployment_spec_digest=SHA,
        generation=7,
        strategy_id=UUID("40000000-0000-4000-8000-000000000004"),
        capability_version_id=UUID("50000000-0000-4000-8000-000000000005"),
        capability_version=1,
        capability_manifest_digest=SHA,
    )


@pytest.mark.asyncio
async def test_runtime_log_event_identity_is_deterministic_within_one_stream() -> None:
    capture = CapturingEmitter()
    capability = RunnerCapabilityReceipt.load(
        ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
    )
    emitter = RunnerRuntimeLogEmitter(
        emitter=cast(RunnerFactEmitter, capture),
        capability=capability,
        redactor=RuntimeLogRedactor(),
    )
    authority = _authority()

    for _ in range(2):
        await emitter.emit(
            authority,
            level="WARN",
            component="local_cap",
            message="risk-increasing order denied",
            structured_fields={"reason_code": "runner_cap_exceeded"},
            correlation_id=CORRELATION_ID,
        )

    assert capture.facts[0]["event_id"] == capture.facts[1]["event_id"]


@pytest.mark.asyncio
async def test_same_runtime_log_content_cannot_collide_across_authority_streams() -> None:
    capture = CapturingEmitter()
    capability = RunnerCapabilityReceipt.load(
        ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
    )
    emitter = RunnerRuntimeLogEmitter(
        emitter=cast(RunnerFactEmitter, capture),
        capability=capability,
        redactor=RuntimeLogRedactor(),
    )
    base = _authority()
    authorities = (
        base,
        replace(base, tenant_id="other-tenant"),
        replace(base, trading_mode="testnet"),
        replace(base, runner_id=UUID("10000000-0000-4000-8000-000000000099")),
        replace(
            base,
            deployment_instance_id=UUID("20000000-0000-4000-8000-000000000099"),
        ),
    )

    for authority in authorities:
        await emitter.emit(
            authority,
            level="WARN",
            component="local_cap",
            message="risk-increasing order denied",
            structured_fields={"reason_code": "runner_cap_exceeded"},
            correlation_id=CORRELATION_ID,
        )

    assert len({fact["event_id"] for fact in capture.facts}) == len(authorities)


def test_runtime_log_can_be_committed_from_a_synchronous_engine_callback() -> None:
    capture = CapturingEmitter()
    capability = RunnerCapabilityReceipt.load(
        ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
    )
    emitter = RunnerRuntimeLogEmitter(
        emitter=cast(RunnerFactEmitter, capture),
        capability=capability,
        redactor=RuntimeLogRedactor(),
    )

    emitter.emit_sync(
        _authority(),
        level="WARN",
        component="custos.execution.order",
        message="order_rejected",
        structured_fields={
            "client_order_id": "supertrend-entry-1",
            "reason_code": "custos_runner_notional_policy_rejected",
        },
        correlation_id=CORRELATION_ID,
    )

    assert capture.facts[0]["message"] == "order_rejected"
