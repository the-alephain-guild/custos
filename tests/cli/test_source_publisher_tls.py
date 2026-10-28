"""A private, target-neutral publisher binds its own source JWT and TLS trust."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custos.cli.subcommands.source_publisher_tls import (
    _AuthorizedPublicationWorker,
    _build_parser,
    build_settings,
)
from custos.engines.nautilus.external_source_publisher_nats import SourcePublisherNatsProfile


def files(tmp_path: Path):
    credentials = tmp_path / "source-publisher.creds"
    credentials.write_bytes(b"synthetic-publisher-jwt-and-seed")
    credentials.chmod(0o600)
    authorization = tmp_path / "publisher-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "tenantId": "tenant-a",
                "sourceId": "synthetic-supertrend",
                "allowedSymbols": ["ETH-USDT"],
                "signalTokenSha256": hashlib.sha256(
                    b"synthetic-token-0123456789abcdef"
                ).hexdigest(),
                "natsCredentialsSha256": hashlib.sha256(credentials.read_bytes()).hexdigest(),
            }
        )
    )
    authorization.chmod(0o600)
    return authorization, credentials


def args(tmp_path: Path):
    authorization, credentials = files(tmp_path)
    return _build_parser().parse_args(
        [
            "--authorization",
            str(authorization),
            "--nats-credentials",
            str(credentials),
            "--outbox",
            str(tmp_path / "source.sqlite"),
            "--nats-url",
            "nats://broker.example:4222",
            "--nats-ca",
            str(tmp_path / "ca.pem"),
            "--nats-server-name",
            "broker.example",
            "--listen-port",
            "0",
        ]
    )


def test_settings_bind_private_source_scope_and_credential_digest(tmp_path: Path) -> None:
    settings = build_settings(args(tmp_path))
    assert settings.ingress.tenant_id == "tenant-a"
    assert settings.ingress.source_id == "synthetic-supertrend"
    assert settings.ingress.allowed_symbols == frozenset({"ETH-USDT"})
    assert (
        settings.profile.credentials_sha256
        == hashlib.sha256(settings.profile.credentials_path.read_bytes()).hexdigest()
    )
    assert settings.listen_port == 0


def test_settings_reject_wide_identity_or_changed_private_files(tmp_path: Path) -> None:
    parsed = args(tmp_path)
    parsed.authorization.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        build_settings(parsed)
    parsed.authorization.chmod(0o600)
    document = json.loads(parsed.authorization.read_text())
    document["sourceId"] = "other.>"
    parsed.authorization.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="identity"):
        build_settings(parsed)
    document["sourceId"] = "synthetic-supertrend"
    parsed.authorization.write_text(json.dumps(document))
    parsed.nats_credentials.write_bytes(b"rotated-without-authorizing")
    with pytest.raises(ValueError, match="credential"):
        build_settings(parsed)


@pytest.mark.asyncio
async def test_profile_uses_separate_tls_jwt_and_rechecks_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    settings = build_settings(args(tmp_path))
    seen = []

    async def connect(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "custos.engines.nautilus.external_source_publisher_nats.nats.connect", connect
    )
    monkeypatch.setattr(
        "custos.engines.nautilus.external_source_publisher_nats.ssl.create_default_context",
        lambda *, cafile: SimpleNamespace(check_hostname=False, verify_mode=None, cafile=cafile),
    )
    assert await settings.profile.connect() is not None
    assert seen[0]["servers"] == ["nats://broker.example:4222"]
    assert seen[0]["tls_hostname"] == "broker.example"
    assert seen[0]["allow_reconnect"] is False
    assert seen[0]["user_credentials"] is not None
    assert seen[0]["tls"].check_hostname is True
    settings.profile.credentials_path.write_bytes(b"rotated-after-settings")
    with pytest.raises(ValueError, match="credential"):
        await settings.profile.connect()
    assert len(seen) == 1


def test_publisher_profile_rejects_url_credentials_and_non_tls_name(tmp_path: Path) -> None:
    settings = build_settings(args(tmp_path))
    with pytest.raises(ValueError, match="URL"):
        SourcePublisherNatsProfile(
            tenant_id=settings.ingress.tenant_id,
            source_id=settings.ingress.source_id,
            credentials_path=settings.profile.credentials_path,
            credentials_sha256=settings.profile.credentials_sha256,
            url="nats://user:secret@broker.example:4222",
            ca_path=settings.profile.ca_path,
            server_name="broker.example",
        )


@pytest.mark.asyncio
async def test_changed_authorization_stops_publication_before_broker_call(tmp_path: Path) -> None:
    settings = build_settings(args(tmp_path))

    class BrokerWorker:
        calls = 0

        async def publish_once(self, *, at):
            self.calls += 1
            return False

    broker = BrokerWorker()
    worker = _AuthorizedPublicationWorker(settings, broker)
    assert await worker.publish_once(at=None) is False
    settings.authorization_path.write_text(settings.authorization_path.read_text() + " ")
    with pytest.raises(ValueError, match="changed"):
        await worker.publish_once(at=None)
    assert broker.calls == 1
