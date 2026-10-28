"""Target inbox handoff is distinct from order and venue-flat facts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from custos.engines.nautilus.external_target_dispatcher import ExternalTargetDispatcher
from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)
from custos.engines.nautilus.host import ExternalSourceRuntimeScope
from tests.test_external_target_inbox import BAR, FIRST, NOW, directive


class Host:
    def __init__(self, scope: ExternalSourceRuntimeScope) -> None:
        self.scope = scope
        self.queued = []
        self.flattened = []
        self.refuse_queue = False
        self.flat_local_clear = True
        self.flat_local_checks = []

    def external_source_runtime_scope(self, instance_id: str) -> ExternalSourceRuntimeScope:
        assert instance_id == str(self.scope.authority.deployment_instance_id)
        return self.scope

    def queue_external_source_signal(self, authority, signal) -> bool:
        self.queued.append((authority, signal))
        if self.refuse_queue:
            raise RuntimeError("strategy queue failed")
        return True

    async def request_external_source_flatten(self, authority, *, risk_action=None) -> int:
        self.flattened.append((authority, risk_action))
        return 0

    async def external_source_flat_local_clear(self, authority, pair: str) -> bool:
        self.flat_local_checks.append((authority, pair))
        return self.flat_local_clear


def host(authority=FIRST) -> Host:
    return Host(
        ExternalSourceRuntimeScope(
            authority=authority,
            connector="okx_perpetual",
            allowed_pairs=frozenset({"ETH-USDT"}),
            authorized_leverage=3,
        )
    )


@pytest.mark.asyncio
async def test_entry_handoff_is_durable_but_not_an_order_fact(tmp_path: Path) -> None:
    path = tmp_path / "target.sqlite"
    inbox = ExternalTargetInbox(path)
    delivery = directive(FIRST)
    inbox.accept(delivery, received_at=NOW)
    runtime = host()
    receipt = await ExternalTargetDispatcher(inbox, runtime).process_once(FIRST, at=NOW)
    assert receipt is not None and receipt.status == "queued"
    assert len(runtime.queued) == 1
    assert runtime.queued[0][1].amount is None
    assert runtime.queued[0][1].metadata["target_delivery_id"] == delivery.delivery_id
    assert inbox.pending(FIRST, at=NOW) == []
    restarted = ExternalTargetInbox(path)
    assert restarted.recover_uncertain(FIRST.deployment_instance_id, at=NOW) == 1


@pytest.mark.asyncio
async def test_unknown_queue_outcome_blocks_following_entry(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    delivery = directive(FIRST)
    inbox.accept(delivery, received_at=NOW)
    runtime = host()
    runtime.refuse_queue = True
    with pytest.raises(RuntimeError, match="strategy queue failed"):
        await ExternalTargetDispatcher(inbox, runtime).process_once(FIRST, at=NOW)
    assert len(runtime.queued) == 1
    later_time = NOW + timedelta(minutes=30)
    later = directive(FIRST, target="LONG", bar=BAR + timedelta(minutes=30), received_at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="uncertain"):
        inbox.accept(later, received_at=later_time)


@pytest.mark.asyncio
async def test_cancelled_handoff_is_quarantined_before_new_entry(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    first = directive(FIRST)
    inbox.accept(first, received_at=NOW)
    runtime = host()

    def cancelled(authority, signal):
        raise asyncio.CancelledError()

    runtime.queue_external_source_signal = cancelled
    with pytest.raises(asyncio.CancelledError):
        await ExternalTargetDispatcher(inbox, runtime).process_once(FIRST, at=NOW)
    later_time = NOW + timedelta(minutes=30)
    later = directive(FIRST, target="LONG", bar=BAR + timedelta(minutes=30), received_at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="uncertain"):
        inbox.accept(later, received_at=later_time)


@pytest.mark.asyncio
async def test_flat_request_never_confirms_venue_flatness(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    risk = directive(FIRST, target="FLAT", risk_action="FLATTEN")
    inbox.accept(risk, received_at=NOW)
    runtime = host()
    receipt = await ExternalTargetDispatcher(inbox, runtime).process_once(FIRST, at=NOW)
    assert receipt is not None and receipt.status == "flat_requested"
    assert runtime.flattened == [(FIRST, "FLATTEN")]
    later_time = NOW + timedelta(minutes=30)
    later = directive(FIRST, target="LONG", bar=BAR + timedelta(minutes=30), received_at=later_time)
    with pytest.raises(ExternalTargetInboxError, match="flat confirmation"):
        inbox.accept(later, received_at=later_time)
    assert inbox.confirm_flat(risk.delivery_id, authority=FIRST, at=NOW)
    assert inbox.accept(later, received_at=later_time).status == "accepted"


@pytest.mark.asyncio
async def test_stale_runtime_generation_cannot_claim_a_delivery(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    delivery = directive(FIRST)
    inbox.accept(delivery, received_at=NOW)
    runtime = host(replace(FIRST, generation=2))
    with pytest.raises(RuntimeError, match="authority"):
        await ExternalTargetDispatcher(inbox, runtime).process_once(FIRST, at=NOW)
    assert inbox.pending(FIRST, at=NOW)[0].delivery_id == delivery.delivery_id
    assert runtime.queued == []
