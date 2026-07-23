"""Real-NATS User JWT resolver revocation acceptance."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import nkeys  # type: ignore[import-untyped]
import pytest

from custos.core.nats_transport import (
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportCredential,
    assert_old_generation_reconnect_denied,
)

_IMAGE = os.environ.get("CUSTOS_NATS_TEST_IMAGE", "nats:2.10-alpine")
_ENABLED = os.environ.get("CUSTOS_RUN_REAL_NATS_REVOCATION") == "1"
_TENANT = "tenant-t7c"
_RUNNER = UUID("77777777-7777-4777-8777-777777777777")
_MODE = "sandbox"
_DOMAIN = "sim"


def _require_local_gate() -> None:
    if not _ENABLED:
        pytest.skip("set CUSTOS_RUN_REAL_NATS_REVOCATION=1 to run the real NATS gate")
    for binary in ("docker", "openssl"):
        if shutil.which(binary) is None:
            pytest.fail(f"{binary} is required by the real NATS revocation gate")
    inspected = subprocess.run(
        ["docker", "image", "inspect", _IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        pytest.fail(
            f"immutable test image {_IMAGE} is unavailable; preload it before running the gate"
        )


def _run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _keypair(prefix: int) -> tuple[bytes, Any, str]:
    seed = nkeys.encode_seed(os.urandom(32), prefix)
    pair = nkeys.from_seed(bytearray(seed))
    return seed, pair, pair.public_key.decode("ascii")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _compact_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def _encode_nats_jwt(
    *,
    signer: Any,
    subject: str,
    issued_at: datetime,
    nats_claims: dict[str, Any],
    expires_at: datetime | None = None,
) -> str:
    """Encode the subset of the official NATS JWT v2 contract used by this gate."""

    issuer = signer.public_key.decode("ascii")
    claims_without_jti: dict[str, Any] = {}
    if expires_at is not None:
        claims_without_jti["exp"] = int(expires_at.timestamp())
    claims_without_jti["iat"] = int(issued_at.timestamp())
    claims_without_jti["iss"] = issuer
    claims_without_jti["sub"] = subject
    digest = hashlib.new("sha512_256", _compact_json(claims_without_jti)).digest()
    jti = base64.b32encode(digest).decode("ascii").rstrip("=")

    payload: dict[str, Any] = {}
    if expires_at is not None:
        payload["exp"] = int(expires_at.timestamp())
    payload["jti"] = jti
    payload["iat"] = int(issued_at.timestamp())
    payload["iss"] = issuer
    payload["sub"] = subject
    payload["nats"] = nats_claims
    header = _b64url(_compact_json({"typ": "JWT", "alg": "ed25519-nkey"}))
    claims = _b64url(_compact_json(payload))
    signing_input = f"{header}.{claims}".encode("ascii")
    return f"{header}.{claims}.{_b64url(signer.sign(signing_input))}"


def _permission_profile() -> dict[str, Any]:
    runner = str(_RUNNER)
    durable = f"custos-control-v1-{_TENANT}-{runner}-{_MODE}"
    stream = "CRUCIBLE_RUNNER_CONTROL_SIM_V1"
    return {
        "schema_version": 1,
        "profile": "crucible.runner-nats-transport.v1",
        "tenant_id": _TENANT,
        "runner_id": runner,
        "trading_mode": _MODE,
        "transport_domain": _DOMAIN,
        "publish_allow": [
            f"crucible.runner.fact.v1.{_TENANT}.{runner}.{_MODE}",
            f"$JS.ACK.{stream}.{durable}.>",
            f"$JS.API.CONSUMER.INFO.{stream}.{durable}",
        ],
        "subscribe_allow": [
            f"custos.runner.control.v1.delivery.{_TENANT}.{runner}.{_MODE}",
            "_INBOX.>",
        ],
        "publish_deny": [
            "$JS.API.STREAM.>",
            "$JS.API.CONSUMER.CREATE.>",
            "$JS.API.CONSUMER.DURABLE.CREATE.>",
            "$JS.API.CONSUMER.DELETE.>",
            "$SYS.>",
        ],
        "subscribe_deny": ["$SYS.>"],
    }


def _durable_config() -> dict[str, Any]:
    runner = str(_RUNNER)
    return {
        "schema_version": 1,
        "transport_domain": _DOMAIN,
        "stream_name": "CRUCIBLE_RUNNER_CONTROL_SIM_V1",
        "durable_name": f"custos-control-v1-{_TENANT}-{runner}-{_MODE}",
        "delivery_subject": (f"custos.runner.control.v1.delivery.{_TENANT}.{runner}.{_MODE}"),
        "filter_subjects": [
            f"crucible.runner.command.v1.{_TENANT}.{runner}.{_MODE}",
            f"crucible.runner.policy.v1.{_TENANT}.{runner}.{_MODE}",
        ],
        "deliver_policy": "all",
        "ack_policy": "explicit",
        "replay_policy": "instant",
        "max_ack_pending": 1,
        "consumer_mode": "push_existing_only",
    }


def _sha256_document(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _transport_credential(
    *,
    user_seed: bytes,
    user_public: str,
    account_pair: Any,
    account_public: str,
    issued_at: datetime,
    expires_at: datetime,
    generation: int,
) -> RunnerNatsTransportCredential:
    permission = _permission_profile()
    durable = _durable_config()
    user_jwt = _encode_nats_jwt(
        signer=account_pair,
        subject=user_public,
        issued_at=issued_at,
        expires_at=expires_at,
        nats_claims={
            "pub": {
                "allow": permission["publish_allow"],
                "deny": permission["publish_deny"],
            },
            "sub": {
                "allow": permission["subscribe_allow"],
                "deny": permission["subscribe_deny"],
            },
            "subs": -1,
            "data": -1,
            "payload": -1,
            "type": "user",
            "version": 2,
        },
    )
    response: dict[str, Any] = {
        "schema_version": 1,
        "authority_coordinate": "crucible.runner-nats-transport.v1",
        "authority_id": str(uuid4()),
        "tenant_id": _TENANT,
        "runner_id": str(_RUNNER),
        "trading_mode": _MODE,
        "transport_domain": _DOMAIN,
        "credential_generation": generation,
        "user_public_key": user_public,
        "user_jwt": user_jwt,
        "user_jwt_sha256": hashlib.sha256(user_jwt.encode()).hexdigest(),
        "issuer_public_key": account_public,
        "signing_key_id": "account-signer-integration",
        "claims_sha256": hashlib.sha256(user_jwt.split(".")[1].encode()).hexdigest(),
        "permission_profile": permission,
        "permission_profile_sha256": _sha256_document(permission),
        "durable_config": durable,
        "durable_config_sha256": _sha256_document(durable),
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "not_before": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "status": "active",
        "operation_id": str(uuid4()),
    }
    response["authority_digest"] = _sha256_document(response)
    return RunnerNatsTransportCredential.from_authority_document(
        response,
        user_seed=user_seed,
        expected_tenant_id=_TENANT,
        expected_runner_id=_RUNNER,
        expected_trading_mode=_MODE,
        expected_issuer_public_key=account_public,
    )


def _account_jwt(
    *,
    operator_pair: Any,
    account_public: str,
    issued_at: datetime,
    revoked_user: str | None = None,
    revoke_at: datetime | None = None,
) -> str:
    nats_claims: dict[str, Any] = {
        "limits": {
            "subs": -1,
            "data": -1,
            "payload": -1,
            "imports": -1,
            "exports": -1,
            "wildcards": True,
            "conn": -1,
            "leaf": -1,
        }
    }
    if revoked_user is not None and revoke_at is not None:
        nats_claims["revocations"] = {
            revoked_user: int(revoke_at.timestamp()),
        }
    nats_claims["type"] = "account"
    nats_claims["version"] = 2
    return _encode_nats_jwt(
        signer=operator_pair,
        subject=account_public,
        issued_at=issued_at,
        nats_claims=nats_claims,
    )


def _server_config(*, account_public: str, account_jwt: str) -> str:
    return (
        'port: 4222\nserver_name: "custos-t7c-real-nats"\n'
        'operator: "/config/operator.jwt"\nresolver: MEMORY\n'
        f'resolver_preload: {{ {account_public}: "{account_jwt}" }}\n'
        'tls { cert_file: "/config/server.crt"; '
        'key_file: "/config/server.key"; timeout: 2 }\n'
    )


def _write_tls_material(root: Path) -> Path:
    openssl_config = root / "openssl.cnf"
    openssl_config.write_text(
        """\
[req]
distinguished_name=dn
x509_extensions=v3_req
prompt=no
[dn]
CN=localhost
[v3_req]
subjectAltName=@alt_names
[alt_names]
DNS.1=localhost
IP.1=127.0.0.1
"""
    )
    certificate = root / "server.crt"
    _run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-keyout",
        str(root / "server.key"),
        "-out",
        str(certificate),
        "-config",
        str(openssl_config),
    )
    return certificate


def _wait_ready(container: str) -> None:
    for _ in range(50):
        logs = _run("docker", "logs", container, check=False)
        if "Server is ready" in f"{logs.stdout}\n{logs.stderr}":
            return
        time.sleep(0.1)
    logs = _run("docker", "logs", container, check=False)
    pytest.fail(f"NATS did not become ready:\n{logs.stdout}\n{logs.stderr}")


async def _exercise_revocation(
    *,
    old_credential: RunnerNatsTransportCredential,
    replacement_credential: RunnerNatsTransportCredential,
    nats_url: str,
    certificate: Path,
    container: str,
    active_config: Path,
    revoked_config: str,
) -> None:
    connection_errors: list[str] = []

    async def record_connection_error(error: Exception) -> None:
        connection_errors.append(f"{type(error).__name__}: {error}")

    old_profile = RunnerNatsTransportConnectionProfile(
        old_credential,
        nats_url,
        certificate,
        "localhost",
        old_credential.issuer_public_key,
    )
    replacement_profile = RunnerNatsTransportConnectionProfile(
        replacement_credential,
        nats_url,
        certificate,
        "localhost",
        replacement_credential.issuer_public_key,
    )
    try:
        old = await old_profile.connect(
            name="custos-t7c-old",
            error_cb=record_connection_error,
            max_reconnect_attempts=1,
        )
    except Exception as error:
        logs = _run("docker", "logs", container, check=False)
        detail = " | ".join(connection_errors) or "no client connection detail"
        raise AssertionError(
            "initial authenticated NATS connection failed: "
            f"{detail}; broker logs: {logs.stdout}{logs.stderr}"
        ) from error
    replacement = await replacement_profile.connect(
        name="custos-t7c-replacement",
        allow_reconnect=False,
        max_reconnect_attempts=0,
    )
    try:
        active_config.write_text(revoked_config)
        _run("docker", "kill", "--signal", "HUP", container)
        await asyncio.wait_for(old_profile.wait_disconnected(), timeout=8)
        await asyncio.sleep(0.3)

        reconnect_profile = RunnerNatsTransportConnectionProfile(
            old_credential,
            nats_url,
            certificate,
            "localhost",
            old_credential.issuer_public_key,
        )
        await assert_old_generation_reconnect_denied(
            reconnect_profile,
            name="custos-t7c-old-reconnect",
            timeout_seconds=3,
        )

        subject = f"crucible.runner.fact.v1.{_TENANT}.{_RUNNER}.{_MODE}"
        replacement_profile.assert_publish_subject(subject)
        await replacement.publish(subject, b"replacement-active")
        await replacement.flush(timeout=2)
        assert replacement.is_connected
    finally:
        await replacement.close()
        if not old.is_closed:
            await old.close()


@pytest.mark.integration
@pytest.mark.docker
def test_real_nats_memory_resolver_revokes_old_user_jwt_and_keeps_replacement(
    tmp_path: Path,
) -> None:
    _require_local_gate()
    now = datetime.now(UTC).replace(microsecond=0)
    operator_seed, operator_pair, operator_public = _keypair(nkeys.PREFIX_BYTE_OPERATOR)
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    old_seed, old_pair, old_public = _keypair(nkeys.PREFIX_BYTE_USER)
    new_seed, new_pair, new_public = _keypair(nkeys.PREFIX_BYTE_USER)
    try:
        operator_jwt = _encode_nats_jwt(
            signer=operator_pair,
            subject=operator_public,
            issued_at=now - timedelta(seconds=20),
            nats_claims={"type": "operator", "version": 2},
        )
        initial_account_jwt = _account_jwt(
            operator_pair=operator_pair,
            account_public=account_public,
            issued_at=now - timedelta(seconds=15),
        )
        revoked_account_jwt = _account_jwt(
            operator_pair=operator_pair,
            account_public=account_public,
            issued_at=now,
            revoked_user=old_public,
            revoke_at=now - timedelta(seconds=1),
        )
        issued_at = now - timedelta(seconds=5)
        expires_at = now + timedelta(hours=1)
        old_credential = _transport_credential(
            user_seed=old_seed,
            user_public=old_public,
            account_pair=account_pair,
            account_public=account_public,
            issued_at=issued_at,
            expires_at=expires_at,
            generation=1,
        )
        replacement_credential = _transport_credential(
            user_seed=new_seed,
            user_public=new_public,
            account_pair=account_pair,
            account_public=account_public,
            issued_at=issued_at,
            expires_at=expires_at,
            generation=2,
        )
    finally:
        for pair in (operator_pair, account_pair, old_pair, new_pair):
            pair.wipe()
        del operator_seed, account_seed

    certificate = _write_tls_material(tmp_path)
    (tmp_path / "operator.jwt").write_text(operator_jwt)
    active_config = tmp_path / "nats.conf"
    active_config.write_text(
        _server_config(
            account_public=account_public,
            account_jwt=initial_account_jwt,
        )
    )
    revoked_config = _server_config(
        account_public=account_public,
        account_jwt=revoked_account_jwt,
    )
    tmp_path.chmod(0o755)
    certificate.chmod(0o644)
    (tmp_path / "operator.jwt").chmod(0o644)
    active_config.chmod(0o644)

    container = f"custos-t7c-{uuid4().hex}"
    started = _run(
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        container,
        "-p",
        "127.0.0.1::4222",
        "-v",
        f"{tmp_path}:/config",
        _IMAGE,
        "-c",
        "/config/nats.conf",
    )
    assert started.stdout.strip()
    try:
        _wait_ready(container)
        port_output = _run("docker", "port", container, "4222/tcp").stdout.strip()
        port = int(port_output.rsplit(":", 1)[1])
        asyncio.run(
            _exercise_revocation(
                old_credential=old_credential,
                replacement_credential=replacement_credential,
                nats_url=f"tls://localhost:{port}",
                certificate=certificate,
                container=container,
                active_config=active_config,
                revoked_config=revoked_config,
            )
        )
        logs = _run("docker", "logs", container, check=False)
        combined = f"{logs.stdout}\n{logs.stderr}"
        assert "Server is ready" in combined
        assert "Reloaded server configuration" in combined
    finally:
        _run("docker", "stop", "-t", "1", container, check=False)
