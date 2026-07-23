from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from custos.artifacts.immutable_material import RegistryStrategyReleaseMaterializerV1
from custos.artifacts.release_resolver import StrategyReleaseResolutionRejected


class _Transport:
    def __init__(self, blobs: dict[tuple[str, str, str], bytes]) -> None:
        self.blobs = blobs
        self.calls: list[tuple[str, str, str, int]] = []

    def fetch_blob(
        self,
        *,
        registry: str,
        repository: str,
        digest: str,
        max_bytes: int,
    ) -> bytes:
        self.calls.append((registry, repository, digest, max_bytes))
        return self.blobs[(registry, repository, digest)]


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture() -> tuple[object, bytes, dict[tuple[str, str, str], bytes]]:
    registry = "ghcr.io"
    repository = "alchymia-labs/v1-team-strategy-artifacts"
    statement = b'{"statement":true}'
    bundle = b'{"bundle":true}'
    wheel = b"strategy-wheel"
    statement_digest = _digest(statement)
    bundle_digest = _digest(bundle)
    wheel_digest = _digest(wheel)
    authority = SimpleNamespace(
        detached_attestation_ref={
            "statement_coordinate": (
                f"oci://{registry}/{repository}/statement.json@sha256:{statement_digest}"
            ),
            "statement_sha256": statement_digest,
            "bundle_coordinate": (
                f"oci://{registry}/{repository}/bundle.json@sha256:{bundle_digest}"
            ),
            "bundle_sha256": bundle_digest,
        },
        artifact_ref=SimpleNamespace(
            artifact_sha256=wheel_digest,
            artifact_size_bytes=len(wheel),
        ),
        release_bom={
            "members": [
                {
                    "role": "strategy_wheel",
                    "name": "strategy.whl",
                    "size_bytes": len(wheel),
                    "sha256": wheel_digest,
                }
            ]
        },
    )
    blobs = {
        (registry, repository, statement_digest): statement,
        (registry, repository, bundle_digest): bundle,
        (registry, repository, wheel_digest): wheel,
    }
    return authority, statement, blobs


@pytest.mark.asyncio
async def test_materializer_fetches_exact_execution_bytes_once_into_digest_cache(
    tmp_path: Path,
) -> None:
    authority, statement, blobs = _fixture()
    transport = _Transport(blobs)
    subject = RegistryStrategyReleaseMaterializerV1(
        cache_root=(tmp_path / "cache").resolve(),
        transport=transport,
    )

    first = await subject.materialize(
        release_authority=authority,
        authority_statement_bytes=statement,
    )
    second = await subject.materialize(
        release_authority=authority,
        authority_statement_bytes=statement,
    )

    assert len(transport.calls) == 3
    assert first.release_statement_bytes == statement
    assert first.detached_bundle_path.is_file()
    assert first.member_paths["strategy.whl"].is_file()
    assert second.member_paths == first.member_paths
    assert first.verified_at.utcoffset() is not None


@pytest.mark.asyncio
async def test_materializer_rejects_cross_repository_attestation_coordinates(
    tmp_path: Path,
) -> None:
    authority, statement, blobs = _fixture()
    authority.detached_attestation_ref["bundle_coordinate"] = (
        authority.detached_attestation_ref["bundle_coordinate"].replace(
            "alchymia-labs/v1-team-strategy-artifacts",
            "other/repository",
        )
    )
    subject = RegistryStrategyReleaseMaterializerV1(
        cache_root=(tmp_path / "cache").resolve(),
        transport=_Transport(blobs),
    )

    with pytest.raises(StrategyReleaseResolutionRejected, match="repositories differ"):
        await subject.materialize(
            release_authority=authority,
            authority_statement_bytes=statement,
        )


@pytest.mark.asyncio
async def test_materializer_rejects_registry_byte_drift(tmp_path: Path) -> None:
    authority, statement, blobs = _fixture()
    wheel_digest = authority.artifact_ref.artifact_sha256
    blobs[("ghcr.io", "alchymia-labs/v1-team-strategy-artifacts", wheel_digest)] = (
        b"drift"
    )
    subject = RegistryStrategyReleaseMaterializerV1(
        cache_root=(tmp_path / "cache").resolve(),
        transport=_Transport(blobs),
    )

    with pytest.raises(StrategyReleaseResolutionRejected, match="size differs"):
        await subject.materialize(
            release_authority=authority,
            authority_statement_bytes=statement,
        )
