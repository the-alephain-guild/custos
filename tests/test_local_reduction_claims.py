"""Durable full-position exits share the account cost and evidence transaction."""

from __future__ import annotations

import hashlib
import os
import select
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from custos.core.reduction_claims import ReductionTarget
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from tests.test_local_reservations import NOW, fill, reserve, snapshot
from tests.test_local_reservations import local_risk as local_risk


@pytest.fixture(autouse=True)
def bind_actual_routing(local_risk):
    def bind(risk):
        risk.store.bind_reductions(account_id="PAPERTRADE-001", client_id="PAPERTRADE")
        return risk

    original_make = local_risk.make
    local_risk.make = lambda **kwargs: bind(original_make(**kwargs))
    bind(local_risk)


def target(entry="entry", *, side="SELL"):
    return ReductionTarget(
        account_id="PAPERTRADE-001",
        client_id="PAPERTRADE",
        position_id="position-" + entry,
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side=side,
        quantity=Decimal("0.005"),
        params_digest=hashlib.sha256(entry.encode()).hexdigest(),
    )


def open_position(risk, entry="entry", *, short=False):
    reserve(risk, order=entry)
    if not short:
        fill(risk, order=entry, trade="open-" + entry)
        return
    token = hashlib.sha256(("open-" + entry).encode()).hexdigest()
    risk.store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{token}:{entry}",
        runtime_id=risk.manifest.run_id,
        client_order_id=entry,
        fill_notional=Decimal("500"),
        fill_quantity=Decimal("0.005"),
        leaves_quantity=Decimal(0),
        position_id="position-" + entry,
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="SELL",
    )


def prepare(risk, close="close-entry", entry="entry", **changes):
    return risk.store.reductions.prepare(
        closing_client_order_id=close, target=replace(target(entry), **changes)
    )


def close_fill(risk, *, close="close-entry", entry="entry", trade="close-trade", **changes):
    token = hashlib.sha256(trade.encode()).hexdigest()
    fields = dict(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:{close}",
        runtime_id=risk.manifest.run_id,
        closing_client_order_id=close,
        position_id="position-" + entry,
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="SELL",
        reduction_notional=Decimal("480"),
        reduction_quantity=Decimal("0.005"),
    )
    fields.update(changes)
    return risk.store.record_claimed_reduction_sync(**fields)


def test_claim_is_bound_to_actual_permit_epoch_and_original_position(local_risk):
    risk = local_risk
    open_position(risk)
    claim = prepare(risk)
    assert claim.runtime_id == risk.manifest.run_id
    assert claim.permit_digest == risk.authorization.permit_digest == risk.lease.permit_digest
    assert claim.authorization_epoch == risk.lease.fence.authorization_epoch
    assert claim.process_generation == risk.lease.fence.process_generation
    assert claim.target == target()
    assert claim.state == "prepared"
    assert risk.events.risk_events(risk.manifest.run_id)[-1].risk.operation == "close_prepare"


def test_same_close_replay_does_not_dispatch_twice(local_risk):
    open_position(local_risk)
    first = prepare(local_risk)
    assert prepare(local_risk) == first
    assert local_risk.store.reductions.begin_dispatch("close-entry") is True
    assert local_risk.store.reductions.begin_dispatch("close-entry") is False
    assert prepare(local_risk).state == "dispatching"


def test_two_closes_for_one_position_have_one_durable_winner(local_risk):
    risk = local_risk
    open_position(risk)

    def attempt(close):
        try:
            return prepare(risk, close=close).closing_client_order_id
        except RuntimeAuthorityError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["close-a", "close-b"]))
    assert sum(value is not None for value in results) == 1
    assert len(risk.store.reductions.pending()) == 1


@pytest.mark.parametrize(
    "change",
    [{"quantity": Decimal("0.004")}, {"side": "BUY"}, {"instrument_id": "ETH-USD-PERP.PAPERTRADE"}],
)
def test_full_original_cost_position_must_match_the_target(local_risk, change):
    open_position(local_risk)
    with pytest.raises(RuntimeAuthorityError):
        prepare(local_risk, **change)
    assert not local_risk.store.reductions.pending()
    assert snapshot(local_risk).filled_exposure == Decimal("500")


def test_conflicting_close_id_cannot_retarget_another_position(local_risk):
    open_position(local_risk)
    open_position(local_risk, "second")
    prepare(local_risk)
    with pytest.raises(RuntimeAuthorityError):
        prepare(local_risk, entry="second")
    assert local_risk.store.reductions.load("close-entry").target.position_id == "position-entry"


def test_separate_hedged_position_claims_do_not_share_a_net_quantity(local_risk):
    open_position(local_risk)
    open_position(local_risk, "second", short=True)
    prepare(local_risk)
    prepare(local_risk, close="close-second", entry="second", side="BUY")
    assert len(local_risk.store.reductions.pending()) == 2
    local_risk.store.reductions.begin_dispatch("close-entry")
    local_risk.store.reductions.begin_dispatch("close-second")
    close_fill(local_risk)
    close_fill(local_risk, close="close-second", entry="second", trade="close-second", side="BUY")
    assert (
        snapshot(local_risk).filled_exposure == snapshot(local_risk, "second").filled_exposure == 0
    )


def test_unknown_dispatch_survives_restart_and_never_becomes_not_sent(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    assert risk.store.reductions.begin_dispatch("close-entry")
    risk.store.reductions.mark_unknown("close-entry")
    with pytest.raises(RuntimeAuthorityError):
        risk.store.reductions.mark_not_sent("close-entry")
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    assert recovered.blocker.is_frozen()
    assert recovered.store.reductions.load("close-entry").state == "unknown"
    assert recovered.store.reductions.begin_dispatch("close-entry") is False
    with pytest.raises(RuntimeAuthorityError):
        prepare(recovered, close="replacement")


def test_prepared_claim_from_a_previous_process_is_not_redispatched(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    assert recovered.blocker.is_frozen()
    assert recovered.store.reductions.begin_dispatch("close-entry") is False
    with pytest.raises(RuntimeAuthorityError):
        recovered.store.reductions.mark_not_sent("close-entry")


def test_proved_pre_native_failure_releases_only_a_current_prepared_claim(local_risk):
    open_position(local_risk)
    prepare(local_risk)
    local_risk.store.reductions.mark_not_sent("close-entry")
    local_risk.store.reductions.mark_not_sent("close-entry")
    assert not local_risk.store.reductions.pending()
    prepare(local_risk, close="retry")
    assert local_risk.store.reductions.load("close-entry").state == "not_sent"


def test_frozen_new_risk_and_expired_entry_permit_allow_exit_until_its_own_bound(local_risk):
    risk = local_risk
    open_position(risk)
    risk.blocker.freeze()
    risk.clock[0] = NOW + timedelta(days=2, hours=1)
    prepare(risk)
    risk.clock[0] = NOW + timedelta(days=3)
    with pytest.raises(RuntimeAuthorityError):
        risk.store.reductions.begin_dispatch("close-entry")
    assert risk.store.reductions.load("close-entry").state == "prepared"


def test_late_fill_after_expiry_settles_claim_and_entry_cost_atomically(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    risk.clock[0] = NOW + timedelta(days=4)
    first = close_fill(risk)
    assert first.filled_exposure == first.filled_quantity == 0
    assert risk.store.reductions.load("close-entry").state == "filled"
    assert not risk.store.reductions.pending()
    assert close_fill(risk) == first
    risk.store.reductions.mark_unknown("close-entry")
    assert risk.store.reductions.load("close-entry").state == "filled"


def test_recovered_late_fill_uses_durable_closing_identity(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    close_fill(recovered)
    assert snapshot(recovered).filled_exposure == 0
    assert recovered.store.reductions.load("close-entry").state == "filled"
    assert recovered.blocker.is_frozen()


@pytest.mark.parametrize(
    "change",
    [
        {"closing_client_order_id": "unknown"},
        {"reduction_quantity": Decimal("0.001")},
        {"side": "BUY"},
        {"position_id": "position-other"},
    ],
)
def test_mismatched_close_fill_is_pending_without_cost_or_claim_release(local_risk, change):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    with pytest.raises(RuntimeAuthorityError):
        close_fill(risk, **change)
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert risk.store.reductions.load("close-entry").state == "dispatching"
    assert risk.blocker.is_frozen()
    with closing(sqlite3.connect(risk.events.path)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM local_venue_fill WHERE action='reduce'"
            ).fetchone()[0]
            == 1
        )


def test_fill_and_claim_update_roll_back_together_if_audit_cannot_commit(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    with closing(sqlite3.connect(risk.events.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_close BEFORE INSERT ON local_venue_fill WHEN NEW.action='reduce' BEGIN SELECT RAISE(ABORT,'injected close failure'); END"
        )
    with pytest.raises(RuntimeStorageUnavailable):
        close_fill(risk)
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert risk.store.reductions.load("close-entry").state == "dispatching"


@pytest.mark.parametrize("operation", ["prepare", "dispatch"])
def test_failed_command_audit_never_leaves_a_dispatchable_half_transition(local_risk, operation):
    risk = local_risk
    open_position(risk)
    if operation == "dispatch":
        prepare(risk)
    with closing(sqlite3.connect(risk.events.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_close_audit BEFORE INSERT ON execution_event WHEN json_extract(NEW.payload,'$.operation') LIKE 'close_%' BEGIN SELECT RAISE(ABORT,'injected close audit failure'); END"
        )
    with pytest.raises(RuntimeStorageUnavailable):
        prepare(risk) if operation == "prepare" else risk.store.reductions.begin_dispatch(
            "close-entry"
        )
    claim = risk.store.reductions.load("close-entry")
    assert claim is None if operation == "prepare" else claim.state == "prepared"
    assert risk.blocker.is_frozen()


def test_concurrent_dispatch_marker_has_one_winner(local_risk):
    open_position(local_risk)
    prepare(local_risk)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: local_risk.store.reductions.begin_dispatch("close-entry"), range(2)
            )
        )
    assert sorted(results) == [False, True]


@pytest.mark.parametrize("change", [{"account_id": "OTHER"}, {"client_id": "OTHER"}])
def test_target_cannot_change_the_factory_bound_native_route(local_risk, change):
    open_position(local_risk)
    with pytest.raises(RuntimeAuthorityError):
        prepare(local_risk, **change)
    assert not local_risk.store.reductions.pending()


def test_new_claim_is_refused_at_the_exclusive_exit_deadline(local_risk):
    open_position(local_risk)
    local_risk.clock[0] = NOW + timedelta(days=3)
    with pytest.raises(RuntimeAuthorityError):
        prepare(local_risk)
    assert not local_risk.store.reductions.pending()


def test_other_run_cannot_close_an_inherited_position(local_risk):
    risk = local_risk
    open_position(risk)
    risk.lease.retire()
    risk.lease.close()
    replacement = risk.make(previous=risk)
    with pytest.raises(RuntimeAuthorityError):
        prepare(replacement)
    assert replacement.blocker.is_frozen()
    assert not replacement.store.reductions.pending()


def test_bound_local_route_cannot_release_cost_through_the_legacy_reduction_api(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    token = hashlib.sha256(b"legacy-close").hexdigest()
    with pytest.raises(RuntimeAuthorityError):
        risk.store.record_position_reduction_fifo_sync(
            event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:position-entry",
            runtime_id=risk.manifest.run_id,
            position_id="position-entry",
            reduction_notional=Decimal("480"),
            reduction_quantity=Decimal("0.005"),
        )
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert risk.store.reductions.load("close-entry").state == "dispatching"


def test_an_unbound_store_cannot_bypass_the_same_database_claim_mode(local_risk):
    from custos.local.reservations import LocalReservationStore

    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    unbound = LocalReservationStore(
        store=risk.events,
        lease=risk.lease,
        authorization=risk.authorization,
        risk_blocker=risk.blocker,
        clock=lambda: risk.clock[0],
    )
    token = hashlib.sha256(b"unbound-legacy-close").hexdigest()
    with pytest.raises(RuntimeAuthorityError):
        unbound.record_position_reduction_fifo_sync(
            event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:position-entry",
            runtime_id=risk.manifest.run_id,
            position_id="position-entry",
            reduction_notional=Decimal("480"),
            reduction_quantity=Decimal("0.005"),
        )
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert risk.store.reductions.load("close-entry").state == "dispatching"


def test_durable_native_binding_refuses_another_factory_route(local_risk):
    from custos.local.reservations import LocalReservationStore

    risk = local_risk
    another = LocalReservationStore(
        store=risk.events,
        lease=risk.lease,
        authorization=risk.authorization,
        risk_blocker=risk.blocker,
        clock=lambda: risk.clock[0],
    )
    with pytest.raises(RuntimeAuthorityError):
        another.bind_reductions(account_id="OTHER", client_id="PAPERTRADE")
    assert risk.blocker.is_frozen()


def test_missing_durable_binding_is_not_recreated_from_claim_history(local_risk):
    from custos.local.reservations import LocalReservationStore

    risk = local_risk
    open_position(risk)
    prepare(risk)
    with closing(sqlite3.connect(risk.events.path)) as connection, connection:
        connection.execute("UPDATE local_risk_meta SET native_account_id=NULL,client_id=NULL")
    with pytest.raises(RuntimeStorageUnavailable):
        LocalReservationStore(
            store=risk.events,
            lease=risk.lease,
            authorization=risk.authorization,
            risk_blocker=risk.blocker,
            clock=lambda: risk.clock[0],
        )
    assert risk.blocker.is_frozen()


def test_conflicting_context_retains_venue_identity_against_later_valid_replay(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    risk.store.reductions.begin_dispatch("close-entry")
    with pytest.raises(RuntimeAuthorityError):
        close_fill(risk, closing_client_order_id="unknown")
    with pytest.raises(RuntimeAuthorityError):
        close_fill(risk)
    assert snapshot(risk).filled_exposure == Decimal("500")


def test_inherited_fork_cannot_prepare_or_dispatch_parent_claim(local_risk):
    risk = local_risk
    open_position(risk)
    prepare(risk)
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(reader)
            rejected = 0
            for action in (
                lambda: prepare(risk),
                lambda: risk.store.reductions.begin_dispatch("close-entry"),
            ):
                try:
                    action()
                except RuntimeAuthorityError:
                    rejected += 1
            os.write(writer, str(rejected).encode())
        finally:
            os._exit(0)
    os.close(writer)
    try:
        assert select.select([reader], [], [], 10)[0]
        assert os.read(reader, 16) == b"2"
    finally:
        os.close(reader)
        os.waitpid(pid, 0)
    assert risk.store.reductions.begin_dispatch("close-entry") is True
