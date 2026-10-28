from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custos.core.execution_reservation_store import PlatformReservationStore
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.local_runtime_state import RuntimeAuthorityRegistry
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import RunnerStateAuthorityError
from tests.test_local_runtime_state import probe, stores
from tests.test_order_reservation import INSTANCE_A, INSTANCE_B, POLICY_ID, _store
from tests.test_runtime_authority import account


@pytest.fixture
def runtime_lease(tmp_path):
    registry = RuntimeAuthorityRegistry.initialize(tmp_path / "control", identity_probe=probe)
    lease = registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=stores(tmp_path / "run"),
        control_source="platform",
    )
    try:
        yield lease
    finally:
        lease.close()


def test_a_local_reservation_port_receives_a_runtime_without_platform_fields():
    run_id, policy_id = uuid4(), uuid4()
    reserved = []

    class NeutralStore:
        def reserve_order_notional_sync(
            self, *, runtime_id, event_id, client_order_id, policy_id, requested_notional
        ):
            reserved.append((runtime_id, event_id, client_order_id, policy_id, requested_notional))

    class Semantics:
        def order_instrument_id(self, order):
            return "BTCUSD.PAPERTRADE"

        def order_is_risk_reducing(self, order, already_reducing):
            return False

        def order_notional(self, order):
            return Decimal("100")

    boundary = RunnerReservationBoundary(
        store=NeutralStore(),
        runtime_id=run_id,
        policy_id=policy_id,
        fallback_breaker=FallbackBreaker(FallbackBreakerConfig(Decimal("1000"), Decimal("10"))),
        trading_mode="live",
        semantics=Semantics(),
    )
    boundary.before_submit_order(
        SimpleNamespace(
            id="local-command-1", order=SimpleNamespace(client_order_id="local-order-1")
        )
    )
    assert reserved == [
        (
            run_id,
            f"runner-order-reservation:{run_id}:submit:local-command-1:local-order-1",
            "local-order-1",
            policy_id,
            Decimal("100"),
        )
    ]


async def test_the_platform_adapter_keeps_real_instance_scope_and_stable_event_identity(tmp_path):
    store = _store(tmp_path / "platform.sqlite3")
    adapter = PlatformReservationStore(store, deployment_instance_id=INSTANCE_A)
    event_id = f"runner-order-reservation:{INSTANCE_A}:submit:command-1:order-1"
    fields = dict(
        event_id=event_id,
        runtime_id=INSTANCE_A,
        client_order_id="order-1",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )
    first = adapter.reserve_order_notional_sync(**fields)
    assert first.runtime_id == INSTANCE_A
    assert first.reserved_notional == Decimal("100")
    assert adapter.reserve_order_notional_sync(**fields) == first
    raw = store.load_order_reservation_sync(INSTANCE_A, "order-1")
    assert raw.deployment_instance_id == INSTANCE_A
    assert adapter.load_order_reservation_sync(INSTANCE_A, "order-1") == first
    assert (await adapter.load_runner_exposure(POLICY_ID)).total_exposure == Decimal("100")
    with pytest.raises(RunnerStateAuthorityError, match="bound platform instance"):
        adapter.reserve_order_notional_sync(**{**fields, "runtime_id": INSTANCE_B})
    assert not store.has_order_reservation_sync(INSTANCE_B, "order-1")


def test_a_platform_snapshot_cannot_be_mislabeled_as_another_runtime(tmp_path):
    store = _store(tmp_path / "misrouted.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve-b",
        deployment_instance_id=INSTANCE_B,
        client_order_id="order-b",
        policy_id=POLICY_ID,
        requested_notional=Decimal("50"),
    )

    class MisroutingPort:
        def load_order_reservation_sync(self, _instance, _order):
            return store.load_order_reservation_sync(INSTANCE_B, "order-b")

    adapter = PlatformReservationStore(MisroutingPort(), deployment_instance_id=INSTANCE_A)
    with pytest.raises(RunnerStateAuthorityError, match="result differs"):
        adapter.load_order_reservation_sync(INSTANCE_A, "order-b")
