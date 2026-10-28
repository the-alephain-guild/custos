"""Native-bound economic mutations require their dedicated dispatch/evidence path."""

import importlib
from dataclasses import replace
from decimal import Decimal

import pytest

from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from custos.local.reservations import LocalReservationStore
from tests.test_local_reservations import fill, reserve, snapshot
from tests.test_local_reservations import local_risk as local_risk


@pytest.mark.parametrize("operation", ["release", "replace"])
@pytest.mark.parametrize("another_store", [False, True])
def test_native_binding_refuses_legacy_reservation_reduction_on_every_store(
    local_risk, operation, another_store
):
    risk = local_risk
    reserve(risk)
    risk.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
    store = (
        LocalReservationStore(
            store=risk.events,
            lease=risk.lease,
            authorization=risk.authorization,
            risk_blocker=risk.blocker,
            clock=lambda: risk.clock[0],
        )
        if another_store
        else risk.store
    )
    request = {
        "event_id": "legacy-mutation",
        "runtime_id": risk.manifest.run_id,
        "client_order_id": "entry",
    }
    with pytest.raises(RuntimeAuthorityError):
        if operation == "release":
            store.release_order_reservation_sync(**request, reason="rejected")
        else:
            store.replace_order_reservation_sync(**request, new_reserved_notional=Decimal(0))
    assert snapshot(risk).reserved_notional == Decimal("500")
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize("operation", ["release", "replace"])
def test_a_prior_unbound_mutation_does_not_bypass_the_persistent_native_gate(local_risk, operation):
    risk = local_risk
    reserve(risk)
    request = {
        "event_id": "prior-unbound-mutation",
        "runtime_id": risk.manifest.run_id,
        "client_order_id": "entry",
    }

    def mutate():
        if operation == "release":
            return risk.store.release_order_reservation_sync(**request, reason="rejected")
        return risk.store.replace_order_reservation_sync(
            **request, new_reserved_notional=Decimal(0)
        )

    mutate()
    risk.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
    with pytest.raises(RuntimeAuthorityError):
        mutate()
    assert risk.blocker.is_frozen()


@pytest.fixture
def native_risk(local_risk):
    risk = local_risk
    risk.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
    risk.dispatch = risk.store.bind_native_dispatch(journal=risk.bindings.journal)
    return risk


def opening_binding(identifier="entry"):
    api = importlib.import_module("custos.core.order_nonexecution")
    return api.NativeDispatchBinding(
        client_order_id=identifier,
        native_account_id="PAPERTRADE-001",
        client_id="PAPERTRADE",
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="BUY",
        quantity=Decimal("500"),
        quote_quantity=True,
        reduce_only=False,
        target=None,
        submission_params_keccak256="0x" + "11" * 32,
    )


def no_fill(risk, binding=None, *, source="never_submitted"):
    api = importlib.import_module("custos.core.order_nonexecution")
    return api.NativeNoFillSettlement(
        binding=binding or opening_binding(),
        journal_id=risk.bindings.journal.store_id,
        journal_revision=12,
        sealed_at_revision=11,
        intent_id="0x" + "22" * 32,
        command_keccak256="0x" + "33" * 32,
        proof_digest="44" * 32,
        source=source,
    )


def prepare_native(risk, binding=None):
    reserve(risk)
    return risk.dispatch.prepare(binding=binding or opening_binding(), missing_revision=10)


def test_native_marker_is_durable_and_single_use(native_risk):
    risk = native_risk
    prepared = prepare_native(risk)
    assert prepared.state == "prepared"
    assert prepared.missing_revision == 10
    assert risk.dispatch.begin_dispatch("entry") is True
    assert risk.dispatch.begin_dispatch("entry") is False
    assert risk.dispatch.load("entry").state == "dispatched"
    assert snapshot(risk).reserved_notional == Decimal("500")


def test_current_unsent_withdrawal_releases_only_its_prepared_reservation(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.withdraw_unsent("entry")
    risk.dispatch.withdraw_unsent("entry")
    assert risk.dispatch.load("entry").state == "not_sent"
    assert snapshot(risk).reserved_notional == 0
    assert risk.dispatch.begin_dispatch("entry") is False


def test_dispatched_opening_cannot_use_local_unsent_withdrawal(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.withdraw_unsent("entry")
    assert snapshot(risk).reserved_notional == Decimal("500")
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize("source", ["never_submitted", "expired_with_coverage"])
def test_proven_no_fill_releases_opening_capacity_once_without_clearing_a_latch(
    native_risk, source
):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    risk.store.record_dispatch_unknown("entry", reason="native_outcome_unconfirmed")
    proof = no_fill(risk, source=source)
    risk.dispatch.settle_no_fill(proof)
    risk.dispatch.settle_no_fill(replace(proof, journal_revision=13))
    assert risk.dispatch.load("entry").state == "proven_no_fill"
    assert snapshot(risk).reserved_notional == 0
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize(
    "change", ["params", "instrument", "side", "quantity", "target", "revision"]
)
def test_no_fill_must_name_the_original_binding_and_follow_the_missing_observation(
    native_risk, change
):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    proof = no_fill(risk)
    if change == "revision":
        proof = replace(proof, sealed_at_revision=10)
    else:
        values = {
            "params": {"submission_params_keccak256": "0x" + "99" * 32},
            "instrument": {"instrument_id": "ETH-USD-PERP.PAPERTRADE"},
            "side": {"side": "SELL"},
            "quantity": {"quantity": Decimal("501")},
            "target": {"target": "7", "reduce_only": True, "quote_quantity": False},
        }
        proof = replace(proof, binding=replace(proof.binding, **values[change]))
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.settle_no_fill(proof)
    assert snapshot(risk).reserved_notional == Decimal("500")


def test_known_or_later_fill_cannot_be_erased_by_no_execution_proof(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    fill(risk)
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.settle_no_fill(no_fill(risk))
    assert snapshot(risk).filled_exposure == Decimal("500")


def test_conflicting_duplicate_proof_does_not_skip_its_identity_checks(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    proof = no_fill(risk)
    risk.dispatch.settle_no_fill(proof)
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.settle_no_fill(replace(proof, proof_digest="55" * 32))
    assert risk.blocker.is_frozen()


def test_late_fill_after_no_execution_proof_keeps_venue_economics_and_latches(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    risk.dispatch.settle_no_fill(no_fill(risk))
    with pytest.raises(RuntimeAuthorityError):
        fill(risk)
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert risk.blocker.is_frozen()
    with pytest.raises(RuntimeAuthorityError):
        fill(risk)
    assert snapshot(risk).filled_exposure == Decimal("500")


def prepare_native_close(risk, identifier="close-7", *, with_binding=True):
    import hashlib

    from custos.core.reduction_claims import ReductionTarget

    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    token = hashlib.sha256(b"native-entry-fill").hexdigest()
    risk.store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{token}:entry",
        runtime_id=risk.manifest.run_id,
        client_order_id="entry",
        fill_notional=Decimal("500"),
        fill_quantity=Decimal("0.005"),
        leaves_quantity=Decimal(0),
        position_id="PT-7",
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="BUY",
    )
    target = ReductionTarget(
        "PAPERTRADE-001",
        "PAPERTRADE",
        "PT-7",
        "BTC-USD-PERP.PAPERTRADE",
        "SELL",
        Decimal("0.005"),
        "66" * 32,
    )
    risk.store.reductions.prepare(closing_client_order_id=identifier, target=target)
    binding = replace(
        opening_binding(identifier),
        quantity=Decimal("0.005"),
        side="SELL",
        reduce_only=True,
        quote_quantity=False,
        target="7",
    )
    if with_binding:
        risk.dispatch.prepare(binding=binding, missing_revision=15)
    return binding, target


def test_no_fill_close_releases_only_the_claim_and_keeps_original_position_cost(native_risk):
    risk = native_risk
    binding, target = prepare_native_close(risk)
    assert risk.dispatch.begin_dispatch("close-7")
    assert risk.store.reductions.load("close-7").state == "dispatching"
    proof = replace(no_fill(risk, binding), sealed_at_revision=16, journal_revision=17)
    risk.dispatch.settle_no_fill(proof)
    assert risk.store.reductions.load("close-7").state == "proven_no_fill"
    assert snapshot(risk).filled_exposure == Decimal("500")
    risk.store.reductions.prepare(closing_client_order_id="close-next", target=target)


def test_prepared_close_withdrawal_settles_the_claim_without_releasing_position_cost(native_risk):
    risk = native_risk
    prepare_native_close(risk)
    risk.dispatch.withdraw_unsent("close-7")
    assert risk.store.reductions.load("close-7").state == "not_sent"
    assert snapshot(risk).filled_exposure == Decimal("500")


def test_pending_unusable_close_fact_blocks_no_fill_settlement(native_risk):
    import hashlib

    risk = native_risk
    binding, _ = prepare_native_close(risk)
    risk.dispatch.begin_dispatch("close-7")
    token = hashlib.sha256(b"unknown-close-fill").hexdigest()
    risk.store.retain_unapplied_fill(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:close-7",
        context="close-7",
        reducing=True,
        raw={"position_id": "PT-7"},
    )
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.settle_no_fill(
            replace(no_fill(risk, binding), sealed_at_revision=16, journal_revision=17)
        )
    assert risk.store.reductions.load("close-7").state == "dispatching"
    assert snapshot(risk).filled_exposure == Decimal("500")


def test_proof_observation_cannot_lag_the_fresh_guard_journal_floor(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    risk.bindings.journal.path.write_text(f"{risk.bindings.journal.store_id}:13")
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.settle_no_fill(no_fill(risk))
    assert snapshot(risk).reserved_notional == Decimal("500")


def test_recovered_prepared_dispatch_cannot_be_withdrawn_or_resent_without_native_proof(
    native_risk,
):
    risk = native_risk
    prepare_native(risk)
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    recovered.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
    dispatch = recovered.store.bind_native_dispatch(journal=recovered.bindings.journal)
    assert recovered.blocker.is_frozen()
    assert dispatch.begin_dispatch("entry") is False
    with pytest.raises(RuntimeAuthorityError):
        dispatch.withdraw_unsent("entry")
    assert snapshot(recovered).reserved_notional == Decimal("500")
    dispatch.settle_no_fill(no_fill(recovered))
    assert snapshot(recovered).reserved_notional == 0
    assert recovered.blocker.is_frozen()


def test_new_authorization_cannot_reuse_an_old_dispatch_identifier(native_risk):
    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    risk.dispatch.settle_no_fill(no_fill(risk))
    risk.lease.retire()
    risk.lease.close()
    successor = risk.make(previous=risk)
    successor.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
    dispatch = successor.store.bind_native_dispatch(journal=successor.bindings.journal)
    with pytest.raises(RuntimeAuthorityError):
        dispatch.prepare(binding=opening_binding(), missing_revision=20)


def test_no_fill_audit_failure_rolls_back_capacity_and_dispatch_settlement(
    native_risk, monkeypatch
):
    import sqlite3

    risk = native_risk
    prepare_native(risk)
    risk.dispatch.begin_dispatch("entry")
    append = risk.events._append_risk

    def fail_settlement(*args, **kwargs):
        if kwargs["risk"].operation == "native_no_fill":
            raise sqlite3.OperationalError("injected audit storage failure")
        return append(*args, **kwargs)

    monkeypatch.setattr(risk.events, "_append_risk", fail_settlement)
    with pytest.raises(RuntimeStorageUnavailable):
        risk.dispatch.settle_no_fill(no_fill(risk))
    assert snapshot(risk).reserved_notional == Decimal("500")
    assert risk.dispatch.load("entry").state == "dispatched"
    assert risk.blocker.is_frozen()


def test_native_marker_failure_rolls_back_close_claim_dispatching(native_risk):
    import sqlite3

    risk = native_risk
    prepare_native_close(risk)
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_marker BEFORE UPDATE ON local_native_dispatch WHEN NEW.state='dispatched' BEGIN SELECT RAISE(ABORT,'injected marker failure'); END"
        )
    with pytest.raises(RuntimeStorageUnavailable):
        risk.dispatch.begin_dispatch("close-7")
    assert risk.dispatch.load("close-7").state == "prepared"
    assert risk.store.reductions.load("close-7").state == "prepared"
    assert snapshot(risk).filled_exposure == Decimal("500")


@pytest.mark.parametrize("operation", ["begin_dispatch", "mark_not_sent"])
@pytest.mark.parametrize("bound", [True, False])
def test_managed_close_cannot_use_an_independent_legacy_claim_transition(
    native_risk, operation, bound
):
    risk = native_risk
    prepare_native_close(risk, with_binding=bound)
    with pytest.raises(RuntimeAuthorityError):
        getattr(risk.store.reductions, operation)("close-7")
    assert risk.store.reductions.load("close-7").state == "prepared"
    if bound:
        assert risk.dispatch.load("close-7").state == "prepared"
    else:
        assert risk.dispatch.load("close-7") is None


@pytest.mark.parametrize("state", ["dispatching", "unknown"])
def test_unsent_withdrawal_refuses_a_contradictory_already_dispatched_claim(native_risk, state):
    import sqlite3

    risk = native_risk
    prepare_native_close(risk)
    with sqlite3.connect(risk.events.path) as connection:
        connection.execute(
            "UPDATE local_reduction_claim SET state=? WHERE closing_client_order_id='close-7'",
            (state,),
        )
    with pytest.raises(RuntimeAuthorityError):
        risk.dispatch.withdraw_unsent("close-7")
    assert risk.store.reductions.load("close-7").state == state
    assert snapshot(risk).filled_exposure == Decimal("500")
