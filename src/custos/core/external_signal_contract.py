"""Strict, side-effect-free parsing for authorized external strategy alerts.

The OKX Section B payload and the runner extension have separate closed shapes.
Parsing a token never authenticates its sender; the ingress boundary owns that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID


class ExternalSignalContractError(ValueError):
    """An alert does not meet the selected message contract."""


_ACTIONS = frozenset({"ENTER_LONG", "ENTER_SHORT", "EXIT_LONG", "EXIT_SHORT"})
_ENTRY_TYPES = frozenset({"margin", "contract", "percentage_balance", "percentage_investment"})
_EXIT_TYPES = frozenset({"percentage_position"})
_B_REQUIRED = frozenset({"action", "instrument", "signalToken", "timestamp"})
_B_OPTIONAL = frozenset({"maxLag", "orderType", "orderPriceOffset", "investmentType", "amount"})
_EXT_REQUIRED = frozenset(
    {
        "schemaVersion",
        "sourceId",
        "strategyRevision",
        "deploymentInstanceId",
        "timeframe",
        "barCloseTime",
        "requestedLeverage",
        "eventId",
    }
)
_EXT_OPTIONAL = frozenset({"riskAction"})
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class OkxBAlert:
    action: str
    instrument: str
    timestamp: datetime
    max_lag: int
    order_type: str | None
    order_price_offset: Decimal | None
    investment_type: str | None
    amount: Decimal | None
    signal_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CustosV1Alert:
    trade: OkxBAlert
    source_id: str
    strategy_revision: str
    deployment_instance_id: UUID
    timeframe: str
    bar_close_time: datetime
    requested_leverage: int
    event_id: str
    risk_action: str | None


def _fields(value: object, *, required: frozenset[str], allowed: frozenset[str]) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ExternalSignalContractError("alert must be an object with string keys")
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing or unknown:
        raise ExternalSignalContractError("alert has missing or unsupported fields")
    return value


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ExternalSignalContractError(f"{name} has an invalid format")
    return value


def _utc_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ExternalSignalContractError(f"{name} must be an ISO 8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExternalSignalContractError(f"{name} must be an ISO 8601 UTC string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExternalSignalContractError(f"{name} must include UTC timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ExternalSignalContractError(f"{name} must be UTC")
    return parsed.astimezone(UTC)


def _positive_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise ExternalSignalContractError(f"{name} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and len(value) <= 10 and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ExternalSignalContractError(f"{name} must be an integer")
    if not 1 <= parsed <= maximum:
        raise ExternalSignalContractError(f"{name} is out of range")
    return parsed


def _decimal(value: object, name: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ExternalSignalContractError(f"{name} must be exact decimal input")
    if not isinstance(value, (str, int, Decimal)):
        raise ExternalSignalContractError(f"{name} must be exact decimal input")
    if isinstance(value, str) and len(value) > 128:
        raise ExternalSignalContractError(f"{name} is too long")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ExternalSignalContractError(f"{name} must be exact decimal input") from error
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise ExternalSignalContractError(f"{name} is out of range")
    return parsed


def parse_okx_b_alert(
    payload: object, *, now: datetime, allowed_instruments: set[str] | frozenset[str]
) -> OkxBAlert:
    """Parse an OKX Section B trade message without treating its token as auth."""
    value = _fields(payload, required=_B_REQUIRED, allowed=_B_REQUIRED | _B_OPTIONAL)
    action = value["action"]
    if action not in _ACTIONS:
        raise ExternalSignalContractError("action is unsupported")
    instrument = value["instrument"]
    if not isinstance(instrument, str) or instrument not in allowed_instruments:
        raise ExternalSignalContractError("instrument is not allowlisted")
    token = value["signalToken"]
    if not isinstance(token, str) or not token or len(token) > 512:
        raise ExternalSignalContractError("signalToken is missing or invalid")
    timestamp = _utc_time(value["timestamp"], "timestamp")
    max_lag = _positive_int(value.get("maxLag", 60), "maxLag", 3600)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ExternalSignalContractError("receiver clock must include UTC timezone")
    age_seconds = (now.astimezone(UTC) - timestamp).total_seconds()
    if age_seconds < 0 or age_seconds > max_lag:
        raise ExternalSignalContractError("alert timestamp is future or expired")

    order_type = value.get("orderType")
    if order_type is not None and order_type not in {"market", "limit"}:
        raise ExternalSignalContractError("orderType is unsupported")
    offset_raw = value.get("orderPriceOffset")
    if order_type == "limit" and offset_raw is None:
        raise ExternalSignalContractError("limit order requires orderPriceOffset")
    if offset_raw is not None and order_type != "limit":
        raise ExternalSignalContractError("orderPriceOffset requires limit order")
    offset = (
        _decimal(offset_raw, "orderPriceOffset", allow_zero=True)
        if offset_raw is not None
        else None
    )
    if offset is not None and offset > 100:
        raise ExternalSignalContractError("orderPriceOffset is out of range")

    investment_type = value.get("investmentType")
    allowed_types = _ENTRY_TYPES if action.startswith("ENTER") else _EXIT_TYPES
    if investment_type is not None and investment_type not in allowed_types:
        raise ExternalSignalContractError("investmentType is incompatible with action")
    amount_raw = value.get("amount")
    amount = _decimal(amount_raw, "amount") if amount_raw is not None else None
    if (
        amount is not None
        and investment_type == "contract"
        and amount != amount.to_integral_value()
    ):
        raise ExternalSignalContractError("contract amount must be integral")
    if (
        amount is not None
        and investment_type
        in {"percentage_balance", "percentage_investment", "percentage_position"}
        and not Decimal("0.01") <= amount <= 100
    ):
        raise ExternalSignalContractError("percentage amount is out of range")

    return OkxBAlert(
        action=action,
        instrument=instrument,
        timestamp=timestamp,
        max_lag=max_lag,
        order_type=order_type,
        order_price_offset=offset,
        investment_type=investment_type,
        amount=amount,
        signal_token=token,
    )


def make_event_id(
    *,
    source_id: str,
    strategy_revision: str,
    instrument: str,
    timeframe: str,
    bar_close_time: datetime,
    action: object,
    risk_action: object,
) -> str:
    """Build an event key that Pine can reproduce without a hash function."""
    _label(source_id, "sourceId")
    _label(strategy_revision, "strategyRevision")
    if not isinstance(instrument, str) or not _LABEL.fullmatch(instrument):
        raise ExternalSignalContractError("instrument has an invalid format")
    if timeframe != "30" or action not in _ACTIONS or risk_action not in (None, "FLATTEN"):
        raise ExternalSignalContractError("event identity fields are invalid")
    if bar_close_time.tzinfo is None or bar_close_time.utcoffset() is None:
        raise ExternalSignalContractError("barCloseTime must include UTC timezone")
    milliseconds = int(bar_close_time.astimezone(UTC).timestamp() * 1000)
    return ":".join(
        (
            source_id,
            strategy_revision,
            instrument,
            timeframe,
            str(milliseconds),
            action,
            risk_action or "TRADE",
        )
    )


def parse_custos_v1_alert(
    payload: object,
    *,
    now: datetime,
    allowed_instruments: set[str] | frozenset[str],
    expected_deployment_instance_id: UUID,
    authorized_leverage: int,
) -> CustosV1Alert:
    """Parse a runner extension and bind it to an already authorized instance."""
    allowed = _B_REQUIRED | _B_OPTIONAL | _EXT_REQUIRED | _EXT_OPTIONAL
    value = _fields(payload, required=_B_REQUIRED | _EXT_REQUIRED, allowed=allowed)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ExternalSignalContractError("schemaVersion is unsupported")
    trade = parse_okx_b_alert(
        {key: item for key, item in value.items() if key in _B_REQUIRED | _B_OPTIONAL},
        now=now,
        allowed_instruments=allowed_instruments,
    )
    source_id = _label(value["sourceId"], "sourceId")
    revision = _label(value["strategyRevision"], "strategyRevision")
    try:
        instance_id = UUID(str(value["deploymentInstanceId"]))
    except (ValueError, TypeError) as error:
        raise ExternalSignalContractError("deploymentInstanceId is invalid") from error
    if instance_id != expected_deployment_instance_id:
        raise ExternalSignalContractError("deploymentInstanceId is not authorized")
    timeframe = value["timeframe"]
    if timeframe != "30":
        raise ExternalSignalContractError("timeframe is unsupported")
    bar_close = _utc_time(value["barCloseTime"], "barCloseTime")
    if bar_close.second or bar_close.microsecond or bar_close.minute % 30:
        raise ExternalSignalContractError("barCloseTime is not a 30-minute close")
    if bar_close > trade.timestamp:
        raise ExternalSignalContractError("barCloseTime is later than timestamp")
    requested_leverage = _positive_int(value["requestedLeverage"], "requestedLeverage", 125)
    risk_action = value.get("riskAction")
    if risk_action not in (None, "FLATTEN"):
        raise ExternalSignalContractError("riskAction is unsupported")
    if risk_action == "FLATTEN" and not trade.action.startswith("EXIT"):
        raise ExternalSignalContractError("FLATTEN requires an exit action")
    if trade.action.startswith("ENTER") and requested_leverage != authorized_leverage:
        raise ExternalSignalContractError("requestedLeverage differs from deployment")
    event_id = make_event_id(
        source_id=source_id,
        strategy_revision=revision,
        instrument=trade.instrument,
        timeframe=timeframe,
        bar_close_time=bar_close,
        action=trade.action,
        risk_action=risk_action,
    )
    if value["eventId"] != event_id:
        raise ExternalSignalContractError("eventId differs from alert identity")
    return CustosV1Alert(
        trade=trade,
        source_id=source_id,
        strategy_revision=revision,
        deployment_instance_id=instance_id,
        timeframe=timeframe,
        bar_close_time=bar_close,
        requested_leverage=requested_leverage,
        event_id=event_id,
        risk_action=risk_action,
    )
