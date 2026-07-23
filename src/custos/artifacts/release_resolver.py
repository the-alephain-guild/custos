"""Authenticated Crucible StrategyRelease material boundary for Custos V1."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from custos.artifacts.runtime import StrategyReleaseArtifactAuthorityV1
from custos.core.runner_command_intake import VerifiedRunnerCommand
from custos.core.runner_material_authority import (
    RunnerMaterialResolutionError,
    RunnerMaterialUnavailableError,
    StrategyReleaseRunnerMaterialResolver,
)


class StrategyReleaseResolutionError(RuntimeError):
    """Base error for the authenticated owner-material lookup."""


class StrategyReleaseResolutionUnavailable(StrategyReleaseResolutionError):
    """The owner endpoint or authenticated transport is temporarily unavailable."""


class StrategyReleaseResolutionRejected(StrategyReleaseResolutionError):
    """Crucible rejected the requested release or returned conflicting authority."""


@dataclass(frozen=True, slots=True)
class ResolvedStrategyReleaseArtifactV1:
    release_authority: StrategyReleaseArtifactAuthorityV1
    release_statement_bytes: bytes
    detached_bundle_path: Path
    member_paths: Mapping[str, Path]
    verified_at: datetime

    def __post_init__(self) -> None:
        if not self.release_statement_bytes:
            raise ValueError("resolved StrategyRelease statement bytes are required")
        if self.verified_at.tzinfo is None:
            raise ValueError("StrategyRelease verification time must be timezone-aware")
        paths = (self.detached_bundle_path, *self.member_paths.values())
        if not self.member_paths or any(not path.is_absolute() for path in paths):
            raise ValueError("resolved StrategyRelease member paths must be absolute")


@dataclass(frozen=True, slots=True)
class MaterializedStrategyReleaseArtifactV1:
    release_statement_bytes: bytes
    detached_bundle_path: Path
    member_paths: Mapping[str, Path]
    verified_at: datetime

    def __post_init__(self) -> None:
        if not self.release_statement_bytes:
            raise ValueError("materialized StrategyRelease statement bytes are required")
        if self.verified_at.tzinfo is None:
            raise ValueError("materialization verification time must be timezone-aware")
        paths = (self.detached_bundle_path, *self.member_paths.values())
        if not self.member_paths or any(not path.is_absolute() for path in paths):
            raise ValueError("materialized StrategyRelease paths must be absolute")


class StrategyReleaseArtifactMaterializerV1(Protocol):
    async def materialize(
        self,
        *,
        release_authority: StrategyReleaseArtifactAuthorityV1,
        authority_statement_bytes: bytes,
    ) -> MaterializedStrategyReleaseArtifactV1: ...


class StrategyReleaseArtifactResolverV1(Protocol):
    async def resolve(
        self,
        verified: VerifiedRunnerCommand,
    ) -> ResolvedStrategyReleaseArtifactV1: ...


class CrucibleStrategyReleaseArtifactResolverV1:
    """Resolve owner facts, then independently materialize immutable execution bytes."""

    def __init__(
        self,
        *,
        authority: StrategyReleaseRunnerMaterialResolver,
        materializer: StrategyReleaseArtifactMaterializerV1,
    ) -> None:
        self._authority = authority
        self._materializer = materializer

    async def resolve(
        self,
        verified: VerifiedRunnerCommand,
    ) -> ResolvedStrategyReleaseArtifactV1:
        try:
            material = await self._authority.resolve_strategy_release(
                command=verified.command,
                command_fingerprint=verified.command_fingerprint,
            )
        except RunnerMaterialUnavailableError as error:
            raise StrategyReleaseResolutionUnavailable(
                "authenticated Crucible StrategyRelease authority is unavailable"
            ) from error
        except RunnerMaterialResolutionError as error:
            raise StrategyReleaseResolutionRejected(
                "Crucible StrategyRelease authority conflicts with the signed command"
            ) from error
        materialized = await self._materializer.materialize(
            release_authority=material.release_authority,
            authority_statement_bytes=material.release_statement_bytes,
        )
        if materialized.release_statement_bytes != material.release_statement_bytes:
            raise StrategyReleaseResolutionRejected(
                "materialized release statement differs from Crucible authority"
            )
        return ResolvedStrategyReleaseArtifactV1(
            release_authority=material.release_authority,
            release_statement_bytes=materialized.release_statement_bytes,
            detached_bundle_path=materialized.detached_bundle_path,
            member_paths=materialized.member_paths,
            verified_at=materialized.verified_at,
        )


class UnavailableStrategyReleaseArtifactResolverV1:
    """Production-safe composition default until Crucible publishes its receipt."""

    async def resolve(
        self,
        verified: VerifiedRunnerCommand,
    ) -> ResolvedStrategyReleaseArtifactV1:
        raise StrategyReleaseResolutionUnavailable(
            "authenticated Crucible StrategyRelease resolver is not composed"
        )


__all__ = [
    "CrucibleStrategyReleaseArtifactResolverV1",
    "MaterializedStrategyReleaseArtifactV1",
    "ResolvedStrategyReleaseArtifactV1",
    "StrategyReleaseArtifactResolverV1",
    "StrategyReleaseArtifactMaterializerV1",
    "StrategyReleaseResolutionError",
    "StrategyReleaseResolutionRejected",
    "StrategyReleaseResolutionUnavailable",
    "UnavailableStrategyReleaseArtifactResolverV1",
]
