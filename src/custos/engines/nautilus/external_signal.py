"""Convert already accepted external events into bounded toolkit signals.

This pure conversion never queues a signal or places an order. Its caller must
obtain the active scope and maximum order notional from verified runtime state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from custos_toolkit.signals.types import Signal, SignalDirection

from custos.core.external_signal_inbox import StoredExternalSignal
from custos.engines.nautilus.host import ExternalSignalRuntimeScope


class ExternalSignalConversionError(ValueError):
    """A stored event cannot be safely represented as a toolkit signal."""


def _decimal_string(value: object, label: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 128:
        raise ExternalSignalConversionError(f"{label} must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ExternalSignalConversionError(f"{label} is invalid") from error
    if not result.is_finite() or result <= 0:
        raise ExternalSignalConversionError(f"{label} must be positive and finite")
    return result


def _timestamp_ns(value: object) -> int:
    if not isinstance(value, str):
        raise ExternalSignalConversionError("event timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ExternalSignalConversionError("event timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds():
        raise ExternalSignalConversionError("event timestamp must be UTC")
    utc = parsed.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def to_toolkit_signal(
    event: StoredExternalSignal,
    scope: ExternalSignalRuntimeScope,
    *,
    max_order_notional: Decimal,
    limit_currency: str,
) -> Signal:
    """Use signed leverage to convert quote margin to capped quote notional."""
    authority = scope.authority
    if (
        event.deployment_instance_id != authority.deployment_instance_id
        or event.deployment_spec_id != authority.deployment_spec_id
        or event.deployment_spec_digest != authority.deployment_spec_digest
        or event.generation != authority.generation
        or authority.trading_mode not in {"sandbox", "testnet"}
    ):
        raise ExternalSignalConversionError("event differs from runtime authority")
    if (
        event.instrument not in scope.allowed_instruments
        or not event.instrument.endswith("-SWAP")
        or event.payload.get("instrument") != event.instrument
        or event.payload.get("action") != event.action
    ):
        raise ExternalSignalConversionError("event differs from runtime instrument or action")
    if event.risk_action is not None:
        raise ExternalSignalConversionError("risk flatten requires the containment path")
    if event.payload.get("orderType") != "market":
        raise ExternalSignalConversionError("only market external signals are admitted")
    instrument_quote = event.instrument.removesuffix("-SWAP").rsplit("-", 1)[-1]
    if instrument_quote not in {"USDT", "USDC"} or limit_currency != instrument_quote:
        raise ExternalSignalConversionError("order limit currency differs from instrument quote")
    max_lag = event.payload.get("maxLag")
    if type(max_lag) is not int or not 1 <= max_lag <= 3600:
        raise ExternalSignalConversionError("event maxLag is invalid")
    if not isinstance(max_order_notional, Decimal) or not max_order_notional.is_finite():
        raise ExternalSignalConversionError("order notional limit is invalid")
    if max_order_notional <= 0:
        raise ExternalSignalConversionError("order notional limit is invalid")

    amount: Decimal | None
    if event.action in {"ENTER_LONG", "ENTER_SHORT"}:
        if (
            event.payload.get("investmentType") != "margin"
            or type(event.payload.get("requestedLeverage")) is not int
            or event.payload["requestedLeverage"] != scope.authorized_leverage
            or scope.authorized_leverage < 1
        ):
            raise ExternalSignalConversionError("entry margin or leverage differs")
        margin = _decimal_string(event.payload.get("amount"), "entry margin")
        amount = margin * Decimal(scope.authorized_leverage)
        if amount > max_order_notional:
            raise ExternalSignalConversionError("order notional exceeds local limit")
    elif event.action in {"EXIT_LONG", "EXIT_SHORT"}:
        if (
            event.payload.get("investmentType") != "percentage_position"
            or _decimal_string(event.payload.get("amount"), "exit percentage") != 100
        ):
            raise ExternalSignalConversionError("only full-position external exit is admitted")
        amount = None
    else:
        raise ExternalSignalConversionError("event action is unsupported")

    return Signal(
        direction=SignalDirection[event.action],
        pair=event.instrument.removesuffix("-SWAP"),
        price=Decimal("0"),
        timestamp=_timestamp_ns(event.payload.get("timestamp")),
        amount=amount,
        order_type="market",
        max_lag=max_lag,
        metadata={"external_event_id": event.event_id},
    )
