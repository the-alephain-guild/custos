"""Owned ledger groups sign frozen collector output under a real SQLite writer."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.core.okx_ledger_binding import OkxLedgerOwnerSlot
from custos.core.runner_fact import (
    RunnerFactEmitter,
    reconciliation_period_closed,
    venue_ledger_snapshot_facts,
)
from custos.engines.nautilus.okx_cash_flow_mapping import map_main_account_transfers
from tests.engines.nautilus.test_okx_bill_window import END, START, Source
from tests.engines.nautilus.test_okx_cash_flow_mapping import transfer
from tests.test_okx_ledger_binding import ScopeVault, running_store
from tests.test_runner_fact_store import _runner_authority


async def ledger_fixture(path):
    outbox, store, engine_authority, verified = await running_store(path)
    scope = verified.command.to_runtime_spec().credential_scope
    slot = OkxLedgerOwnerSlot()
    resolved = await slot.replace(
        store,
        engine_authority,
        VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
    )

    class RecordingSource(Source):
        def __init__(self):
            super().__init__({None: [transfer()]}, identities=[("42", "42")] * 2)
            self.account_responses = []

        def _get_exact(self, path, params, *, private=True):
            response = super()._get_exact(path, params, private=private)
            if path.endswith("/config"):
                self.account_responses.append(response)
            return response

    source = RecordingSource()
    raw = await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="42")
    resolved.owner.observe_account(source.account_responses[0])
    account_binding = resolved.owner.observe_account(source.account_responses[-1])
    mapping = map_main_account_transfers(
        raw, api_origin=raw.api_origin, trading_mode=raw.trading_mode
    )
    snapshot_id = uuid4()
    facts = venue_ledger_snapshot_facts(
        snapshot_id=snapshot_id,
        venue="OKX",
        sub_account=None,
        source="venue_api",
        watermark=raw.content_digest,
        coverage_from=START,
        observed_through=END,
        completeness={
            "balances_complete": False,
            "positions_complete": False,
            "fills_complete": False,
            "fees_complete": False,
            "cash_flows_complete": False,
        },
        balances=(),
        positions=(),
        fills=(),
        fees=(),
        cash_flows=mapping.cash_flows,
    )
    facts.append(
        reconciliation_period_closed(
            event_id=uuid4(),
            period="engineering-raw-window",
            period_started_at=START,
            closed_at=END,
            venue_snapshots=({"venue": "OKX", "snapshot_id": snapshot_id},),
        )
    )
    authority = replace(_runner_authority(verified), strategy_id=verified.command.strategy_id)
    emitter = RunnerFactEmitter(outbox, store._identity, lambda: None)
    return SimpleNamespace(
        outbox=outbox,
        store=store,
        engine_authority=engine_authority,
        verified=verified,
        slot=slot,
        owner=resolved.owner,
        account_binding=account_binding,
        raw=raw,
        batches=[[fact] for fact in facts],
        authority=authority,
        emitter=emitter,
    )


async def test_real_collector_mapping_freezes_and_signs_one_partial_ledger_group(tmp_path):
    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    ids = await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    pending = await fixture.outbox.pending()
    assert len(ids) == len(pending) == 3
    documents = [json.loads(batch.payload) for batch in pending]
    assert documents[1]["facts"][0]["cash_flows"][0]["amount"] == "100.000000000000000001"
    assert documents[0]["facts"][0]["completeness"]["cash_flows_complete"] is False
    assert [doc["source_seq_start"] for doc in documents] == [1, 2, 3]
    assert await fixture.emitter.emit_ledger_group(fixture.authority, capture) == ()


async def test_frozen_bytes_are_independent_of_mutable_collector_batches(tmp_path):
    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    frozen = capture.facts_bytes
    fixture.batches[1][0]["cash_flows"][0]["amount"] = "999"
    fixture.batches.clear()
    await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    assert capture.facts_bytes == frozen
    pending = await fixture.outbox.pending()
    assert (
        json.loads(pending[1].payload)["facts"][0]["cash_flows"][0]["amount"]
        == "100.000000000000000001"
    )


async def test_retirement_rejects_group_replay_without_changing_committed_delivery_bytes(tmp_path):
    import pytest

    from custos.core.okx_ledger_binding import OkxLedgerOwnerRetired

    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    capture = fixture.owner.freeze_group(
        fixture.authority, fixture.account_binding, fixture.raw.content_digest, fixture.batches
    )
    await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    before = [(item.batch_id, item.payload) for item in await fixture.outbox.pending()]
    fixture.owner.retire("stopped")
    with pytest.raises(OkxLedgerOwnerRetired):
        await fixture.emitter.emit_ledger_group(fixture.authority, capture)
    after = [(item.batch_id, item.payload) for item in await fixture.outbox.pending()]
    assert after == before
