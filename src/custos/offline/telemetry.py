"""Local telemetry: what each running deployment holds, and what it traded.

The operator's own tools read this to show positions, orders and results while a
strategy runs. It is unsigned and best effort, it is not a RunnerFact, and
nothing in this lane reads it back: no decision here waits on it, and losing it
changes nothing about what trades.

Snapshots are polled on their own clock, apart from the exposure guard, so a
slow or absent transport never delays a risk evaluation. Fills and closed
positions arrive inside the strategy's own callbacks, where a sink that raises
makes the host's status unreliable and trips the guard; the sinks here therefore
copy what they need into plain data, hand it to a bounded queue and never raise.
A full queue drops the event and counts it, and the count rides on the next
snapshot.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol, runtime_checkable

import uuid6

from custos.core.engine_protocol import EngineStatus, OrderSnapshot, PositionSnapshot
from custos.core.log import get_logger
from custos.offline.spec import now_rfc3339_nanos, offline_subject

_log = get_logger("custos.offline.telemetry")

SNAPSHOT_SECS: Final = 10.0
QUEUE_SIZE: Final = 1_000
PAYLOAD_SCHEMA_VERSION: Final = 1

SNAPSHOT: Final = "snapshot"
FILL: Final = "fill"
POSITION_CLOSED: Final = "position_closed"

_FILL_FIELDS: Final = (
    "trade_id",
    "client_order_id",
    "venue_order_id",
    "instrument_id",
    "order_side",
    "order_type",
    "last_qty",
    "last_px",
    "commission",
    "liquidity_side",
)
_POSITION_CLOSED_FIELDS: Final = (
    "position_id",
    "instrument_id",
    "entry",
    "side",
    "peak_qty",
    "avg_px_open",
    "avg_px_close",
    "realized_pnl",
)

Publish = Callable[[str, bytes], Awaitable[Any]]
EventSink = Callable[[str, Any], None]


class TelemetryEngine(Protocol):
    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus: ...

    async def get_positions(self, deployment_instance_id: str) -> list[PositionSnapshot]: ...

    async def get_orders(self, deployment_instance_id: str) -> list[OrderSnapshot]: ...


@runtime_checkable
class ObservableEngine(Protocol):
    """An engine that can also hand this lane the events its strategies receive."""

    def add_observation_sinks(self, *, order: EventSink, position: EventSink) -> None: ...


class OfflineTelemetry:
    def __init__(
        self,
        *,
        tenant_id: str,
        runner_label: str,
        engine: TelemetryEngine,
        publish: Publish,
        deployments: Callable[[], Mapping[str, str]],
        interval: float = SNAPSHOT_SECS,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        """``deployments`` names what is running now, as spec id to deployment instance id."""

        self._tenant_id = tenant_id
        self._runner_label = runner_label
        self._engine = engine
        self._publish = publish
        self._deployments = deployments
        self._interval = interval
        self._events: asyncio.Queue[tuple[str, str, dict[str, str]]] = asyncio.Queue(queue_size)
        self._dropped: dict[str, int] = {}

    def attach(self, engine: object) -> bool:
        """Receive fills and closed positions from ``engine``, if it can send them."""

        if not isinstance(engine, ObservableEngine):
            _log.info("offline_telemetry_events_unavailable", engine=type(engine).__name__)
            return False
        engine.add_observation_sinks(order=self.on_order_event, position=self.on_position_event)
        return True

    def on_order_event(self, deployment_instance_id: str, event: Any) -> None:
        if type(event).__name__ == "OrderFilled":
            self._offer(deployment_instance_id, FILL, event, _FILL_FIELDS)

    def on_position_event(self, deployment_instance_id: str, event: Any) -> None:
        if type(event).__name__ == "PositionClosed":
            self._offer(deployment_instance_id, POSITION_CLOSED, event, _POSITION_CLOSED_FIELDS)

    def _offer(
        self,
        deployment_instance_id: str,
        kind: str,
        event: Any,
        fields: tuple[str, ...],
    ) -> None:
        # Runs inside the strategy's callback: anything raised here would mark the
        # engine unreliable and stop the deployment, which telemetry must never do.
        try:
            data = _event_data(event, fields)
        except Exception as exc:  # noqa: BLE001 - see above
            _log.warning(
                "offline_telemetry_event_unreadable",
                kind=kind,
                error_type=type(exc).__name__,
            )
            return
        try:
            self._events.put_nowait((deployment_instance_id, kind, data))
        except asyncio.QueueFull:
            self._dropped[deployment_instance_id] = self._dropped.get(deployment_instance_id, 0) + 1

    async def run(self, stop: asyncio.Event) -> None:
        """Publish events as they arrive and a snapshot every interval, until stopped."""

        loop = asyncio.get_running_loop()
        next_snapshot = loop.time() + self._interval
        while not stop.is_set():
            timeout = max(0.0, next_snapshot - loop.time())
            try:
                deployment_instance_id, kind, data = await asyncio.wait_for(
                    self._next_event(stop), timeout=timeout
                )
            except TimeoutError:
                await self.publish_snapshots()
                next_snapshot = loop.time() + self._interval
                continue
            except _Stopped:
                break
            await self._publish_event(deployment_instance_id, kind, data)
        # Events that arrived while stopping still say what happened.
        while not self._events.empty():
            await self._publish_event(*self._events.get_nowait())

    async def _next_event(self, stop: asyncio.Event) -> tuple[str, str, dict[str, str]]:
        getter = asyncio.ensure_future(self._events.get())
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            return getter.result()
        raise _Stopped

    async def publish_snapshots(self) -> None:
        for spec_id, deployment_instance_id in tuple(self._deployments().items()):
            try:
                payload = await self._snapshot(deployment_instance_id)
            except Exception as exc:  # noqa: BLE001 - one unreadable deployment skips a turn
                _log.warning(
                    "offline_telemetry_snapshot_unavailable",
                    spec_id=spec_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue
            await self._send(spec_id, SNAPSHOT, payload)

    async def _snapshot(self, deployment_instance_id: str) -> dict[str, Any]:
        status = await self._engine.get_engine_status(deployment_instance_id)
        # Positions are only as good as the valuation behind them; an unreliable
        # status says why, and the positions it would have listed are left out
        # rather than shown as if they were right.
        positions = (
            await self._engine.get_positions(deployment_instance_id) if status.reliable else []
        )
        orders = await self._engine.get_orders(deployment_instance_id)
        return {
            "deployment_instance_id": deployment_instance_id,
            "status": _plain(asdict(status)),
            "positions": [_plain(asdict(position)) for position in positions],
            "orders": [_plain(asdict(order)) for order in orders],
            "dropped_events": self._dropped.get(deployment_instance_id, 0),
        }

    async def _publish_event(
        self, deployment_instance_id: str, kind: str, data: dict[str, str]
    ) -> None:
        spec_id = self._spec_for(deployment_instance_id)
        if spec_id is None:
            _log.warning("offline_telemetry_event_unowned", kind=kind)
            return
        await self._send(spec_id, kind, {"deployment_instance_id": deployment_instance_id, **data})

    def _spec_for(self, deployment_instance_id: str) -> str | None:
        for spec_id, instance in self._deployments().items():
            if instance == deployment_instance_id:
                return spec_id
        return None

    async def _send(self, spec_id: str, kind: str, payload: dict[str, Any]) -> None:
        envelope = {
            "envelope_version": 1,
            "event_id": str(uuid6.uuid7()),
            "tenant_id": self._tenant_id,
            "occurred_at": now_rfc3339_nanos(),
            "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
            "payload": {"kind": kind, **payload},
        }
        subject = telemetry_subject(self._tenant_id, self._runner_label, spec_id, kind)
        try:
            await self._publish(subject, json.dumps(envelope, separators=(",", ":")).encode())
        except Exception as exc:  # noqa: BLE001 - telemetry is not a condition of trading
            _log.warning("offline_telemetry_publish_failed", subject=subject, error=str(exc))


def telemetry_subject(tenant_id: str, runner_label: str, spec_id: str, kind: str) -> str:
    return offline_subject(tenant_id, "telemetry", runner_label, spec_id, kind)


class _Stopped(Exception):
    pass


def _event_data(event: Any, fields: tuple[str, ...]) -> dict[str, str]:
    converter = getattr(type(event), "to_dict", None)
    data = converter(event) if callable(converter) else None
    if not isinstance(data, dict):
        data = {field: getattr(event, field, None) for field in fields}
    selected = {field: str(data[field]) for field in fields if data.get(field) is not None}
    selected["ts_event"] = _timestamp(data.get("ts_event") or data.get("ts_closed"))
    return selected


def _timestamp(value: Any) -> str:
    if value is None:
        return ""
    seconds, nanos = divmod(int(value), 1_000_000_000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{nanos:09d}Z"


def _plain(document: dict[str, Any]) -> dict[str, Any]:
    """Money leaves as a string, never a float."""

    return {
        key: str(value) if isinstance(value, Decimal) else value for key, value in document.items()
    }
