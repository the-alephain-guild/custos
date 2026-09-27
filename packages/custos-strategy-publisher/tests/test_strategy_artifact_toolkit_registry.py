from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from custos_strategy_publisher.ghcr_registry import HttpResponse
from custos_strategy_publisher.toolkit_authority import (
    CustosToolkitAuthorityV1,
    RegistryArtifactV1,
    load_toolkit_authority,
)
from custos_strategy_publisher.toolkit_registry import (
    OCI_MANIFEST_MEDIA_TYPE,
    OCI_ROLE_ANNOTATION,
    OCI_SOURCE_COORDINATE_ANNOTATION,
    OCI_TITLE_ANNOTATION,
    TOOLKIT_ARTIFACT_TYPE,
    ToolkitRegistryError,
    ToolkitRegistryV1,
)

# The publisher lives in the Custos repository, whose checkout holds the receipts.
CUSTOS_ROOT = Path(__file__).resolve().parents[3]
RC8_AUTHORITY_COMMIT = "4c5112da29d19ade016e8630cf576678e2e484d2"


def _descriptor(artifact: RegistryArtifactV1) -> dict[str, object]:
    annotations = {
        OCI_ROLE_ANNOTATION: artifact.role,
        OCI_TITLE_ANNOTATION: artifact.title,
    }
    if artifact.source_coordinate is not None:
        annotations[OCI_SOURCE_COORDINATE_ANNOTATION] = artifact.source_coordinate
    return {
        "annotations": annotations,
        "digest": artifact.digest,
        "mediaType": artifact.media_type,
        "size": artifact.size_bytes,
    }


def _registry_fixture() -> tuple[
    CustosToolkitAuthorityV1,
    bytes,
    dict[str, bytes],
]:
    authority = load_toolkit_authority(
        CUSTOS_ROOT, candidate_version="0.1.0rc8", authority_commit=RC8_AUTHORITY_COMMIT
    )
    fetched_roles = {
        authority.contract_asset_index.role,
        authority.base_contracts_wheel.role,
        authority.nautilus_wheel.role,
        authority.base_contracts_sbom.role,
        authority.nautilus_sbom.role,
    }
    blobs: dict[str, bytes] = {}
    replacements: dict[str, RegistryArtifactV1] = {}
    layers: list[RegistryArtifactV1] = []
    for artifact in authority.publication_artifacts:
        if artifact.role in fetched_roles:
            content = f"verified:{artifact.role}\n".encode()
            artifact = replace(
                artifact,
                digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
                size_bytes=len(content),
            )
            blobs[artifact.digest] = content
            replacements[artifact.role] = artifact
        layers.append(artifact)
    manifest = json.dumps(
        {
            "annotations": {
                "org.opencontainers.image.revision": authority.source_commit,
                "org.opencontainers.image.source": authority.authority_repository,
                "org.opencontainers.image.version": authority.candidate_version,
            },
            "artifactType": TOOLKIT_ARTIFACT_TYPE,
            "config": _descriptor(authority.publication_config),
            "layers": [_descriptor(artifact) for artifact in layers],
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    authority = replace(
        authority,
        manifest_digest=f"sha256:{hashlib.sha256(manifest).hexdigest()}",
        manifest_size_bytes=len(manifest),
        publication_artifacts=tuple(layers),
        contract_asset_index=replacements[authority.contract_asset_index.role],
        base_contracts_wheel=replacements[authority.base_contracts_wheel.role],
        nautilus_wheel=replacements[authority.nautilus_wheel.role],
        base_contracts_sbom=replacements[authority.base_contracts_sbom.role],
        nautilus_sbom=replacements[authority.nautilus_sbom.role],
    )
    return authority, manifest, blobs


class FakeTransport:
    def __init__(
        self,
        authority: CustosToolkitAuthorityV1,
        manifest: bytes,
        blobs: dict[str, bytes],
    ) -> None:
        self.authority = authority
        self.manifest = manifest
        self.blobs = blobs

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        assert method == "GET"
        assert body is None
        if "/token?" in url:
            assert headers["Authorization"].startswith("Basic ")
            return HttpResponse(200, {}, b'{"token":"pull-token"}')
        assert headers["Authorization"] == "Bearer pull-token"
        if "/manifests/" in url:
            return HttpResponse(
                200,
                {
                    "content-type": OCI_MANIFEST_MEDIA_TYPE,
                    "docker-content-digest": self.authority.manifest_digest,
                },
                self.manifest,
            )
        digest = url.rsplit("/", 1)[-1]
        content = self.blobs[digest]
        return HttpResponse(
            200,
            {"docker-content-digest": digest},
            content,
        )


def test_fetches_receipt_pinned_toolkit_registry_snapshot() -> None:
    authority, manifest, blobs = _registry_fixture()

    snapshot = ToolkitRegistryV1(
        authority,
        actor="release-actor",
        token="release-token",
        transport=FakeTransport(authority, manifest, blobs),
    ).fetch()

    assert snapshot.manifest == manifest
    assert snapshot.contract_asset_index == blobs[authority.contract_asset_index.digest]
    assert snapshot.base_contracts_wheel == blobs[authority.base_contracts_wheel.digest]
    assert snapshot.nautilus_wheel == blobs[authority.nautilus_wheel.digest]
    assert snapshot.base_contracts_sbom == blobs[authority.base_contracts_sbom.digest]
    assert snapshot.nautilus_sbom == blobs[authority.nautilus_sbom.digest]


def test_rejects_manifest_descriptor_omission() -> None:
    authority, manifest, blobs = _registry_fixture()
    document = json.loads(manifest)
    document["layers"].pop()
    manifest = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    authority = replace(
        authority,
        manifest_digest=f"sha256:{hashlib.sha256(manifest).hexdigest()}",
        manifest_size_bytes=len(manifest),
    )

    with pytest.raises(ToolkitRegistryError, match="layer count"):
        ToolkitRegistryV1(
            authority,
            actor="release-actor",
            token="release-token",
            transport=FakeTransport(authority, manifest, blobs),
        ).fetch()


def test_rejects_blob_content_digest_drift() -> None:
    authority, manifest, blobs = _registry_fixture()
    digest = authority.nautilus_wheel.digest
    original = blobs[digest]
    blobs[digest] = bytes([original[0] ^ 1]) + original[1:]

    with pytest.raises(ToolkitRegistryError, match="content digest"):
        ToolkitRegistryV1(
            authority,
            actor="release-actor",
            token="release-token",
            transport=FakeTransport(authority, manifest, blobs),
        ).fetch()
