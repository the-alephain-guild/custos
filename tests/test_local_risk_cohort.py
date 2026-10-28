"""Original SQLite cohorts remain observations, not Native readiness proofs."""

import sqlite3
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from tests.test_local_native_dispatch import native_risk as native_risk
from tests.test_local_native_dispatch import prepare_native
from tests.test_local_reduction_claims import prepare as prepare_close
from tests.test_local_reservations import fill, reserve
from tests.test_local_reservations import local_risk as local_risk


def test_empty_cohort_keeps_the_actual_account_route_and_registered_store(native_risk):
    risk = native_risk
    before = risk.events.identity().sequence
    observed = risk.store.risk_cohort()
    assert observed.runtime_id == risk.manifest.run_id
    assert observed.account_scope == risk.permit.account_scope
    assert observed.journal_id == risk.lease.journal_store.store_id
    assert observed.native_account_id == "PAPERTRADE-001"
    assert observed.client_id == "PAPERTRADE"
    assert observed.orders == observed.closings == ()
    assert observed.state_sequence == before == risk.events.identity().sequence
    with pytest.raises(FrozenInstanceError):
        observed.state_sequence = 0


def test_current_prepared_and_dispatched_orders_are_distinct_in_the_cohort(native_risk):
    risk = native_risk
    reserve(risk)
    unprepared = risk.store.risk_cohort()
    assert len(unprepared.orders) == 1
    assert unprepared.orders[0].native_dispatch is None
    prepare_native(risk)
    prepared = risk.store.risk_cohort()
    assert prepared.orders[0].reservation.reserved_notional == 500
    assert prepared.orders[0].native_dispatch.state == "prepared"
    risk.dispatch.begin_dispatch("entry")
    dispatched = risk.store.risk_cohort()
    assert dispatched.orders[0].native_dispatch.state == "dispatched"
    assert prepared.orders[0].native_dispatch.state == "prepared"


def test_the_original_exact_cost_and_closing_claim_share_one_cohort(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    fill(risk)
    claim = prepare_close(risk)
    observed = risk.store.risk_cohort()
    cost = observed.orders[0]
    assert cost.reservation.filled_exposure == Decimal("500")
    assert cost.reservation.filled_quantity == Decimal("0.005")
    assert cost.position_id == "position-entry"
    assert cost.instrument_id == "BTC-USD-PERP.PAPERTRADE"
    assert cost.side == "BUY"
    assert observed.closings[0].claim == claim
    assert observed.closings[0].native_dispatch is None
    assert observed.state_sequence == risk.events.identity().sequence


def test_proven_current_unsent_withdrawal_is_not_reported_as_active_risk(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.withdraw_unsent("entry")
    observed = risk.store.risk_cohort()
    assert observed.orders == observed.closings == ()
    assert not risk.blocker.is_frozen()


@pytest.mark.parametrize("damage", ["route", "cost"])
def test_invalid_original_cohort_material_refuses_and_freezes(native_risk, damage):
    risk = native_risk
    prepare_native(risk)
    if damage == "cost":
        risk.dispatch.begin_dispatch("entry")
        fill(risk)
    with sqlite3.connect(risk.events.path) as connection:
        if damage == "route":
            connection.execute("UPDATE local_risk_meta SET client_id='OTHER'")
        else:
            connection.execute("UPDATE local_order_reservation SET filled_exposure='NaN'")
    with pytest.raises((RuntimeStorageUnavailable, RuntimeAuthorityError)):
        risk.store.risk_cohort()
    assert risk.blocker.is_frozen()


def test_unbound_amount_components_cannot_supply_a_native_cohort(local_risk):
    with pytest.raises(RuntimeAuthorityError):
        local_risk.store.risk_cohort()


def test_released_lease_cannot_keep_reading_a_cohort(native_risk):
    native_risk.lease.close()
    with pytest.raises(RuntimeAuthorityError):
        native_risk.store.risk_cohort()


@pytest.mark.parametrize("attempted", [False, True])
@pytest.mark.parametrize("damage", ["missing", "terminal"])
def test_orphaned_opening_marker_is_not_silently_an_empty_cohort(native_risk, attempted, damage):
    risk = native_risk
    prepare_native(risk)
    if attempted:
        risk.dispatch.begin_dispatch("entry")
    with sqlite3.connect(risk.events.path) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM local_order_reservation")
        else:
            connection.execute(
                "UPDATE local_order_reservation SET state='released',reserved_notional='0',filled_exposure='0',filled_quantity='0'"
            )
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        risk.store.risk_cohort()
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize("attempted", [False, True])
def test_orphaned_closing_marker_is_not_hidden_by_a_missing_claim(native_risk, attempted):
    from tests.test_local_native_dispatch import prepare_native_close

    risk = native_risk
    prepare_native_close(risk)
    if attempted:
        risk.dispatch.begin_dispatch("close-7")
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute("DELETE FROM local_reduction_claim")
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        risk.store.risk_cohort()
    assert risk.blocker.is_frozen()


def completed_round_trip(risk):
    import hashlib

    from tests.test_local_native_dispatch import prepare_native_close

    prepare_native_close(risk)
    risk.dispatch.begin_dispatch("close-7")
    token = hashlib.sha256(b"complete-cohort-close").hexdigest()
    risk.store.record_claimed_reduction_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:close-7",
        runtime_id=risk.manifest.run_id,
        closing_client_order_id="close-7",
        position_id="PT-7",
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="SELL",
        reduction_notional=Decimal("480"),
        reduction_quantity=Decimal("0.005"),
    )


@pytest.mark.parametrize("new_run", [False, True])
def test_successful_native_round_trip_does_not_leave_false_active_risk(native_risk, new_run):
    risk = native_risk
    completed_round_trip(risk)
    if new_run:
        risk.lease.retire()
        risk.lease.close()
        risk = risk.make(previous=risk)
        risk.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
        risk.store.bind_native_dispatch(journal=risk.bindings.journal)
    frozen_before_read = risk.blocker.is_frozen()
    observed = risk.store.risk_cohort()
    assert observed.orders == observed.closings == ()
    assert risk.blocker.is_frozen() == frozen_before_read
    if not new_run:
        assert not frozen_before_read


def test_terminal_economics_without_the_original_venue_fact_is_not_completion(native_risk):
    risk = native_risk
    completed_round_trip(risk)
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute("DELETE FROM local_venue_fill WHERE action='reduce'")
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        risk.store.risk_cohort()
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize(
    "state,notional,quantity",
    [("filled", "0", "0"), ("reserved", "500", "0.005"), ("partially_filled", "500", "0.005")],
)
def test_active_order_state_cannot_disagree_with_its_exact_economics(
    native_risk, state, notional, quantity
):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    fill(risk)
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute(
            "UPDATE local_order_reservation SET state=?,reserved_notional='0',filled_exposure=?,filled_quantity=?",
            (state, notional, quantity),
        )
    with pytest.raises((RuntimeAuthorityError, RuntimeStorageUnavailable)):
        risk.store.risk_cohort()
    assert risk.blocker.is_frozen()
