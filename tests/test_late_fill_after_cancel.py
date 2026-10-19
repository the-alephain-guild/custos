"""A fill that arrives after the order was canceled is still the instance's fill.

The fact bridge only signs facts for orders this instance initialized, and it
used to let go of an order the moment the venue reported it canceled, expired or
rejected. NautilusTrader accepts a fill on an order that is already canceled --
the cancel acknowledgement and the fill can cross on the wire -- so the bridge
dropped exactly the fills it most needed to sign: ``execution_fill``,
``settlement_fill`` and ``settlement_fee`` for an execution that really happened.

These tests require a canceled or expired order to stay attributed for a bounded
window, durably and across a restart, while a rejected order is still let go at
once, released attribution is eventually removed, and a fill nobody here owns is
reported instead of disappearing without a trace.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from structlog.testing import capture_logs

from custos.core import runner_fact_producer
from custos.core.runner_fact import (
    RunnerFactEmitter,
    RunnerFactIdentity,
    RunnerFactOutbox,
    runner_fact_event_id,
)
from custos.core.runner_fact_producer import RunnerFactEventBridge
from tests.test_order_reservation import INSTANCE_A, POLICY_ID
from tests.test_order_reservation import _store as _reservation_store
from tests.test_strategy_signal_bridge import (
    OrderCanceled,
    OrderFilled,
    OrderInitialized,
    _deployment,
    _OrderEvent,
)

ORDER = "supertrend-entry-1"
INSTANCE = "20000000-0000-4000-8000-000000000001"
_HOUR_NS = 3_600 * 1_000_000_000
_FILL_KINDS = {"execution_fill", "fill", "fee"}
_DROPPED = "runner_fact_fill_for_unowned_order_dropped"


class OrderExpired(_OrderEvent):
    def __init__(self, client_order_id: str = ORDER) -> None:
        super().__init__(event_id="a0000000-0000-4000-8000-000000000001")
        self.values["client_order_id"] = client_order_id


class OrderRejected(_OrderEvent):
    def __init__(self, client_order_id: str = ORDER) -> None:
        super().__init__(event_id="b0000000-0000-4000-8000-000000000001")
        self.values["client_order_id"] = client_order_id
        self.values["reason"] = "venue_rejected"


class _Clock:
    """Stands in for the wall clock the bridge stamps a release with."""

    def __init__(self) -> None:
        self.now_ns = 1_786_586_400_000_000_000

    def __call__(self) -> int:
        return self.now_ns

    def advance_hours(self, hours: int) -> None:
        self.now_ns += hours * _HOUR_NS


def _emitter(path: Path) -> RunnerFactEmitter:
    return RunnerFactEmitter(
        RunnerFactOutbox(path),
        RunnerFactIdentity(Ed25519PrivateKey.generate(), "late-fill-test-key"),
        lambda: None,
    )


def _forwarder() -> SimpleNamespace:
    return SimpleNamespace(
        add_order_sink=lambda _name, _sink: None,
        add_position_sink=lambda _name, _sink: None,
    )


def _bridge(emitter: RunnerFactEmitter) -> RunnerFactEventBridge:
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())
    bridge.bootstrap(_forwarder())
    return bridge


def _restarted(path: Path) -> tuple[RunnerFactEmitter, RunnerFactEventBridge]:
    """A fresh emitter over the same database, which is what a restart is."""
    emitter = _emitter(path)
    return emitter, _bridge(emitter)


def _late_fill(client_order_id: str = ORDER, *, trade_id: str = "trade-late") -> OrderFilled:
    return OrderFilled(
        client_order_id,
        trade_id=trade_id,
        event_id=str(uuid4()),
        last_qty="0.002",
    )


async def _signed_for_trade(emitter: RunnerFactEmitter, trade_id: str) -> list[str]:
    """The kinds of every pending fact that belongs to one venue trade."""
    deployment = _deployment()
    fill_id = str(
        runner_fact_event_id(
            deployment.authority.stream_key,
            "fill_identity",
            deployment.venue,
            "BTCUSDT-PERP.BINANCE",
            trade_id,
        )
    )
    kinds: list[str] = []
    for batch in await emitter._outbox.pending(limit=100):
        for fact in json.loads(batch.payload)["facts"]:
            if fact.get("venue_trade_id") == trade_id or fact.get("fill_id") == fill_id:
                kinds.append(fact["kind"])
    return kinds


def _released_rows(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'runner_order_identity_release'"
        ).fetchone()
        if exists is None:
            return set()
        return {
            row[0]
            for row in connection.execute(
                "SELECT client_order_id FROM runner_order_identity_release"
            )
        }


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    # raising=False: the seam is what the fix adds, and the baseline run of these
    # tests has to fail on the defect, not on a missing attribute.
    monkeypatch.setattr(runner_fact_producer, "_now_ns", fake, raising=False)
    return fake


@pytest.mark.asyncio
async def test_a_fill_before_the_cancel_is_signed(tmp_path: Path, clock: _Clock) -> None:
    """The control: the lookup below finds a fill that was never in question."""
    emitter = _emitter(tmp_path / "control.sqlite3")
    bridge = _bridge(emitter)
    bridge._on_order_event(OrderInitialized())

    bridge._on_order_event(_late_fill())

    assert sorted(await _signed_for_trade(emitter, "trade-late")) == sorted(_FILL_KINDS)


@pytest.mark.asyncio
async def test_a_fill_after_the_cancel_is_still_signed(tmp_path: Path, clock: _Clock) -> None:
    emitter = _emitter(tmp_path / "cancel.sqlite3")
    bridge = _bridge(emitter)
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderCanceled())

    bridge._on_order_event(_late_fill())

    assert sorted(await _signed_for_trade(emitter, "trade-late")) == sorted(_FILL_KINDS)


@pytest.mark.asyncio
async def test_a_fill_after_expiry_is_still_signed(tmp_path: Path, clock: _Clock) -> None:
    """The runner side only.

    The engine currently has no expired-to-filled transition, so in production such
    a fill is refused before it reaches the strategy. The runner must not be a second
    place that drops it once the engine lets it through.
    """
    emitter = _emitter(tmp_path / "expired.sqlite3")
    bridge = _bridge(emitter)
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderExpired())

    bridge._on_order_event(_late_fill())

    assert sorted(await _signed_for_trade(emitter, "trade-late")) == sorted(_FILL_KINDS)


@pytest.mark.asyncio
async def test_a_fill_after_the_cancel_is_still_signed_after_a_restart(
    tmp_path: Path, clock: _Clock
) -> None:
    path = tmp_path / "restart.sqlite3"
    first = _bridge(_emitter(path))
    first._on_order_event(OrderInitialized())
    first._on_order_event(OrderCanceled())

    restarted_emitter, replacement = _restarted(path)
    replacement._on_order_event(_late_fill())

    assert sorted(await _signed_for_trade(restarted_emitter, "trade-late")) == sorted(_FILL_KINDS)


@pytest.mark.asyncio
async def test_a_rejected_order_is_let_go_at_once(tmp_path: Path, clock: _Clock) -> None:
    """The venue never accepted it, and the engine refuses a fill on a rejected order.

    So a rejected order takes up no room in the window, and if a fill for it ever did
    arrive it would be reported rather than signed.
    """
    path = tmp_path / "rejected.sqlite3"
    emitter = _emitter(path)
    bridge = _bridge(emitter)
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderRejected())

    assert ORDER not in emitter.recall_orders(INSTANCE)
    assert ORDER not in _released_rows(path)

    with capture_logs() as logs:
        bridge._on_order_event(_late_fill())

    assert await _signed_for_trade(emitter, "trade-late") == []
    dropped = [entry for entry in logs if entry["event"] == _DROPPED]
    assert [(entry["client_order_id"], entry["trade_id"]) for entry in dropped] == [
        (ORDER, "trade-late")
    ]


@pytest.mark.asyncio
async def test_a_fill_after_the_window_is_dropped_out_loud(tmp_path: Path, clock: _Clock) -> None:
    path = tmp_path / "window.sqlite3"
    first = _bridge(_emitter(path))
    first._on_order_event(OrderInitialized())
    first._on_order_event(OrderCanceled())
    clock.advance_hours(25)

    restarted_emitter, replacement = _restarted(path)
    with capture_logs() as logs:
        replacement._on_order_event(_late_fill())

    assert await _signed_for_trade(restarted_emitter, "trade-late") == []
    dropped = [entry for entry in logs if entry["event"] == _DROPPED]
    assert len(dropped) == 1
    assert dropped[0]["log_level"] == "warning"
    assert dropped[0]["client_order_id"] == ORDER
    assert dropped[0]["trade_id"] == "trade-late"
    assert dropped[0]["deployment_instance_id"] == INSTANCE


@pytest.mark.asyncio
async def test_released_attribution_is_removed_once_the_window_has_passed(
    tmp_path: Path, clock: _Clock
) -> None:
    path = tmp_path / "bounded.sqlite3"
    emitter = _emitter(path)
    bridge = _bridge(emitter)
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderCanceled())
    clock.advance_hours(25)

    # Any later release is a moment to remove what has outlived the window.
    second = OrderInitialized(event_id="60000000-0000-4000-8000-000000000002")
    second.values["client_order_id"] = "supertrend-entry-2"
    bridge._on_order_event(second)
    canceled = OrderCanceled()
    canceled.values["client_order_id"] = "supertrend-entry-2"
    bridge._on_order_event(canceled)

    assert ORDER not in emitter.recall_orders(INSTANCE)
    assert ORDER not in _released_rows(path)
    assert ORDER not in _restarted(path)[0].recall_orders(INSTANCE)


@pytest.mark.asyncio
async def test_released_attribution_is_removed_at_restart_once_the_window_has_passed(
    tmp_path: Path, clock: _Clock
) -> None:
    path = tmp_path / "bounded-restart.sqlite3"
    first = _bridge(_emitter(path))
    first._on_order_event(OrderInitialized())
    first._on_order_event(OrderCanceled())
    clock.advance_hours(25)

    restarted_emitter, _replacement = _restarted(path)

    assert ORDER not in restarted_emitter.recall_orders(INSTANCE)
    assert ORDER not in _released_rows(path)


@pytest.mark.asyncio
async def test_a_repeated_late_fill_is_signed_once(tmp_path: Path, clock: _Clock) -> None:
    emitter = _emitter(tmp_path / "duplicate.sqlite3")
    bridge = _bridge(emitter)
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderCanceled())

    bridge._on_order_event(_late_fill())
    bridge._on_order_event(_late_fill())

    assert sorted(await _signed_for_trade(emitter, "trade-late")) == sorted(_FILL_KINDS)


def test_the_reservation_store_still_books_a_fill_after_the_cancel(tmp_path: Path) -> None:
    """The reservation boundary makes the same judgement and already gets it right.

    A cancel releases the reservation but keeps its row, so a fill that crosses the
    cancel is still booked as exposure. Pinned here so the two sinks cannot drift.
    """
    store = _reservation_store(tmp_path / "reservation.sqlite3")
    store.reserve_order_notional_sync(
        event_id="reserve-late",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-late",
        policy_id=POLICY_ID,
        requested_notional=Decimal("25"),
    )
    store.release_order_reservation_sync(
        event_id="cancel-late",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-late",
        reason="canceled",
    )

    assert store.has_order_reservation_sync(INSTANCE_A, "order-late")
    filled = store.record_order_fill_sync(
        event_id="fill-late",
        deployment_instance_id=INSTANCE_A,
        client_order_id="order-late",
        fill_notional=Decimal("10"),
        fill_quantity=Decimal("10"),
    )
    assert filled.filled_exposure == Decimal("10")


def test_the_instance_constant_matches_the_fixture() -> None:
    assert UUID(INSTANCE) == _deployment().authority.deployment_instance_id
