"""A synthetic source event can be posted without exposing its bearer token."""

import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custos.cli.subcommands.source_signal_sender import _build_parser, build_settings, send
from custos.cli.subcommands.source_webhook import (
    SourceWebhookConfig,
    SourceWebhookIngress,
    create_source_webhook_server,
)
from custos.core.external_source_outbox import ExternalSourceOutbox

NOW = datetime(2026, 10, 28, 12, 0, 15, tzinfo=UTC)
BAR = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TOKEN = "synthetic-only-never-brokered-0123456789"


def args(tmp_path: Path, *, url="http://127.0.0.1:8765/v1/external-source-signals"):
    token = tmp_path / "source-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return _build_parser().parse_args(
        [
            "--url",
            url,
            "--source-id",
            "synthetic-supertrend",
            "--strategy-revision",
            "fixture-v1",
            "--symbol",
            "ETH-USDT",
            "--bar-close",
            BAR.isoformat(),
            "--target",
            "FLAT",
            "--risk-action",
            "FLATTEN",
            "--token-file",
            str(token),
        ]
    )


def test_sender_rejects_remote_endpoint_and_public_token_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        build_settings(
            args(tmp_path, url="https://remote.example/v1/external-source-signals"), now=NOW
        )
    parsed = args(tmp_path)
    parsed.token_file.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        build_settings(parsed, now=NOW)


def test_sender_posts_one_redacted_receipt_to_local_ingress(tmp_path: Path) -> None:
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    ingress = SourceWebhookIngress(
        SourceWebhookConfig(
            tenant_id="tenant-a",
            source_id="synthetic-supertrend",
            allowed_symbols=frozenset({"ETH-USDT"}),
            signal_token_sha256=hashlib.sha256(TOKEN.encode()).digest(),
        ),
        outbox,
    )
    server = create_source_webhook_server(("127.0.0.1", 0), ingress, clock=lambda: NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        parsed = args(
            tmp_path, url=f"http://127.0.0.1:{server.server_port}/v1/external-source-signals"
        )
        settings = build_settings(parsed, now=NOW)
        first = send(settings)
        second = send(settings)
        assert first.status_code == 202 and second.status_code == 200
        assert first.event_id == second.event_id
        assert TOKEN not in repr(first)
        assert TOKEN not in repr(settings)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
