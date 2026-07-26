"""Real-NATS User JWT resolver revocation acceptance."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import nats
import nkeys  # type: ignore[import-untyped]
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, ReplayPolicy

from custos.core.nats_transport import (
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportCredential,
    assert_old_generation_reconnect_denied,
)
from custos.core.runner_fact import RunnerFactOutbox

_IMAGE = os.environ.get("CUSTOS_NATS_TEST_IMAGE", "nats:2.10-alpine")
_ENABLED = os.environ.get("CUSTOS_RUN_REAL_NATS_REVOCATION") == "1"
_TENANT = "acme"
_RUNNER = UUID("10000000-0000-4000-8000-000000000001")
_MODE = "sandbox"
_DOMAIN = "sim"
_FACT_STREAM = "CRUCIBLE_RUNNER_FACT_V1"
_FACT_STREAM_SUBJECTS = "crucible.runner.fact.v1.*.*.*"
_CONTROL_STREAM = "CRUCIBLE_RUNNER_CONTROL_SIM_V1"
_COMMAND_FIXTURE = (
    Path(__file__).resolve().parents[2] / "docs/authority/runner-deployment-command-golden-v1.json"
)
_COMMAND_PRIVATE_KEY_BYTES = bytes(range(1, 33))
_COMMAND_KEY_ID = "fixture-domain-key-v1"


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


def _control_stream_subjects() -> list[str]:
    return [
        "crucible.runner.command.v1.*.*.sandbox",
        "crucible.runner.policy.v1.*.*.sandbox",
        "crucible.runner.command.v1.*.*.testnet",
        "crucible.runner.policy.v1.*.*.testnet",
    ]


def _sha256_document(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _recursively_sorted(value: object) -> object:
    if isinstance(value, dict):
        return {key: _recursively_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_recursively_sorted(item) for item in value]
    return value


def _signed_runner_command() -> tuple[str, bytes]:
    fixture = json.loads(_COMMAND_FIXTURE.read_text(encoding="utf-8"))
    case = next(
        value for value in fixture["cases"] if value["name"] == "deployment_spec_ready_for_runner"
    )
    event = dict(case["event_document"])
    event["payload"] = _recursively_sorted(event["payload"])
    event_bytes = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    subject = str(case["subject"])
    subject_bytes = subject.encode("utf-8")
    framed = b"".join(
        (
            b"CRUCIBLE-DOMAIN-EVENT-V1\0",
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )
    private_key = Ed25519PrivateKey.from_private_bytes(_COMMAND_PRIVATE_KEY_BYTES)

    def base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    envelope = {
        "schema_version": 1,
        "signature_profile": "crucible-domain-event-v1-exact-bytes",
        "event_encoding": "application/json;base64url",
        "event_bytes": base64url(event_bytes),
        "signature_key_id": _COMMAND_KEY_ID,
        "signature": base64url(private_key.sign(framed)),
    }
    return subject, json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def _signed_runner_policy() -> tuple[str, bytes, dict[str, Any]]:
    now = datetime.now(UTC).replace(microsecond=0)
    policy_id = UUID("20000000-0000-4000-8000-000000000001")
    body: dict[str, Any] = {
        "schema_version": 1,
        "authority_coordinate": "crucible.runner-aggregate-cap-policy.v1",
        "policy_id": str(policy_id),
        "tenant_id": _TENANT,
        "runner_id": str(_RUNNER),
        "trading_mode": _MODE,
        "revision": 1,
        "settlement_currency": "USDT",
        "max_order_notional": "25000",
        "max_total_notional": "1000000",
        "exposure_model": "filled_plus_active_reservations",
        "breach_action": "freeze_risk_increasing",
        "risk_reducing_orders": "always_permitted",
        "effective_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "status": "active",
        "previous": None,
    }
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    policy = {**body, "policy_digest": hashlib.sha256(canonical_body).hexdigest()}
    subject = f"crucible.runner.policy.v1.{_TENANT}.{_RUNNER}.{_MODE}"
    payload = {
        "policy": policy,
        "policy_id": str(policy_id),
        "revision": 1,
        "policy_digest": policy["policy_digest"],
        "exact_subject": subject,
    }
    event = {
        "schema_version": 1,
        "event_id": "30000000-0000-4000-8000-000000000001",
        "tenant_id": _TENANT,
        "event_plane": {"kind": "mode", "trading_mode": _MODE},
        "bounded_context": "risk",
        "aggregate_type": "runner_safety_policy",
        "aggregate_id": str(policy_id),
        "aggregate_version": 1,
        "event_type": "RunnerSafetyPolicyPublished",
        "payload": payload,
        "correlation_id": "30000000-0000-4000-8000-000000000002",
        "actor_assertion_jti": "30000000-0000-4000-8000-000000000003",
        "occurred_at": now.isoformat().replace("+00:00", "Z"),
    }
    event_bytes = json.dumps(event, separators=(",", ":")).encode()
    subject_bytes = subject.encode()
    framed = b"".join(
        (
            b"CRUCIBLE-DOMAIN-EVENT-V1\0",
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )
    private_key = Ed25519PrivateKey.from_private_bytes(_COMMAND_PRIVATE_KEY_BYTES)
    envelope = {
        "schema_version": 1,
        "signature_profile": "crucible-domain-event-v1-exact-bytes",
        "event_encoding": "application/json;base64url",
        "event_bytes": _b64url(event_bytes),
        "signature_key_id": _COMMAND_KEY_ID,
        "signature": _b64url(private_key.sign(framed)),
    }
    return subject, json.dumps(envelope, separators=(",", ":")).encode(), policy


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
    jetstream_enabled: bool = True,
    revoked_user: str | None = None,
    revoke_at: datetime | None = None,
) -> str:
    storage = -1 if jetstream_enabled else 0
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
            "mem_storage": storage,
            "disk_storage": storage,
            "streams": storage,
            "consumer": storage,
            "max_ack_pending": -1,
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


def _server_config(
    *,
    system_account_public: str,
    system_account_jwt: str,
    account_public: str,
    account_jwt: str,
) -> str:
    return (
        'port: 4222\nserver_name: "custos-t7c-real-nats"\n'
        'jetstream { store_dir: "/tmp/nats/jetstream" }\n'
        'operator: "/config/operator.jwt"\nresolver: MEMORY\n'
        "resolver_preload: {\n"
        f'  {system_account_public}: "{system_account_jwt}"\n'
        f'  {account_public}: "{account_jwt}"\n'
        "}\n"
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


def _unrestricted_user_jwt(
    *,
    account_pair: Any,
    user_public: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return _encode_nats_jwt(
        signer=account_pair,
        subject=user_public,
        issued_at=issued_at,
        expires_at=expires_at,
        nats_claims={
            "pub": {"allow": [">"]},
            "sub": {"allow": [">"]},
            "subs": -1,
            "data": -1,
            "payload": -1,
            "type": "user",
            "version": 2,
        },
    )


async def _connect_admin(
    *,
    user_jwt: str,
    user_seed: bytes,
    nats_url: str,
    certificate: Path,
) -> Any:
    context = ssl.create_default_context(cafile=str(certificate))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    def user_jwt_cb() -> bytearray:
        return bytearray(user_jwt.encode("ascii"))

    def signature_cb(nonce: str) -> bytes:
        seed = bytearray(user_seed)
        pair = nkeys.from_seed(seed)
        try:
            return base64.b64encode(pair.sign(nonce.encode("utf-8")))
        finally:
            pair.wipe()
            for index in range(len(seed)):
                seed[index] = 0

    last_error: Exception | None = None
    for _ in range(5):
        try:
            return await nats.connect(
                servers=[nats_url],
                name="custos-runner-fact-provisioner",
                tls=context,
                tls_hostname="localhost",
                user_jwt_cb=user_jwt_cb,
                signature_cb=signature_cb,
                allow_reconnect=False,
                max_reconnect_attempts=0,
                connect_timeout=1,
            )
        except Exception as error:  # noqa: BLE001 - bounded local broker readiness retry
            last_error = error
            await asyncio.sleep(0.1)
    raise AssertionError("authenticated NATS admin connection did not become ready") from last_error


def _wait_ready(container: str) -> None:
    for _ in range(50):
        logs = _run("docker", "logs", container, check=False)
        if "Server is ready" in f"{logs.stdout}\n{logs.stderr}":
            return
        time.sleep(0.1)
    logs = _run("docker", "logs", container, check=False)
    pytest.fail(f"NATS did not become ready:\n{logs.stdout}\n{logs.stderr}")


async def _start_crucible_projection(
    *,
    crucible_repo: Path,
    nats_url: str,
    certificate: Path,
    admin_jwt: str,
    admin_seed: bytes,
    database: Path,
) -> tuple[asyncio.subprocess.Process, Path]:
    jwt_path = database.with_suffix(".projection-admin.jwt")
    seed_path = database.with_suffix(".projection-admin.seed")
    ready_path = database.with_suffix(".projection-ready")
    receipt_path = database.with_suffix(".projection-receipt.json")
    for path in (ready_path, receipt_path):
        path.unlink(missing_ok=True)
    jwt_path.write_text(admin_jwt, encoding="ascii")
    seed_path.write_bytes(admin_seed)
    jwt_path.chmod(0o600)
    seed_path.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        {
            "RUNNER_FACT_NATS_URL": nats_url,
            "RUNNER_FACT_NATS_USER_JWT_PATH": str(jwt_path),
            "RUNNER_FACT_NATS_USER_SEED_PATH": str(seed_path),
            "RUNNER_FACT_NATS_CA_PATH": str(certificate),
            "RUNNER_FACT_PROJECTOR_READY_PATH": str(ready_path),
            "RUNNER_FACT_PROJECTOR_RECEIPT_PATH": str(receipt_path),
        }
    )
    process = await asyncio.create_subprocess_exec(
        "cargo",
        "test",
        "--quiet",
        "-p",
        "server-http",
        "--test",
        "runner_fact_transport_pg",
        "authenticated_custos_command_lifecycle_reaches_projection",
        "--",
        "--ignored",
        "--nocapture",
        cwd=crucible_repo,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    for _ in range(2400):
        if ready_path.is_file():
            return process, receipt_path
        if process.returncode is not None:
            stdout, stderr = await process.communicate()
            raise AssertionError(
                "Crucible authenticated projector exited before readiness: "
                f"{stderr.decode() or stdout.decode()}"
            )
        await asyncio.sleep(0.05)
    process.terminate()
    stdout, stderr = await process.communicate()
    raise AssertionError(
        "Crucible authenticated projector did not become ready: "
        f"{stderr.decode() or stdout.decode()}"
    )


async def _publish_crucible_policy(
    *,
    crucible_repo: Path,
    nats_url: str,
    certificate: Path,
    admin_jwt: str,
    admin_seed: bytes,
    database: Path,
) -> dict[str, Any]:
    jwt_path = database.with_suffix(".policy-admin.jwt")
    seed_path = database.with_suffix(".policy-admin.seed")
    receipt_path = database.with_suffix(".policy-publication-receipt.json")
    receipt_path.unlink(missing_ok=True)
    jwt_path.write_text(admin_jwt, encoding="ascii")
    seed_path.write_bytes(admin_seed)
    jwt_path.chmod(0o600)
    seed_path.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        {
            "RUNNER_POLICY_NATS_URL": nats_url,
            "RUNNER_POLICY_NATS_USER_JWT_PATH": str(jwt_path),
            "RUNNER_POLICY_NATS_USER_SEED_PATH": str(seed_path),
            "RUNNER_POLICY_NATS_CA_PATH": str(certificate),
            "RUNNER_POLICY_RECEIPT_PATH": str(receipt_path),
            "RUNNER_POLICY_TENANT_ID": _TENANT,
            "RUNNER_POLICY_RUNNER_ID": str(_RUNNER),
            "RUNNER_POLICY_SIGNING_KEY_ID": _COMMAND_KEY_ID,
        }
    )
    process = await asyncio.create_subprocess_exec(
        "cargo",
        "test",
        "--quiet",
        "-p",
        "server-http",
        "--test",
        "runner_safety_policy_transport_pg",
        "external_policy_reaches_custos_daemon",
        "--",
        "--ignored",
        "--nocapture",
        cwd=crucible_repo,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    assert process.returncode == 0, stderr.decode() or stdout.decode()
    assert receipt_path.is_file(), "Crucible policy publisher did not write its receipt"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "CRUCIBLE_PRODUCTION_PUBLISHER_PATH_VERIFIED"
    assert receipt["subject"] == f"crucible.runner.policy.v1.{_TENANT}.{_RUNNER}.{_MODE}"
    assert receipt["real_postgresql"] is True
    assert receipt["real_authenticated_nats_jetstream"] is True
    assert receipt["production_repository_path_exercised"] is True
    assert receipt["production_outbox_worker_path_exercised"] is True
    assert receipt["production_signed_publisher_path_exercised"] is True
    assert receipt["puback_before_outbox_completion"] is True
    assert receipt["production_service_processes_launched"] is False
    assert receipt["production_policy_issued"] is False
    return receipt


async def _exercise_revocation(
    *,
    admin_jwt: str,
    admin_seed: bytes,
    old_credential: RunnerNatsTransportCredential,
    replacement_credential: RunnerNatsTransportCredential,
    nats_url: str,
    certificate: Path,
    database: Path,
    container: str,
    active_config: Path,
    revoked_config: str,
    crucible_repo: Path | None,
) -> None:
    connection_errors: list[str] = []
    projection_process: asyncio.subprocess.Process | None = None
    policy_publication: dict[str, Any] | None = None
    subject = f"crucible.runner.fact.v1.{_TENANT}.{_RUNNER}.{_MODE}"
    admin = await _connect_admin(
        user_jwt=admin_jwt,
        user_seed=admin_seed,
        nats_url=nats_url,
        certificate=certificate,
    )
    jetstream = admin.jetstream()
    await jetstream.add_stream(name=_FACT_STREAM, subjects=[_FACT_STREAM_SUBJECTS])
    durable = _durable_config()
    await jetstream.add_stream(
        name=_CONTROL_STREAM,
        subjects=_control_stream_subjects(),
    )
    await jetstream.add_consumer(
        stream=_CONTROL_STREAM,
        config=ConsumerConfig(
            durable_name=str(durable["durable_name"]),
            deliver_subject=str(durable["delivery_subject"]),
            filter_subjects=list(durable["filter_subjects"]),
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            replay_policy=ReplayPolicy.INSTANT,
            max_ack_pending=1,
        ),
    )

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

        projection_receipt_path: Path | None = None
        if crucible_repo is not None:
            projection_process, projection_receipt_path = await _start_crucible_projection(
                crucible_repo=crucible_repo,
                nats_url=nats_url,
                certificate=certificate,
                admin_jwt=admin_jwt,
                admin_seed=admin_seed,
                database=database,
            )

        replacement_profile.assert_publish_subject(subject)
        credential_path = database.with_suffix(".credential.json")
        credential_path.write_text(
            json.dumps(
                replacement_credential.to_document(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        credential_path.chmod(0o600)
        ready_path = database.with_suffix(".control-ready")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("runner_daemon_lifecycle_process.py")),
            "--nats-url",
            nats_url,
            "--database",
            str(database),
            "--transport-credential",
            str(credential_path),
            "--ca-path",
            str(certificate),
            "--server-name",
            "localhost",
            "--pinned-issuer-public-key",
            replacement_credential.issuer_public_key,
            "--ready-file",
            str(ready_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(100):
            if ready_path.is_file():
                break
            if process.returncode is not None:
                stdout, stderr = await process.communicate()
                raise AssertionError(
                    "authenticated command consumer exited before durable readiness: "
                    f"{stderr.decode() or stdout.decode()}"
                )
            await asyncio.sleep(0.05)
        else:
            process.terminate()
            stdout, stderr = await process.communicate()
            raise AssertionError(
                "authenticated command consumer did not bind its durable: "
                f"{stderr.decode() or stdout.decode()}"
            )
        if crucible_repo is None:
            policy_subject, signed_policy, policy_payload = _signed_runner_policy()
            policy_puback = await jetstream.publish(policy_subject, signed_policy)
        else:
            policy_publication = await _publish_crucible_policy(
                crucible_repo=crucible_repo,
                nats_url=nats_url,
                certificate=certificate,
                admin_jwt=admin_jwt,
                admin_seed=admin_seed,
                database=database,
            )
            policy_payload = {
                "policy_id": policy_publication["policy_id"],
                "revision": policy_publication["policy_revision"],
                "policy_digest": policy_publication["policy_digest"],
                "status": policy_publication["policy_status"],
            }
            policy_puback = None
        command_subject, signed_command = _signed_runner_command()
        command_puback = await jetstream.publish(command_subject, signed_command)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode == 0, stderr.decode()
        output_lines = [line for line in stdout.decode().splitlines() if line.strip()]
        assert output_lines
        publication = json.loads(output_lines[-1])
        assert publication["delivered"] == 1
        assert publication["pending_after"] == 0
        assert publication["subject"] == subject
        assert publication["command_acked"] is True
        assert publication["runtime_status"] == "applied_acked"
        assert publication["engine_ready"] is True
        assert publication["lifecycle_fact_kind"] == "RunnerDeploymentLifecycleFact.v1"
        assert publication["lifecycle_state"] == "running"
        assert publication["lifecycle_outcome"] == "applied"
        assert publication["daemon_launched"] is True
        assert publication["authenticated_policy_consumed"] is True
        assert publication["policy_id"] == policy_payload["policy_id"]
        assert publication["policy_revision"] == policy_payload["revision"]
        assert publication["policy_digest"] == policy_payload["policy_digest"]
        assert publication["policy_status"] == policy_payload["status"]
        assert publication["durable_puback_receipt"] is True
        assert publication["broker_stream"] == _FACT_STREAM
        assert publication["broker_sequence"] > 0
        assert publication["puback_duplicate"] is False
        assert publication["production_authority_issued"] is False
        assert publication["production_policy_issued"] is False
        assert publication["immutable_artifact_materialized"] is False
        if policy_puback is not None:
            assert policy_puback.seq < command_puback.seq
        else:
            assert policy_publication is not None
            assert policy_publication["production_signed_publisher_path_exercised"] is True

        control_consumer = await jetstream.consumer_info(
            _CONTROL_STREAM,
            str(durable["durable_name"]),
        )
        assert control_consumer.num_ack_pending == 0
        assert control_consumer.ack_floor.stream_seq == command_puback.seq
        assert control_consumer.delivered.stream_seq == command_puback.seq

        if projection_process is not None and projection_receipt_path is not None:
            projection_stdout, projection_stderr = await asyncio.wait_for(
                projection_process.communicate(),
                timeout=60,
            )
            assert projection_process.returncode == 0, (
                projection_stderr.decode() or projection_stdout.decode()
            )
            projection = json.loads(projection_receipt_path.read_text(encoding="utf-8"))
            assert projection["batch_id"] == publication["batch_id"]
            assert projection["ingest_status"] == "accepted"
            assert projection["projector_work_count"] == 1
            assert projection["real_postgresql"] is True
            assert projection["production_consumer"] is True

        subscription = await jetstream.pull_subscribe(
            subject,
            durable="runner-fact-authenticated-acceptance",
            stream=_FACT_STREAM,
        )
        messages = await subscription.fetch(1, timeout=2)
        assert len(messages) == 1
        message = messages[0]
        document = json.loads(message.data)
        assert document["batch_id"] == publication["batch_id"]
        assert document["deployment_instance_id"] == publication["deployment_instance_id"]
        assert document["facts"][0]["kind"] == "RunnerDeploymentLifecycleFact.v1"
        assert document["facts"][0]["outcome"] == "applied"
        assert message.headers is not None
        assert message.headers["Nats-Msg-Id"] == publication["batch_id"]
        await message.ack()
        stream = await jetstream.stream_info(_FACT_STREAM)
        assert stream.state.messages == 1
        assert await RunnerFactOutbox(database).pending() == []
        assert replacement.is_connected
    finally:
        if projection_process is not None and projection_process.returncode is None:
            projection_process.terminate()
            try:
                await asyncio.wait_for(projection_process.communicate(), timeout=5)
            except TimeoutError:
                projection_process.kill()
                await projection_process.communicate()
        await admin.close()
        await replacement.close()
        if not old.is_closed:
            await old.close()


@pytest.mark.integration
@pytest.mark.docker
def test_real_nats_memory_resolver_revokes_old_user_jwt_and_keeps_replacement(
    tmp_path: Path,
) -> None:
    _require_local_gate()
    crucible_repo_value = os.environ.get("CRUCIBLE_REPO")
    crucible_repo = (
        Path(crucible_repo_value).expanduser().resolve() if crucible_repo_value else None
    )
    if crucible_repo is not None and not (crucible_repo / "Cargo.toml").is_file():
        pytest.fail("CRUCIBLE_REPO must point to the crucible-rust repository")
    now = datetime.now(UTC).replace(microsecond=0)
    operator_seed, operator_pair, operator_public = _keypair(nkeys.PREFIX_BYTE_OPERATOR)
    system_seed, system_pair, system_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    admin_seed, admin_pair, admin_public = _keypair(nkeys.PREFIX_BYTE_USER)
    old_seed, old_pair, old_public = _keypair(nkeys.PREFIX_BYTE_USER)
    new_seed, new_pair, new_public = _keypair(nkeys.PREFIX_BYTE_USER)
    try:
        operator_jwt = _encode_nats_jwt(
            signer=operator_pair,
            subject=operator_public,
            issued_at=now - timedelta(seconds=20),
            nats_claims={
                "system_account": system_public,
                "type": "operator",
                "version": 2,
            },
        )
        system_account_jwt = _account_jwt(
            operator_pair=operator_pair,
            account_public=system_public,
            issued_at=now - timedelta(seconds=15),
            jetstream_enabled=False,
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
        admin_jwt = _unrestricted_user_jwt(
            account_pair=account_pair,
            user_public=admin_public,
            issued_at=issued_at,
            expires_at=expires_at,
        )
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
        for pair in (
            operator_pair,
            system_pair,
            account_pair,
            admin_pair,
            old_pair,
            new_pair,
        ):
            pair.wipe()
        del operator_seed, system_seed, account_seed

    certificate = _write_tls_material(tmp_path)
    (tmp_path / "operator.jwt").write_text(operator_jwt)
    active_config = tmp_path / "nats.conf"
    active_config.write_text(
        _server_config(
            system_account_public=system_public,
            system_account_jwt=system_account_jwt,
            account_public=account_public,
            account_jwt=initial_account_jwt,
        )
    )
    revoked_config = _server_config(
        system_account_public=system_public,
        system_account_jwt=system_account_jwt,
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
                admin_jwt=admin_jwt,
                admin_seed=admin_seed,
                nats_url=f"tls://localhost:{port}",
                certificate=certificate,
                database=tmp_path / "authenticated-runner-fact.sqlite3",
                container=container,
                active_config=active_config,
                revoked_config=revoked_config,
                crucible_repo=crucible_repo,
            )
        )
        logs = _run("docker", "logs", container, check=False)
        combined = f"{logs.stdout}\n{logs.stderr}"
        assert "Server is ready" in combined
        assert "Reloaded server configuration" in combined
    finally:
        _run("docker", "stop", "-t", "1", container, check=False)
