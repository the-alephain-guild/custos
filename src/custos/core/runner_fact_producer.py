"""Production bridges from execution and venue facts into the signed outbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    strategy_version: str
    timeframe: str

    def __post_init__(self) -> None:
        if (
            str(self.authority.deployment_instance_id) != self.deployment_instance_id
            or str(self.authority.deployment_spec_id) != self.deployment_spec_id
            or self.authority.deployment_spec_digest != self.deployment_spec_digest
        ):
            raise RunnerFactContractError(
                "RunnerFactDeployment identity differs from its signed authority"
            )
        if not self.strategy_version.strip() or not self.timeframe.strip():
            raise RunnerFactContractError(
                "RunnerFactDeployment requires strategy version and timeframe metadata"
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


@dataclass(frozen=True, slots=True)
class RunnerCapitalBasisSnapshot:
    """Non-secret execution-capital inputs observed by the running strategy."""

    currency: str
    venue_available: str
    strategy_sizing_basis: str
    configured_initial_capital: str
    capital_mode: str
    reserved_notional: str
    open_exposure: str
    total_exposure: str
    max_total_notional: str
    within_policy: bool


class RunnerFactHost(Protocol):
    def runner_fact_deployments(self) -> Sequence[RunnerFactDeployment]: ...

    async def runner_fact_risk_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> tuple[Decimal, Sequence[Mapping[str, Any]]]: ...

    async def runner_fact_capital_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> RunnerCapitalBasisSnapshot: ...

    async def runner_fact_venue_ledger(
        self, deployment_instance_id: str, coverage_from: datetime, closed_at: datetime
    ) -> VenueLedgerEvidence: ...


class RunnerRuntimeLogPort(Protocol):
    def emit_sync(self, authority: RunnerFactAuthority, **event: Any) -> object: ...


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


def _capital_basis_fact(
    authority: RunnerFactAuthority,
    *,
    observed_at: datetime,
    venue_equity: Decimal | str | int,
    snapshot: RunnerCapitalBasisSnapshot,
) -> dict[str, Any]:
    """Build one signed, structured capital-basis observation.

    This is deliberately a typed message inside the existing signed RunnerFact
    stream. It never parses stdout and contains no credential or order identity.
    """

    if snapshot.currency != str(snapshot.currency).strip().upper():
        raise RunnerFactContractError("capital basis currency must be uppercase")
    decimal_fields = {
        "venue_equity": venue_equity,
        "venue_available": snapshot.venue_available,
        "strategy_sizing_basis": snapshot.strategy_sizing_basis,
        "configured_initial_capital": snapshot.configured_initial_capital,
        "reserved_notional": snapshot.reserved_notional,
        "open_exposure": snapshot.open_exposure,
        "total_exposure": snapshot.total_exposure,
        "max_total_notional": snapshot.max_total_notional,
    }
    normalized = {}
    for field, value in decimal_fields.items():
        amount = Decimal(str(value))
        text = format(amount, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        normalized[field] = text or "0"
    if any(Decimal(value) < 0 for value in normalized.values()):
        raise RunnerFactContractError("capital basis amounts must be non-negative")
    if snapshot.capital_mode not in {"compound", "fixed_capital"}:
        raise RunnerFactContractError("capital basis mode is not canonical")
    correlation_id = _scoped_event_id(
        authority, "capital_basis_correlation", observed_at.isoformat()
    )
    return {
        "kind": "RunnerRuntimeLogFact.v1",
        "event_id": _scoped_event_id(authority, "capital_basis", observed_at.isoformat()),
        "occurred_at": observed_at.isoformat().replace("+00:00", "Z"),
        "level": "INFO",
        "component": "custos.capital_basis",
        "message": "runner_capital_basis_observed",
        "structured_fields": {
            "schema_version": 1,
            "currency": snapshot.currency,
            **normalized,
            "capital_mode": snapshot.capital_mode,
            "within_policy": snapshot.within_policy,
        },
        "correlation_id": correlation_id,
        "causation_id": None,
    }


def _metadata_field(value: object, field: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(field)
    return getattr(value, field, None)


def _runtime_strategy_timeframe(strategy: object | None) -> str | None:
    """Read the bar type the verified Nautilus strategy will actually use."""

    if strategy is None:
        return None
    config = _metadata_field(strategy, "config")
    platforms = _metadata_field(config, "platforms")
    nautilus = _metadata_field(platforms, "nautilus")
    bar_type = _metadata_field(nautilus, "bar_type")
    if bar_type is None:
        return None
    timeframe = str(bar_type).strip()
    if not timeframe:
        raise RunnerFactContractError("verified runtime strategy timeframe is empty")
    return timeframe


def strategy_signal_metadata(
    spec: Mapping[str, Any],
    *,
    runtime_strategy: object | None = None,
) -> tuple[str, str]:
    """Derive honest signal labels from signed input and verified runtime config."""

    source = spec.get("artifact_source")
    source = source if isinstance(source, Mapping) else {}
    snapshot = source.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if source.get("kind") == "strategy_release" and type(snapshot.get("release_version")) is int:
        strategy_version = f"v{snapshot['release_version']}"
    elif source.get("kind") == "development_source" and isinstance(
        snapshot.get("source_sha256"), str
    ):
        strategy_version = f"development-{snapshot['source_sha256'][:12]}"
    else:
        strategy_version = str(spec.get("strategy_version") or "unversioned").strip()

    config = spec.get("strategy_config")
    config = config if isinstance(config, Mapping) else {}
    nautilus = spec.get("nautilus_config")
    nautilus = nautilus if isinstance(nautilus, Mapping) else {}
    declared_value = config.get("timeframe") or config.get("bar_type") or nautilus.get("bar_type")
    declared_timeframe = str(declared_value).strip() if declared_value is not None else None
    if declared_timeframe == "unspecified":
        declared_timeframe = None
    runtime_timeframe = _runtime_strategy_timeframe(runtime_strategy)
    if (
        declared_timeframe is not None
        and runtime_timeframe is not None
        and declared_timeframe != runtime_timeframe
    ):
        raise RunnerFactContractError(
            "declared signal timeframe differs from verified runtime strategy timeframe"
        )
    timeframe = runtime_timeframe or declared_timeframe or "unspecified"
    if not strategy_version or not timeframe:
        raise RunnerFactContractError("strategy signal metadata is empty")
    return strategy_version, timeframe


class RunnerFactMessageBusBridge:
    """Synchronously commits execution events to SQLite before returning."""

    def __init__(
        self,
        *,
        emitter: RunnerFactEmitter,
        deployment: RunnerFactDeployment,
        runtime_log_emitter: RunnerRuntimeLogPort | None = None,
    ) -> None:
        self._emitter = emitter
        self._deployment = deployment
        self._runtime_log_emitter = runtime_log_emitter
        self._order_directions: dict[str, str] = {}
        self._order_roles: dict[str, str] = {}
        self._owned_order_ids: set[str] = set()

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
        event_name = type(event).__name__
        if event_name == "OrderInitialized":
            self._on_order_initialized(event)
            return
        client_order_id = self._event_client_order_id(event)
        if client_order_id and client_order_id not in self._owned_order_ids:
            # A live venue stream is account-wide. Separate deployment nodes can see
            # sibling/manual orders on the same account; only a locally initialized
            # order may enter this instance-scoped signed fact stream.
            return
        if event_name == "OrderSubmitted":
            self._on_order_lifecycle(event, lifecycle="submitted", level="INFO")
            return
        if event_name in {"OrderRejected", "OrderDenied"}:
            self._on_order_lifecycle(
                event,
                lifecycle="rejected",
                level="WARN",
                include_reason=True,
            )
            return
        if event_name == "OrderCanceled":
            self._on_order_lifecycle(event, lifecycle="canceled", level="INFO")
            return
        if event_name == "OrderExpired":
            self._on_order_lifecycle(event, lifecycle="expired", level="WARN")
            return
        if event_name != "OrderFilled":
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

    def _on_order_initialized(self, event: Any) -> None:
        try:
            data = type(event).to_dict(event)
            authority = self._deployment.authority
            event_id = str(data.get("event_id") or "").strip()
            client_order_id = str(data.get("client_order_id") or "").strip()
            stable_identity = event_id or client_order_id
            if not stable_identity:
                raise RunnerFactContractError("OrderInitialized has no stable event/order identity")
            instrument = str(data.get("instrument_id") or "").strip()
            if not instrument:
                raise RunnerFactContractError("OrderInitialized has no instrument identity")
            side = str(data.get("order_side") or "").strip().lower().split(".")[-1]
            if side not in {"buy", "sell"}:
                raise RunnerFactContractError("OrderInitialized has an unsupported order side")
            reduce_only = bool(data.get("reduce_only"))
            order_type = str(data.get("order_type") or "unknown").strip().upper()
            quantity = str(data.get("quantity") or "").strip()
            if not quantity:
                raise RunnerFactContractError("OrderInitialized has no requested quantity")
            protective = reduce_only and any(
                marker in order_type for marker in ("STOP", "TAKE_PROFIT", "TRAILING")
            )
            if reduce_only:
                direction = "flat"
            elif side == "buy":
                direction = "long"
            else:
                direction = "short"
            order_role = (
                "protective_stop"
                if protective
                else "position_reduction"
                if reduce_only
                else "strategy_entry"
            )
            lifecycle_side = side if reduce_only else direction
            if client_order_id:
                self._owned_order_ids.add(client_order_id)
                self._order_directions[client_order_id] = lifecycle_side
                self._order_roles[client_order_id] = order_role
            occurred_at = _nt_timestamp(data.get("ts_event") or data.get("ts_init"))
            if self._runtime_log_emitter is not None:
                self._runtime_log_emitter.emit_sync(
                    authority,
                    level="INFO",
                    component="custos.execution.order",
                    message="order_initialized",
                    structured_fields={
                        "client_order_id": client_order_id,
                        "instrument": instrument,
                        "side": lifecycle_side,
                        "lifecycle": "initialized",
                        "order_role": order_role,
                        "order_type": order_type.lower(),
                        "quantity": quantity,
                    },
                    correlation_id=_scoped_event_id(
                        authority, "order_trace", client_order_id or stable_identity
                    ),
                )
            if protective:
                return
            input_document = {
                "client_order_id": client_order_id or None,
                "direction": direction,
                "event_id": event_id or None,
                "instrument": instrument,
                "occurred_at": occurred_at,
                "quantity": quantity,
                "strategy_version": self._deployment.strategy_version,
                "timeframe": self._deployment.timeframe,
            }
            input_digest = hashlib.sha256(
                json.dumps(
                    input_document,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            self._emitter.emit_strategy_signal_sync(
                authority,
                fact_id=_scoped_event_id(authority, "strategy_signal", stable_identity),
                instrument=instrument,
                client_order_id=client_order_id or None,
                timeframe=self._deployment.timeframe,
                direction=direction,
                occurred_at=occurred_at,
                input_digest=input_digest,
                strategy_version=self._deployment.strategy_version,
                trace_id=_scoped_event_id(authority, "strategy_trace", stable_identity),
            )
        except Exception as exc:  # audit loss is loud but never kills the engine thread
            _log.error("runner_strategy_signal_event_failed", error=str(exc))

    def _on_order_lifecycle(
        self,
        event: Any,
        *,
        lifecycle: str,
        level: str,
        include_reason: bool = False,
    ) -> None:
        if self._runtime_log_emitter is None:
            return
        try:
            data = type(event).to_dict(event)
            client_order_id = str(data.get("client_order_id") or "").strip()
            if not client_order_id:
                raise RunnerFactContractError("order lifecycle event has no client order id")
            instrument = str(data.get("instrument_id") or "").strip()
            if not instrument:
                raise RunnerFactContractError("order lifecycle event has no instrument identity")
            side = self._order_directions.get(client_order_id)
            if side is None:
                raw_side = str(data.get("order_side") or "").strip().lower().split(".")[-1]
                side = {"buy": "long", "sell": "short"}.get(raw_side)
            if side is None:
                raise RunnerFactContractError("order lifecycle event has no known side")
            fields: dict[str, Any] = {
                "client_order_id": client_order_id,
                "instrument": instrument,
                "side": side,
                "lifecycle": lifecycle,
                "order_role": self._order_roles.get(client_order_id, "strategy_order"),
            }
            if include_reason:
                reason = str(data.get("reason") or "unknown_rejection").strip()
                fields["reason_code"] = reason or "unknown_rejection"
            authority = self._deployment.authority
            self._runtime_log_emitter.emit_sync(
                authority,
                level=level,
                component="custos.execution.order",
                message=f"order_{lifecycle}",
                structured_fields=fields,
                correlation_id=_scoped_event_id(authority, "order_trace", client_order_id),
            )
            if lifecycle in {"rejected", "canceled", "expired"}:
                self._order_directions.pop(client_order_id, None)
                self._order_roles.pop(client_order_id, None)
                self._owned_order_ids.discard(client_order_id)
        except Exception as exc:  # signed lifecycle loss is visible but never kills execution
            _log.error("runner_order_lifecycle_event_failed", error=str(exc))

    @staticmethod
    def _event_client_order_id(event: Any) -> str:
        try:
            data = type(event).to_dict(event)
        except Exception:
            data = {}
        return str(data.get("client_order_id") or getattr(event, "client_order_id", "")).strip()

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
            facts: tuple[dict[str, Any], ...] = (
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
            try:
                capital = await self._host.runner_fact_capital_snapshot(
                    deployment.deployment_instance_id, deployment.currency
                )
                facts += (
                    _capital_basis_fact(
                        authority,
                        observed_at=observed_at,
                        venue_equity=equity,
                        snapshot=capital,
                    ),
                )
            except Exception as exc:
                _log.error(
                    "runner_fact_capital_snapshot_failed",
                    deployment_instance_id=deployment.deployment_instance_id,
                    deployment_spec_id=str(authority.deployment_spec_id),
                    error=str(exc),
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
