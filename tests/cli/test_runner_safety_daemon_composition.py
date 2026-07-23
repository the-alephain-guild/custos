from __future__ import annotations

import base64
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.artifacts.release_resolver import (
    CrucibleStrategyReleaseArtifactResolverV1,
    StrategyReleaseResolutionUnavailable,
    UnavailableStrategyReleaseArtifactResolverV1,
)
from custos.cli._daemon import (
    _build_runner_safety_boundary_factory,
    _build_strategy_release_runtime,
)
from custos.core.fallback_breaker import FallbackBreakerConfig

POLICY_ID = UUID("22222222-2222-4222-8222-222222222222")
DEPLOYMENT_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")


class _Resolver:
    def __init__(self, *, policy_id=POLICY_ID, owner_policy: bool = True) -> None:
        self.policy_id = policy_id
        self.owner_policy = owner_policy
        self.modes: list[str] = []

    async def resolve(self, trading_mode: str):
        self.modes.append(trading_mode)
        return SimpleNamespace(
            policy_id=self.policy_id,
            owner_policy=self.owner_policy,
            breaker=FallbackBreakerConfig(
                max_notional=Decimal("100"),
                max_drawdown_pct=Decimal("10"),
            ),
        )


@pytest.mark.asyncio
async def test_boundary_factory_uses_durable_owner_policy_identity() -> None:
    store = object()
    resolver = _Resolver()
    factory = _build_runner_safety_boundary_factory(
        state_store=store,
        safety_policy_resolver=resolver,
    )

    boundary = await factory(
        {
            "deployment_instance_id": str(DEPLOYMENT_INSTANCE_ID),
            "trading_mode": "testnet",
        }
    )

    assert resolver.modes == ["testnet"]
    assert boundary._store is store
    assert boundary._deployment_instance_id == DEPLOYMENT_INSTANCE_ID
    assert boundary._policy_id == POLICY_ID
    assert boundary._fallback_breaker.config.max_notional == Decimal("100")


@pytest.mark.asyncio
async def test_boundary_factory_fails_closed_without_owner_policy() -> None:
    factory = _build_runner_safety_boundary_factory(
        state_store=object(),
        safety_policy_resolver=_Resolver(policy_id=None, owner_policy=False),
    )

    with pytest.raises(RuntimeError, match="verified owner policy"):
        await factory(
            {
                "deployment_instance_id": str(DEPLOYMENT_INSTANCE_ID),
                "trading_mode": "sandbox",
            }
        )


@pytest.mark.asyncio
async def test_uncomposed_strategy_release_authority_fails_closed() -> None:
    resolver = UnavailableStrategyReleaseArtifactResolverV1()

    with pytest.raises(StrategyReleaseResolutionUnavailable, match="not composed"):
        await resolver.resolve(object())


def _artifact_args(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "artifact_release_policy_envelope": None,
        "artifact_release_policy_key_id": "",
        "artifact_release_policy_public_key": None,
        "artifact_sigstore_trusted_root": None,
        "artifact_registry": "ghcr.io",
        "artifact_registry_username": "",
        "artifact_registry_token": "",
        "artifact_cache_dir": tmp_path / "cache",
        "artifact_quarantine_dir": tmp_path / "quarantine",
        "artifact_activation_dir": tmp_path / "activations",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_strategy_release_runtime_remains_uncomposed_without_any_trust_input(
    tmp_path: Path,
) -> None:
    resolver, runtime = _build_strategy_release_runtime(
        args=_artifact_args(tmp_path),
        state_store=object(),
        material_authority=object(),
    )

    assert isinstance(resolver, UnavailableStrategyReleaseArtifactResolverV1)
    assert runtime is None


def test_strategy_release_runtime_rejects_partial_trust_configuration(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="configuration is incomplete"):
        _build_strategy_release_runtime(
            args=_artifact_args(
                tmp_path,
                artifact_release_policy_key_id="release-policy-key",
            ),
            state_store=object(),
            material_authority=object(),
        )


def test_strategy_release_runtime_composes_only_with_complete_local_trust(
    tmp_path: Path,
) -> None:
    envelope = tmp_path / "release-policy.json"
    public_key = tmp_path / "release-policy.pub"
    trusted_root = tmp_path / "sigstore-trusted-root.json"
    envelope.write_bytes(b"{}")
    public_key.write_text(base64.b64encode(bytes(range(32))).decode("ascii"), encoding="ascii")
    trusted_root.write_bytes(b"{}")

    resolver, runtime = _build_strategy_release_runtime(
        args=_artifact_args(
            tmp_path,
            artifact_release_policy_envelope=envelope,
            artifact_release_policy_key_id="release-policy-key",
            artifact_release_policy_public_key=public_key,
            artifact_sigstore_trusted_root=trusted_root,
        ),
        state_store=object(),
        material_authority=object(),
    )

    assert isinstance(resolver, CrucibleStrategyReleaseArtifactResolverV1)
    assert runtime is not None
    assert runtime.capability_ready is True
