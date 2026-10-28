"""Persistent cash-budget behavior over explicit synthetic internal observations."""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from uuid import uuid4

import pytest

from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from tests.test_local_reservations import local_risk as local_risk
from tests.test_local_reservations import reserve

UNIT = 10**18
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def timestamp(risk):
    elapsed = risk.clock[0] - EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 10**9 + elapsed.microseconds * 1000


def observation(risk, **changes):
    module = importlib.import_module("custos.local.loss_state")
    values = {
        "journal_id": risk.lease.journal_store.store_id,
        "checkpoint_sequence": 1,
        "source_digest": "0x" + "11" * 32,
        "source_timestamp_ns": timestamp(risk),
        "cash_equity_raw": 1000 * UNIT,
        "cashflow_counters": (1000 * UNIT, 0, 0),
        "position_ids": (),
        "unresolved_intent_ids": (),
        "queued_raw": 0,
        "debt_raw": 0,
        "cohort_ready": True,
        "adverse_pnl": (),
        "mark_timestamps_ns": (),
        "market_timestamps_ns": tuple(
            (identifier, timestamp(risk)) for identifier in risk.manifest.instrument_ids
        ),
    }
    values.update(changes)
    return module.CashRiskObservation(**values)


def initialized(risk):
    budget = risk.store.bind_loss_budget()
    initial = observation(risk)
    state = budget.initialize(initial)
    assert state.state == "ready"
    assert state.baseline_raw == 1000 * UNIT
    return budget, initial


def test_baseline_is_fixed_once_and_initialization_is_idempotent(local_risk):
    budget, initial = initialized(local_risk)
    first = budget.snapshot()
    count = local_risk.events.identity().sequence
    assert budget.initialize(initial) == first
    assert local_risk.events.identity().sequence == count
    with pytest.raises(RuntimeAuthorityError):
        budget.initialize(replace(initial, cash_equity_raw=900 * UNIT))
    assert budget.snapshot().baseline_raw == 1000 * UNIT


@pytest.mark.parametrize(
    "raw,expected", [(970 * UNIT + 1, "ready"), (970 * UNIT, "latched"), (969 * UNIT, "latched")]
)
def test_cash_loss_trips_at_exact_threshold_before_cohort_completion(local_risk, raw, expected):
    budget, initial = initialized(local_risk)
    sample = replace(
        initial,
        checkpoint_sequence=2,
        source_digest="0x" + "22" * 32,
        cash_equity_raw=raw,
        cohort_ready=False,
        position_ids=("PT-7",),
    )
    state = budget.observe(sample)
    assert state.state == ("pending" if expected == "ready" else expected)
    assert local_risk.blocker.is_frozen() == (expected == "latched")
    assert state.loss == max(Fraction(0), Fraction(1000 * UNIT - raw, UNIT))


def test_exact_adverse_pnl_combines_with_cash_without_fee_double_count(local_risk):
    budget, initial = initialized(local_risk)
    state = budget.observe(
        replace(
            initial,
            checkpoint_sequence=2,
            source_digest="0x" + "22" * 32,
            cash_equity_raw=990 * UNIT,
            position_ids=("PT-7",),
            adverse_pnl=(Fraction(-20),),
            mark_timestamps_ns=(timestamp(local_risk),),
        )
    )
    assert state.loss == 30
    assert state.state == "latched"


def test_ready_budget_allows_reservation_but_missing_baseline_is_retryable(local_risk):
    budget = local_risk.store.bind_loss_budget()
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk)
    assert not local_risk.blocker.is_frozen()
    budget.initialize(observation(local_risk))
    assert reserve(local_risk, event="reserve-after-baseline").reserved_notional == 500


def test_normal_pending_is_audited_without_latching_and_can_recover(local_risk):
    budget, initial = initialized(local_risk)
    pending = replace(
        initial, checkpoint_sequence=2, source_digest="0x" + "22" * 32, cohort_ready=False
    )
    result = budget.observe(pending)
    assert result.state == "pending"
    before = local_risk.events.identity().sequence
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk)
    assert local_risk.events.identity().sequence > before
    assert not local_risk.blocker.is_frozen()
    budget.observe(replace(pending, cohort_ready=True))
    assert reserve(local_risk, event="reserve-after-pending").reserved_notional == 500


def test_pending_origin_survives_restart_and_new_source_observations(local_risk):
    risk = local_risk
    budget, initial = initialized(risk)
    risk.clock[0] += timedelta(seconds=1)
    pending = replace(
        initial,
        checkpoint_sequence=2,
        source_digest="0x" + "22" * 32,
        source_timestamp_ns=timestamp(risk),
        cohort_ready=False,
    )
    first = budget.observe(pending)
    risk.lease.close()
    recovered = risk.make(previous=risk.authorization, recover=True)
    budget = recovered.store.bind_loss_budget()
    assert budget.snapshot().pending_since_ns == first.pending_since_ns
    risk.clock[0] += timedelta(seconds=risk.manifest.data_policy.max_account_age_seconds)
    state = budget.observe(
        replace(
            pending,
            checkpoint_sequence=3,
            source_digest="0x" + "33" * 32,
            source_timestamp_ns=timestamp(risk),
        )
    )
    assert state.pending_since_ns == first.pending_since_ns
    assert state.state == "latched"
    assert recovered.blocker.is_frozen()


def test_new_run_keeps_the_original_account_baseline(local_risk):
    risk = local_risk
    budget, _ = initialized(risk)
    identity = budget.snapshot().baseline_id
    risk.lease.retire()
    risk.lease.close()
    replacement = risk.make(previous=risk.authorization)
    restored = replacement.store.bind_loss_budget().snapshot()
    assert restored.baseline_id == identity
    assert restored.baseline_raw == 1000 * UNIT


@pytest.mark.parametrize(
    "change",
    [
        "future",
        "stale",
        "clock_back",
        "cashflow",
        "queued",
        "debt",
        "checkpoint_back",
        "checkpoint_conflict",
        "journal",
    ],
)
def test_source_faults_persistently_freeze_the_account(local_risk, change):
    budget, initial = initialized(local_risk)
    sample = replace(initial, checkpoint_sequence=2, source_digest="0x" + "22" * 32)
    if change == "future":
        sample = replace(sample, source_timestamp_ns=timestamp(local_risk) + 1)
    elif change == "stale":
        local_risk.clock[0] += timedelta(
            seconds=local_risk.manifest.data_policy.max_account_age_seconds
        )
    elif change == "clock_back":
        local_risk.clock[0] -= timedelta(microseconds=1)
    elif change == "cashflow":
        sample = replace(sample, cashflow_counters=(1001 * UNIT, 0, 0))
    elif change == "queued":
        sample = replace(sample, queued_raw=1)
    elif change == "debt":
        sample = replace(sample, debt_raw=1)
    elif change == "checkpoint_back":
        budget.observe(sample)
        sample = initial
    elif change == "checkpoint_conflict":
        sample = replace(initial, cash_equity_raw=999 * UNIT)
    else:
        sample = replace(sample, journal_id=uuid4())
    result = budget.observe(sample)
    assert result.state == "latched"
    assert local_risk.blocker.is_frozen()


def test_admission_rechecks_the_ready_deadline_even_for_a_replayed_reservation(local_risk):
    _, _ = initialized(local_risk)
    original = reserve(local_risk)
    local_risk.clock[0] += timedelta(
        seconds=local_risk.manifest.data_policy.max_account_age_seconds
    )
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk)
    assert local_risk.blocker.is_frozen()
    assert (
        local_risk.store.load_order_reservation_sync(local_risk.manifest.run_id, "entry")
        == original
    )


@pytest.mark.parametrize("damage", ["row", "metadata"])
def test_missing_budget_or_cleared_enable_marker_cannot_return_to_unbound_mode(local_risk, damage):
    _, _ = initialized(local_risk)
    with sqlite3.connect(local_risk.events.path) as connection:
        if damage == "row":
            connection.execute("DELETE FROM local_account_loss")
        else:
            connection.execute(
                "UPDATE local_risk_meta SET loss_budget_required=0,loss_budget_id=NULL"
            )
    with pytest.raises((RuntimeStorageUnavailable, RuntimeAuthorityError)):
        local_risk.store.bind_loss_budget()
    assert local_risk.blocker.is_frozen()


def test_first_binding_refuses_released_economic_history(local_risk):
    reserve(local_risk)
    local_risk.store.release_order_reservation_sync(
        event_id="confirmed-unsent",
        runtime_id=local_risk.manifest.run_id,
        client_order_id="entry",
        reason="rejected",
    )
    with pytest.raises(RuntimeAuthorityError):
        local_risk.store.bind_loss_budget()
    assert local_risk.blocker.is_frozen()


def test_audit_failure_rolls_back_baseline_and_freezes_native_risk(local_risk, monkeypatch):
    budget = local_risk.store.bind_loss_budget()
    original = local_risk.events._append_risk

    def fail(*args, **kwargs):
        raise OSError("injected loss audit storage failure")

    monkeypatch.setattr(local_risk.events, "_append_risk", fail)
    with pytest.raises(OSError):
        budget.initialize(observation(local_risk))
    assert local_risk.blocker.is_frozen()
    monkeypatch.setattr(local_risk.events, "_append_risk", original)
    with sqlite3.connect(local_risk.events.path) as connection:
        assert (
            connection.execute("SELECT baseline_json FROM local_account_loss").fetchone()[0] is None
        )
