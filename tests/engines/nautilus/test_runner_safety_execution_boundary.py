from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.common.providers import InstrumentProvider  # noqa: E402
from nautilus_trader.core.rust.model import AccountType, OmsType  # noqa: E402
from nautilus_trader.live.execution_client import LiveExecutionClient  # noqa: E402
from nautilus_trader.live.factories import LiveExecClientFactory  # noqa: E402
from nautilus_trader.model.identifiers import ClientId, Venue  # noqa: E402
from nautilus_trader.model.objects import Currency  # noqa: E402
from nautilus_trader.test_kit.stubs.component import TestComponentStubs  # noqa: E402

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig  # noqa: E402
from custos.core.order_reservation_boundary import RunnerReservationBoundary  # noqa: E402
from custos.engines.nautilus.runner_safety import (  # noqa: E402
    GuardedLiveExecutionClient,
    RunnerSafetyExecutionDispatch,
    guarded_exec_client_factory,
)

DEPLOYMENT_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
POLICY_ID = UUID("22222222-2222-4222-8222-222222222222")


class _Store:
    def __init__(self, log: list[tuple]) -> None:
        self.log = log
        self.reject_reservation = False
        self.reservations: dict[str, SimpleNamespace] = {}

    def reserve_order_notional(self, **kwargs):
        self.log.append(("reserve", kwargs))
        if self.reject_reservation:
            raise RuntimeError("runner cap exceeded")
        snapshot = SimpleNamespace(
            client_order_id=kwargs["client_order_id"],
            reserved_notional=kwargs["requested_notional"],
        )
        self.reservations[kwargs["client_order_id"]] = snapshot
        return snapshot

    def load_order_reservation(self, deployment_instance_id, client_order_id):
        del deployment_instance_id
        return self.reservations[client_order_id]

    def replace_order_reservation(self, **kwargs):
        self.log.append(("replace", kwargs))
        snapshot = SimpleNamespace(
            client_order_id=kwargs["client_order_id"],
            reserved_notional=kwargs["new_reserved_notional"],
        )
        self.reservations[kwargs["client_order_id"]] = snapshot
        return snapshot

    def release_order_reservation(self, **kwargs):
        self.log.append(("release", kwargs))
        return self.reservations.get(kwargs["client_order_id"])

    def record_order_fill(self, **kwargs):
        self.log.append(("fill", kwargs))
        return self.reservations.get(kwargs["client_order_id"])

    def record_position_reduction(self, **kwargs):
        self.log.append(("reduce", kwargs))
        return self.reservations.get(kwargs["client_order_id"])


class _Semantics:
    def order_notional(self, order) -> Decimal:
        return Decimal(str(order.notional))

    def modified_order_notional(self, command) -> Decimal:
        return Decimal(str(command.notional))

    def fill_notional(self, event) -> Decimal:
        return Decimal(str(event.notional))

    def order_is_risk_reducing(self, order) -> bool:
        return bool(order.reduce_only)

    def event_is_risk_reducing(self, event) -> bool:
        return bool(event.reduce_only)


class _InnerClient:
    def __init__(self, log: list[tuple]) -> None:
        self.log = log
        self.rejections: list[tuple] = []

    def submit_order(self, command) -> None:
        self.log.append(("submit", command.order.client_order_id))

    def submit_order_list(self, command) -> None:
        self.log.append(
            ("submit_list", tuple(o.client_order_id for o in command.order_list.orders))
        )

    def modify_order(self, command) -> None:
        self.log.append(("modify_upstream", command.client_order_id))

    def cancel_order(self, command) -> None:
        self.log.append(("cancel_upstream", command.client_order_id))

    def cancel_all_orders(self, command) -> None:
        self.log.append(("cancel_all_upstream", command.command_id))

    def batch_cancel_orders(self, command) -> None:
        self.log.append(("batch_cancel_upstream", command.command_id))

    def generate_order_rejected(
        self,
        strategy_id,
        instrument_id,
        client_order_id,
        reason,
        ts_event,
    ) -> None:
        self.rejections.append((strategy_id, instrument_id, client_order_id, reason, ts_event))

    def generate_order_modify_rejected(
        self,
        strategy_id,
        instrument_id,
        client_order_id,
        venue_order_id,
        reason,
        ts_event,
    ) -> None:
        self.rejections.append(
            (
                strategy_id,
                instrument_id,
                client_order_id,
                venue_order_id,
                reason,
                ts_event,
            )
        )


def _order(
    client_order_id: str,
    *,
    notional: str = "25",
    reduce_only: bool = False,
):
    return SimpleNamespace(
        client_order_id=client_order_id,
        strategy_id="STRATEGY-001",
        instrument_id="BTCUSDT-PERP.BINANCE",
        notional=notional,
        reduce_only=reduce_only,
    )


def _submit_command(order, command_id: str = "submit-1"):
    return SimpleNamespace(order=order, command_id=command_id)


def _breaker() -> FallbackBreaker:
    return FallbackBreaker(
        FallbackBreakerConfig(
            max_notional=Decimal("1000"),
            max_drawdown_pct=Decimal("10"),
        )
    )


def _boundary(
    store: _Store,
    *,
    fallback_breaker: FallbackBreaker | None = None,
) -> RunnerReservationBoundary:
    return RunnerReservationBoundary(
        store=store,
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        policy_id=POLICY_ID,
        fallback_breaker=fallback_breaker or _breaker(),
        semantics=_Semantics(),
    )


def test_direct_submit_reserves_before_the_upstream_client() -> None:
    log: list[tuple] = []
    dispatch = RunnerSafetyExecutionDispatch(
        inner=_InnerClient(log),
        boundary=_boundary(_Store(log)),
        timestamp_ns=lambda: 17,
    )

    dispatch.submit_order(_submit_command(_order("order-1")))

    assert [entry[0] for entry in log] == ["reserve", "submit"]
    assert log[0][1]["deployment_instance_id"] == DEPLOYMENT_INSTANCE_ID
    assert log[0][1]["policy_id"] == POLICY_ID
    assert log[0][1]["requested_notional"] == Decimal("25")


def test_cap_rejection_emits_standard_order_rejected_without_submit() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reject_reservation = True
    inner = _InnerClient(log)
    dispatch = RunnerSafetyExecutionDispatch(
        inner=inner,
        boundary=_boundary(store),
        timestamp_ns=lambda: 23,
    )

    dispatch.submit_order(_submit_command(_order("order-denied")))

    assert [entry[0] for entry in log] == ["reserve"]
    assert inner.rejections == [
        (
            "STRATEGY-001",
            "BTCUSDT-PERP.BINANCE",
            "order-denied",
            "custos_runner_notional_policy_rejected",
            23,
        )
    ]


def test_risk_reducing_and_cancel_commands_are_never_blocked() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reject_reservation = True
    inner = _InnerClient(log)
    dispatch = RunnerSafetyExecutionDispatch(
        inner=inner,
        boundary=_boundary(store),
        timestamp_ns=lambda: 29,
    )

    dispatch.submit_order(_submit_command(_order("reduce-1", notional="500", reduce_only=True)))
    dispatch.cancel_order(SimpleNamespace(client_order_id="reduce-1", command_id="cancel-1"))

    assert [entry[0] for entry in log] == ["submit", "cancel_upstream"]
    assert inner.rejections == []


def test_frozen_breaker_rejects_risk_increasing_but_not_reduce_only_or_cancel() -> None:
    log: list[tuple] = []
    breaker = _breaker()
    breaker.fail_closed("portfolio_snapshot_unreliable")
    inner = _InnerClient(log)
    dispatch = RunnerSafetyExecutionDispatch(
        inner=inner,
        boundary=_boundary(_Store(log), fallback_breaker=breaker),
        timestamp_ns=lambda: 30,
    )

    dispatch.submit_order(_submit_command(_order("risk-increasing")))
    dispatch.submit_order(_submit_command(_order("reduce-only", reduce_only=True)))
    dispatch.cancel_order(SimpleNamespace(client_order_id="reduce-only", command_id="cancel"))

    assert [entry[0] for entry in log] == ["submit", "cancel_upstream"]
    assert inner.rejections[0][2] == "risk-increasing"


def test_modify_reserves_new_notional_before_upstream() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-2",
        policy_id=POLICY_ID,
        requested_notional=Decimal("10"),
    )
    log.clear()
    dispatch = RunnerSafetyExecutionDispatch(
        inner=_InnerClient(log),
        boundary=_boundary(store),
        timestamp_ns=lambda: 31,
    )
    command = SimpleNamespace(
        command_id="modify-1",
        client_order_id="order-2",
        strategy_id="STRATEGY-001",
        instrument_id="BTCUSDT-PERP.BINANCE",
        venue_order_id="venue-2",
        notional="40",
    )

    dispatch.modify_order(command)

    assert [entry[0] for entry in log] == ["replace", "modify_upstream"]
    assert log[0][1]["new_reserved_notional"] == Decimal("40")


class OrderFilled:
    def __init__(self, *, reduce_only: bool = False) -> None:
        self.event_id = "fill-event-1"
        self.client_order_id = "order-3"
        self.notional = "7"
        self.reduce_only = reduce_only

    @classmethod
    def to_dict(cls, event):
        return {
            "event_id": event.event_id,
            "client_order_id": event.client_order_id,
        }


class OrderCanceled:
    event_id = "cancel-event-1"
    client_order_id = "order-3"

    @classmethod
    def to_dict(cls, event):
        return {
            "event_id": event.event_id,
            "client_order_id": event.client_order_id,
        }


class _MessageBus:
    def __init__(self) -> None:
        self.subscriptions: dict[str, object] = {}

    def subscribe(self, topic: str, handler) -> None:
        self.subscriptions[topic] = handler


def test_order_events_advance_fill_and_cancel_reservations() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-3",
        policy_id=POLICY_ID,
        requested_notional=Decimal("25"),
    )
    log.clear()
    boundary = _boundary(store)
    bus = _MessageBus()
    boundary.bootstrap(bus)

    handler = bus.subscriptions["events.order.*"]
    handler(OrderFilled())
    handler(OrderCanceled())

    assert [entry[0] for entry in log] == ["fill", "release"]
    assert log[0][1]["fill_notional"] == Decimal("7")
    assert log[1][1]["reason"] == "canceled"


def test_reduce_only_fill_releases_open_exposure_when_reservation_exists() -> None:
    log: list[tuple] = []
    store = _Store(log)
    store.reserve_order_notional(
        event_id="seed",
        deployment_instance_id=DEPLOYMENT_INSTANCE_ID,
        client_order_id="order-3",
        policy_id=POLICY_ID,
        requested_notional=Decimal("0"),
    )
    log.clear()
    boundary = _boundary(store)

    boundary.on_order_event(OrderFilled(reduce_only=True))

    assert [entry[0] for entry in log] == ["reduce"]
    assert log[0][1]["reduction_notional"] == Decimal("7")


class _UpstreamFactory(LiveExecClientFactory):
    @staticmethod
    def create(**kwargs):
        return SimpleNamespace(kwargs=kwargs)


def test_guarded_factory_remains_a_public_factory_and_preserves_adapter_name() -> None:
    boundary = _boundary(_Store([]))

    factory = guarded_exec_client_factory(_UpstreamFactory, boundary)

    assert issubclass(factory, LiveExecClientFactory)
    assert factory is not _UpstreamFactory
    assert factory.__name__ == _UpstreamFactory.__name__


class _DummyLiveExecutionClient(LiveExecutionClient):
    async def _connect(self) -> None:
        return None

    async def _disconnect(self) -> None:
        return None

    async def _submit_order(self, command) -> None:
        return None

    async def _submit_order_list(self, command) -> None:
        return None

    async def _modify_order(self, command) -> None:
        return None

    async def _cancel_order(self, command) -> None:
        return None

    async def _cancel_all_orders(self, command) -> None:
        return None

    async def _batch_cancel_orders(self, command) -> None:
        return None

    async def generate_order_status_report(self, command):
        return None

    async def generate_order_status_reports(self, command):
        return []

    async def generate_fill_reports(self, command):
        return []

    async def generate_position_status_reports(self, command):
        return []


def test_guarded_client_is_a_real_live_execution_client() -> None:
    loop = asyncio.new_event_loop()
    try:
        clock = TestComponentStubs.clock()
        message_bus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        inner = _DummyLiveExecutionClient(
            loop=loop,
            client_id=ClientId("BINANCE"),
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=Currency.from_str("USDT"),
            instrument_provider=InstrumentProvider(),
            msgbus=message_bus,
            cache=cache,
            clock=clock,
        )

        guarded = GuardedLiveExecutionClient(
            inner=inner,
            boundary=_boundary(_Store([])),
            loop=loop,
            msgbus=message_bus,
            cache=cache,
            clock=clock,
            config=None,
        )

        assert isinstance(guarded, LiveExecutionClient)
        assert guarded.id == inner.id
        assert guarded.venue == inner.venue
        assert guarded.account_id == inner.account_id
        assert guarded.is_connected is inner.is_connected
    finally:
        loop.close()


def test_an_id_the_venue_would_refuse_is_rejected_here_instead() -> None:
    """The venue's id-length limit is enforced at the boundary, not just in the builder.

    The id's shape is decided where the strategy config is built, and that is the only
    place it can be decided — the flags are read-only on the strategy. Which makes it a
    convention: a signed artifact whose adapter builds its own config, or a strategy
    passing an explicit client_order_id, reaches the venue without consulting that
    builder and reproduces the rejection of every order.

    Enforced here, that becomes an invariant regardless of who built the config, and the
    failure is local and named rather than a venue answering -4015 to everything.
    """

    log: list[tuple] = []
    inner = _InnerClient(log)
    dispatch = RunnerSafetyExecutionDispatch(
        inner=inner,
        boundary=_boundary(_Store(log)),
        timestamp_ns=lambda: 17,
    )

    # The exact id the venue refused: 44 characters, the shape before the fix.
    refused = "O-20260730-044937-dcb00e520b45569e83b0-000-2"
    dispatch.submit_order(_submit_command(_order(refused)))

    assert log == [], "the order reached the reservation or the venue despite an unusable id"
    assert [entry[3] for entry in inner.rejections] == [
        "custos_runner_client_order_id_too_long_for_venue"
    ], "rejected for the wrong reason, so the cause would not be diagnosable from the event"


def test_the_boundary_lets_the_shape_the_builder_produces_through() -> None:
    """The guard must not reject what the fix produces, or it would block every order."""

    log: list[tuple] = []
    inner = _InnerClient(log)
    dispatch = RunnerSafetyExecutionDispatch(
        inner=inner,
        boundary=_boundary(_Store(log)),
        timestamp_ns=lambda: 17,
    )

    # A hyphen-free UUID, which is what build_nautilus_base_config now asks for.
    dispatch.submit_order(_submit_command(_order("33e0789d38f9419aa8a0e00e98d07878")))

    assert [entry[0] for entry in log] == ["reserve", "submit"]
    assert inner.rejections == []


def test_one_unusable_id_fails_the_whole_order_list() -> None:
    """A partly-submitted bracket is worse than none: the venue would refuse that leg."""

    log: list[tuple] = []
    inner = _InnerClient(log)
    dispatch = RunnerSafetyExecutionDispatch(
        inner=inner,
        boundary=_boundary(_Store(log)),
        timestamp_ns=lambda: 17,
    )
    orders = (
        _order("33e0789d38f9419aa8a0e00e98d07878"),
        _order("O-20260730-044937-dcb00e520b45569e83b0-000-2"),
    )

    dispatch.submit_order_list(
        SimpleNamespace(order_list=SimpleNamespace(orders=orders), command_id="submit-list-1")
    )

    assert log == [], "part of the list reached the venue"
    assert len(inner.rejections) == 2, "both legs must be rejected, not only the unusable one"
    assert {entry[3] for entry in inner.rejections} == {
        "custos_runner_client_order_id_too_long_for_venue"
    }
