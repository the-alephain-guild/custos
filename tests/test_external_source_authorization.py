"""A private operator binding cannot replace signed deployment authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.external_source_authorization import (
    ExternalSourceAuthorizationError,
    load_source_subscription,
)
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

RUNNER = UUID("10000000-0000-4000-8000-000000000001")
INSTANCE = UUID("20000000-0000-4000-8000-000000000001")
SPEC = UUID("30000000-0000-4000-8000-000000000001")
AUTHORITY = EngineLifecycleAuthority(
    deployment_instance_id=INSTANCE,
    deployment_spec_id=SPEC,
    deployment_spec_digest="a" * 64,
    generation=7,
    trading_mode="testnet",
)


def scope(authority: EngineLifecycleAuthority = AUTHORITY) -> ExternalSourceRuntimeScope:
    return ExternalSourceRuntimeScope(
        authority=authority,
        connector="okx_perpetual",
        allowed_pairs=frozenset({"ETH-USDT"}),
        authorized_leverage=3,
    )


def local_files(tmp_path: Path) -> tuple[Path, Path]:
    credentials = tmp_path / "source-subscriber.creds"
    credentials.write_bytes(b"synthetic-separate-nats-jwt-and-seed")
    credentials.chmod(0o600)
    authorization = tmp_path / "source-subscription.json"
    authorization.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "tenantId": "tenant-a",
                "runnerId": str(RUNNER),
                "deploymentInstanceId": str(INSTANCE),
                "deploymentSpecId": str(SPEC),
                "deploymentSpecDigest": "a" * 64,
                "generation": 7,
                "tradingMode": "testnet",
                "sourceId": "tradingview",
                "sourceSymbol": "ETH-USDT-SWAP",
                "targetPair": "ETH-USDT",
                "natsCredentialsSha256": hashlib.sha256(credentials.read_bytes()).hexdigest(),
            }
        )
    )
    authorization.chmod(0o600)
    return authorization, credentials


def load(path: Path, credentials: Path, *, active: ExternalSourceRuntimeScope | None = None):
    return load_source_subscription(
        path,
        credentials_path=credentials,
        expected_tenant_id="tenant-a",
        expected_runner_id=RUNNER,
        active_scope=active or scope(),
    )


def rewrite(path: Path, **changes: object) -> None:
    payload = json.loads(path.read_text())
    payload.update(changes)
    path.write_text(json.dumps(payload))


def test_exact_private_binding_produces_one_deployment_subscription(tmp_path: Path) -> None:
    path, credentials = local_files(tmp_path)
    bound = load(path, credentials)
    assert bound.binding.tenant_id == "tenant-a"
    assert bound.binding.authority == AUTHORITY
    assert bound.binding.source_id == "tradingview"
    assert bound.binding.source_symbol == "ETH-USDT-SWAP"
    assert bound.binding.target_pair == "ETH-USDT"
    assert bound.spec.authority == AUTHORITY
    assert bound.spec.runner_id == RUNNER
    assert bound.spec.filter_subject.endswith(hashlib.sha256(b"tradingview").hexdigest())
    assert bound.credentials_path == credentials


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenantId", "tenant-b"),
        ("runnerId", "10000000-0000-4000-8000-000000000002"),
        ("deploymentInstanceId", "20000000-0000-4000-8000-000000000002"),
        ("deploymentSpecDigest", "b" * 64),
        ("generation", 6),
        ("tradingMode", "live"),
        ("targetPair", "BTC-USDT"),
    ],
)
def test_scope_or_generation_drift_refuses_before_consumer_binding(
    tmp_path: Path, field: str, value: object
) -> None:
    path, credentials = local_files(tmp_path)
    rewrite(path, **{field: value})
    with pytest.raises(ExternalSourceAuthorizationError):
        load(path, credentials)


def test_credential_replacement_and_unowned_files_are_refused(tmp_path: Path) -> None:
    path, credentials = local_files(tmp_path)
    credentials.write_bytes(b"different-jwt-and-seed")
    with pytest.raises(ExternalSourceAuthorizationError, match="credential"):
        load(path, credentials)
    path, credentials = local_files(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ExternalSourceAuthorizationError, match="private"):
        load(path, credentials)
    path, credentials = local_files(tmp_path)
    link = tmp_path / "subscription-link.json"
    link.symlink_to(path)
    with pytest.raises(ExternalSourceAuthorizationError, match="private"):
        load(link, credentials)


def test_payload_cannot_add_amount_or_subscription_override(tmp_path: Path) -> None:
    path, credentials = local_files(tmp_path)
    rewrite(path, amount="100", maxLag=3600)
    with pytest.raises(ExternalSourceAuthorizationError, match="fields"):
        load(path, credentials)


def test_active_host_generation_is_authoritative(tmp_path: Path) -> None:
    path, credentials = local_files(tmp_path)
    stale = EngineLifecycleAuthority(
        deployment_instance_id=INSTANCE,
        deployment_spec_id=SPEC,
        deployment_spec_digest="a" * 64,
        generation=8,
        trading_mode="testnet",
    )
    with pytest.raises(ExternalSourceAuthorizationError, match="active"):
        load(path, credentials, active=scope(stale))
