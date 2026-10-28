"""Venue fills retain one durable accounting identity across engine replays."""

import sqlite3
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custos.core.execution_reservation_store import PlatformReservationStore
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import RunnerStateAuthorityError
from tests.test_order_reservation import (
    INSTANCE_A,
    INSTANCE_B,
    POLICY_ID,
    RUNNER_ID,
    TENANT_ID,
    _store,
)


class OrderFilled:
    def __init__(
        self,
        event_id,
        trade_id,
        *,
        serialized=True,
        reducing=False,
        notional="10",
        position_id=None,
    ):
        self.event_id = event_id
        self.trade_id = trade_id
        self.serialized = serialized
        self.reducing = reducing
        self.notional = Decimal(notional)
        self.quantity = Decimal("0.1")
        self.client_order_id = "close" if reducing else "entry"
        self.position_id = position_id

    @staticmethod
    def to_dict(event):
        document = {"event_id": event.event_id, "client_order_id": event.client_order_id}
        if event.serialized:
            document["trade_id"] = event.trade_id
        return document


class _Semantics:
    def order_instrument_id(self, order):
        return "BTCUSD.PAPERTRADE"

    def order_is_risk_reducing(self, order, already_reducing):
        return False

    def order_notional(self, order):
        return Decimal("10")

    def fill_notional(self, event):
        return event.notional

    def fill_quantity(self, event):
        return event.quantity

    def fill_leaves_quantity(self, event):
        return None

    def event_is_risk_reducing(self, event):
        return event.reducing

    def event_exposure_source_order_id(self, event):
        return "entry" if event.reducing else None

    def event_position_id(self, event):
        return event.position_id

    def event_instrument_id(self, event):
        return "BTCUSD.PAPERTRADE"

    def event_side(self, event):
        return "SELL" if event.reducing else "BUY"


def _boundary(path, *, seed=False):
    store = _store(path)
    if seed:
        store.reserve_order_notional_sync(
            event_id="reserve-entry",
            deployment_instance_id=INSTANCE_A,
            client_order_id="entry",
            policy_id=POLICY_ID,
            requested_notional=Decimal("100"),
        )
    breaker = FallbackBreaker(FallbackBreakerConfig(Decimal("1000"), Decimal("10")))
    boundary = RunnerReservationBoundary(
        store=PlatformReservationStore(store, deployment_instance_id=INSTANCE_A),
        runtime_id=INSTANCE_A,
        policy_id=POLICY_ID,
        fallback_breaker=breaker,
        trading_mode="sandbox",
        semantics=_Semantics(),
    )
    return store, boundary, breaker


@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_same_venue_fill_with_new_engine_event_counts_once(serialized, restart, tmp_path):
    path = tmp_path / "state.sqlite3"
    store, boundary, breaker = _boundary(path, seed=True)
    boundary.on_order_event(OrderFilled("engine-1", "venue-1", serialized=serialized))
    if restart:
        store, boundary, breaker = _boundary(path)
    boundary.on_order_event(OrderFilled("engine-2", "venue-1", serialized=serialized))
    snapshot = store.load_order_reservation_sync(INSTANCE_A, "entry")
    assert snapshot.filled_exposure == Decimal("10")
    assert snapshot.filled_quantity == Decimal("0.1")
    assert snapshot.reserved_notional == Decimal("90")
    assert not breaker.frozen


@pytest.mark.parametrize("serialized", [False, True])
def test_different_venue_fills_with_one_engine_event_both_count(serialized, tmp_path):
    store, boundary, breaker = _boundary(tmp_path / "state.sqlite3", seed=True)
    for trade in ("venue-1", "venue-2"):
        boundary.on_order_event(OrderFilled("same-engine-id", trade, serialized=serialized))
    snapshot = store.load_order_reservation_sync(INSTANCE_A, "entry")
    assert snapshot.filled_exposure == Decimal("20")
    assert snapshot.filled_quantity == Decimal("0.2")
    assert snapshot.reserved_notional == Decimal("80")
    assert not breaker.frozen


@pytest.mark.parametrize("restart", [False, True])
def test_replayed_venue_close_releases_cost_once(restart, tmp_path):
    path = tmp_path / "state.sqlite3"
    store, boundary, breaker = _boundary(path, seed=True)
    store.record_order_fill_sync(
        event_id="seed-open-fill",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("100"),
        fill_quantity=Decimal("1"),
    )
    boundary.on_order_event(OrderFilled("close-engine-1", "venue-close", reducing=True))
    if restart:
        store, boundary, breaker = _boundary(path)
    boundary.on_order_event(OrderFilled("close-engine-2", "venue-close", reducing=True))
    snapshot = store.load_order_reservation_sync(INSTANCE_A, "entry")
    assert snapshot.filled_exposure == Decimal("90")
    assert snapshot.filled_quantity == Decimal("0.9")
    assert not breaker.frozen


def test_conflicting_venue_fill_keeps_committed_fact_and_freezes_risk(tmp_path):
    store, boundary, breaker = _boundary(tmp_path / "state.sqlite3", seed=True)
    boundary.on_order_event(OrderFilled("engine-1", "venue-1"))
    boundary.on_order_event(OrderFilled("engine-2", "venue-1", notional="11"))
    snapshot = store.load_order_reservation_sync(INSTANCE_A, "entry")
    assert snapshot.filled_exposure == Decimal("10")
    assert snapshot.filled_quantity == Decimal("0.1")
    assert breaker.frozen


@pytest.mark.parametrize("trade_id", [None, ""])
def test_absent_venue_identity_retains_engine_identity_fallback(trade_id, tmp_path):
    store, boundary, breaker = _boundary(tmp_path / "state.sqlite3", seed=True)
    boundary.on_order_event(OrderFilled("engine-1", trade_id))
    boundary.on_order_event(OrderFilled("engine-1", trade_id))
    assert store.load_order_reservation_sync(INSTANCE_A, "entry").filled_exposure == Decimal("10")
    assert not breaker.frozen


def test_nonfill_events_keep_their_distinct_engine_identities(tmp_path):
    path = tmp_path / "state.sqlite3"
    store, boundary, breaker = _boundary(path, seed=True)

    class OrderCanceled:
        client_order_id = "entry"
        trade_id = "same-historical-trade"

        def __init__(self, event_id):
            self.event_id = event_id

        @staticmethod
        def to_dict(event):
            return {"event_id": event.event_id, "trade_id": event.trade_id}

    boundary.on_order_event(OrderCanceled("cancel-1"))
    boundary.on_order_event(OrderCanceled("cancel-2"))
    with closing(sqlite3.connect(path)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM runner_order_reservation_event WHERE event_kind='release'"
        ).fetchone()[0]
    assert count == 2
    assert store.load_order_reservation_sync(INSTANCE_A, "entry").reserved_notional == 0
    assert not breaker.frozen


def test_different_trade_and_order_pairs_cannot_collide_on_delimiters(tmp_path):
    store, boundary, breaker = _boundary(tmp_path / "state.sqlite3")
    for trade_id, order_id in [("venue:a", "b"), ("venue", "a:b")]:
        store.reserve_order_notional_sync(
            event_id="reserve-" + order_id,
            deployment_instance_id=INSTANCE_A,
            client_order_id=order_id,
            policy_id=POLICY_ID,
            requested_notional=Decimal("50"),
        )
        event = OrderFilled("engine-" + order_id, trade_id)
        event.client_order_id = order_id
        boundary.on_order_event(event)
    for order_id in ["b", "a:b"]:
        assert store.load_order_reservation_sync(INSTANCE_A, order_id).filled_exposure == Decimal(
            "10"
        )
    assert not breaker.frozen


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("kind", ["opening", "reduction", "fifo"])
def test_predecessor_fill_history_blocks_reapplication(restart, kind, tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store, boundary, breaker = _boundary(path, seed=True)
    reducing = kind != "opening"
    if reducing:
        store.record_order_fill_sync(
            event_id="seed-open",
            deployment_instance_id=INSTANCE_A,
            client_order_id="entry",
            fill_notional=Decimal("100"),
            fill_quantity=Decimal("1"),
            position_id="position-1",
            instrument_id="BTCUSD.PAPERTRADE",
            side="BUY",
        )
        fields = dict(
            event_id=f"runner-order-reservation:{INSTANCE_A}:fill_reduce:legacy-engine:entry",
            deployment_instance_id=INSTANCE_A,
            reduction_notional=Decimal("10"),
            reduction_quantity=Decimal("0.1"),
        )
        if kind == "fifo":
            store.record_position_reduction_fifo_sync(**fields, position_id="position-1")
        else:
            store.record_position_reduction_sync(**fields, client_order_id="entry")
    else:
        store.record_order_fill_sync(
            event_id=f"runner-order-reservation:{INSTANCE_A}:fill:legacy-engine:entry",
            deployment_instance_id=INSTANCE_A,
            client_order_id="entry",
            fill_notional=Decimal("10"),
            fill_quantity=Decimal("0.1"),
        )
    before = store.load_order_reservation_sync(INSTANCE_A, "entry")
    if restart:
        store, boundary, breaker = _boundary(path)
    boundary.on_order_event(
        OrderFilled(
            "legacy-engine",
            "venue-legacy",
            reducing=reducing,
            position_id="position-1" if kind == "fifo" else None,
        )
    )
    assert store.load_order_reservation_sync(INSTANCE_A, "entry") == before
    assert breaker.frozen


def test_predecessor_fill_history_blocks_new_risk_without_erasing_records(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store, boundary, _breaker = _boundary(path, seed=True)
    store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{INSTANCE_A}:fill:legacy-engine:entry",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("10"),
        fill_quantity=Decimal("0.1"),
    )
    before = store.load_order_reservation_sync(INSTANCE_A, "entry")
    store, boundary, _breaker = _boundary(path)
    with pytest.raises(RunnerStateAuthorityError, match="fill identity.*reconciliation"):
        boundary.before_submit_order(
            SimpleNamespace(
                id="new-command",
                order=SimpleNamespace(client_order_id="new-order"),
            )
        )
    assert not store.has_order_reservation_sync(INSTANCE_A, "new-order")
    assert store.load_order_reservation_sync(INSTANCE_A, "entry") == before
    with pytest.raises(RunnerStateAuthorityError, match="fill identity.*reconciliation"):
        store.replace_order_reservation_sync(
            event_id="modify-legacy",
            deployment_instance_id=INSTANCE_A,
            client_order_id="entry",
            new_reserved_notional=Decimal("100"),
        )
    assert store.load_order_reservation_sync(INSTANCE_A, "entry") == before
    store.reserve_order_notional_sync(
        event_id="unrelated-instance",
        deployment_instance_id=INSTANCE_B,
        client_order_id="other",
        policy_id=POLICY_ID,
        requested_notional=Decimal("10"),
    )
    assert store.has_order_reservation_sync(INSTANCE_B, "other")


def test_policy_renewal_and_new_direct_event_identity_cannot_unfence_legacy_instance(tmp_path):
    path = tmp_path / "legacy-policy.sqlite3"
    store, _boundary_instance, _breaker = _boundary(path, seed=True)
    store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{INSTANCE_A}:fill:legacy-engine:entry",
        deployment_instance_id=INSTANCE_A,
        client_order_id="entry",
        fill_notional=Decimal("10"),
        fill_quantity=Decimal("0.1"),
    )
    policy = uuid4()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        original = dict(
            connection.execute(
                "SELECT * FROM runner_cap_policy WHERE policy_id=?", (str(POLICY_ID),)
            ).fetchone()
        )
        original.update(
            policy_id=str(policy),
            policy_revision=2,
            policy_digest="1" * 64,
            previous_policy_id=str(POLICY_ID),
            previous_policy_revision=1,
            previous_policy_digest="d" * 64,
            fingerprint="2" * 64,
            verified_event_bytes_digest="3" * 64,
        )
        columns = ",".join(original)
        placeholders = ",".join("?" for _ in original)
        connection.execute(
            f"INSERT INTO runner_cap_policy ({columns}) VALUES ({placeholders})",
            tuple(original.values()),
        )
        connection.execute(
            "UPDATE runner_cap_policy_head SET policy_id=?,policy_revision=2,policy_digest=? WHERE tenant_scope=? AND runner_id=?",
            (str(policy), "1" * 64, TENANT_ID, str(RUNNER_ID)),
        )
    with pytest.raises(RunnerStateAuthorityError, match="fill identity.*reconciliation"):
        store.reserve_order_notional_sync(
            event_id="fresh-direct-event",
            deployment_instance_id=INSTANCE_A,
            client_order_id="fresh-direct-order",
            policy_id=policy,
            requested_notional=Decimal("10"),
        )
    assert not store.has_order_reservation_sync(INSTANCE_A, "fresh-direct-order")
