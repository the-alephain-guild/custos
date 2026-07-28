#!/usr/bin/env python3
"""Publish one immutable Custos toolkit release candidate via OCI Distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from custos_toolkit.contracts import (
    ImmutableToolkitArtifactBindingV1,
    ToolkitRcMemberV1,
    ToolkitRcOciDescriptorV1,
    ToolkitRcOciPublicationReceiptV1,
    ToolkitRcReceiptManifestV1,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from scripts.toolkit_rc_oci import (
    OCI_ARTIFACT_TYPE,
    OCI_CONFIG_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    OCI_ROLE_ANNOTATION,
    OCI_SOURCE_COORDINATE_ANNOTATION,
    OCI_TITLE_ANNOTATION,
    OciCommitUnknownError,
    OciDescriptor,
    OciRegistryClient,
    OciRegistryError,
    canonical_json,
    sha256_digest,
)

BINDING_FIELDS: Final = (
    "wheel",
    "sbom",
    "contract_schema",
    "contract_asset_index",
    "dependency_lock_evidence",
    "slsa_provenance",
    "sigstore_attestation",
    "toolkit_extraction_receipt",
    "toolkit_typing_closure_receipt",
    "pre_import_verifier_receipt",
)
BUILD_DISTRIBUTIONS: Final = frozenset(
    {"custos-strategy-toolkit", "custos-strategy-toolkit-nautilus"}
)
PRODUCTION_WORKFLOW_REF: Final[
    Literal["the-alephain-guild/custos/.github/workflows/release-toolkit-rc.yml@refs/heads/main"]
] = "the-alephain-guild/custos/.github/workflows/release-toolkit-rc.yml@refs/heads/main"
PRODUCTION_WORKFLOW_IDENTITY: Final = (
    "https://github.com/the-alephain-guild/custos/.github/workflows/"
    "release-toolkit-rc.yml@refs/heads/main"
)
PRODUCTION_OIDC_ISSUER: Final = "https://token.actions.githubusercontent.com"
PRODUCTION_RELEASE_ENVIRONMENT: Final[Literal["toolkit-rc-release"]] = "toolkit-rc-release"
SOURCE_REPOSITORY: Final = "https://github.com/the-alephain-guild/custos"


class ArtifactPublicationError(RuntimeError):
    """The candidate failed closed before OCI publication evidence could exist."""


class ArtifactCoordinateExistsError(ArtifactPublicationError):
    """The RC discovery tag points at a different immutable manifest."""


class _RegistryClient(Protocol):
    registry: str
    repository: str

    def resolve_manifest(self, reference: str) -> str | None: ...

    def upload_blob(self, descriptor: OciDescriptor, content: bytes) -> None: ...

    def put_manifest(self, tag: str, content: bytes) -> str: ...

    def verify_release(
        self,
        *,
        tag: str,
        manifest_digest: str,
        manifest_content: bytes,
        descriptors: tuple[OciDescriptor, ...],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PublicationObject:
    label: str
    coordinate: str
    sha256: str
    content: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class PendingPublicationEvidence:
    candidate_version: str
    oci_coordinate: str
    manifest_digest: str
    toolkit_manifest_sha256: str
    build_manifest_sha256: str
    pending_receipt_path: Path
    status: str
    commit_recovered_by_digest: bool


@dataclass(frozen=True, slots=True)
class ProductionOciAuthorization:
    registry_username: str
    registry_token: str
    workflow_ref: str
    release_environment: str
    oidc_request_url: str
    oidc_request_token: str
    workflow_run_id: int
    workflow_run_attempt: int

    def __post_init__(self) -> None:
        if (
            not self.registry_username
            or not self.registry_token
            or self.workflow_ref != PRODUCTION_WORKFLOW_REF
            or self.release_environment != PRODUCTION_RELEASE_ENVIRONMENT
            or not self.oidc_request_url.startswith("https://")
            or not self.oidc_request_token
            or self.workflow_run_id <= 0
            or self.workflow_run_attempt <= 0
        ):
            raise ArtifactPublicationError("production OCI authorization context differs")

    def receipt_context(self) -> dict[str, object]:
        return {
            "workflow_ref": self.workflow_ref,
            "workflow_identity": PRODUCTION_WORKFLOW_IDENTITY,
            "oidc_issuer": PRODUCTION_OIDC_ISSUER,
            "release_environment": self.release_environment,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_attempt": self.workflow_run_attempt,
        }


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _read_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        content = path.read_bytes()
        document = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactPublicationError(f"{label} is not readable canonical input: {exc}") from exc
    if not isinstance(document, dict):
        raise ArtifactPublicationError(f"{label} must be a JSON object")
    return content, document


def _contains_exact_version(coordinate: str, candidate_version: str) -> bool:
    prefix = coordinate.rsplit("@sha256:", 1)[0]
    return candidate_version in prefix.replace(":", "/").split("/")


def _validate_locked_dependencies(
    member: ToolkitRcMemberV1, build_document: Mapping[str, Any]
) -> None:
    locked = {canonicalize_name(dependency.name): dependency for dependency in member.dependencies}
    raw_requirements = build_document.get("requires_dist")
    if not isinstance(raw_requirements, list) or not all(
        isinstance(value, str) for value in raw_requirements
    ):
        raise ArtifactPublicationError("wheel dependency evidence is invalid")
    for value in raw_requirements:
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise ArtifactPublicationError(
                f"wheel dependency is not a valid requirement: {value}"
            ) from exc
        dependency = locked.get(canonicalize_name(requirement.name))
        if dependency is None:
            raise ArtifactPublicationError(
                f"release manifest does not lock build dependency {requirement.name}"
            )
        try:
            version = Version(dependency.version)
        except InvalidVersion as exc:
            raise ArtifactPublicationError(
                f"release manifest dependency version is invalid: {dependency.version}"
            ) from exc
        if requirement.url is not None or (
            requirement.specifier and version not in requirement.specifier
        ):
            raise ArtifactPublicationError(
                f"release manifest dependency lock does not satisfy build evidence requirement {value}"
            )


def _validate_build_evidence(
    manifest: ToolkitRcReceiptManifestV1,
    build_document: Mapping[str, Any],
) -> None:
    required_flags = {
        "status": "BUILD_CANDIDATE_ONLY",
        "candidate_version": manifest.candidate_version,
        "reproducible": True,
        "registry_accessed": False,
        "ready_receipt_created": False,
        "strategy_release_bom_created": False,
    }
    for name, expected in required_flags.items():
        if build_document.get(name) != expected:
            raise ArtifactPublicationError(f"build evidence has invalid {name}")
    source_commit = build_document.get("source_commit")
    if not isinstance(source_commit, str) or any(
        member.source_commit != source_commit for member in manifest.members
    ):
        raise ArtifactPublicationError("release manifest and build source commits differ")
    builds = build_document.get("builds")
    if not isinstance(builds, dict):
        raise ArtifactPublicationError("build evidence has no isolated builds")
    first = builds.get("build-1")
    second = builds.get("build-2")
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise ArtifactPublicationError("build evidence requires build-1 and build-2")
    if set(first) != BUILD_DISTRIBUTIONS or set(second) != BUILD_DISTRIBUTIONS:
        raise ArtifactPublicationError("build evidence distribution matrix differs")
    members = {member.distribution_name: member for member in manifest.members}
    for distribution in sorted(BUILD_DISTRIBUTIONS):
        first_wheel = first[distribution]
        second_wheel = second[distribution]
        if not isinstance(first_wheel, dict) or first_wheel != second_wheel:
            raise ArtifactPublicationError(
                f"build evidence {distribution} build records are not reproducible"
            )
        member = members[distribution]
        for name, expected in {
            "distribution_name": member.distribution_name,
            "version": member.version,
            "sha256": member.wheel.sha256,
            "size_bytes": member.wheel.size_bytes,
        }.items():
            if first_wheel.get(name) != expected:
                raise ArtifactPublicationError(
                    f"release manifest and build evidence {distribution} {name} differ"
                )
        try:
            python_policy_matches = SpecifierSet(
                str(first_wheel.get("requires_python", ""))
            ) == SpecifierSet(member.python_requires)
        except InvalidSpecifier as exc:
            raise ArtifactPublicationError(
                f"build evidence {distribution} Python policy is invalid"
            ) from exc
        if not python_policy_matches:
            raise ArtifactPublicationError(
                f"release manifest and build evidence {distribution} requires_python differ"
            )
        if set(first_wheel.get("top_level_modules", ())) != set(member.top_level_modules):
            raise ArtifactPublicationError(
                f"release manifest and build evidence {distribution} top-level modules differ"
            )
        _validate_locked_dependencies(member, first_wheel)


def _binding_objects(
    *,
    manifest: ToolkitRcReceiptManifestV1,
    object_sources: Mapping[str, Path],
) -> list[PublicationObject]:
    expected_coordinates: set[str] = set()
    objects: dict[str, PublicationObject] = {}
    for member in manifest.members:
        for field_name in BINDING_FIELDS:
            binding = getattr(member, field_name)
            if not isinstance(binding, ImmutableToolkitArtifactBindingV1):
                raise ArtifactPublicationError(f"invalid release manifest binding {field_name}")
            coordinate = binding.coordinate
            expected_coordinates.add(coordinate)
            if not _contains_exact_version(coordinate, manifest.candidate_version):
                raise ArtifactPublicationError(
                    f"artifact coordinate does not contain exact {manifest.candidate_version}"
                )
            source = object_sources.get(coordinate)
            if source is None:
                raise ArtifactPublicationError(
                    f"missing local source for {member.role.value}.{field_name}"
                )
            try:
                content = Path(source).read_bytes()
            except OSError as exc:
                raise ArtifactPublicationError(
                    f"cannot read local source for {member.role.value}.{field_name}: {exc}"
                ) from exc
            if _sha256(content) != binding.sha256 or len(content) != binding.size_bytes:
                raise ArtifactPublicationError(
                    f"local source digest or size differs for {member.role.value}.{field_name}"
                )
            existing = objects.get(coordinate)
            if existing is not None:
                if existing.sha256 != binding.sha256 or existing.content != content:
                    raise ArtifactPublicationError("one coordinate binds different local objects")
                continue
            objects[coordinate] = PublicationObject(
                label=f"{member.role.value}.{field_name}",
                coordinate=coordinate,
                sha256=binding.sha256,
                content=content,
            )
    supplied_coordinates = set(object_sources)
    if supplied_coordinates != expected_coordinates:
        raise ArtifactPublicationError(
            "object source matrix differs; "
            f"missing={sorted(expected_coordinates - supplied_coordinates)}, "
            f"unexpected={sorted(supplied_coordinates - expected_coordinates)}"
        )
    return list(objects.values())


def _verify_production_attestation(
    *,
    manifest: ToolkitRcReceiptManifestV1,
    object_sources: Mapping[str, Path],
    authorization: ProductionOciAuthorization | None,
) -> bool:
    if authorization is None:
        return False
    provenance_coordinates = {member.slsa_provenance.coordinate for member in manifest.members}
    bundle_coordinates = {member.sigstore_attestation.coordinate for member in manifest.members}
    if len(provenance_coordinates) != 1 or len(bundle_coordinates) != 1:
        raise ArtifactPublicationError("production members must share one signed provenance")
    try:
        verification = subprocess.run(
            [
                "sigstore",
                "verify",
                "identity",
                "--bundle",
                str(object_sources[next(iter(bundle_coordinates))]),
                "--cert-identity",
                PRODUCTION_WORKFLOW_IDENTITY,
                "--cert-oidc-issuer",
                PRODUCTION_OIDC_ISSUER,
                str(object_sources[next(iter(provenance_coordinates))]),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ArtifactPublicationError(
            f"production Sigstore verification command is unavailable: {exc}"
        ) from exc
    if verification.returncode != 0:
        raise ArtifactPublicationError(
            f"production Sigstore provenance verification failed: {verification.stderr.strip()}"
        )
    return True


def _provenance_object(
    *, label: str, candidate_version: str, filename: str, content: bytes
) -> PublicationObject:
    digest = _sha256(content)
    return PublicationObject(
        label=label,
        coordinate=(
            f"artifact://custos/toolkit-rc/{candidate_version}/provenance/{filename}"
            f"@sha256:{digest}"
        ),
        sha256=digest,
        content=content,
    )


def _layer_descriptor(artifact: PublicationObject) -> OciDescriptor:
    title = artifact.coordinate.rsplit("@sha256:", 1)[0].rsplit("/", 1)[-1]
    media_type = (
        "application/vnd.pypa.wheel"
        if title.endswith(".whl")
        else "application/json"
        if title.endswith((".json", ".json.sha256"))
        else "application/octet-stream"
    )
    return OciDescriptor(
        media_type=media_type,
        digest=f"sha256:{artifact.sha256}",
        size=artifact.size_bytes,
        annotations={
            OCI_TITLE_ANNOTATION: title,
            OCI_ROLE_ANNOTATION: artifact.label,
            OCI_SOURCE_COORDINATE_ANNOTATION: artifact.coordinate,
        },
    )


def _model_descriptor(descriptor: OciDescriptor) -> ToolkitRcOciDescriptorV1:
    return ToolkitRcOciDescriptorV1(
        media_type=descriptor.media_type,
        digest=descriptor.digest,
        size_bytes=descriptor.size,
        title=descriptor.annotations[OCI_TITLE_ANNOTATION],
        role=descriptor.annotations[OCI_ROLE_ANNOTATION],
        source_coordinate=descriptor.annotations.get(OCI_SOURCE_COORDINATE_ANNOTATION),
    )


def publish_toolkit_rc_candidate(
    *,
    manifest_path: Path,
    build_manifest_path: Path,
    object_sources: Mapping[str, Path],
    registry: str,
    repository: str,
    pending_receipt_path: Path,
    production_authorization: ProductionOciAuthorization | None = None,
    registry_client: _RegistryClient | None = None,
) -> PendingPublicationEvidence:
    """Commit one OCI manifest, then prove every descriptor by digest readback."""

    pending_receipt_path = pending_receipt_path.resolve()
    if pending_receipt_path.exists():
        raise ArtifactPublicationError("pending publication evidence must not be overwritten")
    manifest_content, manifest_document = _read_json(manifest_path, "release manifest")
    try:
        manifest = ToolkitRcReceiptManifestV1.model_validate(manifest_document)
    except ValueError as exc:
        raise ArtifactPublicationError(f"release manifest contract differs: {exc}") from exc
    build_content, build_document = _read_json(build_manifest_path, "build manifest")
    _validate_build_evidence(manifest, build_document)
    objects = _binding_objects(manifest=manifest, object_sources=object_sources)
    production_signature_verified = _verify_production_attestation(
        manifest=manifest,
        object_sources=object_sources,
        authorization=production_authorization,
    )
    objects.extend(
        (
            _provenance_object(
                label="release_manifest",
                candidate_version=manifest.candidate_version,
                filename="toolkit-rc-receipt-manifest.json",
                content=manifest_content,
            ),
            _provenance_object(
                label="build_manifest",
                candidate_version=manifest.candidate_version,
                filename="toolkit-rc-build-manifest-input.json",
                content=build_content,
            ),
        )
    )
    objects.sort(key=lambda artifact: artifact.coordinate)
    layers = tuple(_layer_descriptor(artifact) for artifact in objects)
    production_context = (
        production_authorization.receipt_context() if production_authorization is not None else None
    )
    config_content = canonical_json(
        {
            "schema_version": "alephain.custos.toolkit-rc-oci-config.v1",
            "candidate_version": manifest.candidate_version,
            "source_repository": SOURCE_REPOSITORY,
            "source_commit": build_document["source_commit"],
            "source_date_epoch": build_document["source_date_epoch"],
            "registry": registry.removeprefix("https://").removeprefix("http://").rstrip("/"),
            "repository": repository,
            "tag": manifest.candidate_version,
            "toolkit_manifest_sha256": _sha256(manifest_content),
            "build_manifest_sha256": _sha256(build_content),
            "production_credentials_used": production_authorization is not None,
            "production_signature_verified": production_signature_verified,
            "production_context": production_context,
        }
    )
    config_descriptor = OciDescriptor(
        media_type=OCI_CONFIG_MEDIA_TYPE,
        digest=sha256_digest(config_content),
        size=len(config_content),
        annotations={
            OCI_TITLE_ANNOTATION: "toolkit-rc-config.json",
            OCI_ROLE_ANNOTATION: "release_config",
        },
    )
    oci_manifest_content = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": OCI_ARTIFACT_TYPE,
            "config": config_descriptor.document(),
            "layers": [descriptor.document() for descriptor in layers],
            "annotations": {
                "org.opencontainers.image.source": SOURCE_REPOSITORY,
                "org.opencontainers.image.revision": build_document["source_commit"],
                "org.opencontainers.image.version": manifest.candidate_version,
            },
        }
    )
    manifest_digest = sha256_digest(oci_manifest_content)
    client: _RegistryClient = registry_client or OciRegistryClient(
        registry,
        repository,
        username=(
            production_authorization.registry_username
            if production_authorization is not None
            else os.environ.get("CUSTOS_TOOLKIT_OCI_USERNAME", "")
        ),
        token=(
            production_authorization.registry_token
            if production_authorization is not None
            else os.environ.get("CUSTOS_TOOLKIT_OCI_TOKEN", "")
        ),
    )
    expected_registry = registry.removeprefix("https://").removeprefix("http://").rstrip("/")
    if client.registry != expected_registry or client.repository != repository:
        raise ArtifactPublicationError("OCI client authority coordinate differs")

    tag = manifest.candidate_version
    try:
        observed = client.resolve_manifest(tag)
    except OciRegistryError as exc:
        raise ArtifactPublicationError(f"OCI tag preflight failed: {exc}") from exc
    recovered = False
    if observed is not None and observed != manifest_digest:
        raise ArtifactCoordinateExistsError(
            f"OCI tag {tag} already resolves to a different manifest; use the next RC"
        )
    if observed == manifest_digest:
        recovered = True
    else:
        try:
            client.upload_blob(config_descriptor, config_content)
            for descriptor, artifact in zip(layers, objects, strict=True):
                client.upload_blob(descriptor, artifact.content)
        except OciRegistryError as exc:
            raise ArtifactPublicationError(
                f"OCI blob staging failed before authority: {exc}"
            ) from exc
        try:
            committed = client.put_manifest(tag, oci_manifest_content)
            if committed != manifest_digest:
                raise ArtifactPublicationError("OCI manifest commit ACK digest differs")
        except OciCommitUnknownError:
            recovered = True
            try:
                observed = client.resolve_manifest(tag)
                if observed is None:
                    try:
                        observed = client.put_manifest(tag, oci_manifest_content)
                    except OciCommitUnknownError:
                        observed = client.resolve_manifest(tag)
            except OciRegistryError as exc:
                raise ArtifactPublicationError(
                    f"OCI manifest commit recovery failed: {exc}"
                ) from exc
            if observed != manifest_digest:
                if observed is not None:
                    raise ArtifactCoordinateExistsError(
                        "OCI manifest commit recovery observed tag drift"
                    ) from None
                raise ArtifactPublicationError(
                    "OCI manifest commit outcome remains unknown without digest authority"
                ) from None
        except OciRegistryError as exc:
            raise ArtifactPublicationError(f"OCI manifest commit failed: {exc}") from exc
    try:
        client.verify_release(
            tag=tag,
            manifest_digest=manifest_digest,
            manifest_content=oci_manifest_content,
            descriptors=(config_descriptor, *layers),
        )
    except OciRegistryError as exc:
        raise ArtifactPublicationError(f"OCI digest readback failed: {exc}") from exc

    receipt = ToolkitRcOciPublicationReceiptV1(
        status=(
            "PENDING_AUTHORITY_REGISTRATION"
            if production_authorization is not None
            else "PENDING_PROTECTED_RELEASE"
        ),
        candidate_version=manifest.candidate_version,
        source_commit=str(build_document["source_commit"]),
        source_date_epoch=int(build_document["source_date_epoch"]),
        registry=client.registry,
        repository=repository,
        tag=tag,
        oci_coordinate=f"{client.registry}/{repository}@{manifest_digest}",
        manifest_digest=manifest_digest,
        manifest_size_bytes=len(oci_manifest_content),
        config=_model_descriptor(config_descriptor),
        layers=tuple(_model_descriptor(descriptor) for descriptor in layers),
        production_credentials_used=production_authorization is not None,
        production_signature_verified=production_signature_verified,
        workflow_ref=PRODUCTION_WORKFLOW_REF if production_authorization is not None else None,
        workflow_identity=(
            PRODUCTION_WORKFLOW_IDENTITY if production_authorization is not None else None
        ),
        oidc_issuer=PRODUCTION_OIDC_ISSUER if production_authorization is not None else None,
        release_environment=(
            PRODUCTION_RELEASE_ENVIRONMENT if production_authorization is not None else None
        ),
        workflow_run_id=(
            production_authorization.workflow_run_id
            if production_authorization is not None
            else None
        ),
        workflow_run_attempt=(
            production_authorization.workflow_run_attempt
            if production_authorization is not None
            else None
        ),
    )
    pending_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with pending_receipt_path.open("xb") as output:
            output.write(_pretty_json(receipt.model_dump(mode="json")))
    except FileExistsError as exc:
        raise ArtifactPublicationError(
            "pending publication evidence must not be overwritten"
        ) from exc
    return PendingPublicationEvidence(
        candidate_version=manifest.candidate_version,
        oci_coordinate=receipt.oci_coordinate,
        manifest_digest=manifest_digest,
        toolkit_manifest_sha256=_sha256(manifest_content),
        build_manifest_sha256=_sha256(build_content),
        pending_receipt_path=pending_receipt_path,
        status=receipt.status,
        commit_recovered_by_digest=recovered,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--build-manifest", required=True, type=Path)
    parser.add_argument("--object-sources", required=True, type=Path)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pending-receipt", required=True, type=Path)
    parser.add_argument("--production-release-runner", action="store_true")
    return parser


def _production_authorization_from_environment() -> ProductionOciAuthorization:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY") != "the-alephain-guild/custos"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ArtifactPublicationError(
            "production OCI publication is restricted to protected Custos main"
        )
    try:
        workflow_run_id = int(os.environ.get("GITHUB_RUN_ID", ""))
        workflow_run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    except ValueError as exc:
        raise ArtifactPublicationError("production workflow run identity is invalid") from exc
    return ProductionOciAuthorization(
        registry_username=os.environ.get("CUSTOS_TOOLKIT_OCI_USERNAME", ""),
        registry_token=os.environ.get("CUSTOS_TOOLKIT_OCI_TOKEN", ""),
        workflow_ref=os.environ.get("GITHUB_WORKFLOW_REF", ""),
        release_environment=os.environ.get("CUSTOS_TOOLKIT_RELEASE_ENVIRONMENT", ""),
        oidc_request_url=os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
        oidc_request_token=os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    _, source_document = _read_json(arguments.object_sources, "object source map")
    source_root = arguments.object_sources.resolve().parent
    object_sources = {
        coordinate: (Path(path) if Path(path).is_absolute() else source_root / Path(path))
        for coordinate, path in source_document.items()
        if isinstance(coordinate, str) and isinstance(path, str)
    }
    if len(object_sources) != len(source_document):
        raise ArtifactPublicationError("object source map must contain string paths")
    authorization = (
        _production_authorization_from_environment()
        if arguments.production_release_runner
        else None
    )
    if authorization is not None and arguments.registry.startswith("http://"):
        raise ArtifactPublicationError("production OCI registry must use HTTPS")
    evidence = publish_toolkit_rc_candidate(
        manifest_path=arguments.manifest,
        build_manifest_path=arguments.build_manifest,
        object_sources=object_sources,
        registry=arguments.registry,
        repository=arguments.repository,
        pending_receipt_path=arguments.pending_receipt,
        production_authorization=authorization,
    )
    result = {
        "candidate_version": evidence.candidate_version,
        "pending_receipt_path": str(evidence.pending_receipt_path),
        "oci_coordinate": evidence.oci_coordinate,
        "manifest_digest": evidence.manifest_digest,
        "commit_recovered_by_digest": evidence.commit_recovered_by_digest,
        "status": evidence.status,
    }
    print(json.dumps(result, sort_keys=True))
    if arguments.production_release_runner and os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            for name in ("oci_coordinate", "manifest_digest"):
                output.write(f"{name}={result[name]}\n")
    return 0


__all__ = [
    "ArtifactCoordinateExistsError",
    "ArtifactPublicationError",
    "PendingPublicationEvidence",
    "ProductionOciAuthorization",
    "PublicationObject",
    "publish_toolkit_rc_candidate",
]


if __name__ == "__main__":
    raise SystemExit(main())
