"""The HTTP boundary authenticates before storing an external signal."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from custos.cli.subcommands.webhook import (
    WebhookAuthenticationError,
    WebhookIngress,
    WebhookIngressConfig,
    WebhookRuntimeUnavailable,
    create_webhook_server,
)
from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_signal_contract import make_event_id
from custos.core.external_signal_inbox import ExternalSignalInbox
from custos.engines.nautilus.external_signal import to_toolkit_signal
from custos.engines.nautilus.host import ExternalSignalRuntimeScope

INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 27, 12, 0, 15, tzinfo=UTC)
KEY = b"local-fixture-key-never-commit-real-keys"
AUTHORITY = EngineLifecycleAuthority(
    deployment_instance_id=INSTANCE_ID,
    deployment_spec_id=UUID("20000000-0000-4000-8000-000000000001"),
    deployment_spec_digest="a" * 64,
    generation=1,
    trading_mode="testnet",
)


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
        "orderType": "market",
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


def ingress(
    tmp_path: Path,
    *,
    trading_mode: str = "testnet",
    scope_provider=None,
) -> WebhookIngress:
    authority = EngineLifecycleAuthority(
        deployment_instance_id=AUTHORITY.deployment_instance_id,
        deployment_spec_id=AUTHORITY.deployment_spec_id,
        deployment_spec_digest=AUTHORITY.deployment_spec_digest,
        generation=AUTHORITY.generation,
        trading_mode=trading_mode,
    )
    config = WebhookIngressConfig(
        source_id="independent-demo",
        hmac_key=KEY,
        authority=authority,
        authorized_leverage=3,
        allowed_instruments=frozenset({"ETH-USDT-SWAP"}),
    )
    return WebhookIngress(
        config,
        ExternalSignalInbox(tmp_path / "inbox.sqlite"),
        scope_provider=scope_provider
        or (
            lambda: ExternalSignalRuntimeScope(
                authority=authority,
                allowed_instruments=config.allowed_instruments,
                authorized_leverage=config.authorized_leverage,
            )
        ),
    )


def test_valid_signed_body_is_durable_before_success(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    receipt = receiver.accept(body(), signature(body()), received_at=NOW)
    assert receipt.status == "accepted"
    assert len(ExternalSignalInbox(tmp_path / "inbox.sqlite").pending(AUTHORITY, now=NOW)) == 1
    assert b"demo-secret-not-an-authenticator" not in (tmp_path / "inbox.sqlite").read_bytes()


def test_signed_intake_reaches_bounded_non_delivering_toolkit_intent(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    receiver.accept(body(), signature(body()), received_at=NOW)
    pending = receiver.inbox.pending(AUTHORITY, now=NOW)

    assert len(pending) == 1
    scope = ExternalSignalRuntimeScope(
        authority=AUTHORITY,
        allowed_instruments=frozenset({"ETH-USDT-SWAP"}),
        authorized_leverage=3,
    )
    signal = to_toolkit_signal(
        pending[0],
        scope,
        max_order_notional=Decimal("40"),
        limit_currency="USDT",
    )

    assert signal.amount == Decimal("30.75")
    assert signal.signal_token is None
    assert signal.metadata["external_event_id"] == pending[0].event_id
    assert len(receiver.inbox.pending(AUTHORITY, now=NOW)) == 1


def test_wrong_signature_and_live_mode_are_refused(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    with pytest.raises(WebhookAuthenticationError):
        receiver.accept(body(), "sha256=" + "0" * 64, received_at=NOW)
    assert receiver.inbox.pending(AUTHORITY, now=NOW) == []
    with pytest.raises(ValueError, match="live"):
        ingress(tmp_path, trading_mode="live")


def test_signed_body_is_refused_after_active_generation_changes(tmp_path: Path) -> None:
    new_generation = ExternalSignalRuntimeScope(
        authority=replace(AUTHORITY, generation=AUTHORITY.generation + 1),
        allowed_instruments=frozenset({"ETH-USDT-SWAP"}),
        authorized_leverage=3,
    )
    receiver = ingress(tmp_path, scope_provider=lambda: new_generation)

    with pytest.raises(WebhookRuntimeUnavailable, match="runtime scope"):
        receiver.accept(body(), signature(body()), received_at=NOW)
    assert receiver.inbox.pending(AUTHORITY, now=NOW) == []


def test_unavailable_runtime_returns_503_without_accepting_event(tmp_path: Path) -> None:
    def unavailable_scope():
        raise RuntimeError("node stopped")

    receiver = ingress(tmp_path, scope_provider=unavailable_scope)
    server = create_webhook_server(("127.0.0.1", 0), receiver, clock=lambda: NOW)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        data = body()
        connection.request(
            "POST",
            "/v1/external-signals",
            body=data,
            headers={"Content-Type": "application/json", "X-Custos-Signature": signature(data)},
        )
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read()) == {"error": "runtime_unavailable"}
        connection.close()
        assert receiver.inbox.pending(AUTHORITY, now=NOW) == []
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


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
        assert len(receiver.inbox.pending(AUTHORITY, now=NOW)) == 1

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
