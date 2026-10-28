"""Post a synthetic target-neutral alert to a local source receiver."""

from __future__ import annotations

import argparse
import http.client
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from custos.core.external_source_signal import (
    make_source_event_id,
    parse_external_source_signal,
)
from custos.engines.nautilus.external_source_authorization import read_private_source_file

_PATH = "/v1/external-source-signals"


@dataclass(frozen=True, slots=True)
class SourceSenderSettings:
    port: int
    source_id: str
    strategy_revision: str
    symbol: str
    bar_close_time: datetime
    sent_at: datetime
    target: str
    risk_action: str | None
    max_lag: int
    event_id: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceSendReceipt:
    status_code: int
    event_id: str
    status: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send one synthetic local source signal.")
    parser.add_argument("--url", default="http://127.0.0.1:8765" + _PATH)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--strategy-revision", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--bar-close", required=True)
    parser.add_argument("--target", choices=("LONG", "SHORT", "FLAT"), required=True)
    parser.add_argument("--risk-action", choices=("FLATTEN",))
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--max-lag", type=int, default=60)
    return parser


def build_settings(
    args: argparse.Namespace, *, now: datetime | None = None
) -> SourceSenderSettings:
    parsed = urlparse(args.url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("source sender URL is invalid") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != _PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source sender requires an IPv4 loopback URL")
    try:
        bar_close = datetime.fromisoformat(args.bar_close.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("source bar close is not an ISO timestamp") from error
    if bar_close.tzinfo is None or bar_close.utcoffset() != timedelta(0):
        raise ValueError("source bar close must be UTC")
    sent_at = now or datetime.now(UTC)
    if sent_at.tzinfo is None or sent_at.utcoffset() != timedelta(0):
        raise ValueError("source sender clock must be UTC")
    token_bytes = read_private_source_file(args.token_file, max_bytes=512)
    try:
        token = token_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("source token must be ASCII") from error
    if not 32 <= len(token) <= 512 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("source token format is invalid")
    event_id = make_source_event_id(
        source_id=args.source_id,
        strategy_revision=args.strategy_revision,
        symbol=args.symbol,
        timeframe="30",
        bar_close_time=bar_close,
        target=args.target,
        risk_action=args.risk_action,
    )
    payload = {
        "schemaVersion": 1,
        "sourceId": args.source_id,
        "strategyRevision": args.strategy_revision,
        "symbol": args.symbol,
        "timeframe": "30",
        "barCloseTime": bar_close.isoformat(),
        "timestamp": sent_at.isoformat(),
        "maxLag": args.max_lag,
        "target": args.target,
        "eventId": event_id,
        "signalToken": token,
    }
    if args.risk_action is not None:
        payload["riskAction"] = args.risk_action
    parse_external_source_signal(
        payload,
        tenant_id="synthetic-local",
        expected_source_id=args.source_id,
        allowed_symbols=frozenset({args.symbol}),
        now=sent_at,
    )
    return SourceSenderSettings(
        port=port,
        source_id=args.source_id,
        strategy_revision=args.strategy_revision,
        symbol=args.symbol,
        bar_close_time=bar_close,
        sent_at=sent_at,
        target=args.target,
        risk_action=args.risk_action,
        max_lag=args.max_lag,
        event_id=event_id,
        token=token,
    )


def send(settings: SourceSenderSettings) -> SourceSendReceipt:
    payload = {
        "schemaVersion": 1,
        "sourceId": settings.source_id,
        "strategyRevision": settings.strategy_revision,
        "symbol": settings.symbol,
        "timeframe": "30",
        "barCloseTime": settings.bar_close_time.isoformat(),
        "timestamp": settings.sent_at.isoformat(),
        "maxLag": settings.max_lag,
        "target": settings.target,
        "eventId": settings.event_id,
        "signalToken": settings.token,
    }
    if settings.risk_action is not None:
        payload["riskAction"] = settings.risk_action
    connection = http.client.HTTPConnection("127.0.0.1", settings.port, timeout=5)
    try:
        connection.request(
            "POST",
            _PATH,
            json.dumps(payload, separators=(",", ":")).encode(),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read(4097)
    finally:
        connection.close()
    if response.status not in {200, 202} or len(body) > 4096:
        raise RuntimeError(f"source receiver returned HTTP {response.status}")
    try:
        receipt = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("source receiver receipt is invalid") from error
    status = "accepted" if response.status == 202 else "duplicate"
    if (
        not isinstance(receipt, dict)
        or receipt.get("eventId") != settings.event_id
        or receipt.get("status") != status
    ):
        raise RuntimeError("source receiver receipt differs from sent event")
    return SourceSendReceipt(response.status, settings.event_id, status)


def main(argv: list[str] | None = None) -> int:
    receipt = send(build_settings(_build_parser().parse_args(argv)))
    print(
        json.dumps(
            {
                "eventId": receipt.event_id,
                "httpStatus": receipt.status_code,
                "status": receipt.status,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
