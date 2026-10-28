"""Adversarial source ordering and retained cash-budget evidence."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
from uuid import uuid4

import pytest

from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from tests.test_local_loss_budget import initialized, observation, timestamp
from tests.test_local_reservations import _Blocker, reserve
from tests.test_local_reservations import local_risk as local_risk


@pytest.mark.parametrize("limit", ["loss", "quote_age"])
def test_replacement_permit_immediately_rechecks_tighter_limits(local_risk, limit):
    risk = local_risk
    budget, initial = initialized(risk)
    values = {"checkpoint_sequence": 2, "source_digest": "0x" + "22" * 32}
    if limit == "loss":
        values["cash_equity_raw"] = 975 * 10**18
    else:
        values.update(
            position_ids=("PT-7",),
            adverse_pnl=(Fraction(0),),
            mark_timestamps_ns=(timestamp(risk),),
        )
    assert budget.observe(replace(initial, **values)).state == "ready"
    document = risk.manifest.model_dump(mode="json")
    document["run_id"] = str(uuid4())
    if limit == "loss":
        document["limits"]["max_loss_usd"] = "20"
    else:
        document["data_policy"]["max_quote_age_seconds"] = 1
        risk.clock[0] += timedelta(seconds=1)
    risk.lease.retire()
    risk.lease.close()
    replacement = risk.make(previous=risk, manifest_override=document)
    with pytest.raises(RuntimeAuthorityError):
        reserve(replacement)
    assert replacement.blocker.is_frozen()


@pytest.mark.parametrize("restart", [False, True])
def test_late_ready_cannot_erase_an_expired_pending_deadline(local_risk, restart):
    risk = local_risk
    budget, initial = initialized(risk)
    pending = replace(initial, cohort_ready=False)
    assert budget.observe(pending).state == "pending"
    if restart:
        risk.lease.close()
        risk = risk.make(previous=risk, recover=True)
        budget = risk.store.bind_loss_budget()
    risk.clock[0] += timedelta(seconds=risk.manifest.data_policy.max_account_age_seconds)
    late = replace(
        initial,
        checkpoint_sequence=2,
        source_digest="0x" + "22" * 32,
        source_timestamp_ns=timestamp(risk),
    )
    assert budget.observe(late).state == "latched"
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize("pending", [False, True])
def test_mark_watermark_cannot_regress_even_across_pending(local_risk, pending):
    budget, initial = initialized(local_risk)
    marked = replace(
        initial,
        checkpoint_sequence=2,
        source_digest="0x" + "22" * 32,
        position_ids=("PT-7",),
        adverse_pnl=(Fraction(-1),),
        mark_timestamps_ns=(timestamp(local_risk),),
    )
    assert budget.observe(marked).state == "ready"
    if pending:
        budget.observe(replace(marked, cohort_ready=False, adverse_pnl=(), mark_timestamps_ns=()))
    result = budget.observe(replace(marked, mark_timestamps_ns=(timestamp(local_risk) - 1,)))
    assert result.state == "latched"
    assert local_risk.blocker.is_frozen()


def test_pending_intents_can_resolve_without_changing_the_account_checkpoint(local_risk):
    budget, initial = initialized(local_risk)
    pending = replace(initial, unresolved_intent_ids=("intent-7",))
    assert budget.observe(pending).state == "pending"
    assert budget.observe(initial).state == "ready"
    assert not local_risk.blocker.is_frozen()


@pytest.mark.parametrize(
    "change",
    [
        "positive_pnl",
        "float_cash",
        "bool_cash",
        "mutable_ids",
        "duplicate_ids",
        "oversized_ids",
        "bad_hash",
        "zero_sequence",
    ],
)
def test_internal_observation_rejects_unsafe_numeric_and_identity_inputs(local_risk, change):
    values = {
        "positive_pnl": {
            "position_ids": ("PT-7",),
            "adverse_pnl": (Fraction(1),),
            "mark_timestamps_ns": (timestamp(local_risk),),
        },
        "float_cash": {"cash_equity_raw": 1.0},
        "bool_cash": {"cash_equity_raw": True},
        "mutable_ids": {"position_ids": []},
        "duplicate_ids": {"position_ids": ("PT-7", "PT-7")},
        "oversized_ids": {"unresolved_intent_ids": tuple(str(i) for i in range(257))},
        "bad_hash": {"source_digest": "0xBAD"},
        "zero_sequence": {"checkpoint_sequence": 0},
    }
    with pytest.raises(RuntimeAuthorityError):
        observation(local_risk, **values[change])


@pytest.mark.parametrize("damage", ["all_markers", "body", "schema3", "baseline_fields"])
def test_loss_history_or_audit_cannot_be_discarded_to_reinitialize(local_risk, damage):
    budget, _ = initialized(local_risk)
    with sqlite3.connect(local_risk.events.path) as connection:
        if damage == "all_markers":
            connection.execute("DELETE FROM local_account_loss")
            connection.execute(
                "UPDATE local_risk_meta SET loss_budget_required=0,loss_budget_id=NULL"
            )
        elif damage == "body":
            connection.execute("UPDATE local_account_loss SET observation_json='{}'")
        elif damage == "baseline_fields":
            connection.execute(
                "UPDATE local_account_loss SET baseline_json=NULL,baseline_audit_sequence=NULL,observation_json=NULL,observation_audit_sequence=NULL"
            )
        else:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE local_risk_meta SET schema_version=3")
    with pytest.raises((RuntimeStorageUnavailable, RuntimeAuthorityError)):
        budget.snapshot()
    assert local_risk.blocker.is_frozen()


def test_budget_cannot_repoint_to_an_old_valid_ready_audit(local_risk):
    budget, initial = initialized(local_risk)
    with sqlite3.connect(local_risk.events.path) as connection:
        old = connection.execute(
            "SELECT observation_json,observation_audit_sequence FROM local_account_loss"
        ).fetchone()
    budget.observe(replace(initial, cohort_ready=False))
    with sqlite3.connect(local_risk.events.path) as connection:
        connection.execute(
            "UPDATE local_account_loss SET observation_json=?,observation_audit_sequence=?", old
        )
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        reserve(local_risk)
    assert local_risk.blocker.is_frozen()


def test_fixed_clock_ready_pending_ready_has_a_new_transition_head(local_risk):
    budget, initial = initialized(local_risk)
    original_sequence = local_risk.events.identity().sequence
    budget.observe(replace(initial, cohort_ready=False))
    pending_sequence = local_risk.events.identity().sequence
    assert budget.observe(initial).state == "ready"
    assert local_risk.events.identity().sequence > pending_sequence > original_sequence
    final_sequence = local_risk.events.identity().sequence
    budget.observe(initial)
    assert local_risk.events.identity().sequence == final_sequence


@pytest.mark.parametrize("cause", ["loss", "source"])
def test_latched_budget_requires_its_original_persistent_account_latch(local_risk, cause):
    risk = local_risk
    budget, initial = initialized(risk)
    if cause == "source":
        assert budget.freeze_source(reason="account").state == "latched"
    else:
        assert (
            budget.observe(
                replace(
                    initial,
                    checkpoint_sequence=2,
                    source_digest="0x" + "22" * 32,
                    cash_equity_raw=970 * 10**18,
                )
            ).state
            == "latched"
        )
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute("UPDATE local_account_risk SET reason_code=NULL,source_event_id=NULL")
    risk.lease.close()
    blocker = _Blocker()
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        recovered = risk.make(previous=risk, recover=True, blocker=blocker)
        recovered.store.bind_loss_budget().observe(
            replace(initial, checkpoint_sequence=3, source_digest="0x" + "33" * 32)
        )
        reserve(recovered)
    assert blocker.is_frozen()


def test_missing_source_can_latch_without_fabricating_a_cash_observation(local_risk):
    risk = local_risk
    budget, initial = initialized(risk)
    first = budget.snapshot()
    state = budget.freeze_source(reason="account")
    assert state.state == "latched"
    assert state.baseline_raw == first.baseline_raw
    assert risk.blocker.is_frozen()
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    assert recovered.blocker.is_frozen()
    assert recovered.store.bind_loss_budget().observe(initial).state == "latched"


def test_admission_fault_cannot_be_erased_before_a_fresh_observation(local_risk):
    risk = local_risk
    _, initial = initialized(risk)
    risk.clock[0] += timedelta(seconds=risk.manifest.data_policy.max_account_age_seconds)
    with pytest.raises(RuntimeAuthorityError):
        reserve(risk)
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute("UPDATE local_account_risk SET reason_code=NULL,source_event_id=NULL")
    risk.lease.close()
    blocker = _Blocker()
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        recovered = risk.make(previous=risk, recover=True, blocker=blocker)
        recovered.store.bind_loss_budget().observe(
            replace(
                initial,
                checkpoint_sequence=2,
                source_digest="0x" + "22" * 32,
                source_timestamp_ns=timestamp(risk),
            )
        )
        reserve(recovered, event="after-erased-loss-latch")
    assert blocker.is_frozen()
