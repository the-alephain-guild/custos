"""Acyclic OCI publication contract for signed strategy releases."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, Self, cast

from .model import ArtifactAttestationRefV1, canonical_json_bytes
from .oci_primitives import (
    ATTESTATION_REF_MEDIA_TYPE,
    DISCOVERY_TAG_PATTERN,
    GHCR_REPOSITORY_PATTERN,
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_ISSUER,
    OCI_MANIFEST_MEDIA_TYPE,
    OCI_ROLE_ANNOTATION,
    OCI_TITLE_ANNOTATION,
    OIDC_SUBJECT_PATTERN,
    RELEASE_ARTIFACT_TYPE,
    RELEASE_LAYER_MEDIA_TYPES,
    SIGSTORE_ARTIFACT_TYPE,
    SIGSTORE_BUNDLE_MEDIA_TYPE,
    SOURCE_REF_PATTERN,
    WORKFLOW_REF_PATTERN,
    OciBlobReadbackV1,
    OciBlobV1,
    OciDescriptorV1,
    OciManifestReadbackV1,
    OciPublicationDescriptorV1,
    OciPublicationError,
    PublicationWorkflowIdentityV1,
    descriptor_matrix_digest,
    require_ghcr_repository,
)

PUBLICATION_RECEIPT_V1_SCHEMA_VERSION = "alephain.strategy-artifact-oci-publication-receipt.v1"
PUBLISH_INPUT_V1_SCHEMA_VERSION = "alephain.strategy-artifact-oci-publish-input.v1"
GITHUB_OIDC_PUBLISHER_PROFILE = "github_oidc"
RELEASE_CONFIG_V1_MEDIA_TYPE = "application/vnd.alephain.strategy-release.config.v1+json"
ATTESTATION_CONFIG_V1_MEDIA_TYPE = (
    "application/vnd.alephain.strategy-release.attestation.config.v1+json"
)
STRATEGY_OCI_ARTIFACT_REF_SCHEMA_VERSION = 1
ARTIFACT_REF_KIND = "oci"
ARTIFACT_REF_REGISTRY = "ghcr.io"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# A discovery tag names the strategy and its version, such as trend-supertrend-2.1.1;
# it is a pointer only, never authority.
_TAG_RE = re.compile(DISCOVERY_TAG_PATTERN)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


class OciRegistryV1(Protocol):
    """Minimal content-addressed registry surface required by the V1 publisher."""

    def put_blob(
        self,
        repository: str,
        descriptor: OciDescriptorV1,
        content: bytes,
    ) -> OciDescriptorV1: ...

    def get_blob(self, repository: str, digest: str) -> OciBlobReadbackV1: ...

    def resolve_manifest(
        self,
        repository: str,
        reference: str,
    ) -> OciManifestReadbackV1 | None: ...

    def put_manifest(
        self,
        repository: str,
        reference: str,
        media_type: str,
        content: bytes,
    ) -> OciDescriptorV1: ...

    def get_manifest(
        self,
        repository: str,
        digest: str,
    ) -> OciManifestReadbackV1: ...


@dataclass(frozen=True, slots=True)
class StrategyOciConfigDescriptorV1:
    media_type: str
    digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        _require_media_type(self.media_type, "StrategyOciArtifactRefV1 config")
        _require_digest(self.digest, "StrategyOciArtifactRefV1 config")
        _require_positive(self.size_bytes, "StrategyOciArtifactRefV1 config size")

    def to_mapping(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        _require_exact_keys(value, {"digest", "media_type", "size_bytes"}, "config")
        return cls(
            media_type=_as_str(value["media_type"], "config.media_type"),
            digest=_as_str(value["digest"], "config.digest"),
            size_bytes=_as_int(value["size_bytes"], "config.size_bytes"),
        )


@dataclass(frozen=True, slots=True)
class StrategyOciLayerDescriptorV1:
    media_type: str
    digest: str
    size_bytes: int
    role: str
    name: str

    def __post_init__(self) -> None:
        _require_media_type(self.media_type, "StrategyOciArtifactRefV1 layer")
        _require_digest(self.digest, "StrategyOciArtifactRefV1 layer")
        _require_positive(self.size_bytes, "StrategyOciArtifactRefV1 layer size")
        if self.role not in RELEASE_LAYER_MEDIA_TYPES:
            raise ValueError("StrategyOciArtifactRefV1 layer role differs")
        if self.media_type != RELEASE_LAYER_MEDIA_TYPES[self.role]:
            raise ValueError("StrategyOciArtifactRefV1 layer media type differs")
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError("StrategyOciArtifactRefV1 layer name differs")

    def to_mapping(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "name": self.name,
            "role": self.role,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        _require_exact_keys(
            value,
            {"digest", "media_type", "name", "role", "size_bytes"},
            "layer",
        )
        return cls(
            media_type=_as_str(value["media_type"], "layer.media_type"),
            digest=_as_str(value["digest"], "layer.digest"),
            size_bytes=_as_int(value["size_bytes"], "layer.size_bytes"),
            role=_as_str(value["role"], "layer.role"),
            name=_as_str(value["name"], "layer.name"),
        )


@dataclass(frozen=True, slots=True)
class StrategyOciArtifactRefV1:
    """Producer-published immutable OCI artifact identity derived after manifest publication."""

    manifest_digest: str
    config: StrategyOciConfigDescriptorV1
    layers: tuple[StrategyOciLayerDescriptorV1, ...]
    schema_version: int = STRATEGY_OCI_ARTIFACT_REF_SCHEMA_VERSION
    artifact_kind: str = ARTIFACT_REF_KIND
    registry: str = ARTIFACT_REF_REGISTRY
    repository: str = ""
    artifact_type: str = RELEASE_ARTIFACT_TYPE

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_OCI_ARTIFACT_REF_SCHEMA_VERSION:
            raise ValueError("StrategyOciArtifactRefV1 schema version differs")
        if self.artifact_kind != ARTIFACT_REF_KIND:
            raise ValueError("StrategyOciArtifactRefV1 artifact kind differs")
        if self.registry != ARTIFACT_REF_REGISTRY:
            raise ValueError("StrategyOciArtifactRefV1 registry differs")
        # The reference names the repository without the registry, which is separate.
        require_ghcr_repository(f"{ARTIFACT_REF_REGISTRY}/{self.repository}")
        if self.artifact_type != RELEASE_ARTIFACT_TYPE:
            raise ValueError("StrategyOciArtifactRefV1 artifact type differs")
        _require_digest(self.manifest_digest, "StrategyOciArtifactRefV1 manifest")
        roles = tuple(layer.role for layer in self.layers)
        if roles != tuple(RELEASE_LAYER_MEDIA_TYPES):
            raise ValueError("StrategyOciArtifactRefV1 layer order differs")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "artifact_type": self.artifact_type,
            "config": self.config.to_mapping(),
            "layers": [layer.to_mapping() for layer in self.layers],
            "manifest_digest": self.manifest_digest,
            "registry": self.registry,
            "repository": self.repository,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        _require_exact_keys(
            value,
            {
                "artifact_kind",
                "artifact_type",
                "config",
                "layers",
                "manifest_digest",
                "registry",
                "repository",
                "schema_version",
            },
            "StrategyOciArtifactRefV1",
        )
        config = _as_mapping(value["config"], "artifact_ref.config")
        raw_layers = _as_list(value["layers"], "artifact_ref.layers")
        return cls(
            manifest_digest=_as_str(value["manifest_digest"], "artifact_ref.manifest_digest"),
            config=StrategyOciConfigDescriptorV1.from_mapping(config),
            layers=tuple(
                StrategyOciLayerDescriptorV1.from_mapping(_as_mapping(item, "artifact_ref.layer"))
                for item in raw_layers
            ),
            schema_version=_as_int(value["schema_version"], "artifact_ref.schema_version"),
            artifact_kind=_as_str(value["artifact_kind"], "artifact_ref.artifact_kind"),
            registry=_as_str(value["registry"], "artifact_ref.registry"),
            repository=_as_str(value["repository"], "artifact_ref.repository"),
            artifact_type=_as_str(value["artifact_type"], "artifact_ref.artifact_type"),
        )


@dataclass(frozen=True, slots=True)
class OciCandidateV1:
    repository: str
    discovery_tag: str
    producer_repository: str
    producer_commit: str
    strategy_coordinate: str
    workflow: PublicationWorkflowIdentityV1
    release_layers: tuple[OciBlobV1, ...]
    attestation_bundle: OciBlobV1
    attestation_ref: ArtifactAttestationRefV1

    def __post_init__(self) -> None:
        require_ghcr_repository(self.repository)
        if not _TAG_RE.fullmatch(self.discovery_tag):
            raise ValueError("discovery_tag must name the strategy and its version")
        if not self.producer_repository:
            raise ValueError("producer_repository must not be empty")
        if not _COMMIT_RE.fullmatch(self.producer_commit):
            raise ValueError("producer_commit must be a full lowercase commit digest")
        if not self.strategy_coordinate:
            raise ValueError("strategy_coordinate must not be empty")
        if not isinstance(self.workflow, PublicationWorkflowIdentityV1):
            raise TypeError("workflow must be PublicationWorkflowIdentityV1")
        roles = tuple(layer.role for layer in self.release_layers)
        if roles != tuple(RELEASE_LAYER_MEDIA_TYPES):
            raise ValueError("release layer order differs")
        for layer in self.release_layers:
            if layer.media_type != RELEASE_LAYER_MEDIA_TYPES[layer.role]:
                raise ValueError(f"release layer media type differs for {layer.role}")
        if self.attestation_bundle.role != "attestation_bundle":
            raise ValueError("attestation bundle role differs")
        if self.attestation_bundle.media_type != SIGSTORE_BUNDLE_MEDIA_TYPE:
            raise ValueError("attestation bundle media type differs")
        bundle_digest = hashlib.sha256(self.attestation_bundle.content).hexdigest()
        if self.attestation_ref.bundle_sha256 != bundle_digest:
            raise ValueError("attestation ref bundle digest differs")
        statement = next(
            layer for layer in self.release_layers if layer.role == "strategy_release_statement"
        )
        statement_digest = hashlib.sha256(statement.content).hexdigest()
        if self.attestation_ref.statement_sha256 != statement_digest:
            raise ValueError("attestation ref statement digest differs")


@dataclass(frozen=True, slots=True)
class StrategyArtifactOciPublicationReceiptV1:
    schema_version: str
    repository: str
    discovery_tag: str
    tag_is_authority: bool
    producer_repository: str
    producer_commit: str
    publisher_profile: str
    strategy_coordinate: str
    artifact_ref: StrategyOciArtifactRefV1
    artifact_ref_digest: str
    workflow: PublicationWorkflowIdentityV1
    release_manifest_digest: str
    release_manifest_size_bytes: int
    release_descriptor_matrix: tuple[OciPublicationDescriptorV1, ...]
    release_descriptor_matrix_digest: str
    attestation_manifest_digest: str
    attestation_manifest_size_bytes: int
    attestation_descriptor_matrix: tuple[OciPublicationDescriptorV1, ...]
    attestation_descriptor_matrix_digest: str
    blob_readback_verified: bool
    manifest_readback_verified: bool
    # Attests that the attestation manifest names this exact release as its OCI
    # subject and that the named release resolves. It does not claim the registry
    # served a referrers index: GHCR implements no such endpoint, and covering that
    # gap would require a mutable tag this lane refuses to publish.
    referrer_verified: bool
    external_publication_completed: bool

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_RECEIPT_V1_SCHEMA_VERSION:
            raise ValueError("publication receipt V1 schema version differs")
        require_ghcr_repository(self.repository)
        if self.tag_is_authority:
            raise ValueError("publication receipt V1 tag cannot be authority")
        if not _TAG_RE.fullmatch(self.discovery_tag):
            raise ValueError("publication receipt V1 discovery tag differs")
        if not _COMMIT_RE.fullmatch(self.producer_commit):
            raise ValueError("publication receipt V1 producer commit differs")
        if self.publisher_profile != GITHUB_OIDC_PUBLISHER_PROFILE:
            raise ValueError("publication receipt V1 publisher profile differs")
        if self.artifact_ref_digest != self.artifact_ref.digest():
            raise ValueError("StrategyOciArtifactRefV1 digest differs")
        if self.artifact_ref.manifest_digest != self.release_manifest_digest:
            raise ValueError("StrategyOciArtifactRefV1 release manifest differs")
        _require_digest(self.release_manifest_digest, "receipt release manifest")
        _require_digest(self.attestation_manifest_digest, "receipt attestation manifest")
        _require_positive(self.release_manifest_size_bytes, "receipt release manifest size")
        _require_positive(
            self.attestation_manifest_size_bytes,
            "receipt attestation manifest size",
        )
        if descriptor_matrix_digest(self.release_descriptor_matrix) != (
            self.release_descriptor_matrix_digest
        ):
            raise ValueError("receipt release descriptor matrix digest differs")
        if descriptor_matrix_digest(self.attestation_descriptor_matrix) != (
            self.attestation_descriptor_matrix_digest
        ):
            raise ValueError("receipt attestation descriptor matrix digest differs")
        _validate_artifact_ref_matrix(self.artifact_ref, self.release_descriptor_matrix)
        if tuple(item.role for item in self.attestation_descriptor_matrix) != (
            "attestation_config",
            "attestation_bundle",
            "artifact_attestation_ref",
        ):
            raise ValueError("receipt attestation descriptor matrix order differs")
        PublicationWorkflowIdentityV1(**asdict(self.workflow))
        if not (
            self.blob_readback_verified
            and self.manifest_readback_verified
            and self.referrer_verified
        ):
            raise ValueError("publication receipt V1 verification flags must be true")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_ref": self.artifact_ref.to_mapping(),
            "artifact_ref_digest": self.artifact_ref_digest,
            "attestation_descriptor_matrix": [
                item.to_mapping() for item in self.attestation_descriptor_matrix
            ],
            "attestation_descriptor_matrix_digest": (self.attestation_descriptor_matrix_digest),
            "attestation_manifest_digest": self.attestation_manifest_digest,
            "attestation_manifest_size_bytes": self.attestation_manifest_size_bytes,
            "blob_readback_verified": self.blob_readback_verified,
            "discovery_tag": self.discovery_tag,
            "external_publication_completed": self.external_publication_completed,
            "manifest_readback_verified": self.manifest_readback_verified,
            "producer_commit": self.producer_commit,
            "producer_repository": self.producer_repository,
            "publisher_profile": self.publisher_profile,
            "referrer_verified": self.referrer_verified,
            "release_descriptor_matrix": [
                item.to_mapping() for item in self.release_descriptor_matrix
            ],
            "release_descriptor_matrix_digest": self.release_descriptor_matrix_digest,
            "release_manifest_digest": self.release_manifest_digest,
            "release_manifest_size_bytes": self.release_manifest_size_bytes,
            "repository": self.repository,
            "schema_version": self.schema_version,
            "strategy_coordinate": self.strategy_coordinate,
            "tag_is_authority": self.tag_is_authority,
            "workflow": asdict(self.workflow),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Self:
        expected = {
            "artifact_ref",
            "artifact_ref_digest",
            "attestation_descriptor_matrix",
            "attestation_descriptor_matrix_digest",
            "attestation_manifest_digest",
            "attestation_manifest_size_bytes",
            "blob_readback_verified",
            "discovery_tag",
            "external_publication_completed",
            "manifest_readback_verified",
            "producer_commit",
            "producer_repository",
            "publisher_profile",
            "referrer_verified",
            "release_descriptor_matrix",
            "release_descriptor_matrix_digest",
            "release_manifest_digest",
            "release_manifest_size_bytes",
            "repository",
            "schema_version",
            "strategy_coordinate",
            "tag_is_authority",
            "workflow",
        }
        _require_exact_keys(value, expected, "publication receipt V1")
        return cls(
            schema_version=_as_str(value["schema_version"], "schema_version"),
            repository=_as_str(value["repository"], "repository"),
            discovery_tag=_as_str(value["discovery_tag"], "discovery_tag"),
            tag_is_authority=_as_bool(value["tag_is_authority"], "tag_is_authority"),
            producer_repository=_as_str(value["producer_repository"], "producer_repository"),
            producer_commit=_as_str(value["producer_commit"], "producer_commit"),
            publisher_profile=_as_str(value["publisher_profile"], "publisher_profile"),
            strategy_coordinate=_as_str(value["strategy_coordinate"], "strategy_coordinate"),
            artifact_ref=StrategyOciArtifactRefV1.from_mapping(
                _as_mapping(value["artifact_ref"], "artifact_ref")
            ),
            artifact_ref_digest=_as_str(value["artifact_ref_digest"], "artifact_ref_digest"),
            workflow=_parse_workflow(value["workflow"]),
            release_manifest_digest=_as_str(
                value["release_manifest_digest"], "release_manifest_digest"
            ),
            release_manifest_size_bytes=_as_int(
                value["release_manifest_size_bytes"],
                "release_manifest_size_bytes",
            ),
            release_descriptor_matrix=_parse_matrix(
                value["release_descriptor_matrix"], "release_descriptor_matrix"
            ),
            release_descriptor_matrix_digest=_as_str(
                value["release_descriptor_matrix_digest"],
                "release_descriptor_matrix_digest",
            ),
            attestation_manifest_digest=_as_str(
                value["attestation_manifest_digest"],
                "attestation_manifest_digest",
            ),
            attestation_manifest_size_bytes=_as_int(
                value["attestation_manifest_size_bytes"],
                "attestation_manifest_size_bytes",
            ),
            attestation_descriptor_matrix=_parse_matrix(
                value["attestation_descriptor_matrix"],
                "attestation_descriptor_matrix",
            ),
            attestation_descriptor_matrix_digest=_as_str(
                value["attestation_descriptor_matrix_digest"],
                "attestation_descriptor_matrix_digest",
            ),
            blob_readback_verified=_as_bool(
                value["blob_readback_verified"], "blob_readback_verified"
            ),
            manifest_readback_verified=_as_bool(
                value["manifest_readback_verified"], "manifest_readback_verified"
            ),
            referrer_verified=_as_bool(value["referrer_verified"], "referrer_verified"),
            external_publication_completed=_as_bool(
                value["external_publication_completed"],
                "external_publication_completed",
            ),
        )


@dataclass(frozen=True, slots=True)
class OciPublicationBundleV1:
    artifact_ref: StrategyOciArtifactRefV1
    receipt: StrategyArtifactOciPublicationReceiptV1

    def __post_init__(self) -> None:
        if self.receipt.artifact_ref != self.artifact_ref:
            raise ValueError("publication bundle StrategyOciArtifactRefV1 differs from receipt")
        if self.receipt.artifact_ref_digest != self.artifact_ref.digest():
            raise ValueError("publication bundle StrategyOciArtifactRefV1 digest differs")


def publish_verified_candidate(
    candidate: OciCandidateV1,
    registry: OciRegistryV1,
    *,
    external_publication_completed: bool = False,
) -> OciPublicationBundleV1:
    """Publish exact bytes in acyclic order and return cross-bound evidence."""

    if registry.resolve_manifest(candidate.repository, candidate.discovery_tag) is not None:
        raise OciPublicationError("immutable discovery tag already exists")

    release_config = OciBlobV1(
        role="release_config",
        name="strategy-release-oci-config-v1.json",
        media_type=RELEASE_CONFIG_V1_MEDIA_TYPE,
        content=canonical_json_bytes(
            {
                "artifact_kind": ARTIFACT_REF_KIND,
                "artifact_type": RELEASE_ARTIFACT_TYPE,
                "schema_version": "alephain.strategy-release-oci-config.v1",
            }
        ),
    )
    release_blobs = (release_config, *candidate.release_layers)
    release_descriptors = tuple(
        _put_blob(registry, candidate.repository, blob) for blob in release_blobs
    )
    release_manifest_bytes = _release_manifest_bytes(
        release_descriptors[0],
        candidate.release_layers,
        release_descriptors[1:],
    )
    release_descriptor = _put_and_read_manifest(
        registry,
        candidate.repository,
        release_manifest_bytes,
        RELEASE_ARTIFACT_TYPE,
    )
    for blob, descriptor in zip(release_blobs, release_descriptors, strict=True):
        _read_blob(registry, candidate.repository, blob, descriptor)

    artifact_ref = StrategyOciArtifactRefV1(
        repository=candidate.repository.removeprefix(f"{ARTIFACT_REF_REGISTRY}/"),
        manifest_digest=release_descriptor.digest,
        config=StrategyOciConfigDescriptorV1(
            media_type=release_descriptors[0].media_type,
            digest=release_descriptors[0].digest,
            size_bytes=release_descriptors[0].size_bytes,
        ),
        layers=tuple(
            StrategyOciLayerDescriptorV1(
                media_type=descriptor.media_type,
                digest=descriptor.digest,
                size_bytes=descriptor.size_bytes,
                role=blob.role,
                name=blob.name,
            )
            for blob, descriptor in zip(
                candidate.release_layers,
                release_descriptors[1:],
                strict=True,
            )
        ),
    )
    artifact_ref_digest = artifact_ref.digest()

    attestation_config = OciBlobV1(
        role="attestation_config",
        name="strategy-release-attestation-oci-config-v1.json",
        media_type=ATTESTATION_CONFIG_V1_MEDIA_TYPE,
        content=canonical_json_bytes(
            {
                "artifact_ref_digest": artifact_ref_digest,
                "oidc_audience": candidate.workflow.oidc_audience,
                "oidc_issuer": candidate.workflow.oidc_issuer,
                "oidc_subject": candidate.workflow.oidc_subject,
                "producer_commit": candidate.producer_commit,
                "producer_repository": candidate.producer_repository,
                "publisher_profile": GITHUB_OIDC_PUBLISHER_PROFILE,
                "source_ref": candidate.workflow.source_ref,
                "release_manifest_digest": release_descriptor.digest,
                "schema_version": ("alephain.strategy-release-attestation-oci-config.v1"),
                "workflow_identity": candidate.workflow.workflow_identity,
                "workflow_ref": candidate.workflow.workflow_ref,
                "workflow_run_attempt": candidate.workflow.workflow_run_attempt,
                "workflow_run_id": candidate.workflow.workflow_run_id,
            }
        ),
    )
    attestation_ref_blob = OciBlobV1(
        role="artifact_attestation_ref",
        name="artifact-attestation-ref.json",
        media_type=ATTESTATION_REF_MEDIA_TYPE,
        content=canonical_json_bytes(asdict(candidate.attestation_ref)),
    )
    attestation_blobs = (
        attestation_config,
        candidate.attestation_bundle,
        attestation_ref_blob,
    )
    attestation_descriptors = tuple(
        _put_blob(registry, candidate.repository, blob) for blob in attestation_blobs
    )
    attestation_manifest_bytes = _attestation_manifest_bytes(
        release_descriptor,
        attestation_blobs,
        attestation_descriptors,
    )
    attestation_descriptor = _put_and_read_manifest(
        registry,
        candidate.repository,
        attestation_manifest_bytes,
        SIGSTORE_ARTIFACT_TYPE,
    )
    for blob, descriptor in zip(attestation_blobs, attestation_descriptors, strict=True):
        _read_blob(registry, candidate.repository, blob, descriptor)
    _verify_subject_binding(
        registry,
        candidate.repository,
        attestation_manifest_bytes,
        release_descriptor,
    )

    if registry.resolve_manifest(candidate.repository, candidate.discovery_tag) is not None:
        raise OciPublicationError("immutable discovery tag already exists")
    tag_descriptor = registry.put_manifest(
        candidate.repository,
        candidate.discovery_tag,
        OCI_MANIFEST_MEDIA_TYPE,
        release_manifest_bytes,
    )
    if tag_descriptor != release_descriptor:
        raise OciPublicationError("tag descriptor differs from release manifest")
    tag_readback = registry.resolve_manifest(candidate.repository, candidate.discovery_tag)
    if tag_readback is None or tag_readback.descriptor != release_descriptor:
        raise OciPublicationError("tag readback differs from release manifest")
    if tag_readback.content != release_manifest_bytes:
        raise OciPublicationError("tag readback bytes differ from release manifest")

    release_matrix = tuple(
        _matrix_entry(blob, descriptor)
        for blob, descriptor in zip(release_blobs, release_descriptors, strict=True)
    )
    attestation_matrix = tuple(
        _matrix_entry(blob, descriptor)
        for blob, descriptor in zip(attestation_blobs, attestation_descriptors, strict=True)
    )
    receipt = StrategyArtifactOciPublicationReceiptV1(
        schema_version=PUBLICATION_RECEIPT_V1_SCHEMA_VERSION,
        repository=candidate.repository,
        discovery_tag=candidate.discovery_tag,
        tag_is_authority=False,
        producer_repository=candidate.producer_repository,
        producer_commit=candidate.producer_commit,
        publisher_profile=GITHUB_OIDC_PUBLISHER_PROFILE,
        strategy_coordinate=candidate.strategy_coordinate,
        artifact_ref=artifact_ref,
        artifact_ref_digest=artifact_ref_digest,
        workflow=candidate.workflow,
        release_manifest_digest=release_descriptor.digest,
        release_manifest_size_bytes=release_descriptor.size_bytes,
        release_descriptor_matrix=release_matrix,
        release_descriptor_matrix_digest=descriptor_matrix_digest(release_matrix),
        attestation_manifest_digest=attestation_descriptor.digest,
        attestation_manifest_size_bytes=attestation_descriptor.size_bytes,
        attestation_descriptor_matrix=attestation_matrix,
        attestation_descriptor_matrix_digest=descriptor_matrix_digest(attestation_matrix),
        blob_readback_verified=True,
        manifest_readback_verified=True,
        referrer_verified=True,
        external_publication_completed=external_publication_completed,
    )
    return OciPublicationBundleV1(artifact_ref=artifact_ref, receipt=receipt)


def strategy_oci_artifact_ref_v1_json_schema() -> dict[str, object]:
    digest = {"pattern": _DIGEST_RE.pattern, "type": "string"}
    media_type = {
        "pattern": ("^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$"),
        "type": "string",
    }
    return {
        "$defs": {
            "OciBlobDescriptorV1": {
                "additionalProperties": False,
                "properties": {
                    "digest": digest,
                    "media_type": media_type,
                    "size_bytes": {"exclusiveMinimum": 0, "type": "integer"},
                },
                "required": ["media_type", "digest", "size_bytes"],
                "type": "object",
            },
            "OciStrategyLayerDescriptorV1": {
                "additionalProperties": False,
                "properties": {
                    "digest": digest,
                    "media_type": media_type,
                    "name": {"pattern": _NAME_RE.pattern, "type": "string"},
                    "role": {
                        "enum": list(RELEASE_LAYER_MEDIA_TYPES),
                        "type": "string",
                    },
                    "size_bytes": {"exclusiveMinimum": 0, "type": "integer"},
                },
                "required": [
                    "media_type",
                    "digest",
                    "size_bytes",
                    "role",
                    "name",
                ],
                "type": "object",
            },
        },
        "$id": (
            "https://custos.the-alephain-guild/contracts/v1/strategy-artifact-ref-v1.schema.json"
        ),
        "additionalProperties": False,
        "properties": {
            "artifact_kind": {"const": ARTIFACT_REF_KIND, "type": "string"},
            "artifact_type": {"const": RELEASE_ARTIFACT_TYPE, "type": "string"},
            "config": {"$ref": "#/$defs/OciBlobDescriptorV1"},
            "layers": {
                "items": {"$ref": "#/$defs/OciStrategyLayerDescriptorV1"},
                "maxItems": 5,
                "minItems": 5,
                "type": "array",
            },
            "manifest_digest": digest,
            "registry": {"const": ARTIFACT_REF_REGISTRY, "type": "string"},
            "repository": {
                "pattern": GHCR_REPOSITORY_PATTERN.replace("^ghcr\\.io/", "^", 1),
                "type": "string",
            },
            "schema_version": {
                "const": STRATEGY_OCI_ARTIFACT_REF_SCHEMA_VERSION,
                "type": "integer",
            },
        },
        "required": [
            "artifact_kind",
            "registry",
            "repository",
            "manifest_digest",
            "artifact_type",
            "config",
            "layers",
        ],
        "title": "StrategyOciArtifactRefV1",
        "type": "object",
    }


def publication_receipt_v1_json_schema(
    artifact_ref_schema: Mapping[str, object] | None = None,
) -> dict[str, object]:
    descriptor = {
        "additionalProperties": False,
        "properties": {
            "digest": {"pattern": _DIGEST_RE.pattern, "type": "string"},
            "media_type": {"minLength": 1, "type": "string"},
            "name": {"pattern": _NAME_RE.pattern, "type": "string"},
            "role": {"minLength": 1, "type": "string"},
            "size_bytes": {"exclusiveMinimum": 0, "type": "integer"},
        },
        "required": ["role", "name", "media_type", "digest", "size_bytes"],
        "type": "object",
    }
    workflow = {
        "additionalProperties": False,
        "properties": {
            "oidc_audience": {"const": GITHUB_OIDC_AUDIENCE, "type": "string"},
            "oidc_issuer": {"const": GITHUB_OIDC_ISSUER, "type": "string"},
            "oidc_subject": {"pattern": OIDC_SUBJECT_PATTERN, "type": "string"},
            "source_ref": {
                "pattern": SOURCE_REF_PATTERN,
                "type": "string",
            },
            "workflow_identity": {
                "pattern": WORKFLOW_REF_PATTERN.replace("^", "^https://github\\.com/", 1),
                "type": "string",
            },
            "workflow_ref": {"pattern": WORKFLOW_REF_PATTERN, "type": "string"},
            "workflow_run_attempt": {"minimum": 1, "type": "integer"},
            "workflow_run_id": {"minimum": 1, "type": "integer"},
        },
        "required": [
            "workflow_identity",
            "workflow_ref",
            "workflow_run_id",
            "workflow_run_attempt",
            "source_ref",
            "oidc_issuer",
            "oidc_subject",
            "oidc_audience",
        ],
        "type": "object",
    }
    properties: dict[str, object] = {
        "artifact_ref": dict(artifact_ref_schema or strategy_oci_artifact_ref_v1_json_schema()),
        "artifact_ref_digest": {"pattern": _SHA256_RE.pattern, "type": "string"},
        "attestation_descriptor_matrix": {
            "items": {"$ref": "#/$defs/PublicationDescriptorV1"},
            "maxItems": 3,
            "minItems": 3,
            "type": "array",
        },
        "attestation_descriptor_matrix_digest": {
            "pattern": _SHA256_RE.pattern,
            "type": "string",
        },
        "attestation_manifest_digest": {
            "pattern": _DIGEST_RE.pattern,
            "type": "string",
        },
        "attestation_manifest_size_bytes": {
            "exclusiveMinimum": 0,
            "type": "integer",
        },
        "blob_readback_verified": {"const": True, "type": "boolean"},
        "discovery_tag": {"pattern": _TAG_RE.pattern, "type": "string"},
        "external_publication_completed": {"type": "boolean"},
        "manifest_readback_verified": {"const": True, "type": "boolean"},
        "producer_commit": {"pattern": _COMMIT_RE.pattern, "type": "string"},
        "producer_repository": {"minLength": 1, "type": "string"},
        "publisher_profile": {
            "const": GITHUB_OIDC_PUBLISHER_PROFILE,
            "type": "string",
        },
        "referrer_verified": {"const": True, "type": "boolean"},
        "release_descriptor_matrix": {
            "items": {"$ref": "#/$defs/PublicationDescriptorV1"},
            "maxItems": len(RELEASE_LAYER_MEDIA_TYPES) + 1,
            "minItems": len(RELEASE_LAYER_MEDIA_TYPES) + 1,
            "type": "array",
        },
        "release_descriptor_matrix_digest": {
            "pattern": _SHA256_RE.pattern,
            "type": "string",
        },
        "release_manifest_digest": {
            "pattern": _DIGEST_RE.pattern,
            "type": "string",
        },
        "release_manifest_size_bytes": {
            "exclusiveMinimum": 0,
            "type": "integer",
        },
        "repository": {
            "pattern": GHCR_REPOSITORY_PATTERN,
            "type": "string",
        },
        "schema_version": {
            "const": PUBLICATION_RECEIPT_V1_SCHEMA_VERSION,
            "type": "string",
        },
        "strategy_coordinate": {"minLength": 1, "type": "string"},
        "tag_is_authority": {"const": False, "type": "boolean"},
        "workflow": workflow,
    }
    return {
        "$defs": {"PublicationDescriptorV1": descriptor},
        "$id": (
            "https://philosophers-stone.the-alephain-guild/contracts/"
            "strategy-artifact-oci-publication-receipt-v1.schema.json"
        ),
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
        "title": "StrategyArtifactOciPublicationReceiptV1",
        "type": "object",
    }


def _put_blob(
    registry: OciRegistryV1,
    repository: str,
    blob: OciBlobV1,
) -> OciDescriptorV1:
    expected = OciDescriptorV1(
        media_type=blob.media_type,
        digest=_digest(blob.content),
        size_bytes=len(blob.content),
    )
    returned = registry.put_blob(repository, expected, blob.content)
    if returned != expected:
        raise OciPublicationError("registry returned blob descriptor differs")
    return expected


def _read_blob(
    registry: OciRegistryV1,
    repository: str,
    blob: OciBlobV1,
    expected: OciDescriptorV1,
) -> None:
    readback = registry.get_blob(repository, expected.digest)
    if readback.descriptor != expected:
        raise OciPublicationError("blob readback returned descriptor differs")
    if readback.content != blob.content:
        raise OciPublicationError("blob readback bytes differ")


def _put_and_read_manifest(
    registry: OciRegistryV1,
    repository: str,
    content: bytes,
    artifact_type: str,
) -> OciDescriptorV1:
    expected = OciDescriptorV1(
        media_type=OCI_MANIFEST_MEDIA_TYPE,
        digest=_digest(content),
        size_bytes=len(content),
        artifact_type=artifact_type,
    )
    returned = registry.put_manifest(
        repository,
        expected.digest,
        OCI_MANIFEST_MEDIA_TYPE,
        content,
    )
    if returned != expected:
        raise OciPublicationError("registry returned manifest descriptor differs")
    readback = registry.get_manifest(repository, expected.digest)
    if readback.descriptor != expected:
        raise OciPublicationError("manifest readback returned descriptor differs")
    if readback.content != content:
        raise OciPublicationError("manifest readback bytes differ")
    return expected


def _verify_subject_binding(
    registry: OciRegistryV1,
    repository: str,
    attestation_manifest: bytes,
    release: OciDescriptorV1,
) -> None:
    """Prove the attestation is bound to this release using only immutable digests.

    The registry's referrers index cannot serve this: GHCR does not implement
    `/v2/<name>/referrers/<digest>` and answers MANIFEST_UNKNOWN for every subject,
    published or not. OCI's fallback for that gap is a mutable tag, which this lane
    must not introduce, because one mutable coordinate inside an otherwise fully
    digest-addressed publication would undo the immutability the receipt asserts.

    Byte equality between what was pushed and what the registry stores is already
    established by the manifest readback, so this does not re-check the registry's
    copy. What it adds is a guard on the binding itself: that the manifest we built
    names this exact release as its subject, and that the named release resolves.
    Without it, a builder that dropped or misfiled `subject` would still publish
    cleanly, since a readback compares wrong bytes against the same wrong bytes.
    """

    document = json.loads(attestation_manifest)
    if not isinstance(document, dict):
        raise OciPublicationError("attestation manifest must be one object")
    subject = document.get("subject")
    if not isinstance(subject, dict):
        raise OciPublicationError("attestation subject binding differs")
    if (
        subject.get("digest") != release.digest
        or subject.get("size") != release.size_bytes
        or subject.get("mediaType") != release.media_type
    ):
        raise OciPublicationError("attestation subject binding differs")
    subject_readback = registry.get_manifest(repository, release.digest)
    if subject_readback.descriptor != release:
        raise OciPublicationError("attestation subject resolves to a different release")


def _release_manifest_bytes(
    config: OciDescriptorV1,
    layers: tuple[OciBlobV1, ...],
    descriptors: tuple[OciDescriptorV1, ...],
) -> bytes:
    return canonical_json_bytes(
        {
            "artifactType": RELEASE_ARTIFACT_TYPE,
            "config": _manifest_descriptor(config),
            "layers": [
                _manifest_descriptor(
                    descriptor,
                    annotations={
                        OCI_ROLE_ANNOTATION: layer.role,
                        OCI_TITLE_ANNOTATION: layer.name,
                    },
                )
                for layer, descriptor in zip(layers, descriptors, strict=True)
            ],
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )


def _attestation_manifest_bytes(
    release_descriptor: OciDescriptorV1,
    blobs: tuple[OciBlobV1, ...],
    descriptors: tuple[OciDescriptorV1, ...],
) -> bytes:
    return canonical_json_bytes(
        {
            "artifactType": SIGSTORE_ARTIFACT_TYPE,
            "config": _manifest_descriptor(descriptors[0]),
            "layers": [
                _manifest_descriptor(
                    descriptor,
                    annotations={
                        OCI_ROLE_ANNOTATION: blob.role,
                        OCI_TITLE_ANNOTATION: blob.name,
                    },
                )
                for blob, descriptor in zip(blobs[1:], descriptors[1:], strict=True)
            ],
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
            "subject": _manifest_descriptor(release_descriptor),
        }
    )


def _manifest_descriptor(
    descriptor: OciDescriptorV1,
    *,
    annotations: Mapping[str, str] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "digest": descriptor.digest,
        "mediaType": descriptor.media_type,
        "size": descriptor.size_bytes,
    }
    if annotations:
        value["annotations"] = dict(sorted(annotations.items()))
    return value


def _matrix_entry(
    blob: OciBlobV1,
    descriptor: OciDescriptorV1,
) -> OciPublicationDescriptorV1:
    return OciPublicationDescriptorV1(
        role=blob.role,
        name=blob.name,
        media_type=descriptor.media_type,
        digest=descriptor.digest,
        size_bytes=descriptor.size_bytes,
    )


def _validate_artifact_ref_matrix(
    artifact_ref: StrategyOciArtifactRefV1,
    matrix: tuple[OciPublicationDescriptorV1, ...],
) -> None:
    if len(matrix) != len(RELEASE_LAYER_MEDIA_TYPES) + 1 or matrix[0].role != "release_config":
        raise ValueError("receipt release descriptor matrix order differs")
    config = matrix[0]
    if (
        config.media_type,
        config.digest,
        config.size_bytes,
    ) != (
        artifact_ref.config.media_type,
        artifact_ref.config.digest,
        artifact_ref.config.size_bytes,
    ):
        raise ValueError("StrategyOciArtifactRefV1 config differs from receipt matrix")
    for layer, item in zip(artifact_ref.layers, matrix[1:], strict=True):
        if layer.to_mapping() != {
            "digest": item.digest,
            "media_type": item.media_type,
            "name": item.name,
            "role": item.role,
            "size_bytes": item.size_bytes,
        }:
            raise ValueError("StrategyOciArtifactRefV1 layer differs from receipt matrix")


def _parse_matrix(value: object, label: str) -> tuple[OciPublicationDescriptorV1, ...]:
    return tuple(
        OciPublicationDescriptorV1(
            role=_as_str(mapping["role"], f"{label}.role"),
            name=_as_str(mapping["name"], f"{label}.name"),
            media_type=_as_str(mapping["media_type"], f"{label}.media_type"),
            digest=_as_str(mapping["digest"], f"{label}.digest"),
            size_bytes=_as_int(mapping["size_bytes"], f"{label}.size_bytes"),
        )
        for mapping in (_as_mapping(item, label) for item in _as_list(value, label))
    )


def _parse_workflow(value: object) -> PublicationWorkflowIdentityV1:
    workflow = _as_mapping(value, "workflow")
    _require_exact_keys(
        workflow,
        {
            "oidc_audience",
            "oidc_issuer",
            "oidc_subject",
            "source_ref",
            "workflow_identity",
            "workflow_ref",
            "workflow_run_attempt",
            "workflow_run_id",
        },
        "workflow",
    )
    return PublicationWorkflowIdentityV1(
        workflow_identity=_as_str(workflow["workflow_identity"], "workflow.workflow_identity"),
        workflow_ref=_as_str(workflow["workflow_ref"], "workflow.workflow_ref"),
        workflow_run_id=_as_int(workflow["workflow_run_id"], "workflow.workflow_run_id"),
        workflow_run_attempt=_as_int(
            workflow["workflow_run_attempt"], "workflow.workflow_run_attempt"
        ),
        source_ref=_as_str(workflow["source_ref"], "workflow.source_ref"),
        oidc_issuer=_as_str(workflow["oidc_issuer"], "workflow.oidc_issuer"),
        oidc_subject=_as_str(workflow["oidc_subject"], "workflow.oidc_subject"),
        oidc_audience=_as_str(workflow["oidc_audience"], "workflow.oidc_audience"),
    )


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _require_digest(value: str, label: str) -> None:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{label} digest differs")


def _require_media_type(value: str, label: str) -> None:
    if "/" not in value or value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{label} media type differs")


def _require_positive(value: int, label: str) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be positive")


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields differ")


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings")
    return cast(Mapping[str, object], value)


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _as_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


__all__ = [
    "STRATEGY_OCI_ARTIFACT_REF_SCHEMA_VERSION",
    "ATTESTATION_CONFIG_V1_MEDIA_TYPE",
    "StrategyOciConfigDescriptorV1",
    "StrategyOciLayerDescriptorV1",
    "OciCandidateV1",
    "OciPublicationBundleV1",
    "OciRegistryV1",
    "GITHUB_OIDC_PUBLISHER_PROFILE",
    "PUBLICATION_RECEIPT_V1_SCHEMA_VERSION",
    "PUBLISH_INPUT_V1_SCHEMA_VERSION",
    "RELEASE_CONFIG_V1_MEDIA_TYPE",
    "StrategyArtifactOciPublicationReceiptV1",
    "StrategyOciArtifactRefV1",
    "strategy_oci_artifact_ref_v1_json_schema",
    "publication_receipt_v1_json_schema",
    "publish_verified_candidate",
]
