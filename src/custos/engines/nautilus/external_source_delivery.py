"""Plan one deployment's bounded intent from a target-neutral source event."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from custos_toolkit.signals.types import Signal, SignalDirection

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_signal import ExternalSourceSignal, make_source_event_id
from custos.engines.nautilus.host import ExternalSourceRuntimeScope


class ExternalSourceDeliveryError(ValueError):
    """The source event cannot target this active deployment."""


@dataclass(frozen=True, slots=True)
class SourceSubscriptionBinding:
    tenant_id: str
    source_id: str
    source_symbol: str
    target_pair: str
    authority: EngineLifecycleAuthority


@dataclass(frozen=True, slots=True)
class PlannedTargetDelivery:
    delivery_id: str
    source_event_id: str
    authority: EngineLifecycleAuthority
    pair: str
    target: str
    risk_action: str | None
    signal: Signal | None


def _epoch_ns(value: datetime) -> int:
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def plan_target_delivery(
    event: ExternalSourceSignal,
    binding: SourceSubscriptionBinding,
    scope: ExternalSourceRuntimeScope,
    *,
    at: datetime,
) -> PlannedTargetDelivery:
    """Map direction only; the subscriber owns sizing, venue and final position."""
    if (
        event.tenant_id != binding.tenant_id
        or event.source_id != binding.source_id
        or event.symbol != binding.source_symbol
    ):
        raise ExternalSourceDeliveryError("source differs from subscription")
    if not isinstance(event.target, str) or event.target not in {"LONG", "SHORT", "FLAT"}:
        raise ExternalSourceDeliveryError("source target is unsupported")
    if event.risk_action is not None and (event.risk_action != "FLATTEN" or event.target != "FLAT"):
        raise ExternalSourceDeliveryError("risk action requires a flat target")
    if event.event_id != make_source_event_id(
        source_id=event.source_id,
        strategy_revision=event.strategy_revision,
        symbol=event.symbol,
        timeframe=event.timeframe,
        bar_close_time=event.bar_close_time,
        target=event.target,
        risk_action=event.risk_action,
    ):
        raise ExternalSourceDeliveryError("source event identity differs from fields")
    authority = binding.authority
    if scope.authority != authority or authority.trading_mode not in {"sandbox", "testnet"}:
        raise ExternalSourceDeliveryError("authority differs from active deployment")
    if scope.connector not in {"okx_perpetual", "binance_perpetual"}:
        raise ExternalSourceDeliveryError("connector is outside external source scope")
    if binding.target_pair not in scope.allowed_pairs:
        raise ExternalSourceDeliveryError("target pair differs from active deployment")
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ExternalSourceDeliveryError("delivery clock must be UTC")
    if at < event.timestamp:
        raise ExternalSourceDeliveryError("source event is future dated")
    if at > event.expires_at:
        raise ExternalSourceDeliveryError("source event is expired")
    identity = "\0".join(
        (
            binding.tenant_id,
            event.event_id,
            str(authority.deployment_instance_id),
            str(authority.deployment_spec_id),
            authority.deployment_spec_digest,
            str(authority.generation),
            scope.connector,
            binding.target_pair,
        )
    )
    delivery_id = hashlib.sha256(identity.encode()).hexdigest()
    signal: Signal | None = None
    if event.target in {"LONG", "SHORT"}:
        direction = (
            SignalDirection.ENTER_LONG if event.target == "LONG" else SignalDirection.ENTER_SHORT
        )
        signal = Signal(
            direction=direction,
            pair=binding.target_pair,
            price=Decimal("0"),
            timestamp=_epoch_ns(event.timestamp),
            amount=None,
            order_type="market",
            max_lag=event.max_lag,
            metadata={"source_event_id": event.event_id, "target_delivery_id": delivery_id},
        )
    return PlannedTargetDelivery(
        delivery_id=delivery_id,
        source_event_id=event.event_id,
        authority=authority,
        pair=binding.target_pair,
        target=event.target,
        risk_action=event.risk_action,
        signal=signal,
    )
