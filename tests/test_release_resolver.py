from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from custos.artifacts.release_resolver import (
    CrucibleStrategyReleaseArtifactResolverV1,
    MaterializedStrategyReleaseArtifactV1,
    StrategyReleaseResolutionRejected,
    StrategyReleaseResolutionUnavailable,
)
from custos.core.runner_material_authority import (
    RunnerMaterialResolutionError,
    RunnerMaterialUnavailableError,
)


class _Authority:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, str]] = []

    async def resolve_strategy_release(self, *, command: object, command_fingerprint: str) -> object:
        self.calls.append((command, command_fingerprint))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Materializer:
    def __init__(self, statement: bytes = b"statement") -> None:
        self.statement = statement
        self.authorities: list[object] = []

    async def materialize(
        self,
        *,
        release_authority: object,
        authority_statement_bytes: bytes,
    ) -> MaterializedStrategyReleaseArtifactV1:
        self.authorities.append(release_authority)
        assert authority_statement_bytes == b"statement"
        return MaterializedStrategyReleaseArtifactV1(
            release_statement_bytes=self.statement,
            detached_bundle_path=Path("/tmp/custos-release/bundle.json"),
            member_paths={"strategy.whl": Path("/tmp/custos-release/strategy.whl")},
            verified_at=datetime(2026, 7, 23, tzinfo=UTC),
        )


VERIFIED = SimpleNamespace(command=object(), command_fingerprint="a" * 64)
OWNER_MATERIAL = SimpleNamespace(
    release_authority=object(),
    release_statement_bytes=b"statement",
)


@pytest.mark.asyncio
async def test_resolver_separates_owner_lookup_from_local_materialization() -> None:
    authority = _Authority(OWNER_MATERIAL)
    materializer = _Materializer()
    subject = CrucibleStrategyReleaseArtifactResolverV1(
        authority=authority,
        materializer=materializer,
    )

    resolved = await subject.resolve(VERIFIED)

    assert authority.calls == [(VERIFIED.command, VERIFIED.command_fingerprint)]
    assert materializer.authorities == [OWNER_MATERIAL.release_authority]
    assert resolved.release_statement_bytes == b"statement"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RunnerMaterialUnavailableError("offline"),
            StrategyReleaseResolutionUnavailable,
        ),
        (
            RunnerMaterialResolutionError("conflict"),
            StrategyReleaseResolutionRejected,
        ),
    ],
)
async def test_resolver_preserves_temporary_and_terminal_owner_failures(
    error: Exception,
    expected: type[Exception],
) -> None:
    subject = CrucibleStrategyReleaseArtifactResolverV1(
        authority=_Authority(error),
        materializer=_Materializer(),
    )

    with pytest.raises(expected):
        await subject.resolve(VERIFIED)


@pytest.mark.asyncio
async def test_resolver_rejects_materialized_statement_drift() -> None:
    subject = CrucibleStrategyReleaseArtifactResolverV1(
        authority=_Authority(OWNER_MATERIAL),
        materializer=_Materializer(b"different"),
    )

    with pytest.raises(StrategyReleaseResolutionRejected, match="statement differs"):
        await subject.resolve(VERIFIED)
