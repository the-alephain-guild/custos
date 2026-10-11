"""The safety path asks a venue to cancel by the venue's answer, not by the clock.

Containment and both shutdown policies used to resend every cancel on every poll,
whatever the venue had said about the last one. On SoDEX spot testnet five orders
the venue no longer held drew a cancel every 0.2 seconds for an hour: each one came
back ``OrderNotFound``, the account hit its per-account rate limit, and nautilus
logged every re-cancel of a pending order with the order's whole event history --
a log that grew with the square of the run.

These tests drive the host's real loops against a venue double that answers each
cancel the way SoDEX did, and a clock the test moves by hand, so the back-off is
observed as requests that were or were not sent.

The SoDEX texts are judged by the SoDEX module on a Binance-shaped node, because the
fake builder can assemble that node without the venue's account check. Which module
classifies a text is the venue's declaration; what the host does with the class is
what is under test here.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

pytest.importorskip("nautilus_trader")

from custos.engines.nautilus import host as nautilus_host  # noqa: E402
from custos.engines.nautilus import venue_binance, venue_sodex  # noqa: E402
from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from custos.offline.safety import OfflineExposureGuard  # noqa: E402
from tests.fixtures.fake_live_node import FakeBuilder, FakeLiveNodeType  # noqa: E402
from tests.test_nt_trading_node_host import (  # noqa: E402
    _credential,
    _spec,
    _StrategyDouble,
    _VenuePosition,
)

# Verbatim from the probe run's logs (plan 06, S12).
NOT_FOUND = "order rejected: OrderNotFound"
TLS_EOF = (
    "transport failed: HTTP transport error: client error (Connect): tls handshake eof "
    "for url (https://testnet-gw.sodex.dev/api/v1/spot/trade/orders/batch)"
)
RATE_LIMITED = (
    'venue returned HTTP 429: {"code":-1,"error":"per-account order rate limit exceeded"}'
)
UNCLASSIFIED = "Unknown order sent."
CANCELED = object()


# Named as nautilus names them: the host's sinks dispatch on the event's type name.
@dataclass
class OrderPendingCancel:
    client_order_id: str


@dataclass
class OrderCancelRejected:
    client_order_id: str
    reason: str


@dataclass
class OrderCanceled:
    client_order_id: str


@dataclass
class _Order:
    client_order_id: str
    is_reduce_only: bool = False
    instrument_id: str = "BTCUSDT-PERP.BINANCE"
    is_pending_cancel: bool = False
    is_closed: bool = False


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True, slots=True)
class _Artifact:
    strategy: object
    activation_id: str = "activation-discipline"


class _Venue(_StrategyDouble):
    """A strategy whose venue answers each cancel the way ``answer`` says.

    Like nautilus, a cancel first marks the order pending; the venue's answer then
    either removes it (``CANCELED``), restores it with a refusal text, or never comes
    (``None``). Every request the safety path makes is recorded.
    """

    def __init__(self, spec: dict, answer, query_answer=None) -> None:
        super().__init__(spec)
        self.node = None
        self.answer = answer
        self.query_answer = query_answer
        self.cancels: list[str] = []
        self.queries: list[str] = []
        self.closes: list[str] = []
        self.cancelled_all: list[str] = []

    def _order(self, client_order_id: str) -> _Order:
        return next(o for o in self.node.cache.orders if o.client_order_id == client_order_id)

    def cancel_order(self, client_order_id: str) -> None:
        self.cancels.append(client_order_id)
        order = self._order(client_order_id)
        order.is_pending_cancel = True
        self.on_order_event(OrderPendingCancel(client_order_id))
        reply = self.answer(client_order_id)
        if reply is None:
            return
        if reply is CANCELED:
            self.node.cache.orders.remove(order)
            self.on_order_event(OrderCanceled(client_order_id))
            return
        order.is_pending_cancel = False
        self.on_order_event(OrderCancelRejected(client_order_id, reply))

    def cancel_all_orders(self, instrument_id: str) -> None:
        self.cancelled_all.append(instrument_id)

    def query_order(self, order) -> None:
        self.queries.append(order.client_order_id)
        if self.query_answer is not None:
            self.query_answer(self, order)

    def close_all_positions_with_fallback(self, instrument_id: str) -> None:
        self.closes.append(instrument_id)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, clock: _Clock):
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    built = NtTradingNodeHost()
    built._shutdown_poll_secs = 0.005  # noqa: SLF001 - fixture wiring
    built._containment_confirm_secs = 0.05  # noqa: SLF001 - fixture wiring
    built._containment_retry_secs = 0  # noqa: SLF001 - fixture wiring
    built._safety_clock = clock  # noqa: SLF001 - fixture wiring
    return built


@pytest.fixture
def sodex_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge refusals by the SoDEX module's declaration on the Binance-shaped node."""
    # Looked up at call time, so the same fixture serves a host that never asks.
    monkeypatch.setattr(
        venue_binance,
        "cancel_rejection_kind",
        lambda reason: venue_sodex.cancel_rejection_kind(reason),
        raising=False,
    )
    monkeypatch.setattr(
        venue_binance,
        "safety_request_budget",
        lambda: venue_sodex.safety_request_budget(),
        raising=False,
    )


async def _deployed(host, label: str, answer, *, orders=(), query_answer=None, **spec_overrides):
    spec = _spec(label, trading_mode="testnet", **spec_overrides)
    strategy = _Venue(spec, answer, query_answer)
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    instance = spec["deployment_instance_id"]
    strategy.node = host._active_nodes[instance].node  # noqa: SLF001 - fixture wiring
    strategy.node.cache.orders.extend(orders)
    return strategy, instance


async def _guard_stop(host, instance: str) -> None:
    """What the offline guard does each tick: a stop it gives up on after a deadline."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(host.stop(instance), timeout=0.03)


def _events(logs, name: str) -> list[dict]:
    return [event for event in logs if event["event"] == name]


@pytest.mark.asyncio
async def test_a_cancel_the_venue_cannot_find_is_sent_once_then_queried(
    host, clock, sodex_texts
) -> None:
    """OrderNotFound ends cancelling: three queries, then the order is left to reconciliation."""
    strategy, instance = await _deployed(
        host, "not-found", lambda _id: NOT_FOUND, orders=[_Order("ghost-1")]
    )
    strategy.node.cache.positions.append(_VenuePosition())

    await host.flatten_positions(instance, "drawdown_breach")
    assert strategy.cancels == ["ghost-1"]
    assert strategy.queries == ["ghost-1"], "not_found is handed to a query straight away"

    for step in (10, 20, 40, 60):
        clock.advance(step)
        with capture_logs() as logs:
            await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancels == ["ghost-1"], "an order the venue cannot find is never re-cancelled"
    assert strategy.queries == ["ghost-1"] * 3
    unconfirmed = _events(logs, "nt_containment_not_confirmed")
    assert unconfirmed, "an order nobody can account for is not containment"
    assert unconfirmed[-1]["residual_orders"] == {"venue_state_contradicted": 1}


@pytest.mark.asyncio
async def test_the_refusal_state_outlives_repeated_guard_stops(host, clock, sodex_texts) -> None:
    """Five guard ticks, each cancelling the stop at its deadline, still cancel once.

    The guard abandons a stop it has waited ten seconds for and starts another on its
    next tick. Kept in the stop's own coroutine, what the venue said would be gone by
    then, and every tick would cancel the ghost order again.
    """
    strategy, instance = await _deployed(
        host, "not-found-guard", lambda _id: NOT_FOUND, orders=[_Order("ghost-1")]
    )
    guard = OfflineExposureGuard(engine=host, deadline=0.03)
    watched = SimpleNamespace(deployment_instance_id=instance)

    for _ in range(6):
        await guard._end_deployment("spec", watched)  # noqa: SLF001 - the guard's own stop path
        clock.advance(15)

    assert strategy.cancels == ["ghost-1"]
    assert len(strategy.queries) <= 3
    assert host.attached(instance), "an unconfirmed preserve stop keeps the node"


@pytest.mark.asyncio
async def test_not_found_resolves_once_reconciliation_reports_the_order_closed(
    host, clock, sodex_texts
) -> None:
    def venue_has_it_canceled(strategy: _Venue, order: _Order) -> None:
        strategy.node.cache.orders.remove(order)
        strategy.on_order_event(OrderCanceled(order.client_order_id))

    strategy, instance = await _deployed(
        host,
        "not-found-resolved",
        lambda _id: NOT_FOUND,
        orders=[_Order("ghost-1")],
        query_answer=venue_has_it_canceled,
    )

    with capture_logs() as logs:
        await host.flatten_positions(instance, "drawdown_breach")
        await host.stop(instance)

    assert strategy.cancels == ["ghost-1"]
    assert strategy.queries == ["ghost-1"]
    assert _events(logs, "nt_containment_confirmed")
    assert not host.attached(instance)


@pytest.mark.asyncio
async def test_a_rate_limited_account_pauses_every_safety_request(host, clock, sodex_texts) -> None:
    """A 429 pauses cancels, closes and queries for the partition: 60s, then 120s."""
    strategy, instance = await _deployed(
        host, "rate-limited", lambda _id: RATE_LIMITED, orders=[_Order("entry-1")]
    )
    strategy.node.cache.positions.append(_VenuePosition())

    await host.flatten_positions(instance, "drawdown_breach")
    assert strategy.cancels == ["entry-1"]
    assert strategy.closes == [], "the close waits out the pause like everything else"

    clock.advance(59)
    await host.flatten_positions(instance, "drawdown_breach")
    assert strategy.cancels == ["entry-1"]
    assert strategy.closes == []
    assert strategy.queries == []

    clock.advance(1)
    await host.flatten_positions(instance, "drawdown_breach")
    assert strategy.cancels == ["entry-1", "entry-1"]

    clock.advance(119)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 2, "the second 429 doubles the pause"
    clock.advance(1)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 3
    assert strategy.cancelled_all == [], "a rate-limited account is never sent cancel_all"


@pytest.mark.asyncio
async def test_the_rate_limit_pause_belongs_to_the_partition(host, clock, sodex_texts) -> None:
    """The pause outlives the instance on that account and stays off other accounts."""
    limited, first = await _deployed(
        host, "partition-a", lambda _id: RATE_LIMITED, orders=[_Order("entry-a")]
    )
    await host.flatten_positions(first, "drawdown_breach")
    assert limited.cancels == ["entry-a"]
    limited.node.cache.orders.clear()
    await host.stop(first)

    other, second = await _deployed(
        host, "partition-b", lambda _id: CANCELED, orders=[_Order("entry-b")]
    )
    await host.flatten_positions(second, "drawdown_breach")
    assert other.cancels == ["entry-b"], "a different account is not paused"
    await host.stop(second)

    same_account = _spec("partition-a")["credential_scope"]
    again, third = await _deployed(
        host,
        "partition-a-next",
        lambda _id: CANCELED,
        orders=[_Order("entry-c")],
        credential_scope=same_account,
    )
    await host.flatten_positions(third, "drawdown_breach")
    assert again.cancels == [], "the account is still inside its 60 second pause"


@pytest.mark.asyncio
async def test_a_transport_failure_backs_off_and_abandons_after_ten(
    host, clock, sodex_texts
) -> None:
    strategy, instance = await _deployed(
        host, "transport", lambda _id: TLS_EOF, orders=[_Order("entry-1")]
    )

    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 1
    clock.advance(4.9)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 1, "the partition waits 5 seconds after a transport failure"
    clock.advance(0.1)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 2
    clock.advance(9.9)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 2, "then 10"
    clock.advance(0.1)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 3

    for _ in range(20):
        clock.advance(120)
        with capture_logs() as logs:
            await host.flatten_positions(instance, "drawdown_breach")

    assert len(strategy.cancels) == 11, "ten retries after the first refusal, then it stops"
    unconfirmed = _events(logs, "nt_containment_not_confirmed")
    assert unconfirmed[-1]["residual_orders"] == {"cancel_abandoned": 1}


@pytest.mark.asyncio
async def test_an_order_pending_cancel_is_never_cancelled_again(host, clock) -> None:
    """Re-cancelling a pending order is what made nautilus log its whole history."""
    pending = _Order("pending-1", is_pending_cancel=True)
    strategy, instance = await _deployed(host, "pending", lambda _id: None, orders=[pending])
    strategy.node.cache.positions.append(_VenuePosition())

    await host.flatten_positions(instance, "drawdown_breach")
    await _guard_stop(host, instance)
    assert strategy.cancels == []
    assert strategy.queries == []

    clock.advance(15)
    await host.flatten_positions(instance, "drawdown_breach")
    await _guard_stop(host, instance)
    assert strategy.cancels == []
    assert strategy.queries == ["pending-1"], "fifteen silent seconds earn one query"

    clock.advance(60)
    await host.flatten_positions(instance, "drawdown_breach")
    assert strategy.queries == ["pending-1"]


@pytest.mark.asyncio
async def test_a_flatten_shutdown_leaves_a_pending_order_alone(host, clock) -> None:
    pending = _Order("pending-1", is_pending_cancel=True)
    strategy, instance = await _deployed(
        host,
        "pending-flatten",
        lambda _id: None,
        orders=[pending],
        shutdown_policy={
            "schema_version": 1,
            "position_policy": "flatten",
            "confirmation_timeout_secs": 5,
        },
    )

    await _guard_stop(host, instance)

    assert strategy.cancels == []
    assert strategy.cancelled_all == []


@pytest.mark.asyncio
async def test_a_flatten_shutdown_cancels_order_by_order(host, clock) -> None:
    """cancel_all_orders would carry every order past its own back-off at once."""
    strategy, instance = await _deployed(
        host,
        "flatten-each",
        lambda _id: CANCELED,
        orders=[_Order("entry-1"), _Order("stop-1", is_reduce_only=True)],
        shutdown_policy={
            "schema_version": 1,
            "position_policy": "flatten",
            "confirmation_timeout_secs": 5,
        },
    )
    host._shutdown_stable_polls = 1  # noqa: SLF001 - fixture wiring

    await host.stop(instance)

    assert sorted(strategy.cancels) == ["entry-1", "stop-1"]
    assert strategy.cancelled_all == []
    assert not host.attached(instance)


@pytest.mark.asyncio
async def test_containment_and_shutdown_share_one_request_budget(host, clock, sodex_texts) -> None:
    """Ten requests at once, then one a second -- for both paths together."""
    orders = [_Order(f"entry-{n:02d}") for n in range(15)]
    strategy, instance = await _deployed(host, "budget", lambda _id: None, orders=orders)

    await asyncio.gather(
        host.flatten_positions(instance, "drawdown_breach"),
        _guard_stop(host, instance),
    )
    assert len(strategy.cancels) == 10

    clock.advance(1)
    await host.flatten_positions(instance, "drawdown_breach")
    assert len(strategy.cancels) == 11


@pytest.mark.asyncio
async def test_an_undeclared_venue_backs_off_and_gives_up(host, clock) -> None:
    """Binance declares no refusal classes: everything is unknown, 2 to 32 seconds, then stop."""
    strategy, instance = await _deployed(
        host, "undeclared", lambda _id: UNCLASSIFIED, orders=[_Order("entry-1")]
    )

    await host.flatten_positions(instance, "drawdown_breach")
    sent_at = [clock.now]
    host._containment_confirm_secs = 0.005  # noqa: SLF001 - eighty ticks, kept short
    for _ in range(80):
        clock.advance(1)
        before = len(strategy.cancels)
        with capture_logs() as logs:
            await host.flatten_positions(instance, "drawdown_breach")
        if len(strategy.cancels) > before:
            sent_at.append(clock.now)

    gaps = [round(later - earlier) for earlier, later in zip(sent_at, sent_at[1:], strict=False)]
    assert gaps == [2, 4, 8, 16, 32]
    unconfirmed = _events(logs, "nt_containment_not_confirmed")
    assert unconfirmed[-1]["residual_orders"] == {"cancel_abandoned": 1}


@pytest.mark.asyncio
async def test_a_failing_discipline_sink_does_not_degrade_the_deployment(
    host, clock, monkeypatch
) -> None:
    def broken(_reason: str):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(venue_binance, "cancel_rejection_kind", broken, raising=False)
    strategy, instance = await _deployed(
        host, "sink-fails", lambda _id: UNCLASSIFIED, orders=[_Order("entry-1")]
    )

    with capture_logs() as logs:
        strategy.on_order_event(OrderCancelRejected("entry-1", UNCLASSIFIED))

    assert host._event_forwarding_failures == {}  # noqa: SLF001 - the degradation record
    assert _events(logs, "cancel_discipline_event_failed")
    assert not _events(logs, "runner_event_forwarding_failed")


@pytest.mark.asyncio
async def test_real_venues_turn_on_the_open_order_check(host) -> None:
    """D3: a not-found order is resolved by nautilus's own open-order check."""
    await _deployed(host, "open-check", lambda _id: CANCELED)
    builder = FakeBuilder.instances[-1]
    config = builder.exec_engine_config

    assert config is not None
    assert config.open_check_interval_secs == 10.0
    assert config.open_check_open_only is True
    assert config.open_check_missing_retries == 3
    assert builder.reconciliation is True, "set after the config, which would otherwise reset it"
    assert builder.calls.index("with_exec_engine_config") < builder.calls.index(
        "with_reconciliation"
    )


@pytest.mark.asyncio
async def test_the_sandbox_keeps_nautilus_defaults(host) -> None:
    spec = _spec("open-check-sandbox")
    strategy = _Venue(spec, lambda _id: CANCELED)
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))

    assert FakeBuilder.instances[-1].exec_engine_config is None


@pytest.mark.asyncio
async def test_an_unconfirmed_preserve_stop_lists_what_is_left(host, clock, sodex_texts) -> None:
    strategy, instance = await _deployed(
        host,
        "preserve-residual",
        lambda _id: NOT_FOUND,
        orders=[_Order("ghost-1"), _Order("silent-1")],
        shutdown_policy={
            "schema_version": 1,
            "position_policy": "preserve",
            "confirmation_timeout_secs": 1,
        },
    )
    strategy.answer = lambda order_id: NOT_FOUND if order_id == "ghost-1" else None

    with capture_logs() as logs, pytest.raises(RuntimeError, match="preserve"):
        await host.stop(instance)

    unconfirmed = _events(logs, "nt_shutdown_preserve_unconfirmed")
    assert unconfirmed[-1]["residual_orders"] == {"not_found": 1, "in_flight": 1}
    assert strategy.cancels == ["ghost-1", "silent-1"]
