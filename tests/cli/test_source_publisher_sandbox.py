"""The source publisher command remains an explicit local sandbox harness."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from custos.cli.subcommands.source_publisher_sandbox import _build_parser, build_settings


def args(tmp_path: Path, *, url: str = "nats://127.0.0.1:4222"):
    digest_file = tmp_path / "token.sha256"
    digest_file.write_text(hashlib.sha256(b"synthetic-token-0123456789abcdef").hexdigest())
    digest_file.chmod(0o600)
    return _build_parser().parse_args(
        [
            "--tenant-id",
            "tenant-a",
            "--source-id",
            "fixture-source",
            "--allow-symbol",
            "ETH-USDT-SWAP",
            "--token-digest-file",
            str(digest_file),
            "--outbox",
            str(tmp_path / "source.sqlite"),
            "--nats-url",
            url,
            "--listen-port",
            "0",
        ]
    )


def test_command_requires_explicit_scope_and_private_digest(tmp_path: Path) -> None:
    settings = build_settings(args(tmp_path))
    assert settings.ingress.tenant_id == "tenant-a"
    assert settings.ingress.source_id == "fixture-source"
    assert settings.ingress.allowed_symbols == frozenset({"ETH-USDT-SWAP"})
    assert settings.nats_url == "nats://127.0.0.1:4222"
    assert settings.listen_port == 0
    assert (
        settings.ingress.signal_token_sha256
        == hashlib.sha256(b"synthetic-token-0123456789abcdef").digest()
    )


def test_command_rejects_nonloopback_broker_and_world_readable_digest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        build_settings(args(tmp_path, url="nats://broker.example:4222"))
    parsed = args(tmp_path)
    parsed.token_digest_file.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        build_settings(parsed)


def test_command_rejects_broad_source_scope_and_invalid_digest(tmp_path: Path) -> None:
    parsed = args(tmp_path)
    parsed.source_id = "other.>"
    with pytest.raises(ValueError, match="identity"):
        build_settings(parsed)
    parsed = args(tmp_path)
    parsed.token_digest_file.write_text("not-a-digest")
    with pytest.raises(ValueError, match="digest"):
        build_settings(parsed)
