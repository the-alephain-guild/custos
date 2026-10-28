"""Native-bound economic mutations require their dedicated dispatch/evidence path."""

from decimal import Decimal

import pytest

from custos.core.runtime_authority import RuntimeAuthorityError
from custos.local.reservations import LocalReservationStore
from tests.test_local_reservations import local_risk as local_risk
from tests.test_local_reservations import reserve, snapshot


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
