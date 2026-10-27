"""The HTTP boundary authenticates before storing an external signal."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from custos.cli.subcommands.webhook import (
    WebhookAuthenticationError,
    WebhookIngress,
    WebhookIngressConfig,
    create_webhook_server,
)
from custos.core.external_signal_contract import make_event_id
from custos.core.external_signal_inbox import ExternalSignalInbox

INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
KEY = b"local-fixture-key-never-commit-real-keys"


def body() -> bytes:
    bar_close = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    payload = {
        "action": "ENTER_LONG",
        "instrument": "ETH-USDT-SWAP",
        "signalToken": "demo-secret-not-an-authenticator",
        "timestamp": "2026-10-27T12:00:00Z",
        "maxLag": "60",
        "investmentType": "margin",
        "amount": "10.25",
        "schemaVersion": 1,
        "sourceId": "independent-demo",
        "strategyRevision": "supertrend-v0.5",
        "deploymentInstanceId": str(INSTANCE_ID),
        "timeframe": "30",
        "barCloseTime": "2026-10-27T12:00:00Z",
        "requestedLeverage": 3,
        "eventId": make_event_id(
            source_id="independent-demo",
            strategy_revision="supertrend-v0.5",
            instrument="ETH-USDT-SWAP",
            timeframe="30",
            bar_close_time=bar_close,
            action="ENTER_LONG",
            risk_action=None,
        ),
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def signature(data: bytes) -> str:
    return "sha256=" + hmac.new(KEY, data, hashlib.sha256).hexdigest()


def ingress(tmp_path: Path, *, trading_mode: str = "testnet") -> WebhookIngress:
    return WebhookIngress(
        WebhookIngressConfig(
            source_id="independent-demo",
            hmac_key=KEY,
            deployment_instance_id=INSTANCE_ID,
            authorized_leverage=3,
            allowed_instruments=frozenset({"ETH-USDT-SWAP"}),
            trading_mode=trading_mode,
        ),
        ExternalSignalInbox(tmp_path / "inbox.sqlite"),
    )


def test_valid_signed_body_is_durable_before_success(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    receipt = receiver.accept(body(), signature(body()), received_at=NOW)
    assert receipt.status == "accepted"
    assert len(ExternalSignalInbox(tmp_path / "inbox.sqlite").pending(INSTANCE_ID, now=NOW)) == 1
    assert b"demo-secret-not-an-authenticator" not in (tmp_path / "inbox.sqlite").read_bytes()


def test_wrong_signature_and_live_mode_are_refused(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    with pytest.raises(WebhookAuthenticationError):
        receiver.accept(body(), "sha256=" + "0" * 64, received_at=NOW)
    assert receiver.inbox.pending(INSTANCE_ID, now=NOW) == []
    with pytest.raises(ValueError, match="live"):
        ingress(tmp_path, trading_mode="live")


def test_actual_http_post_has_bounded_body_and_idempotent_receipt(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    server = create_webhook_server(("127.0.0.1", 0), receiver, clock=lambda: NOW)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        data = body()
        headers = {
            "Content-Type": "application/json",
            "X-Custos-Signature": signature(data),
        }
        connection.request("POST", "/v1/external-signals", body=data, headers=headers)
        response = connection.getresponse()
        assert response.status == 202
        assert json.loads(response.read())["status"] == "accepted"
        connection.request("POST", "/v1/external-signals", body=data, headers=headers)
        duplicate = connection.getresponse()
        assert duplicate.status == 200
        assert json.loads(duplicate.read())["status"] == "duplicate"
        assert len(receiver.inbox.pending(INSTANCE_ID, now=NOW)) == 1

        with patch.object(
            receiver.inbox, "accept", side_effect=sqlite3.OperationalError("disk full")
        ):
            connection.request("POST", "/v1/external-signals", body=data, headers=headers)
            unavailable = connection.getresponse()
            assert unavailable.status == 503
            assert b"demo-secret" not in unavailable.read()

        oversize = b"x" * 5_000
        connection.request(
            "POST",
            "/v1/external-signals",
            body=oversize,
            headers={"Content-Type": "application/json", "X-Custos-Signature": signature(oversize)},
        )
        refused = connection.getresponse()
        assert refused.status == 413
        assert b"demo-secret" not in refused.read()
        connection.close()

        malformed = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        malformed.request(
            "POST",
            "/v1/external-signals",
            body=data,
            headers={**headers, "Content-Length": "9" * 10_000},
        )
        excessive_length = malformed.getresponse()
        assert excessive_length.status == 413
        excessive_length.read()
        malformed.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
