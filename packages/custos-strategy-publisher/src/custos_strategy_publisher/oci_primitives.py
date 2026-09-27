"""Content-addressed OCI publication contract for signed strategy releases."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .model import canonical_json_bytes, sha256_hex

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
RELEASE_ARTIFACT_TYPE = "application/vnd.alephain.strategy-release.v1"
RELEASE_CONFIG_MEDIA_TYPE = "application/vnd.alephain.strategy-release.config.v1+json"
SIGSTORE_ARTIFACT_TYPE = "application/vnd.alephain.strategy-release.attestation.v1"
SIGSTORE_CONFIG_MEDIA_TYPE = "application/vnd.alephain.strategy-release.attestation.config.v1+json"
SIGSTORE_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
ATTESTATION_REF_MEDIA_TYPE = "application/vnd.alephain.artifact-attestation-ref.v1+json"
PUBLICATION_RECEIPT_SCHEMA_VERSION = "alephain.strategy-artifact-oci-publication-receipt.v1"
OCI_ROLE_ANNOTATION = "dev.alephain.layer.role"
OCI_TITLE_ANNOTATION = "org.opencontainers.image.title"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "sigstore"

# Which repository publishes, and through which workflow, is the producer's to say
# and the trust policies' to accept; these only check that each is well formed.
GHCR_REPOSITORY_PATTERN = r"^ghcr\.io/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:/[a-z0-9._-]+)+$"
WORKFLOW_REF_PATTERN = (
    r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml"
    r"@(?:refs/(?:heads|tags)/\S+|[0-9a-f]{40})$"
)
SOURCE_REF_PATTERN = r"^refs/(?:heads|tags)/\S+$"
OIDC_SUBJECT_PATTERN = r"^repo:[A-Za-z0-9-]+/[A-Za-z0-9._-]+:ref:(refs/(?:heads|tags)/\S+)$"
_GHCR_REPOSITORY_RE = re.compile(GHCR_REPOSITORY_PATTERN)
_WORKFLOW_REF_RE = re.compile(WORKFLOW_REF_PATTERN)
_SOURCE_REF_RE = re.compile(SOURCE_REF_PATTERN)
_OIDC_SUBJECT_RE = re.compile(OIDC_SUBJECT_PATTERN)


def require_ghcr_repository(repository: str) -> str:
    """Return `repository` if it names a GHCR repository, such as ghcr.io/owner/name."""

    if not _GHCR_REPOSITORY_RE.fullmatch(repository):
        raise ValueError(f"not a GHCR repository: {repository!r}")
    return repository


RELEASE_LAYER_MEDIA_TYPES: dict[str, str] = {
    "strategy_artifact": "application/vnd.alephain.strategy-wheel.v1+zip",
    "strategy_manifest": "application/vnd.alephain.strategy-manifest.v1+json",
    "strategy_artifact_ref": "application/vnd.alephain.strategy-artifact-ref.v1+json",
    "strategy_release_bom": "application/vnd.alephain.strategy-release-bom.v1+json",
    "strategy_sbom": "application/spdx+json",
    "strategy_release_statement": "application/vnd.in-toto+json",
}

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RC_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,110}rc[1-9][0-9]*$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class OciPublicationError(RuntimeError):
    """A registry response violates the immutable publication contract."""


def _require_non_empty(value: str, label: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")


def _require_digest(value: str, label: str) -> None:
    if not _SHA256_DIGEST_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 OCI digest")


def _digest(content: bytes) -> str:
    return f"sha256:{sha256_hex(content)}"


def _exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"{label} keys differ: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


@dataclass(frozen=True, slots=True)
class OciDescriptorV1:
    media_type: str
    digest: str
    size_bytes: int
    artifact_type: str | None = None
    annotations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.media_type, "descriptor.media_type")
        _require_digest(self.digest, "descriptor.digest")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("descriptor.size_bytes must be a non-negative integer")
        if self.artifact_type is not None:
            _require_non_empty(self.artifact_type, "descriptor.artifact_type")
        keys = [key for key, _ in self.annotations]
        if len(keys) != len(set(keys)):
            raise ValueError("descriptor annotations must have unique keys")
        for key, value in self.annotations:
            _require_non_empty(key, "descriptor annotation key")
            _require_non_empty(value, "descriptor annotation value")

    def to_mapping(self) -> dict[str, object]:
        value: dict[str, object] = {
            "digest": self.digest,
            "mediaType": self.media_type,
            "size": self.size_bytes,
        }
        if self.artifact_type is not None:
            value["artifactType"] = self.artifact_type
        if self.annotations:
            value["annotations"] = dict(self.annotations)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OciDescriptorV1:
        allowed = {"mediaType", "digest", "size", "artifactType", "annotations"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"descriptor has unknown keys: {sorted(unknown)}")
        if not {"mediaType", "digest", "size"}.issubset(value):
            raise ValueError("descriptor is missing required keys")
        media_type = value["mediaType"]
        digest = value["digest"]
        size = value["size"]
        artifact_type = value.get("artifactType")
        annotations_value = value.get("annotations", {})
        if not isinstance(media_type, str) or not isinstance(digest, str):
            raise TypeError("descriptor mediaType and digest must be strings")
        if not isinstance(size, int) or isinstance(size, bool):
            raise TypeError("descriptor size must be an integer")
        if artifact_type is not None and not isinstance(artifact_type, str):
            raise TypeError("descriptor artifactType must be a string")
        if not isinstance(annotations_value, Mapping) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in annotations_value.items()
        ):
            raise TypeError("descriptor annotations must be a string mapping")
        return cls(
            media_type=media_type,
            digest=digest,
            size_bytes=size,
            artifact_type=artifact_type,
            annotations=tuple(sorted(annotations_value.items())),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class OciBlobV1:
    role: str
    name: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if not _ROLE_RE.fullmatch(self.role):
            raise ValueError("blob.role must be a lowercase stable identifier")
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError("blob.name must be a canonical safe filename")
        _require_non_empty(self.media_type, "blob.media_type")
        if not isinstance(self.content, bytes):
            raise TypeError("blob.content must be exact bytes")

    @property
    def descriptor(self) -> OciDescriptorV1:
        return OciDescriptorV1(
            media_type=self.media_type,
            digest=_digest(self.content),
            size_bytes=len(self.content),
            annotations=(
                (OCI_ROLE_ANNOTATION, self.role),
                (OCI_TITLE_ANNOTATION, self.name),
            ),
        )


@dataclass(frozen=True, slots=True)
class OciBlobReadbackV1:
    descriptor: OciDescriptorV1
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("blob readback content must be exact bytes")


@dataclass(frozen=True, slots=True)
class OciManifestReadbackV1:
    descriptor: OciDescriptorV1
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("manifest readback content must be exact bytes")


class OciRegistryV1(Protocol):
    """Minimal distribution port; authentication and HTTP stay in the adapter."""

    def put_blob(
        self,
        repository: str,
        descriptor: OciDescriptorV1,
        content: bytes,
    ) -> OciDescriptorV1: ...

    def get_blob(self, repository: str, digest: str) -> OciBlobReadbackV1: ...

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
class PublicationWorkflowIdentityV1:
    workflow_identity: str
    workflow_ref: str
    workflow_run_id: int
    workflow_run_attempt: int
    source_ref: str
    oidc_issuer: str
    oidc_subject: str
    oidc_audience: str

    def __post_init__(self) -> None:
        if self.oidc_issuer != GITHUB_OIDC_ISSUER:
            raise ValueError(f"oidc_issuer must be {GITHUB_OIDC_ISSUER}")
        if self.oidc_audience != GITHUB_OIDC_AUDIENCE:
            raise ValueError(f"oidc_audience must be {GITHUB_OIDC_AUDIENCE}")
        if not _WORKFLOW_REF_RE.fullmatch(self.workflow_ref):
            raise ValueError("workflow_ref must name a workflow file in a repository at a ref")
        if self.workflow_identity != f"https://github.com/{self.workflow_ref}":
            raise ValueError("workflow_identity must be the workflow_ref on github.com")
        if not _SOURCE_REF_RE.fullmatch(self.source_ref):
            raise ValueError("source_ref must be a branch or tag ref")
        # The subject names the repository that ran the workflow, which for a
        # reusable workflow is not the workflow's own repository; its ref must be
        # the source ref.
        subject = _OIDC_SUBJECT_RE.fullmatch(self.oidc_subject)
        if subject is None or subject.group(1) != self.source_ref:
            raise ValueError("oidc_subject must be repo:<owner>/<repo>:ref:<source_ref>")
        for field in ("workflow_run_id", "workflow_run_attempt"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")

    def to_mapping(self) -> dict[str, object]:
        return {
            "oidc_audience": self.oidc_audience,
            "oidc_issuer": self.oidc_issuer,
            "oidc_subject": self.oidc_subject,
            "source_ref": self.source_ref,
            "workflow_identity": self.workflow_identity,
            "workflow_ref": self.workflow_ref,
            "workflow_run_attempt": self.workflow_run_attempt,
            "workflow_run_id": self.workflow_run_id,
        }


@dataclass(frozen=True, slots=True)
class OciPublicationDescriptorV1:
    role: str
    name: str
    media_type: str
    digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not _ROLE_RE.fullmatch(self.role):
            raise ValueError("publication descriptor role differs")
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError("publication descriptor name differs")
        _require_non_empty(self.media_type, "publication descriptor media_type")
        _require_digest(self.digest, "publication descriptor digest")
        if isinstance(self.size_bytes, bool) or self.size_bytes <= 0:
            raise ValueError("publication descriptor size_bytes must be positive")

    @classmethod
    def from_blob(
        cls,
        blob: OciBlobV1,
        descriptor: OciDescriptorV1,
    ) -> OciPublicationDescriptorV1:
        if descriptor != blob.descriptor:
            raise ValueError("publication descriptor does not match exact blob")
        return cls(
            role=blob.role,
            name=blob.name,
            media_type=descriptor.media_type,
            digest=descriptor.digest,
            size_bytes=descriptor.size_bytes,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "name": self.name,
            "role": self.role,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> OciPublicationDescriptorV1:
        expected = frozenset({"role", "name", "media_type", "digest", "size_bytes"})
        _exact_keys(value, expected, "OciPublicationDescriptorV1")
        if not all(
            isinstance(value[field], str) for field in ("role", "name", "media_type", "digest")
        ):
            raise TypeError("publication descriptor string fields must be strings")
        size_bytes = value["size_bytes"]
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            raise TypeError("publication descriptor size_bytes must be an integer")
        return cls(
            role=value["role"],  # type: ignore[arg-type]
            name=value["name"],  # type: ignore[arg-type]
            media_type=value["media_type"],  # type: ignore[arg-type]
            digest=value["digest"],  # type: ignore[arg-type]
            size_bytes=size_bytes,
        )


def descriptor_matrix_digest(
    matrix: tuple[OciPublicationDescriptorV1, ...],
) -> str:
    return sha256_hex(canonical_json_bytes([item.to_mapping() for item in matrix]))
