"""Transport authority consumer acceptance."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import nkeys
import pytest
from nats.errors import AuthorizationError
from nats.errors import Error as NatsError

from custos.cli.subcommands import nats_transport as nats_transport_cli
from custos.core import nats_transport
from custos.core.nats_transport import (
    DevelopmentLocalNatsConnectionProfile,
    RunnerNatsTransportBundle,
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportCredential,
    RunnerNatsTransportError,
    RunnerNatsTransportPendingOperation,
    RunnerNatsTransportRevokedError,
    RunnerNatsTransportSet,
    RunnerNatsTransportVault,
    assert_old_generation_reconnect_denied,
    runner_control_stream,
    runner_nats_transport_domain,
)
from custos.core.runner_nats_authority import (
    RUNNER_NATS_OPERATION_PATH,
    RUNNER_NATS_OPERATION_RESULT_PATH,
    RunnerNatsTransportAuthorityClient,
    RunnerNatsTransportOperationCompletion,
)

_TENANT = "tenant-a"
_RUNNER = UUID("66666666-6666-4666-8666-666666666666")
_TRANSPORT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_MACHINE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OPERATION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_MODE = "sandbox"
_DOMAIN = "sim"
_AUTHORITY_IDS = {
    "sandbox": _TRANSPORT_ID,
    "testnet": UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
    "live": UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
}


def test_development_local_profile_is_exact_loopback_sandbox() -> None:
    profile = DevelopmentLocalNatsConnectionProfile(
        tenant_id=_TENANT,
        runner_id=_RUNNER,
        nats_url="nats://127.0.0.1:4222",
    )

    assert profile.trading_mode == "sandbox"
    assert profile.durable_config["durable_name"] == (
        f"custos-control-v1-{_TENANT}-{_RUNNER}-sandbox"
    )
    profile.assert_publish_subject(f"crucible.runner.fact.v1.{_TENANT}.{_RUNNER}.sandbox")
    with pytest.raises(RunnerNatsTransportError, match="local sandbox authority"):
        profile.assert_publish_subject(f"crucible.runner.fact.v1.{_TENANT}.{_RUNNER}.live")


@pytest.mark.parametrize(
    "url",
    ("nats://nats.internal:4222", "tls://127.0.0.1:4222", "nats://user@localhost:4222"),
)
def test_development_local_profile_rejects_non_loopback_or_authenticated_urls(url: str) -> None:
    with pytest.raises(RunnerNatsTransportError, match="loopback"):
        DevelopmentLocalNatsConnectionProfile(
            tenant_id=_TENANT,
            runner_id=_RUNNER,
            nats_url=url,
        )


def test_cli_registers_each_transport_action_without_option_conflicts() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    nats_transport_cli.register(subparsers)

    common = [
        "--nats-url",
        "tls://nats.example.test:4222",
        "--nats-server-name",
        "nats.example.test",
        "--issuer-public-key",
        "ACRUCIBLE",
        "--trading-mode",
        _MODE,
    ]
    for action in ("enroll", "rotate", "revoke"):
        parsed = parser.parse_args(
            [
                "nats-transport",
                action,
                *common,
                "--crucible-url",
                "https://crucible.example.test",
                "--authorization-intent-id",
                "11111111-1111-4111-8111-111111111111",
            ]
        )
        assert parsed.transport_action == action
        assert parsed.issuer_public_key == "ACRUCIBLE"

    resumed = parser.parse_args(
        [
            "nats-transport",
            "resume",
            *common,
            "--crucible-url",
            "https://crucible.example.test",
        ]
    )
    assert resumed.transport_action == "resume"
    assert not hasattr(resumed, "authorization_intent_id")

    parsed = parser.parse_args(["nats-transport", "verify", *common])
    assert parsed.transport_action == "verify"
    assert parsed.issuer_public_key == "ACRUCIBLE"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _keypair(prefix: int) -> tuple[bytes, object, str]:
    seed = nkeys.encode_seed(os.urandom(32), prefix)
    pair = nkeys.from_seed(bytearray(seed))
    return seed, pair, pair.public_key.decode("ascii")


def _permission_profile(trading_mode: str = _MODE) -> dict[str, object]:
    domain = runner_nats_transport_domain(trading_mode)
    durable = f"custos-control-v1-{_TENANT}-{_RUNNER}-{trading_mode}"
    stream = runner_control_stream(trading_mode)
    return {
        "schema_version": 1,
        "profile": "crucible.runner-nats-transport.v1",
        "tenant_id": _TENANT,
        "runner_id": str(_RUNNER),
        "trading_mode": trading_mode,
        "transport_domain": domain,
        "publish_allow": [
            f"crucible.runner.fact.v1.{_TENANT}.{_RUNNER}.{trading_mode}",
            f"$JS.ACK.{stream}.{durable}.>",
            f"$JS.API.CONSUMER.INFO.{stream}.{durable}",
        ],
        "subscribe_allow": [
            f"custos.runner.control.v1.delivery.{_TENANT}.{_RUNNER}.{trading_mode}",
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


def _durable_config(trading_mode: str = _MODE) -> dict[str, object]:
    domain = runner_nats_transport_domain(trading_mode)
    return {
        "schema_version": 1,
        "transport_domain": domain,
        "stream_name": runner_control_stream(trading_mode),
        "durable_name": f"custos-control-v1-{_TENANT}-{_RUNNER}-{trading_mode}",
        "delivery_subject": (
            f"custos.runner.control.v1.delivery.{_TENANT}.{_RUNNER}.{trading_mode}"
        ),
        "filter_subjects": [
            f"crucible.runner.command.v1.{_TENANT}.{_RUNNER}.{trading_mode}",
            f"crucible.runner.policy.v1.{_TENANT}.{_RUNNER}.{trading_mode}",
        ],
        "deliver_policy": "all",
        "ack_policy": "explicit",
        "replay_policy": "instant",
        "max_ack_pending": 1,
        "consumer_mode": "push_existing_only",
    }


def _digest(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _issued(
    *,
    user_seed: bytes,
    user_public_key: str,
    account_pair: object,
    account_public_key: str,
    generation: int = 1,
    trading_mode: str = _MODE,
    now: datetime | None = None,
    operation_id: UUID = _OPERATION_ID,
) -> dict[str, object]:
    issued_at = (now or datetime(2026, 7, 19, 8, 0, tzinfo=UTC)).replace(microsecond=0)
    expires_at = issued_at + timedelta(hours=1)
    transport_domain = runner_nats_transport_domain(trading_mode)
    permission = _permission_profile(trading_mode)
    durable = _durable_config(trading_mode)
    header = _b64url(
        json.dumps(
            {"typ": "JWT", "alg": "ed25519-nkey"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    claims = _b64url(
        json.dumps(
            {
                "iss": account_public_key,
                "sub": user_public_key,
                "iat": int(issued_at.timestamp()),
                "exp": int(expires_at.timestamp()),
                "nats": {
                    "type": "user",
                    "version": 2,
                    "pub": {
                        "allow": permission["publish_allow"],
                        "deny": permission["publish_deny"],
                    },
                    "sub": {
                        "allow": permission["subscribe_allow"],
                        "deny": permission["subscribe_deny"],
                    },
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = account_pair.sign(signing_input)  # type: ignore[attr-defined]
    jwt = f"{header}.{claims}.{_b64url(signature)}"
    del user_seed
    authority: dict[str, object] = {
        "schema_version": 1,
        "authority_coordinate": "crucible.runner-nats-transport.v1",
        "authority_id": str(_AUTHORITY_IDS[trading_mode]),
        "tenant_id": _TENANT,
        "runner_id": str(_RUNNER),
        "trading_mode": trading_mode,
        "transport_domain": transport_domain,
        "credential_generation": generation,
        "user_public_key": user_public_key,
        "user_jwt": jwt,
        "user_jwt_sha256": hashlib.sha256(jwt.encode("ascii")).hexdigest(),
        "issuer_public_key": account_public_key,
        "signing_key_id": "account-signer-test",
        "claims_sha256": hashlib.sha256(claims.encode("ascii")).hexdigest(),
        "permission_profile": permission,
        "permission_profile_sha256": _digest(permission),
        "durable_config": durable,
        "durable_config_sha256": _digest(durable),
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "not_before": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "status": "active",
        "operation_id": str(operation_id),
    }
    authority["authority_digest"] = _digest(authority)
    return authority


def _credential(
    *,
    generation: int = 1,
    trading_mode: str = _MODE,
    now: datetime | None = None,
) -> RunnerNatsTransportCredential:
    user_seed, user_pair, user_public = _keypair(nkeys.PREFIX_BYTE_USER)
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    try:
        return RunnerNatsTransportCredential.from_authority_document(
            _issued(
                user_seed=user_seed,
                user_public_key=user_public,
                account_pair=account_pair,
                account_public_key=account_public,
                generation=generation,
                trading_mode=trading_mode,
                now=now,
            ),
            user_seed=user_seed,
            expected_tenant_id=_TENANT,
            expected_runner_id=_RUNNER,
            expected_trading_mode=trading_mode,
            expected_issuer_public_key=account_public,
        )
    finally:
        user_pair.wipe()
        account_pair.wipe()
        del account_seed


def test_issued_credential_verifies_jwt_acl_durable_and_redacts_secrets() -> None:
    credential = _credential()

    rendered = repr(credential)

    assert credential.durable_config["stream_name"] == "CRUCIBLE_RUNNER_CONTROL_SIM_V1"
    assert credential.durable_config["durable_name"] == (
        f"custos-control-v1-{_TENANT}-{_RUNNER}-{_MODE}"
    )
    assert credential.user_jwt not in rendered
    assert base64.b64encode(credential.user_seed).decode("ascii") not in rendered


def test_supervisor_transport_set_keeps_exact_mode_authorities_independent(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    sandbox = _credential(trading_mode="sandbox", now=now)
    testnet = _credential(trading_mode="testnet", now=now)
    live = _credential(trading_mode="live", now=now)
    transports = RunnerNatsTransportSet(
        {
            "sandbox": RunnerNatsTransportBundle(active=sandbox, pending_operation=None),
            "testnet": RunnerNatsTransportBundle(active=testnet, pending_operation=None),
            "live": RunnerNatsTransportBundle(active=live, pending_operation=None),
        }
    )

    assert transports.active("sandbox").authority_id == sandbox.authority_id
    assert transports.active("testnet").permission_profile != sandbox.permission_profile
    assert transports.active("live").transport_domain == "live"
    assert runner_control_stream("sandbox") == runner_control_stream("testnet")
    assert runner_control_stream("live") != runner_control_stream("sandbox")
    assert RunnerNatsTransportVault(tmp_path, "sandbox").path == tmp_path / "sandbox.enc"
    assert RunnerNatsTransportVault(tmp_path, "live").path == tmp_path / "live.enc"

    with pytest.raises(RunnerNatsTransportError, match="mode binding mismatch"):
        RunnerNatsTransportSet(
            {"live": RunnerNatsTransportBundle(active=sandbox, pending_operation=None)}
        )


def test_permission_or_stream_drift_is_rejected_before_socket_open() -> None:
    user_seed, user_pair, user_public = _keypair(nkeys.PREFIX_BYTE_USER)
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    try:
        response = _issued(
            user_seed=user_seed,
            user_public_key=user_public,
            account_pair=account_pair,
            account_public_key=account_public,
        )
        response["durable_config"] = {
            **response["durable_config"],  # type: ignore[dict-item]
            "stream_name": "SECOND_RUNNER_STREAM",
        }
        response["durable_config_sha256"] = _digest(
            response["durable_config"]  # type: ignore[arg-type]
        )
        with pytest.raises(RunnerNatsTransportError, match="exact runner-control authority"):
            RunnerNatsTransportCredential.from_authority_document(
                response,
                user_seed=user_seed,
                expected_tenant_id=_TENANT,
                expected_runner_id=_RUNNER,
                expected_trading_mode=_MODE,
                expected_issuer_public_key=account_public,
            )
    finally:
        user_pair.wipe()
        account_pair.wipe()
        del account_seed


def test_tls_profile_rejects_plaintext_host_drift_and_issuer_drift(tmp_path: Path) -> None:
    credential = _credential(now=datetime.now(UTC))
    ca = tmp_path / "ca.pem"
    ca.write_text("test-ca")

    with pytest.raises(RunnerNatsTransportError, match="tls://"):
        RunnerNatsTransportConnectionProfile(
            credential,
            "nats://nats.internal:4222",
            ca,
            "nats.internal",
            credential.issuer_public_key,
        )
    with pytest.raises(RunnerNatsTransportError, match="server name"):
        RunnerNatsTransportConnectionProfile(
            credential,
            "tls://nats.internal:4222",
            ca,
            "other.internal",
            credential.issuer_public_key,
        )
    with pytest.raises(RunnerNatsTransportError, match="issuer"):
        RunnerNatsTransportConnectionProfile(
            credential,
            "tls://nats.internal:4222",
            ca,
            "nats.internal",
            "A" + "A" * 55,
        )


@pytest.mark.asyncio
async def test_connect_uses_pinned_tls_jwt_and_local_nonce_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(now=datetime.now(UTC))
    ca = tmp_path / "ca.pem"
    ca.write_text("test-ca")
    context = MagicMock()
    connect = AsyncMock(return_value=object())
    monkeypatch.setattr(nats_transport.ssl, "create_default_context", lambda **_: context)
    monkeypatch.setattr(nats_transport.nats, "connect", connect)
    profile = RunnerNatsTransportConnectionProfile(
        credential,
        "tls://nats.internal:4222",
        ca,
        "nats.internal",
        credential.issuer_public_key,
    )

    await profile.connect(name="test-runner")

    kwargs = connect.await_args.kwargs
    assert kwargs["servers"] == ["tls://nats.internal:4222"]
    assert kwargs["tls"] is context
    assert kwargs["tls_hostname"] == "nats.internal"
    assert bytes(kwargs["user_jwt_cb"]()) == credential.user_jwt.encode("ascii")
    signature = base64.b64decode(kwargs["signature_cb"]("nonce"), validate=True)
    pair = nkeys.from_seed(bytearray(credential.user_seed))
    try:
        assert pair.verify(b"nonce", signature) is True
    finally:
        pair.wipe()
    await kwargs["disconnected_cb"]()
    await asyncio.wait_for(profile.wait_disconnected(), timeout=0.1)


@pytest.mark.asyncio
async def test_broker_authorization_denial_invalidates_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(now=datetime.now(UTC))
    ca = tmp_path / "ca.pem"
    ca.write_text("test-ca")
    context = MagicMock()
    connect = AsyncMock(return_value=object())
    monkeypatch.setattr(nats_transport.ssl, "create_default_context", lambda **_: context)
    monkeypatch.setattr(nats_transport.nats, "connect", connect)
    profile = RunnerNatsTransportConnectionProfile(
        credential,
        "tls://nats.internal:4222",
        ca,
        "nats.internal",
        credential.issuer_public_key,
    )
    await profile.connect(name="test-runner")

    await connect.await_args.kwargs["error_cb"](AuthorizationError())

    with pytest.raises(RunnerNatsTransportRevokedError, match="rejected"):
        profile.assert_active()


def _machine() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=_TENANT,
        runner_id=_RUNNER,
        credential_id=_MACHINE_ID,
        credential_version=1,
    )


def test_pending_rotation_vault_round_trip_preserves_active_and_local_seed() -> None:
    active = _credential(now=datetime.now(UTC))
    client = RunnerNatsTransportAuthorityClient(
        "https://crucible.internal",
        _machine(),  # type: ignore[arg-type]
    )
    operation = client.prepare_rotation(
        active,
        authorization_intent_id=UUID("11111111-1111-4111-8111-111111111111"),
    )
    staged = RunnerNatsTransportBundle(
        active=active,
        pending_operation=operation,
    )

    restored = RunnerNatsTransportBundle.from_document(staged.to_document())

    assert restored == staged
    assert restored.active == active
    assert restored.pending_operation == operation
    assert restored.pending_operation.user_seed == operation.user_seed
    assert restored.pending_operation.target_generation == 2


def test_authority_client_uses_only_begin_poll_v1_and_never_sends_seed() -> None:
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    client = RunnerNatsTransportAuthorityClient(
        "https://crucible.internal",
        _machine(),  # type: ignore[arg-type]
    )
    operation = client.prepare_initial(
        authorization_intent_id=UUID("11111111-1111-4111-8111-111111111111"),
        trading_mode=_MODE,
        expected_issuer_public_key=account_public,
        now=datetime.now(UTC),
    )
    operation = RunnerNatsTransportPendingOperation.from_document(operation.to_document())
    calls: list[tuple[str, dict[str, object], dict[str, object]]] = []
    result_reads = 0

    class _Http:
        def post(self, path, body, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal result_reads
            calls.append((path, body, kwargs))
            if path == RUNNER_NATS_OPERATION_PATH:
                return {
                    "operation_id": str(operation.operation_id),
                    "target_generation": 1,
                    "request_fingerprint": "f" * 64,
                    "phase": "pending_signing",
                    "replayed": False,
                }
            result_reads += 1
            base = {
                "schema_version": 1,
                "operation_id": str(operation.operation_id),
                "trading_mode": _MODE,
                "operation_kind": "issue",
                "target_generation": 1,
                "request_fingerprint": "f" * 64,
                "revocation_receipt_digest": None,
            }
            if result_reads == 1:
                return {
                    **base,
                    "phase": "pending_provisioning",
                    "outcome": "pending",
                    "authority": None,
                }
            return {
                **base,
                "phase": "pending_readback",
                "outcome": "succeeded",
                "authority": _issued(
                    user_seed=operation.user_seed,
                    user_public_key=operation.user_public_key,
                    account_pair=account_pair,
                    account_public_key=account_public,
                    now=datetime.now(UTC),
                    operation_id=operation.operation_id,
                ),
            }

    try:
        client.http = _Http()  # type: ignore[assignment]
        completion = client.execute(
            operation,
            timeout_seconds=1,
            poll_interval_seconds=0.001,
        )

        assert completion.credential is not None
        assert completion.credential.user_seed == operation.user_seed
        assert [call[0] for call in calls] == [
            RUNNER_NATS_OPERATION_PATH,
            RUNNER_NATS_OPERATION_RESULT_PATH,
            RUNNER_NATS_OPERATION_RESULT_PATH,
        ]
        assert all(call[2]["canonical_path"] == call[0] for call in calls)
        serialized_requests = json.dumps([body for _, body, _ in calls], sort_keys=True)
        assert base64.b64encode(operation.user_seed).decode("ascii") not in serialized_requests
        assert "user_seed" not in serialized_requests
        assert calls[0][1]["user_public_key"] == operation.user_public_key
        assert calls[0][1]["user_key_possession_signature_base64"]
    finally:
        account_pair.wipe()
        del account_seed


def test_pending_result_rejects_completion_material() -> None:
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    client = RunnerNatsTransportAuthorityClient(
        "https://crucible.internal",
        _machine(),  # type: ignore[arg-type]
    )
    operation = client.prepare_initial(
        authorization_intent_id=UUID("11111111-1111-4111-8111-111111111111"),
        trading_mode=_MODE,
        expected_issuer_public_key=account_public,
    )

    class _Http:
        def post(self, path, body, **_kwargs):  # type: ignore[no-untyped-def]
            if path == RUNNER_NATS_OPERATION_PATH:
                return {
                    "operation_id": str(operation.operation_id),
                    "target_generation": 1,
                    "request_fingerprint": "f" * 64,
                    "phase": "pending_signing",
                    "replayed": False,
                }
            return {
                "schema_version": 1,
                "operation_id": str(operation.operation_id),
                "trading_mode": _MODE,
                "operation_kind": "issue",
                "target_generation": 1,
                "phase": "pending_signing",
                "outcome": "pending",
                "request_fingerprint": "f" * 64,
                "authority": {},
                "revocation_receipt_digest": None,
            }

    try:
        client.http = _Http()  # type: ignore[assignment]
        with pytest.raises(RunnerNatsTransportError, match="exposed completion"):
            client.execute(operation, timeout_seconds=1)
    finally:
        account_pair.wipe()
        del account_seed


def test_revocation_completion_requires_broker_receipt() -> None:
    active = _credential(now=datetime.now(UTC))
    client = RunnerNatsTransportAuthorityClient(
        "https://crucible.internal",
        _machine(),  # type: ignore[arg-type]
    )
    operation = client.prepare_revocation(
        active,
        authorization_intent_id=UUID("11111111-1111-4111-8111-111111111111"),
    )

    class _Http:
        def post(self, path, body, **_kwargs):  # type: ignore[no-untyped-def]
            if path == RUNNER_NATS_OPERATION_PATH:
                return {
                    "operation_id": str(operation.operation_id),
                    "target_generation": active.credential_generation,
                    "request_fingerprint": "f" * 64,
                    "phase": "pending_revocation",
                    "replayed": False,
                }
            return {
                "schema_version": 1,
                "operation_id": str(operation.operation_id),
                "trading_mode": active.trading_mode,
                "operation_kind": "revoke",
                "target_generation": active.credential_generation,
                "phase": "pending_revocation",
                "outcome": "succeeded",
                "request_fingerprint": "f" * 64,
                "authority": None,
                "revocation_receipt_digest": "d" * 64,
            }

    client.http = _Http()  # type: ignore[assignment]
    completion = client.execute(operation, timeout_seconds=1)

    assert completion.credential is None
    assert completion.revocation_receipt_digest == "d" * 64


@pytest.mark.asyncio
async def test_old_generation_probe_requires_typed_authorization_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(now=datetime.now(UTC))
    ca = tmp_path / "ca.pem"
    ca.write_text("test-ca")
    context = MagicMock()
    monkeypatch.setattr(nats_transport.ssl, "create_default_context", lambda **_: context)

    async def rejected(**kwargs):  # type: ignore[no-untyped-def]
        await kwargs["error_cb"](AuthorizationError())
        raise OSError("broker rejected credentials")

    monkeypatch.setattr(nats_transport.nats, "connect", rejected)
    profile = RunnerNatsTransportConnectionProfile(
        credential,
        "tls://nats.internal:4222",
        ca,
        "nats.internal",
        credential.issuer_public_key,
    )

    await assert_old_generation_reconnect_denied(
        profile,
        name="old-generation",
        timeout_seconds=1,
    )

    async def protocol_rejected(**_kwargs):  # type: ignore[no-untyped-def]
        raise NatsError("nats: 'Authorization Violation'")

    monkeypatch.setattr(nats_transport.nats, "connect", protocol_rejected)
    protocol_profile = RunnerNatsTransportConnectionProfile(
        credential,
        "tls://nats.internal:4222",
        ca,
        "nats.internal",
        credential.issuer_public_key,
    )
    await assert_old_generation_reconnect_denied(
        protocol_profile,
        name="old-generation",
        timeout_seconds=1,
    )

    async def unavailable(**_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("network unavailable")

    monkeypatch.setattr(nats_transport.nats, "connect", unavailable)
    second_profile = RunnerNatsTransportConnectionProfile(
        credential,
        "tls://nats.internal:4222",
        ca,
        "nats.internal",
        credential.issuer_public_key,
    )
    with pytest.raises(RunnerNatsTransportError, match="without explicit"):
        await assert_old_generation_reconnect_denied(
            second_profile,
            name="old-generation",
            timeout_seconds=1,
        )

    async def unrelated_protocol_error(**_kwargs):  # type: ignore[no-untyped-def]
        raise NatsError("nats: 'Permissions Violation for Publish'")

    monkeypatch.setattr(nats_transport.nats, "connect", unrelated_protocol_error)
    fourth_profile = RunnerNatsTransportConnectionProfile(
        credential,
        "tls://nats.internal:4222",
        ca,
        "nats.internal",
        credential.issuer_public_key,
    )
    with pytest.raises(RunnerNatsTransportError, match="without explicit"):
        await assert_old_generation_reconnect_denied(
            fourth_profile,
            name="old-generation",
            timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_cli_commits_rotation_only_after_new_connect_and_old_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_seed, account_pair, account_public = _keypair(nkeys.PREFIX_BYTE_ACCOUNT)
    active_seed, active_pair, active_public = _keypair(nkeys.PREFIX_BYTE_USER)
    try:
        active = RunnerNatsTransportCredential.from_authority_document(
            _issued(
                user_seed=active_seed,
                user_public_key=active_public,
                account_pair=account_pair,
                account_public_key=account_public,
                generation=1,
                now=datetime.now(UTC),
            ),
            user_seed=active_seed,
            expected_tenant_id=_TENANT,
            expected_runner_id=_RUNNER,
            expected_trading_mode=_MODE,
            expected_issuer_public_key=account_public,
        )
        client = RunnerNatsTransportAuthorityClient(
            "https://crucible.internal",
            _machine(),  # type: ignore[arg-type]
        )
        operation = client.prepare_rotation(
            active,
            authorization_intent_id=UUID("11111111-1111-4111-8111-111111111111"),
        )
        replacement = RunnerNatsTransportCredential.from_authority_document(
            _issued(
                user_seed=operation.user_seed,
                user_public_key=operation.user_public_key,
                account_pair=account_pair,
                account_public_key=account_public,
                generation=2,
                now=datetime.now(UTC),
                operation_id=operation.operation_id,
            ),
            user_seed=operation.user_seed,
            expected_tenant_id=_TENANT,
            expected_runner_id=_RUNNER,
            expected_trading_mode=_MODE,
            expected_issuer_public_key=account_public,
        )
        bundle = RunnerNatsTransportBundle(
            active=active,
            pending_operation=operation,
        )
        completion = RunnerNatsTransportOperationCompletion(
            operation_id=operation.operation_id,
            operation_kind="rotate",
            target_generation=2,
            request_fingerprint="f" * 64,
            credential=replacement,
            revocation_receipt_digest="d" * 64,
        )

        events: list[str] = []

        class _Connection:
            is_closed = False

            async def close(self) -> None:
                self.is_closed = True
                events.append("new_closed")

        class _Profile:
            def __init__(self, credential, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.credential = credential

            async def connect(self, **_kwargs):  # type: ignore[no-untyped-def]
                events.append("new_connected")
                return _Connection()

        async def denied(profile, **_kwargs):  # type: ignore[no-untyped-def]
            assert profile.credential is active
            events.append("old_denied")

        class _Vault:
            def __init__(self) -> None:
                self.persisted: list[RunnerNatsTransportBundle] = []

            def persist(self, value, **_kwargs):  # type: ignore[no-untyped-def]
                events.append("persisted")
                self.persisted.append(value)

            def delete(self) -> None:
                raise AssertionError("rotation must not delete the vault")

        monkeypatch.setattr(
            nats_transport_cli,
            "RunnerNatsTransportConnectionProfile",
            _Profile,
        )
        monkeypatch.setattr(
            nats_transport_cli,
            "assert_old_generation_reconnect_denied",
            denied,
        )
        args = SimpleNamespace(
            nats_url="tls://nats.internal:4222",
            nats_ca=tmp_path / "ca.pem",
            nats_server_name="nats.internal",
            verification_timeout_secs=30.0,
        )
        vault = _Vault()

        completed = await nats_transport_cli._verify_and_commit(  # noqa: SLF001
            args=args,
            vault=vault,  # type: ignore[arg-type]
            bundle=bundle,
            completion=completion,
            age_recipient="age1test",
        )

        assert completed is not None
        assert completed.active is replacement
        assert completed.pending_operation is None
        assert events == ["new_connected", "new_closed", "old_denied", "persisted"]
        assert vault.persisted == [completed]
    finally:
        active_pair.wipe()
        account_pair.wipe()
        del account_seed
