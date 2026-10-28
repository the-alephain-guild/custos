"""Loopback HTTP intake for a separately authenticated source publisher."""

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

from custos.core.external_source_outbox import (
    ExternalSourceOutbox,
    ExternalSourceOutboxError,
    SourceIntakeReceipt,
)
from custos.core.external_source_signal import (
    ExternalSourceSignalError,
    parse_external_source_signal,
)


class SourceWebhookAuthenticationError(ValueError):
    """The request did not prove possession of this source's secret."""


@dataclass(frozen=True, slots=True)
class SourceWebhookConfig:
    tenant_id: str
    source_id: str
    allowed_symbols: frozenset[str]
    signal_token_sha256: bytes | None = field(default=None, repr=False)
    hmac_key: bytes | None = field(default=None, repr=False)
    max_body_bytes: int = 4096

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.source_id or not self.allowed_symbols:
            raise ValueError("source scope is required")
        if self.signal_token_sha256 is None and self.hmac_key is None:
            raise ValueError("source authentication is required")
        if self.signal_token_sha256 is not None and (
            not isinstance(self.signal_token_sha256, bytes)
            or len(self.signal_token_sha256) != hashlib.sha256().digest_size
        ):
            raise ValueError("source token digest is invalid")
        if self.hmac_key is not None and (
            not isinstance(self.hmac_key, bytes) or len(self.hmac_key) < 32
        ):
            raise ValueError("source HMAC key is invalid")
        if not 256 <= self.max_body_bytes <= 65536:
            raise ValueError("source body size limit is invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalSourceSignalError("source JSON has duplicate fields")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise ExternalSourceSignalError("source JSON has a nonfinite number")


class SourceWebhookIngress:
    """Authenticate raw ingress before accepting one deployment-free source fact."""

    def __init__(self, config: SourceWebhookConfig, outbox: ExternalSourceOutbox) -> None:
        self.config = config
        self.outbox = outbox

    def accept(
        self, body: bytes, signature: str | None, *, received_at: datetime
    ) -> SourceIntakeReceipt:
        if not isinstance(body, bytes) or not 0 < len(body) <= self.config.max_body_bytes:
            raise ExternalSourceSignalError("source request body exceeds limit")
        if signature is not None:
            key = self.config.hmac_key
            if key is None:
                raise SourceWebhookAuthenticationError("source authentication failed")
            expected = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
            if (
                not isinstance(signature, str)
                or len(signature) != len(expected)
                or not signature.isascii()
                or not hmac.compare_digest(signature, expected)
            ):
                raise SourceWebhookAuthenticationError("source authentication failed")
        elif self.config.signal_token_sha256 is None:
            raise SourceWebhookAuthenticationError("source authentication failed")
        try:
            payload = json.loads(
                body.decode("utf-8"),
                parse_float=Decimal,
                parse_constant=_reject_nonfinite,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalSourceSignalError("source request must be UTF-8 JSON") from error
        if signature is None:
            token = payload.get("signalToken") if isinstance(payload, dict) else None
            digest = self.config.signal_token_sha256
            if (
                digest is None
                or not isinstance(token, str)
                or not token.isascii()
                or not 32 <= len(token) <= 512
                or not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), digest)
            ):
                raise SourceWebhookAuthenticationError("source authentication failed")
        event = parse_external_source_signal(
            payload,
            tenant_id=self.config.tenant_id,
            expected_source_id=self.config.source_id,
            allowed_symbols=self.config.allowed_symbols,
            now=received_at,
        )
        return self.outbox.accept(event, received_at=received_at)


def create_source_webhook_server(
    address: tuple[str, int],
    ingress: SourceWebhookIngress,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ThreadingHTTPServer:
    """Bind only to loopback; a source-restricted TLS edge is separate."""
    if address[0] != "127.0.0.1":
        raise ValueError("source HTTP must bind to IPv4 loopback")
    now = clock or (lambda: datetime.now(UTC))

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, result: dict[str, object]) -> None:
            body = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 — HTTP handler API
            if self.path != "/v1/external-source-signals":
                self._reply(404, {"error": "not_found"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self._reply(415, {"error": "unsupported_media_type"})
                return
            declared = self.headers.get("Content-Length")
            if (
                self.headers.get("Transfer-Encoding")
                or declared is None
                or not declared.isdecimal()
            ):
                self.close_connection = True
                self._reply(411, {"error": "content_length_required"})
                return
            if len(declared) > 10 or not 0 < int(declared) <= ingress.config.max_body_bytes:
                self.close_connection = True
                self._reply(413, {"error": "body_size_invalid"})
                return
            self.connection.settimeout(2)
            try:
                body = self.rfile.read(int(declared))
                if len(body) != int(declared):
                    raise ExternalSourceSignalError("source request body is incomplete")
                receipt = ingress.accept(
                    body, self.headers.get("X-Custos-Signature"), received_at=now()
                )
            except SourceWebhookAuthenticationError:
                self._reply(401, {"error": "authentication_failed"})
                return
            except ExternalSourceSignalError:
                self._reply(400, {"error": "contract_rejected"})
                return
            except ExternalSourceOutboxError:
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
            return

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server
