"""Required market quotes are independent from account checkpoints and positions."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from custos.core.runtime_authority import (
    RuntimeAuthorityError,
    RuntimeRiskPending,
    RuntimeStorageUnavailable,
)
from tests.test_local_loss_budget import observation, timestamp
from tests.test_local_native_dispatch import opening_binding
from tests.test_local_reservations import _Blocker, reserve
from tests.test_local_reservations import local_risk as local_risk

BTC = "BTC-USD-PERP.PAPERTRADE"
ETH = "ETH-USD-PERP.PAPERTRADE"


def sample(risk, **changes):
    return observation(risk, market_timestamps_ns=((BTC, timestamp(risk)),), **changes)


def start(risk):
    budget = risk.store.bind_loss_budget()
    initial = sample(risk)
    assert budget.initialize(initial).state == "ready"
    return budget, initial


def test_flat_baseline_requires_the_declared_market_quote(local_risk):
    budget = local_risk.store.bind_loss_budget()
    initial = replace(sample(local_risk), market_timestamps_ns=())
    assert budget.initialize(initial).state == "latched"
    assert budget.snapshot().baseline_raw is None


@pytest.mark.parametrize("offset", [-10_000_000_000, 1])
def test_baseline_refuses_an_expired_or_future_quote(local_risk, offset):
    budget = local_risk.store.bind_loss_budget()
    initial = replace(
        sample(local_risk), market_timestamps_ns=((BTC, timestamp(local_risk) + offset),)
    )
    assert budget.initialize(initial).state == "latched"
    assert budget.snapshot().baseline_raw is None


def test_flat_ready_deadline_includes_market_quote_age(local_risk):
    budget, initial = start(local_risk)
    assert budget.snapshot().ready_until_ns == timestamp(local_risk) + 10 * 10**9
    local_risk.clock[0] += timedelta(seconds=10)
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk)
    assert local_risk.blocker.is_frozen()


def test_quotes_can_advance_without_account_checkpoint_progress(local_risk):
    budget, initial = start(local_risk)
    local_risk.clock[0] += timedelta(seconds=1)
    current = replace(initial, market_timestamps_ns=((BTC, timestamp(local_risk)),))
    assert budget.observe(current).state == "ready"
    assert budget.snapshot().ready_until_ns == timestamp(local_risk) + 10 * 10**9
    assert not local_risk.blocker.is_frozen()


@pytest.mark.parametrize("restart", [False, True])
def test_pending_market_watermarks_survive_missing_quotes_and_restart(local_risk, restart):
    risk = local_risk
    budget, initial = start(risk)
    risk.clock[0] += timedelta(seconds=1)
    newer = timestamp(risk)
    pending = replace(initial, cohort_ready=False, market_timestamps_ns=((BTC, newer),))
    assert budget.observe(pending).state == "pending"
    assert budget.observe(replace(pending, market_timestamps_ns=())).state == "pending"
    if restart:
        risk.lease.close()
        risk = risk.make(previous=risk, recover=True)
        budget = risk.store.bind_loss_budget()
    regressed = replace(initial, market_timestamps_ns=((BTC, newer - 1),))
    assert budget.observe(regressed).state == "latched"
    assert risk.blocker.is_frozen()


def test_missing_market_quote_is_retryable_until_a_complete_observation(local_risk):
    budget, initial = start(local_risk)
    assert budget.observe(replace(initial, market_timestamps_ns=())).state == "pending"
    with pytest.raises(RuntimeRiskPending):
        reserve(local_risk)
    assert not local_risk.blocker.is_frozen()
    assert budget.observe(initial).state == "ready"
    assert reserve(local_risk, event="market-ready").reserved_notional == 500


def test_new_manifest_market_set_cannot_reuse_the_old_ready(local_risk):
    risk = local_risk
    _, initial = start(risk)
    document = risk.manifest.model_dump(mode="json")
    document["run_id"] = str(uuid4())
    document["instrument_ids"].append(ETH)
    risk.lease.retire()
    risk.lease.close()
    replacement = risk.make(previous=risk, manifest_override=document)
    budget = replacement.store.bind_loss_budget()
    with pytest.raises(RuntimeRiskPending):
        reserve(replacement)
    assert not replacement.blocker.is_frozen()
    assert (
        budget.observe(
            replace(initial, market_timestamps_ns=((BTC, timestamp(risk)), (ETH, timestamp(risk))))
        ).state
        == "ready"
    )
    assert reserve(replacement, event="expanded-market-ready").reserved_notional == 500
    assert budget.snapshot().baseline_raw == 1000 * 10**18


@pytest.mark.parametrize(
    "value", [[[BTC, 1]], ((BTC, True),), ((BTC, 1), (BTC, 2)), (("bad\x00", 1),), ((BTC, -1),)]
)
def test_market_watermark_inputs_are_bounded_immutable_and_exact(local_risk, value):
    with pytest.raises(RuntimeAuthorityError):
        replace(sample(local_risk), market_timestamps_ns=value)


@pytest.mark.parametrize("mode", ["reserve", "recover", "marker"])
def test_missing_markets_cannot_hide_an_already_expired_pending_deadline(local_risk, mode):
    risk = local_risk
    budget, initial = start(risk)
    dispatch = None
    if mode == "marker":
        reserve(risk)
        risk.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
        dispatch = risk.store.bind_native_dispatch(journal=risk.bindings.journal)
        dispatch.prepare(binding=opening_binding(), missing_revision=10)
    assert budget.observe(replace(initial, market_timestamps_ns=())).state == "pending"
    risk.clock[0] += timedelta(seconds=10)
    refreshed_account = replace(
        initial,
        checkpoint_sequence=2,
        source_digest="0x" + "22" * 32,
        source_timestamp_ns=timestamp(risk),
        market_timestamps_ns=(),
    )
    assert budget.observe(refreshed_account).state == "pending"
    risk.clock[0] += timedelta(seconds=6)
    if mode == "recover":
        risk.lease.close()
        risk = risk.make(previous=risk, recover=True)
    with pytest.raises(RuntimeAuthorityError):
        dispatch.begin_dispatch("entry") if dispatch else reserve(risk)
    assert risk.blocker.is_frozen()

    with sqlite3.connect(risk.events.path) as connection:
        assert (
            connection.execute("SELECT reason_code FROM local_account_risk").fetchone()[0]
            == "local_loss_pending_timeout"
        )
    if dispatch:
        assert dispatch.load("entry").state == "prepared"


def test_a_new_tighter_quote_policy_rechecks_the_flat_persisted_ready(local_risk):
    risk = local_risk
    start(risk)
    document = risk.manifest.model_dump(mode="json")
    document["run_id"] = str(uuid4())
    document["data_policy"]["max_quote_age_seconds"] = 1
    risk.clock[0] += timedelta(seconds=1)
    risk.lease.retire()
    risk.lease.close()
    replacement = risk.make(previous=risk, manifest_override=document)
    with pytest.raises(RuntimeAuthorityError):
        reserve(replacement)
    assert replacement.blocker.is_frozen()


def test_schema4_cash_history_cannot_be_reopened_as_market_ready(local_risk):

    risk = local_risk
    start(risk)
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE local_risk_meta SET schema_version=4")
    risk.lease.close()
    blocker = _Blocker()
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        risk.make(previous=risk, recover=True, blocker=blocker)
    assert blocker.is_frozen()
    with sqlite3.connect(risk.events.path) as connection:
        assert connection.execute("SELECT schema_version FROM local_risk_meta").fetchone()[0] == 4
