"""Bounded HTTP ingress behind an authenticated, HMAC-signing local edge.

This module intentionally does not register a CLI command yet. The daemon must
first provide a scope derived from its verified deployment and an activated
runtime; operator-supplied scope flags are not sufficient authorization.
TradingView cannot send the required HMAC header directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_signal_contract import (
    ExternalSignalContractError,
    parse_custos_v1_alert,
)
from custos.core.external_signal_inbox import (
    ExternalSignalInbox,
    ExternalSignalInboxError,
    IntakeReceipt,
)


class WebhookAuthenticationError(ValueError):
    """The sender did not prove possession of this source's ingest secret."""


@dataclass(frozen=True, slots=True)
class WebhookIngressConfig:
    source_id: str
    hmac_key: bytes = field(repr=False)
    authority: EngineLifecycleAuthority
    authorized_leverage: int
    allowed_instruments: frozenset[str]
    max_body_bytes: int = 4096

    def __post_init__(self) -> None:
        if self.authority.trading_mode not in {"sandbox", "testnet"}:
            raise ValueError("live external signal ingress is not admitted")
        if not self.source_id or not self.allowed_instruments:
            raise ValueError("source and instrument scope are required")
        if len(self.hmac_key) < 32:
            raise ValueError("source authentication key is too short")
        if self.authorized_leverage < 1 or self.max_body_bytes < 256:
            raise ValueError("ingress limits are invalid")


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalSignalContractError("JSON has duplicate fields")
        result[key] = value
    return result


def _reject_nonfinite_constant(_: str) -> object:
    raise ExternalSignalContractError("JSON has a nonfinite number")


class WebhookIngress:
    """Authenticate raw bytes, parse in scope, then durably accept an event."""

    def __init__(self, config: WebhookIngressConfig, inbox: ExternalSignalInbox) -> None:
        self.config = config
        self.inbox = inbox

    def accept(self, body: bytes, signature: str | None, *, received_at: datetime) -> IntakeReceipt:
        if len(body) > self.config.max_body_bytes:
            raise ExternalSignalContractError("request body exceeds limit")
        expected = "sha256=" + hmac.new(self.config.hmac_key, body, hashlib.sha256).hexdigest()
        if (
            not isinstance(signature, str)
            or len(signature) != len(expected)
            or not signature.isascii()
            or not hmac.compare_digest(signature, expected)
        ):
            raise WebhookAuthenticationError("external signal authentication failed")
        try:
            payload = json.loads(
                body.decode("utf-8"),
                parse_float=Decimal,
                parse_constant=_reject_nonfinite_constant,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalSignalContractError("request body must be UTF-8 JSON") from error
        if not isinstance(payload, dict) or payload.get("sourceId") != self.config.source_id:
            raise ExternalSignalContractError("sourceId is not authorized")
        alert = parse_custos_v1_alert(
            payload,
            now=received_at,
            allowed_instruments=self.config.allowed_instruments,
            expected_deployment_instance_id=self.config.authority.deployment_instance_id,
            authorized_leverage=self.config.authorized_leverage,
        )
        return self.inbox.accept(alert, authority=self.config.authority, received_at=received_at)


def create_webhook_server(
    address: tuple[str, int],
    ingress: WebhookIngress,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ThreadingHTTPServer:
    """Create a loopback-only server; external TLS termination is separate."""
    if address[0] != "127.0.0.1":
        raise ValueError("external signal HTTP must bind to IPv4 loopback")
    now = clock or (lambda: datetime.now(UTC))

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, result: dict[str, object]) -> None:
            body = json.dumps(result, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 — HTTP handler API
            if self.path != "/v1/external-signals":
                self._reply(404, {"error": "not_found"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self._reply(415, {"error": "unsupported_media_type"})
                return
            declared = self.headers.get("Content-Length")
            if declared is None or not declared.isascii() or not declared.isdecimal():
                self.close_connection = True
                self._reply(411, {"error": "content_length_required"})
                return
            if len(declared) > 10:
                self.close_connection = True
                self._reply(413, {"error": "body_size_invalid"})
                return
            length = int(declared)
            if length < 1 or length > ingress.config.max_body_bytes:
                self.close_connection = True
                self._reply(413, {"error": "body_size_invalid"})
                return
            self.connection.settimeout(2)
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ExternalSignalContractError("request body is incomplete")
                receipt = ingress.accept(
                    body, self.headers.get("X-Custos-Signature"), received_at=now()
                )
            except WebhookAuthenticationError:
                self._reply(401, {"error": "authentication_failed"})
                return
            except ExternalSignalContractError:
                self._reply(400, {"error": "contract_rejected"})
                return
            except ExternalSignalInboxError:
                self._reply(409, {"error": "event_rejected"})
                return
            except (OSError, TimeoutError, sqlite3.Error):
                self.close_connection = True
                self._reply(503, {"error": "intake_unavailable"})
                return
            self._reply(
                202 if receipt.status == "accepted" else 200,
                {"eventId": receipt.event_id, "status": receipt.status},
            )

        def log_message(self, format: str, *args: object) -> None:
            # Default HTTP logging may print arbitrary paths or headers.
            return

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server
