"""Local telemetry: what a running deployment holds and trades, and what it must never do.

Telemetry is read by the operator's own tools. The properties that matter are the
ones that keep it harmless: money leaves as strings, a failure to read or publish
skips a turn instead of raising, and the event sinks -- which run inside the
strategy's callbacks, where a raised exception fails the deployment closed --
never raise at all.
"""

from __future__ import annotations

import asyncio
import functools
import json
from decimal import Decimal
from typing import Any

from custos.core.engine_protocol import EngineStatus, OrderSnapshot, PositionSnapshot
from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder
from custos.offline.daemon import _run_together
from custos.offline.telemetry import OfflineTelemetry, telemetry_subject

TENANT = "local"
LABEL = "officina-supertrend"
SPEC = "supertrend-testnet"
INSTANCE = "0b8f1c9e-0000-5000-8000-000000000001"


def _status(**overrides: Any) -> EngineStatus:
    document: dict[str, Any] = {
        "phase": "running",
        "position_count": 1,
        "order_count": 1,
        "open_notional": Decimal("530.25"),
        "peak_equity": Decimal("10000"),
        "current_equity": Decimal("9987.5"),
        "drawdown_pct": Decimal("0.125"),
    }
    document.update(overrides)
    return EngineStatus(**document)


class _Engine:
    def __init__(self) -> None:
        self.status = _status()
        self.fail_on: set[str] = set()
        self.position_calls = 0

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        if deployment_instance_id in self.fail_on:
            raise RuntimeError("cache unavailable")
        return self.status

    async def get_positions(self, deployment_instance_id: str) -> list[PositionSnapshot]:
        self.position_calls += 1
        return [
            PositionSnapshot(
                instrument_id="BTCUSDT-PERP.BINANCE",
                quantity=Decimal("-0.0053"),
                avg_px=Decimal("100047.1"),
                unrealized_pnl=Decimal("-1.25"),
                notional=Decimal("530.25"),
            )
        ]

    async def get_orders(self, deployment_instance_id: str) -> list[OrderSnapshot]:
        return [
            OrderSnapshot(
                client_order_id="O-1",
                instrument_id="BTCUSDT-PERP.BINANCE",
                side="BUY",
                quantity=Decimal("0.0053"),
                price=None,
                status="ACCEPTED",
            )
        ]


class _Publisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    async def __call__(self, subject: str, payload: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.published.append((subject, json.loads(payload)))


class OrderFilled:
    """Named as nautilus names it: the sinks select events by type name."""

    def __init__(self, **fields: Any) -> None:
        self.fields = fields

    @staticmethod
    def to_dict(event: OrderFilled) -> dict[str, Any]:
        return dict(event.fields)


class PositionClosed(OrderFilled):
    pass


class OrderAccepted(OrderFilled):
    pass


class _Unreadable:
    @staticmethod
    def to_dict(event: Any) -> dict[str, Any]:
        raise ValueError("not a dict")


OrderFilledUnreadable = type("OrderFilled", (_Unreadable,), {})


def _fill() -> OrderFilled:
    return OrderFilled(
        trade_id="T-1",
        client_order_id="O-1",
        venue_order_id="V-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        order_side="SELL",
        order_type="MARKET",
        last_qty="0.0053",
        last_px="100047.1",
        commission="0.21 USDT",
        liquidity_side="TAKER",
        ts_event=1_790_000_000_123_456_789,
    )


def _telemetry(
    engine: _Engine | None = None,
    publisher: _Publisher | None = None,
    deployments: dict[str, str] | None = None,
    **overrides: Any,
) -> tuple[OfflineTelemetry, _Engine, _Publisher]:
    engine = engine or _Engine()
    publisher = publisher or _Publisher()
    running = {SPEC: INSTANCE} if deployments is None else deployments
    telemetry = OfflineTelemetry(
        tenant_id=TENANT,
        runner_label=LABEL,
        engine=engine,
        publish=publisher,
        deployments=lambda: running,
        **overrides,
    )
    return telemetry, engine, publisher


async def test_a_snapshot_carries_status_positions_and_orders_with_money_as_strings() -> None:
    telemetry, _, publisher = _telemetry()

    await telemetry.publish_snapshots()

    ((subject, envelope),) = publisher.published
    assert subject == f"arx.{TENANT}.telemetry.{LABEL}.{SPEC}.snapshot"
    assert envelope["tenant_id"] == TENANT
    assert envelope["envelope_version"] == 1
    assert envelope["payload_schema_version"] == 1
    payload = envelope["payload"]
    assert payload["kind"] == "snapshot"
    assert payload["deployment_instance_id"] == INSTANCE
    assert payload["status"]["current_equity"] == "9987.5"
    assert payload["status"]["reliable"] is True
    assert payload["positions"] == [
        {
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "quantity": "-0.0053",
            "avg_px": "100047.1",
            "unrealized_pnl": "-1.25",
            "notional": "530.25",
        }
    ]
    assert payload["orders"][0]["price"] is None
    assert payload["orders"][0]["quantity"] == "0.0053"
    assert payload["dropped_events"] == 0


async def test_an_unreliable_status_is_sent_with_its_reason_and_without_positions() -> None:
    telemetry, engine, publisher = _telemetry()
    engine.status = _status(reliable=False, unreliable_reason="missing_price")

    await telemetry.publish_snapshots()

    payload = publisher.published[0][1]["payload"]
    assert payload["status"]["unreliable_reason"] == "missing_price"
    assert payload["positions"] == []
    assert engine.position_calls == 0


async def test_an_unreadable_deployment_skips_its_turn_and_the_others_still_report() -> None:
    telemetry, engine, publisher = _telemetry(deployments={"broken": "i-1", SPEC: INSTANCE})
    engine.fail_on.add("i-1")

    await telemetry.publish_snapshots()

    assert [subject.split(".")[-2] for subject, _ in publisher.published] == [SPEC]


async def test_a_failed_publish_is_not_raised() -> None:
    publisher = _Publisher()
    publisher.error = ConnectionError("nats gone")
    telemetry, _, _ = _telemetry(publisher=publisher)

    await telemetry.publish_snapshots()


async def test_fills_and_closed_positions_are_published_and_other_events_are_not() -> None:
    telemetry, _, publisher = _telemetry(interval=60)
    stop = asyncio.Event()

    telemetry.on_order_event(INSTANCE, OrderAccepted(client_order_id="O-1"))
    telemetry.on_order_event(INSTANCE, _fill())
    telemetry.on_position_event(
        INSTANCE,
        PositionClosed(
            position_id="P-1", realized_pnl="3.10 USDT", ts_closed=1_790_000_100_000_000_000
        ),
    )
    runner = asyncio.create_task(telemetry.run(stop))
    while len(publisher.published) < 2:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, timeout=1)

    (fill_subject, fill), (closed_subject, closed) = publisher.published
    assert fill_subject == telemetry_subject(TENANT, LABEL, SPEC, "fill")
    assert fill["payload"]["kind"] == "fill"
    assert fill["payload"]["last_px"] == "100047.1"
    assert fill["payload"]["commission"] == "0.21 USDT"
    assert fill["payload"]["ts_event"] == "2026-09-21T14:13:20.123456789Z"
    assert closed_subject == telemetry_subject(TENANT, LABEL, SPEC, "position_closed")
    assert closed["payload"]["realized_pnl"] == "3.10 USDT"


async def test_events_queued_when_the_lane_stops_are_still_published() -> None:
    telemetry, _, publisher = _telemetry(interval=60)
    stop = asyncio.Event()
    stop.set()
    telemetry.on_order_event(INSTANCE, _fill())

    await asyncio.wait_for(telemetry.run(stop), timeout=1)

    assert [envelope["payload"]["kind"] for _, envelope in publisher.published] == ["fill"]


async def test_a_full_queue_drops_events_and_the_next_snapshot_counts_them() -> None:
    telemetry, _, publisher = _telemetry(queue_size=1)

    telemetry.on_order_event(INSTANCE, _fill())
    telemetry.on_order_event(INSTANCE, _fill())
    telemetry.on_order_event(INSTANCE, _fill())
    await telemetry.publish_snapshots()

    assert publisher.published[0][1]["payload"]["dropped_events"] == 2


def test_an_unreadable_event_is_swallowed_by_the_sink() -> None:
    telemetry, _, _ = _telemetry()

    telemetry.on_order_event(INSTANCE, OrderFilledUnreadable())
    telemetry.on_order_event(INSTANCE, OrderFilled(ts_event="not a timestamp"))


def test_the_sinks_never_register_as_a_forwarding_failure() -> None:
    """A sink failure is what turns a deployment's status unreliable and trips the guard."""

    telemetry, _, _ = _telemetry(queue_size=1)
    failures: list[tuple[str, str]] = []
    forwarder = StrategyEventForwarder(
        deployment_instance_id=INSTANCE,
        on_sink_failure=lambda sink, reason: failures.append((sink, reason)),
    )
    forwarder.add_order_sink("observation", functools.partial(telemetry.on_order_event, INSTANCE))
    forwarder.add_position_sink(
        "observation", functools.partial(telemetry.on_position_event, INSTANCE)
    )

    class _Strategy:
        def on_order_event(self, event: Any) -> None: ...

        def on_position_event(self, event: Any) -> None: ...

    strategy = _Strategy()
    forwarder.install(strategy)
    for _ in range(3):  # the second and third overflow the queue
        strategy.on_order_event(_fill())
    strategy.on_order_event(OrderFilledUnreadable())
    strategy.on_position_event(PositionClosed(ts_closed=object()))

    assert failures == []


def test_attaches_only_to_an_engine_that_can_send_events() -> None:
    telemetry, _, _ = _telemetry()
    received: dict[str, Any] = {}

    class _Observable:
        def add_observation_sinks(self, *, order: Any, position: Any) -> None:
            received.update(order=order, position=position)

    assert telemetry.attach(object()) is False
    assert telemetry.attach(_Observable()) is True
    assert received == {"order": telemetry.on_order_event, "position": telemetry.on_position_event}


async def test_an_event_for_a_deployment_no_longer_running_is_dropped() -> None:
    telemetry, _, publisher = _telemetry(deployments={}, interval=60)
    stop = asyncio.Event()
    stop.set()
    telemetry.on_order_event(INSTANCE, _fill())

    await asyncio.wait_for(telemetry.run(stop), timeout=1)

    assert publisher.published == []


async def test_a_telemetry_failure_does_not_stop_the_lane() -> None:
    stop = asyncio.Event()
    finished: list[str] = []

    async def loop(name: str) -> None:
        await stop.wait()
        finished.append(name)

    async def broken() -> None:
        raise RuntimeError("telemetry broke")

    async def stop_later() -> None:
        await asyncio.sleep(0.05)
        assert not stop.is_set(), "a companion failure wound the lane down"
        stop.set()

    asyncio.get_running_loop().create_task(stop_later())
    await asyncio.wait_for(
        _run_together(loop("reconciler"), loop("guard"), stop=stop, alongside=(broken(),)),
        timeout=1,
    )

    assert sorted(finished) == ["guard", "reconciler"]
