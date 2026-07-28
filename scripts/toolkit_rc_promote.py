#!/usr/bin/env python3
"""Read one toolkit OCI manifest by digest and emit a READY V1 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final, Protocol

from custos_toolkit.contracts import (
    ImmutableToolkitArtifactBindingV1,
    ToolkitRcAuthorityReadyReceiptV1,
    ToolkitRcOciDescriptorV1,
    ToolkitRcOciPublicationReceiptV1,
    ToolkitRcReceiptManifestV1,
)

from scripts.toolkit_rc_oci import (
    OCI_ROLE_ANNOTATION,
    OCI_SOURCE_COORDINATE_ANNOTATION,
    OCI_TITLE_ANNOTATION,
    OciDescriptor,
    OciRegistryClient,
    OciRegistryError,
    parse_manifest_document,
    sha256_digest,
)
from scripts.toolkit_rc_publish import ArtifactPublicationError, _validate_build_evidence

WORKFLOW_IDENTITY: Final = (
    "https://github.com/the-alephain-guild/custos/.github/workflows/"
    "release-toolkit-rc.yml@refs/heads/main"
)
WORKFLOW_REF: Final = (
    "the-alephain-guild/custos/.github/workflows/release-toolkit-rc.yml@refs/heads/main"
)
OIDC_ISSUER: Final = "https://token.actions.githubusercontent.com"
STABLE_READY_PATH: Final = Path("docs/authority/receipts/custos-toolkit-rc-authority-v1.json")
PREREQUISITE_PATHS: Final = (
    Path("docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"),
    Path("docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json"),
    Path("docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"),
)
SIGNED_SUBJECT_FIELDS: Final = (
    "wheel",
    "sbom",
    "contract_schema",
    "contract_asset_index",
    "dependency_lock_evidence",
    "toolkit_extraction_receipt",
    "toolkit_typing_closure_receipt",
    "pre_import_verifier_receipt",
)
SHARED_EVIDENCE_FIELDS: Final = (
    "contract_schema",
    "contract_asset_index",
    "dependency_lock_evidence",
    "slsa_provenance",
    "sigstore_attestation",
    "toolkit_extraction_receipt",
    "toolkit_typing_closure_receipt",
    "pre_import_verifier_receipt",
)
BUILD_TYPE: Final = "https://custos.the-alephain-guild/build-types/toolkit-rc/v1"
SOURCE_REPOSITORY: Final = "https://github.com/the-alephain-guild/custos"


class ToolkitRcPromotionError(RuntimeError):
    """OCI evidence cannot be promoted without weakening authority."""


class _RegistryClient(Protocol):
    registry: str
    repository: str

    def resolve_manifest(self, reference: str) -> str | None: ...

    def read_manifest(self, reference: str) -> tuple[bytes, str]: ...

    def read_blob(self, digest: str) -> bytes: ...


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def require_production_publication_receipt(
    receipt: ToolkitRcOciPublicationReceiptV1,
) -> None:
    if (
        receipt.status != "PENDING_AUTHORITY_REGISTRATION"
        or receipt.ready
        or receipt.handoff_ready
        or not receipt.production_credentials_used
        or not receipt.production_signature_verified
        or receipt.authority_registered
        or receipt.workflow_ref != WORKFLOW_REF
        or receipt.workflow_identity != WORKFLOW_IDENTITY
        or receipt.oidc_issuer != OIDC_ISSUER
    ):
        raise ToolkitRcPromotionError("OCI publication receipt is not production evidence")


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolkitRcPromotionError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ToolkitRcPromotionError(f"{label} must be a JSON object")
    return document


def _coordinate_filename(binding: ImmutableToolkitArtifactBindingV1) -> str:
    return binding.coordinate.rsplit("@sha256:", 1)[0].rsplit("/", 1)[-1]


def _shared_binding(
    manifest: ToolkitRcReceiptManifestV1, field_name: str
) -> ImmutableToolkitArtifactBindingV1:
    bindings = tuple(getattr(member, field_name) for member in manifest.members)
    identities = {(value.coordinate, value.sha256, value.size_bytes) for value in bindings}
    if len(identities) != 1:
        raise ToolkitRcPromotionError(
            f"toolkit members do not share one {field_name} authority object"
        )
    return bindings[0]


def _subject_matrix(values: object, label: str) -> dict[str, str]:
    if not isinstance(values, list):
        raise ToolkitRcPromotionError(f"{label} must be an array")
    result: dict[str, str] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ToolkitRcPromotionError(f"{label} contains an invalid subject")
        digest = value.get("digest")
        if not isinstance(digest, dict) or set(digest) != {"sha256"}:
            raise ToolkitRcPromotionError(f"{label} contains a non-SHA-256 subject")
        sha256 = digest.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ToolkitRcPromotionError(f"{label} contains an invalid SHA-256 digest")
        name = value["name"]
        if name in result:
            raise ToolkitRcPromotionError(f"{label} contains a duplicate subject name")
        result[name] = sha256
    return result


def _validate_dependency_lock(
    *,
    manifest: ToolkitRcReceiptManifestV1,
    objects: dict[str, bytes],
    source_commit: str,
) -> str:
    binding = _shared_binding(manifest, "dependency_lock_evidence")
    document = _json_object(objects[binding.coordinate], "dependency lock evidence")
    if (
        document.get("candidate_version") != manifest.candidate_version
        or document.get("source_commit") != source_commit
    ):
        raise ToolkitRcPromotionError("dependency lock identity differs")
    uv_lock_sha256 = document.get("uv_lock_sha256")
    if not isinstance(uv_lock_sha256, str) or len(uv_lock_sha256) != 64:
        raise ToolkitRcPromotionError("dependency lock lacks the committed uv.lock digest")
    distributions = document.get("distributions")
    if not isinstance(distributions, dict):
        raise ToolkitRcPromotionError("dependency lock distribution matrix is absent")
    if set(distributions) != {member.distribution_name for member in manifest.members}:
        raise ToolkitRcPromotionError("dependency lock distribution matrix differs")
    for member in manifest.members:
        records = distributions[member.distribution_name]
        if not isinstance(records, list) or not all(isinstance(value, dict) for value in records):
            raise ToolkitRcPromotionError("dependency lock record matrix is invalid")
        actual = {
            (value.get("name"), value.get("version"), value.get("requirement")) for value in records
        }
        expected = {(value.name, value.version, value.requirement) for value in member.dependencies}
        if actual != expected:
            raise ToolkitRcPromotionError(
                f"{member.distribution_name} dependency lock differs from manifest"
            )
    return uv_lock_sha256


def _validate_signed_provenance(
    *,
    manifest: ToolkitRcReceiptManifestV1,
    build: dict[str, Any],
    build_content: bytes,
    objects: dict[str, bytes],
    source_commit: str,
    source_date_epoch: int,
) -> None:
    try:
        _validate_build_evidence(manifest, build)
    except ArtifactPublicationError as exc:
        raise ToolkitRcPromotionError(f"build evidence differs during promotion: {exc}") from exc
    for field_name in SHARED_EVIDENCE_FIELDS:
        _shared_binding(manifest, field_name)
    uv_lock_sha256 = _validate_dependency_lock(
        manifest=manifest,
        objects=objects,
        source_commit=source_commit,
    )
    provenance_binding = _shared_binding(manifest, "slsa_provenance")
    provenance = _json_object(objects[provenance_binding.coordinate], "signed SLSA provenance")
    if (
        provenance.get("_type") != "https://in-toto.io/Statement/v1"
        or provenance.get("predicateType") != "https://slsa.dev/provenance/v1"
    ):
        raise ToolkitRcPromotionError("signed provenance statement type differs")
    expected_subjects: dict[str, str] = {}
    for member in manifest.members:
        for field_name in SIGNED_SUBJECT_FIELDS:
            binding = getattr(member, field_name)
            filename = _coordinate_filename(binding)
            previous = expected_subjects.setdefault(filename, binding.sha256)
            if previous != binding.sha256:
                raise ToolkitRcPromotionError(
                    "manifest maps one signed provenance subject name to different bytes"
                )
    if _subject_matrix(provenance.get("subject"), "signed provenance subjects") != (
        expected_subjects
    ):
        raise ToolkitRcPromotionError("signed provenance subject matrix differs")
    predicate = provenance.get("predicate")
    if not isinstance(predicate, dict):
        raise ToolkitRcPromotionError("signed provenance predicate is absent")
    build_definition = predicate.get("buildDefinition")
    run_details = predicate.get("runDetails")
    if not isinstance(build_definition, dict) or not isinstance(run_details, dict):
        raise ToolkitRcPromotionError("signed provenance build identity is absent")
    if (
        build_definition.get("buildType") != BUILD_TYPE
        or build_definition.get("externalParameters")
        != {
            "candidate_version": manifest.candidate_version,
            "source_commit": source_commit,
            "source_date_epoch": source_date_epoch,
        }
        or build_definition.get("internalParameters")
        != {
            "build_seam": "scripts/toolkit_rc_build.py",
            "release_readiness_seam": "scripts/toolkit_rc_release_readiness.py",
        }
    ):
        raise ToolkitRcPromotionError("signed provenance build definition differs")
    dependencies = build_definition.get("resolvedDependencies")
    expected_dependencies = {
        (f"git+{SOURCE_REPOSITORY}@{source_commit}", "gitCommit", source_commit),
        ("file:uv.lock", "sha256", uv_lock_sha256),
    }
    actual_dependencies: set[tuple[object, object, object]] = set()
    if isinstance(dependencies, list):
        for dependency in dependencies:
            if not isinstance(dependency, dict) or not isinstance(dependency.get("digest"), dict):
                continue
            digest = dependency["digest"]
            if len(digest) == 1:
                name, value = next(iter(digest.items()))
                actual_dependencies.add((dependency.get("uri"), name, value))
    if actual_dependencies != expected_dependencies:
        raise ToolkitRcPromotionError("signed provenance resolved dependencies differ")
    if run_details.get("builder") != {"id": WORKFLOW_IDENTITY}:
        raise ToolkitRcPromotionError("signed provenance builder identity differs")
    dependency_binding = _shared_binding(manifest, "dependency_lock_evidence")
    if _subject_matrix(run_details.get("byproducts"), "signed provenance byproducts") != {
        "toolkit-rc-build-manifest-input.json": _sha256(build_content),
        _coordinate_filename(dependency_binding): dependency_binding.sha256,
    }:
        raise ToolkitRcPromotionError("signed provenance byproduct matrix differs")


def _model_descriptor(descriptor: OciDescriptor) -> ToolkitRcOciDescriptorV1:
    return ToolkitRcOciDescriptorV1(
        media_type=descriptor.media_type,
        digest=descriptor.digest,
        size_bytes=descriptor.size,
        title=descriptor.annotations[OCI_TITLE_ANNOTATION],
        role=descriptor.annotations[OCI_ROLE_ANNOTATION],
        source_coordinate=descriptor.annotations.get(OCI_SOURCE_COORDINATE_ANNOTATION),
    )


def _read_oci_publication(
    *,
    client: _RegistryClient,
    manifest_digest: str,
    expected_candidate_version: str,
) -> tuple[
    ToolkitRcOciPublicationReceiptV1,
    bytes,
    dict[str, bytes],
    dict[str, Any],
]:
    try:
        manifest_content, observed_digest = client.read_manifest(manifest_digest)
        if observed_digest != manifest_digest or sha256_digest(manifest_content) != manifest_digest:
            raise ToolkitRcPromotionError("OCI manifest digest readback differs")
        if client.resolve_manifest(expected_candidate_version) != manifest_digest:
            raise ToolkitRcPromotionError("OCI discovery tag drifted from immutable digest")
        config_descriptor, layers = parse_manifest_document(manifest_content)
        if (
            config_descriptor.annotations.get(OCI_TITLE_ANNOTATION) != "toolkit-rc-config.json"
            or config_descriptor.annotations.get(OCI_ROLE_ANNOTATION) != "release_config"
            or OCI_SOURCE_COORDINATE_ANNOTATION in config_descriptor.annotations
        ):
            raise ToolkitRcPromotionError("OCI config descriptor annotations differ")
        config_content = client.read_blob(config_descriptor.digest)
        if len(config_content) != config_descriptor.size:
            raise ToolkitRcPromotionError("OCI config size differs")
        config = _json_object(config_content, "OCI release config")
        objects: dict[str, bytes] = {}
        roles: set[str] = set()
        for descriptor in layers:
            role = descriptor.annotations.get(OCI_ROLE_ANNOTATION)
            coordinate = descriptor.annotations.get(OCI_SOURCE_COORDINATE_ANNOTATION)
            title = descriptor.annotations.get(OCI_TITLE_ANNOTATION)
            if not role or role in roles or not coordinate or not title:
                raise ToolkitRcPromotionError("OCI layer annotations differ")
            roles.add(role)
            content = client.read_blob(descriptor.digest)
            if len(content) != descriptor.size:
                raise ToolkitRcPromotionError("OCI layer size differs")
            if coordinate in objects and objects[coordinate] != content:
                raise ToolkitRcPromotionError("OCI coordinate maps to different layer bytes")
            objects[coordinate] = content
    except OciRegistryError as exc:
        raise ToolkitRcPromotionError(f"OCI publication readback failed: {exc}") from exc

    context = config.get("production_context")
    if not isinstance(context, dict):
        context = {}
    expected_config = {
        "schema_version": "alephain.custos.toolkit-rc-oci-config.v1",
        "candidate_version": expected_candidate_version,
        "source_repository": SOURCE_REPOSITORY,
        "registry": client.registry,
        "repository": client.repository,
        "tag": expected_candidate_version,
        "production_credentials_used": True,
        "production_signature_verified": True,
    }
    for name, value in expected_config.items():
        if config.get(name) != value:
            raise ToolkitRcPromotionError(f"OCI release config {name} differs")
    source_commit = config.get("source_commit")
    source_date_epoch = config.get("source_date_epoch")
    if (
        not isinstance(source_commit, str)
        or not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
    ):
        raise ToolkitRcPromotionError("OCI release source identity differs")
    receipt = ToolkitRcOciPublicationReceiptV1(
        status="PENDING_AUTHORITY_REGISTRATION",
        candidate_version=expected_candidate_version,
        source_commit=source_commit,
        source_date_epoch=source_date_epoch,
        registry=client.registry,
        repository=client.repository,
        tag=expected_candidate_version,
        oci_coordinate=f"{client.registry}/{client.repository}@{manifest_digest}",
        manifest_digest=manifest_digest,
        manifest_size_bytes=len(manifest_content),
        config=_model_descriptor(config_descriptor),
        layers=tuple(_model_descriptor(descriptor) for descriptor in layers),
        production_credentials_used=True,
        production_signature_verified=True,
        workflow_ref=context.get("workflow_ref"),
        workflow_identity=context.get("workflow_identity"),
        oidc_issuer=context.get("oidc_issuer"),
        release_environment=context.get("release_environment"),
        workflow_run_id=context.get("workflow_run_id"),
        workflow_run_attempt=context.get("workflow_run_attempt"),
    )
    require_production_publication_receipt(receipt)
    return receipt, manifest_content, objects, config


def promote_toolkit_rc(
    *,
    repository_root: Path,
    registry: str,
    repository: str,
    manifest_digest: str,
    expected_candidate_version: str,
    expected_source_commit: str,
    output_path: Path,
    registry_username: str = "",
    registry_token: str = "",
    registry_client: _RegistryClient | None = None,
) -> Path:
    repository_root = repository_root.resolve()
    output_path = output_path.resolve()
    if output_path == (repository_root / STABLE_READY_PATH).resolve():
        raise ToolkitRcPromotionError("promotion writes a candidate, never the authority path")
    if output_path.exists():
        raise ToolkitRcPromotionError("READY candidate output is immutable and must not exist")
    if not manifest_digest.startswith("sha256:") or len(manifest_digest) != 71:
        raise ToolkitRcPromotionError("operator manifest digest is invalid")
    client: _RegistryClient = registry_client or OciRegistryClient(
        registry,
        repository,
        username=registry_username,
        token=registry_token,
    )
    expected_registry = registry.removeprefix("https://").removeprefix("http://").rstrip("/")
    if client.registry != expected_registry or client.repository != repository:
        raise ToolkitRcPromotionError("OCI client authority coordinate differs")
    receipt, oci_manifest_content, objects, config = _read_oci_publication(
        client=client,
        manifest_digest=manifest_digest,
        expected_candidate_version=expected_candidate_version,
    )
    if receipt.source_commit != expected_source_commit:
        raise ToolkitRcPromotionError("operator source expectation differs from OCI config")

    def unique_named(filename: str) -> bytes:
        matches = [
            content
            for coordinate, content in objects.items()
            if coordinate.rsplit("@sha256:", 1)[0].rsplit("/", 1)[-1] == filename
        ]
        if len(matches) != 1:
            raise ToolkitRcPromotionError(f"OCI publication must contain one {filename}")
        return matches[0]

    manifest_content = unique_named("toolkit-rc-receipt-manifest.json")
    build_content = unique_named("toolkit-rc-build-manifest-input.json")
    try:
        manifest = ToolkitRcReceiptManifestV1.model_validate(json.loads(manifest_content))
        build = json.loads(build_content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ToolkitRcPromotionError(f"OCI publication manifests are invalid: {exc}") from exc
    if (
        manifest.candidate_version != receipt.candidate_version
        or build.get("candidate_version") != receipt.candidate_version
        or build.get("source_commit") != receipt.source_commit
        or build.get("source_date_epoch") != receipt.source_date_epoch
        or config.get("toolkit_manifest_sha256") != _sha256(manifest_content)
        or config.get("build_manifest_sha256") != _sha256(build_content)
    ):
        raise ToolkitRcPromotionError("OCI config and release manifests differ")
    for member in manifest.members:
        if member.source_commit != receipt.source_commit:
            raise ToolkitRcPromotionError("toolkit member source commit differs")
        for binding in (
            member.wheel,
            member.sbom,
            member.contract_schema,
            member.contract_asset_index,
            member.dependency_lock_evidence,
            member.slsa_provenance,
            member.sigstore_attestation,
            member.toolkit_extraction_receipt,
            member.toolkit_typing_closure_receipt,
            member.pre_import_verifier_receipt,
        ):
            content = objects.get(binding.coordinate)
            if (
                content is None
                or len(content) != binding.size_bytes
                or _sha256(content) != binding.sha256
            ):
                raise ToolkitRcPromotionError("manifest binding differs from OCI layer matrix")
    _validate_signed_provenance(
        manifest=manifest,
        build=build,
        build_content=build_content,
        objects=objects,
        source_commit=receipt.source_commit,
        source_date_epoch=receipt.source_date_epoch,
    )
    first = manifest.members[0]
    for path, binding in zip(
        PREREQUISITE_PATHS,
        (
            first.toolkit_extraction_receipt,
            first.toolkit_typing_closure_receipt,
            first.pre_import_verifier_receipt,
        ),
        strict=True,
    ):
        authoritative = (repository_root / path).read_bytes()
        if _sha256(authoritative) != binding.sha256 or len(authoritative) != binding.size_bytes:
            raise ToolkitRcPromotionError(f"authoritative prerequisite drifted: {path}")

    provenance_path = output_path.parent / f".{output_path.name}.provenance"
    bundle_path = output_path.parent / f".{output_path.name}.sigstore"
    provenance_path.write_bytes(objects[first.slsa_provenance.coordinate])
    bundle_path.write_bytes(objects[first.sigstore_attestation.coordinate])
    try:
        try:
            verification = subprocess.run(
                [
                    "sigstore",
                    "verify",
                    "identity",
                    "--bundle",
                    str(bundle_path),
                    "--cert-identity",
                    WORKFLOW_IDENTITY,
                    "--cert-oidc-issuer",
                    OIDC_ISSUER,
                    str(provenance_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise ToolkitRcPromotionError(
                f"Sigstore production identity verification is unavailable: {exc}"
            ) from exc
        if verification.returncode != 0:
            raise ToolkitRcPromotionError(
                f"Sigstore production identity verification failed: {verification.stderr.strip()}"
            )
    finally:
        provenance_path.unlink(missing_ok=True)
        bundle_path.unlink(missing_ok=True)

    predecessor = ImmutableToolkitArtifactBindingV1(
        coordinate=receipt.oci_coordinate,
        sha256=manifest_digest.removeprefix("sha256:"),
        size_bytes=len(oci_manifest_content),
    )
    ready = ToolkitRcAuthorityReadyReceiptV1(
        candidate_version=receipt.candidate_version,
        source_commit=receipt.source_commit,
        source_date_epoch=receipt.source_date_epoch,
        publication_receipt=receipt,
        toolkit_manifest=manifest,
        toolkit_manifest_sha256=_sha256(manifest_content),
        build_manifest_sha256=_sha256(build_content),
        publication_manifest=predecessor,
        final_blockers=(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as output:
        output.write(
            (json.dumps(ready.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
        )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--registry", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--expected-candidate-version", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    result = promote_toolkit_rc(
        repository_root=arguments.repository_root,
        registry=arguments.registry,
        repository=arguments.repository,
        manifest_digest=arguments.manifest_digest,
        expected_candidate_version=arguments.expected_candidate_version,
        expected_source_commit=arguments.expected_source_commit,
        output_path=arguments.output,
        registry_username=os.environ.get("CUSTOS_TOOLKIT_OCI_USERNAME", ""),
        registry_token=os.environ.get("CUSTOS_TOOLKIT_OCI_TOKEN", ""),
    )
    print(
        json.dumps(
            {"ready_candidate": str(result), "status": "READY_CANDIDATE_ONLY"}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
