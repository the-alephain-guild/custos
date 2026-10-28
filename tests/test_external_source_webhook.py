"""A source webhook authenticates once and durably stores a target-neutral event."""

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

import pytest

from custos.cli.subcommands.source_webhook import (
    SourceWebhookAuthenticationError,
    SourceWebhookConfig,
    SourceWebhookIngress,
    create_source_webhook_server,
)
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_signal import make_source_event_id

BAR_CLOSE = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 28, 12, 0, 15, tzinfo=UTC)
TOKEN = "synthetic-dedicated-source-token-0123456789"
KEY = b"synthetic-source-hmac-key-never-use-in-production"


def body(**changes: object) -> bytes:
    payload = {
        "schemaVersion": 1,
        "sourceId": "fixture-source",
        "strategyRevision": "supertrend-source-fill-v1",
        "symbol": "ETH-USDT-SWAP",
        "timeframe": "30",
        "barCloseTime": "2026-10-28T12:00:00Z",
        "timestamp": "2026-10-28T12:00:00Z",
        "maxLag": 60,
        "target": "FLAT",
        "riskAction": "FLATTEN",
        "eventId": make_source_event_id(
            source_id="fixture-source",
            strategy_revision="supertrend-source-fill-v1",
            symbol="ETH-USDT-SWAP",
            timeframe="30",
            bar_close_time=BAR_CLOSE,
            target="FLAT",
            risk_action="FLATTEN",
        ),
        "signalToken": TOKEN,
    }
    payload.update(changes)
    return json.dumps(payload, sort_keys=True).encode()


def ingress(tmp_path: Path, *, hmac_key: bytes | None = None) -> SourceWebhookIngress:
    config = SourceWebhookConfig(
        tenant_id="tenant-a",
        source_id="fixture-source",
        allowed_symbols=frozenset({"ETH-USDT-SWAP"}),
        signal_token_sha256=hashlib.sha256(TOKEN.encode()).digest(),
        hmac_key=hmac_key,
    )
    return SourceWebhookIngress(config, ExternalSourceOutbox(tmp_path / "source.sqlite"))


def test_token_authenticated_event_is_durable_idempotent_and_redacted(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    first = receiver.accept(body(), None, received_at=NOW)
    duplicate = receiver.accept(body(), None, received_at=NOW)
    assert first.status == "accepted"
    assert duplicate.status == "duplicate"
    claim = receiver.outbox.claim_next("tenant-a", at=NOW)
    assert claim is not None
    published = json.loads(claim.payload)
    assert published["tenantId"] == "tenant-a"
    assert published["target"] == "FLAT"
    assert published["riskAction"] == "FLATTEN"
    assert "signalToken" not in published
    assert "deploymentInstanceId" not in published
    assert "requestedLeverage" not in published
    assert TOKEN.encode() not in (tmp_path / "source.sqlite").read_bytes()


def test_bad_authentication_and_contract_cannot_store(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    with pytest.raises(SourceWebhookAuthenticationError):
        receiver.accept(body(signalToken="wrong"), None, received_at=NOW)
    with pytest.raises(SourceWebhookAuthenticationError):
        receiver.accept(body(), "sha256=" + "0" * 64, received_at=NOW)
    with pytest.raises(SourceWebhookAuthenticationError):
        receiver.accept(body(), b"sha256=" + b"0" * 64, received_at=NOW)
    with pytest.raises(ValueError):
        receiver.accept(body(sourceId="wrong-source"), None, received_at=NOW)
    with pytest.raises(ValueError):
        receiver.accept(body(deploymentInstanceId="not-a-source-field"), None, received_at=NOW)
    assert receiver.outbox.claim_next("tenant-a", at=NOW) is None


def test_signed_authentication_never_falls_back_to_a_valid_token(tmp_path: Path) -> None:
    receiver = ingress(tmp_path, hmac_key=KEY)
    with pytest.raises(SourceWebhookAuthenticationError):
        receiver.accept(body(), "sha256=" + "0" * 64, received_at=NOW)
    data = body()
    signature = "sha256=" + hmac.new(KEY, data, hashlib.sha256).hexdigest()
    assert receiver.accept(data, signature, received_at=NOW).status == "accepted"


def test_http_post_acknowledges_only_after_storage_and_bounds_body(tmp_path: Path) -> None:
    receiver = ingress(tmp_path)
    server = create_source_webhook_server(("127.0.0.1", 0), receiver, clock=lambda: NOW)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        data = body()
        client.request(
            "POST",
            "/v1/external-source-signals",
            body=data,
            headers={"Content-Type": "application/json"},
        )
        accepted = client.getresponse()
        assert accepted.status == 202
        assert json.loads(accepted.read())["status"] == "accepted"
        client.request(
            "POST",
            "/v1/external-source-signals",
            body=data,
            headers={"Content-Type": "application/json"},
        )
        duplicate = client.getresponse()
        assert duplicate.status == 200
        assert json.loads(duplicate.read())["status"] == "duplicate"
        with patch.object(receiver.outbox, "accept", side_effect=sqlite3.OperationalError("full")):
            client.request(
                "POST",
                "/v1/external-source-signals",
                body=data,
                headers={"Content-Type": "application/json"},
            )
            unavailable = client.getresponse()
            assert unavailable.status == 503
            assert TOKEN.encode() not in unavailable.read()
        client.request(
            "POST",
            "/v1/external-source-signals",
            body=b"x" * 5000,
            headers={"Content-Type": "application/json"},
        )
        oversized = client.getresponse()
        assert oversized.status == 413
        oversized.read()
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_config_requires_authentication_and_loopback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="authentication"):
        SourceWebhookConfig("tenant-a", "fixture-source", frozenset({"ETH-USDT-SWAP"}))
    with pytest.raises(ValueError, match="loopback"):
        create_source_webhook_server(("0.0.0.0", 0), ingress(tmp_path))
