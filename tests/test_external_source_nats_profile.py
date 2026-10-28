"""The subscriber uses separate, byte-bound NATS credentials and TLS."""

from __future__ import annotations

from pathlib import Path

import nats
import pytest
from nats.aio.client import RawCredentials

from custos.engines.nautilus.external_source_authorization import (
    ExternalSourceAuthorizationError,
    load_source_subscription,
)
from custos.engines.nautilus.external_source_nats import ExternalSourceNatsProfile
from tests.test_external_source_authorization import RUNNER, local_files, scope


def grant(tmp_path: Path):
    path, credentials = local_files(tmp_path)
    authorized = load_source_subscription(
        path,
        credentials_path=credentials,
        expected_tenant_id="tenant-a",
        expected_runner_id=RUNNER,
        active_scope=scope(),
    )
    return authorized, credentials


@pytest.mark.asyncio
async def test_connect_uses_exact_private_creds_and_verified_tls(
    tmp_path: Path, monkeypatch
) -> None:
    authorized, _ = grant(tmp_path)
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic-ca")
    calls = []

    class Context:
        check_hostname = True

    def context(*, cafile):
        assert cafile == str(ca)
        return Context()

    async def connect(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("ssl.create_default_context", context)
    monkeypatch.setattr(nats, "connect", connect)
    profile = ExternalSourceNatsProfile(
        grant=authorized,
        url="nats://broker.example:4222",
        ca_path=ca,
        server_name="broker.example",
    )
    await profile.connect()
    assert len(calls) == 1
    assert calls[0]["servers"] == ["nats://broker.example:4222"]
    assert calls[0]["tls_hostname"] == "broker.example"
    assert calls[0]["tls"].check_hostname
    assert calls[0]["allow_reconnect"] is False
    assert isinstance(calls[0]["user_credentials"], RawCredentials)
    assert "synthetic-separate-nats-jwt-and-seed" in calls[0]["user_credentials"].data

    secure = ExternalSourceNatsProfile(
        grant=authorized,
        url="tls://broker.example:4222",
        ca_path=ca,
        server_name="broker.example",
    )
    await secure.connect()
    assert calls[1]["servers"] == ["tls://broker.example:4222"]


@pytest.mark.asyncio
async def test_changed_credential_or_url_is_refused_before_connection(
    tmp_path: Path, monkeypatch
) -> None:
    authorized, credentials = grant(tmp_path)

    async def unexpected_connect(**kwargs):
        raise AssertionError("broker connect must not occur")

    monkeypatch.setattr(nats, "connect", unexpected_connect)
    credentials.write_bytes(b"replaced")
    profile = ExternalSourceNatsProfile(
        grant=authorized,
        url="nats://broker.example:4222",
        ca_path=tmp_path / "ca.pem",
        server_name="broker.example",
    )
    with pytest.raises(ExternalSourceAuthorizationError, match="credential"):
        await profile.connect()

    with pytest.raises(ValueError, match="NATS URL"):
        ExternalSourceNatsProfile(
            grant=authorized,
            url="nats://user:secret@broker.example:4222",
            ca_path=tmp_path / "ca.pem",
            server_name="broker.example",
        )
