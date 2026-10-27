"""Target-neutral external signal contract for independently authorized runners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class ExternalSourceSignalError(ValueError):
    """A source event is ambiguous, stale, or outside its configured scope."""


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,39}\Z")
_TARGETS = frozenset({"LONG", "SHORT", "FLAT"})
_REQUIRED = frozenset(
    {
        "schemaVersion",
        "sourceId",
        "strategyRevision",
        "symbol",
        "timeframe",
        "barCloseTime",
        "timestamp",
        "maxLag",
        "target",
        "eventId",
    }
)
_OPTIONAL = frozenset({"riskAction", "signalToken", "tenantId"})


@dataclass(frozen=True, slots=True)
class ExternalSourceSignal:
    tenant_id: str
    source_id: str
    strategy_revision: str
    symbol: str
    timeframe: str
    bar_close_time: datetime
    timestamp: datetime
    max_lag: int
    target: str
    risk_action: str | None
    event_id: str

    @property
    def expires_at(self) -> datetime:
        return self.timestamp + timedelta(seconds=self.max_lag)


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ExternalSourceSignalError(f"{name} is invalid")
    return value


def _utc_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ExternalSourceSignalError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ExternalSourceSignalError(f"{name} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ExternalSourceSignalError(f"{name} must be UTC")
    return parsed.astimezone(UTC)


def make_source_event_id(
    *,
    source_id: str,
    strategy_revision: str,
    symbol: str,
    timeframe: str,
    bar_close_time: datetime,
    target: str,
    risk_action: str | None,
) -> str:
    """Build a Pine-reproducible identity without deployment or venue state."""
    _label(source_id, "sourceId")
    _label(strategy_revision, "strategyRevision")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ExternalSourceSignalError("symbol is invalid")
    if timeframe != "30" or not isinstance(target, str) or target not in _TARGETS:
        raise ExternalSourceSignalError("source event identity is invalid")
    if risk_action is not None and (risk_action != "FLATTEN" or target != "FLAT"):
        raise ExternalSourceSignalError("risk action requires a flat target")
    if bar_close_time.tzinfo is None or bar_close_time.utcoffset() != timedelta(0):
        raise ExternalSourceSignalError("barCloseTime must be UTC")
    milliseconds = int(bar_close_time.timestamp()) * 1000 + bar_close_time.microsecond // 1000
    return ":".join(
        (
            source_id,
            strategy_revision,
            symbol,
            timeframe,
            str(milliseconds),
            target,
            risk_action or "TRADE",
        )
    )


def parse_external_source_signal(
    payload: object,
    *,
    tenant_id: str,
    expected_source_id: str,
    allowed_symbols: frozenset[str],
    now: datetime,
) -> ExternalSourceSignal:
    """Parse one authorized source's direction, without granting trading authority."""
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise ExternalSourceSignalError("source signal must be an object")
    if not _REQUIRED <= payload.keys() or payload.keys() - (_REQUIRED | _OPTIONAL):
        raise ExternalSourceSignalError("source signal fields are missing or unsupported")
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
        raise ExternalSourceSignalError("source signal schemaVersion is unsupported")
    tenant = _label(tenant_id, "tenantId")
    if "tenantId" in payload and payload["tenantId"] != tenant:
        raise ExternalSourceSignalError("tenantId differs from trusted scope")
    source = _label(payload["sourceId"], "sourceId")
    if source != _label(expected_source_id, "expected sourceId"):
        raise ExternalSourceSignalError("sourceId is outside configured scope")
    revision = _label(payload["strategyRevision"], "strategyRevision")
    symbol = payload["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ExternalSourceSignalError("symbol is invalid")
    if symbol not in allowed_symbols:
        raise ExternalSourceSignalError("symbol is outside configured scope")
    timeframe = payload["timeframe"]
    if timeframe != "30":
        raise ExternalSourceSignalError("timeframe is unsupported")
    bar_close = _utc_time(payload["barCloseTime"], "barCloseTime")
    if bar_close.minute % 30 or bar_close.second or bar_close.microsecond:
        raise ExternalSourceSignalError("barCloseTime is not aligned")
    timestamp = _utc_time(payload["timestamp"], "timestamp")
    if timestamp < bar_close:
        raise ExternalSourceSignalError("timestamp precedes barCloseTime")
    max_lag = payload["maxLag"]
    if type(max_lag) is not int or not 1 <= max_lag <= 3600:
        raise ExternalSourceSignalError("maxLag is invalid")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ExternalSourceSignalError("receiver clock must be UTC")
    if now < timestamp:
        raise ExternalSourceSignalError("source signal is future dated")
    if now > timestamp + timedelta(seconds=max_lag):
        raise ExternalSourceSignalError("source signal is expired")
    target = payload["target"]
    risk_action = payload.get("riskAction")
    event_id = make_source_event_id(
        source_id=source,
        strategy_revision=revision,
        symbol=symbol,
        timeframe=timeframe,
        bar_close_time=bar_close,
        target=target,
        risk_action=risk_action,
    )
    if payload["eventId"] != event_id:
        raise ExternalSourceSignalError("eventId differs from source identity")
    token = payload.get("signalToken")
    if token is not None and (not isinstance(token, str) or not token or len(token) > 512):
        raise ExternalSourceSignalError("signalToken is invalid")
    return ExternalSourceSignal(
        tenant_id=tenant,
        source_id=source,
        strategy_revision=revision,
        symbol=symbol,
        timeframe=timeframe,
        bar_close_time=bar_close,
        timestamp=timestamp,
        max_lag=max_lag,
        target=target,
        risk_action=risk_action,
        event_id=event_id,
    )
