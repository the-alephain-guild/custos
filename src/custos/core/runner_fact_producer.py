"""Production bridges from execution and venue facts into the signed outbox."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from custos.core.log import get_logger
from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactContractError,
    RunnerFactEmitter,
    equity_snapshot,
    execution_fill,
    heartbeat,
    position_closed,
    position_snapshot,
    reconciliation_period_closed,
    runner_fact_event_id,
    settlement_fee,
    settlement_fill,
    venue_ledger_snapshot_facts,
)

_log = get_logger("custos.runner_fact_producer")


@dataclass(frozen=True, slots=True)
class RunnerFactDeployment:
    authority: RunnerFactAuthority
    deployment_instance_id: str
    deployment_spec_id: str
    deployment_spec_digest: str
    venue: str
    currency: str
    reconciliation_available: bool

    def __post_init__(self) -> None:
        if (
            str(self.authority.deployment_instance_id) != self.deployment_instance_id
            or str(self.authority.deployment_spec_id) != self.deployment_spec_id
            or self.authority.deployment_spec_digest != self.deployment_spec_digest
        ):
            raise RunnerFactContractError(
                "RunnerFactDeployment identity differs from its signed authority"
            )


@dataclass(frozen=True, slots=True)
class VenueLedgerEvidence:
    venue: str
    source: str
    watermark: str
    coverage_from: datetime
    observed_through: datetime
    completeness: Mapping[str, bool]
    balances: Sequence[Mapping[str, Any]]
    positions: Sequence[Mapping[str, Any]]
    fills: Sequence[Mapping[str, Any]]
    fees: Sequence[Mapping[str, Any]]


class RunnerFactHost(Protocol):
    def runner_fact_deployments(self) -> Sequence[RunnerFactDeployment]: ...

    async def runner_fact_risk_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> tuple[Decimal, Sequence[Mapping[str, Any]]]: ...

    async def runner_fact_venue_ledger(
        self, deployment_instance_id: str, coverage_from: datetime, closed_at: datetime
    ) -> VenueLedgerEvidence: ...


def _scoped_event_id(authority: RunnerFactAuthority, kind: str, *identity: object):
    return runner_fact_event_id(authority.stream_key, kind, *identity)


def _nt_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    try:
        nanoseconds = int(value)
    except (TypeError, ValueError) as exc:
        raise RunnerFactContractError(
            "Nautilus event timestamp must be integer nanoseconds"
        ) from exc
    seconds, nanos = divmod(nanoseconds, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{nanos:09d}Z"


def _money(value: Any, field: str) -> tuple[str, str | None]:
    text = str(value).strip()
    if not text:
        raise RunnerFactContractError(f"{field} is empty")
    amount, separator, currency = text.partition(" ")
    return amount, currency if separator else None


class RunnerFactMessageBusBridge:
    """Synchronously commits execution events to SQLite before returning."""

    def __init__(
        self,
        *,
        emitter: RunnerFactEmitter,
        deployment: RunnerFactDeployment,
    ) -> None:
        self._emitter = emitter
        self._deployment = deployment

    def bootstrap(self, message_bus: Any) -> None:
        if message_bus is None:
            raise RuntimeError("Nautilus MessageBus unavailable for RunnerFact bridge")
        message_bus.subscribe("events.order.*", self._on_order_event)
        message_bus.subscribe("events.position.*", self._on_position_event)
        _log.info(
            "runner_fact_bridge_attached",
            deployment_instance_id=self._deployment.deployment_instance_id,
            deployment_spec_id=str(self._deployment.authority.deployment_spec_id),
        )

    def _on_order_event(self, event: Any) -> None:
        if type(event).__name__ != "OrderFilled":
            return
        try:
            data = type(event).to_dict(event)
            authority = self._deployment.authority
            venue = self._deployment.venue
            stable_trade_id = str(data.get("trade_id") or data.get("event_id") or "").strip()
            if not stable_trade_id:
                raise RunnerFactContractError("OrderFilled has no stable trade identity")
            client_order_id = str(data.get("client_order_id") or "").strip() or None
            venue_order_id = str(data.get("venue_order_id") or "").strip()
            if not venue_order_id:
                if client_order_id is None:
                    raise RunnerFactContractError("OrderFilled has no venue or client order id")
                venue_order_id = f"sandbox:{client_order_id}"
            fee_amount, fee_currency = _money(data.get("commission", "0"), "commission")
            currency = fee_currency or self._deployment.currency
            if currency != self._deployment.currency:
                raise RunnerFactContractError(
                    "fill commission currency differs from the deployment settlement currency"
                )
            occurred_at = _nt_timestamp(data.get("ts_event"))
            instrument = str(data.get("instrument_id") or "")
            fill_id = _scoped_event_id(
                authority, "fill_identity", venue, instrument, stable_trade_id
            )
            facts = (
                execution_fill(
                    event_id=_scoped_event_id(
                        authority, "execution_fill", venue, instrument, stable_trade_id
                    ),
                    venue=venue,
                    venue_trade_id=stable_trade_id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    instrument=instrument,
                    side=str(data.get("order_side") or ""),
                    quantity=str(data.get("last_qty") or ""),
                    price=str(data.get("last_px") or ""),
                    fee=fee_amount,
                    currency=currency,
                    occurred_at=occurred_at,
                ),
                settlement_fill(
                    event_id=_scoped_event_id(
                        authority, "settlement_fill", venue, instrument, stable_trade_id
                    ),
                    fill_id=fill_id,
                    order_type=str(data.get("order_type") or "unknown"),
                    category=str(data.get("liquidity_side") or "execution"),
                    price=str(data.get("last_px") or ""),
                    avg_fill_price=str(data.get("avg_px") or data.get("last_px") or ""),
                    currency=currency,
                    filled_at=occurred_at,
                ),
                settlement_fee(
                    event_id=_scoped_event_id(
                        authority, "settlement_fee", venue, instrument, stable_trade_id
                    ),
                    fill_id=fill_id,
                    amount=fee_amount,
                    currency=currency,
                    assessed_at=occurred_at,
                ),
            )
            self._emitter.emit_sync(authority, facts)
        except Exception as exc:  # audit loss is loud but never kills the engine thread
            _log.error("runner_fact_execution_event_failed", error=str(exc))

    def _on_position_event(self, event: Any) -> None:
        if type(event).__name__ != "PositionClosed":
            return
        try:
            data = type(event).to_dict(event)
            authority = self._deployment.authority
            event_identity = str(data.get("event_id") or "").strip()
            position_identity = str(data.get("position_id") or "").strip()
            if not event_identity or not position_identity:
                raise RunnerFactContractError("PositionClosed lacks stable event/position identity")
            pnl, pnl_currency = _money(data.get("realized_pnl", "0"), "realized_pnl")
            currency = pnl_currency or str(data.get("currency") or self._deployment.currency)
            if currency != self._deployment.currency:
                raise RunnerFactContractError(
                    "position PnL currency differs from the deployment settlement currency"
                )
            fact = position_closed(
                event_id=_scoped_event_id(authority, "position_closed", event_identity),
                position_id=_scoped_event_id(authority, "position_identity", position_identity),
                realized_pnl=pnl,
                currency=currency,
                opened_at=_nt_timestamp(data.get("ts_opened")),
                closed_at=_nt_timestamp(data.get("ts_closed") or data.get("ts_event")),
            )
            self._emitter.emit_sync(authority, (fact,))
        except Exception as exc:  # audit loss is loud but never kills the engine thread
            _log.error("runner_fact_position_event_failed", error=str(exc))


class RunnerFactProductionLoop:
    def __init__(
        self,
        *,
        host: RunnerFactHost,
        emitter: RunnerFactEmitter,
        snapshot_interval_secs: float,
        period_secs: int,
        period_retry_secs: float,
    ) -> None:
        if snapshot_interval_secs <= 0 or period_retry_secs <= 0 or period_secs < 60:
            raise ValueError("RunnerFact intervals must be positive and period_secs >= 60")
        self._host = host
        self._emitter = emitter
        self._snapshot_interval_secs = snapshot_interval_secs
        self._period_secs = period_secs
        self._period_retry_secs = period_retry_secs
        self._period_starts: dict[str, datetime] = {}

    async def run_observability(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for deployment in tuple(self._host.runner_fact_deployments()):
                await self._emit_observability(deployment)
            await self._wait(stop, self._snapshot_interval_secs)

    async def _emit_observability(self, deployment: RunnerFactDeployment) -> None:
        observed_at = datetime.now(UTC)
        authority = deployment.authority
        try:
            equity, positions = await self._host.runner_fact_risk_snapshot(
                deployment.deployment_instance_id, deployment.currency
            )
            facts = (
                equity_snapshot(
                    event_id=_scoped_event_id(authority, "equity", observed_at.isoformat()),
                    amount=equity,
                    currency=deployment.currency,
                    observed_at=observed_at,
                ),
                position_snapshot(
                    event_id=_scoped_event_id(authority, "positions", observed_at.isoformat()),
                    positions=positions,
                    observed_at=observed_at,
                ),
                heartbeat(
                    event_id=_scoped_event_id(authority, "heartbeat", observed_at.isoformat()),
                    status="online",
                    observed_at=observed_at,
                ),
            )
        except Exception as exc:
            _log.error(
                "runner_fact_risk_snapshot_failed",
                deployment_instance_id=deployment.deployment_instance_id,
                deployment_spec_id=str(authority.deployment_spec_id),
                error=str(exc),
            )
            facts = (
                heartbeat(
                    event_id=_scoped_event_id(authority, "heartbeat", observed_at.isoformat()),
                    status="degraded",
                    observed_at=observed_at,
                ),
            )
        try:
            await self._emitter.emit(authority, facts)
        except Exception as exc:
            _log.error("runner_fact_observability_enqueue_failed", error=str(exc))

    async def run_periods(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = datetime.now(UTC)
            active = tuple(self._host.runner_fact_deployments())
            active_keys = {deployment.authority.stream_key for deployment in active}
            self._period_starts = {
                key: value for key, value in self._period_starts.items() if key in active_keys
            }
            for deployment in active:
                key = deployment.authority.stream_key
                start = self._period_starts.setdefault(key, self._floor_period(now))
                closed_at = start + timedelta(seconds=self._period_secs)
                if now < closed_at:
                    continue
                if now - closed_at > timedelta(seconds=self._period_secs):
                    _log.error(
                        "runner_fact_period_gap_not_fabricated",
                        deployment_instance_id=deployment.deployment_instance_id,
                        deployment_spec_id=str(deployment.authority.deployment_spec_id),
                        missed_period_started_at=start.isoformat(),
                    )
                    self._period_starts[key] = self._floor_period(now)
                    continue
                if await self._close_reconciliation_period(deployment, start, closed_at):
                    self._period_starts[key] = closed_at
            await self._wait(stop, self._period_retry_secs)

    async def _close_reconciliation_period(
        self,
        deployment: RunnerFactDeployment,
        started_at: datetime,
        closed_at: datetime,
    ) -> bool:
        authority = deployment.authority
        period = f"{started_at:%Y%m%dT%H%M%SZ}_{closed_at:%Y%m%dT%H%M%SZ}"
        try:
            if not deployment.reconciliation_available:
                _log.warning(
                    "runner_fact_reconciliation_unavailable",
                    deployment_instance_id=deployment.deployment_instance_id,
                    deployment_spec_id=str(authority.deployment_spec_id),
                    trading_mode=authority.trading_mode,
                )
                return True
            evidence = await self._host.runner_fact_venue_ledger(
                deployment.deployment_instance_id, started_at, closed_at
            )
            snapshot_id = _scoped_event_id(
                authority, "venue_ledger_snapshot", evidence.venue, period
            )
            snapshot_facts = venue_ledger_snapshot_facts(
                snapshot_id=snapshot_id,
                venue=evidence.venue,
                source=evidence.source,
                watermark=evidence.watermark,
                coverage_from=evidence.coverage_from,
                observed_through=evidence.observed_through,
                completeness=evidence.completeness,
                balances=evidence.balances,
                positions=evidence.positions,
                fills=evidence.fills,
                fees=evidence.fees,
            )
            for fact in snapshot_facts:
                await self._emitter.emit(authority, (fact,))
            await self._emitter.emit(
                authority,
                (
                    reconciliation_period_closed(
                        event_id=_scoped_event_id(authority, "reconciliation_period", period),
                        period=period,
                        period_started_at=started_at,
                        closed_at=closed_at,
                        venue_snapshots=({"venue": evidence.venue, "snapshot_id": snapshot_id},),
                    ),
                ),
            )
            return True
        except Exception as exc:
            _log.error(
                "runner_fact_period_close_failed",
                deployment_instance_id=deployment.deployment_instance_id,
                deployment_spec_id=str(authority.deployment_spec_id),
                period=period,
                error=str(exc),
            )
            return False

    def _floor_period(self, value: datetime) -> datetime:
        seconds = int(value.timestamp())
        return datetime.fromtimestamp(seconds - seconds % self._period_secs, UTC)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
