from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from custos_toolkit.contracts import (
    ImmutableToolkitArtifactBindingV1,
    LockedToolkitDependencyV1,
    ToolkitRcMemberRole,
    ToolkitRcMemberV1,
    ToolkitRcOciPublicationReceiptV1,
    ToolkitRcReceiptManifestV1,
)

from scripts.toolkit_rc_oci import (
    OciCommitUnknownError,
    OciDescriptor,
    OciRegistryError,
    sha256_digest,
)
from scripts.toolkit_rc_publish import (
    ArtifactCoordinateExistsError,
    ArtifactPublicationError,
    publish_toolkit_rc_candidate,
)

SOURCE_COMMIT = "a" * 40
SOURCE_DATE_EPOCH = 1_704_067_200
OBJECT_FIELDS = (
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


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateInputs:
    manifest_path: Path
    build_manifest_path: Path
    object_sources: dict[str, Path]
    candidate_version: str


def _binding(
    *,
    root: Path,
    version: str,
    role: ToolkitRcMemberRole,
    field_name: str,
    content: bytes,
) -> tuple[ImmutableToolkitArtifactBindingV1, Path]:
    path = root / "objects" / f"{role.value}-{field_name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = _sha256(content)
    coordinate = f"artifact://custos/toolkit-rc/{version}/{role.value}/{field_name}@sha256:{digest}"
    return (
        ImmutableToolkitArtifactBindingV1(
            coordinate=coordinate,
            sha256=digest,
            size_bytes=len(content),
        ),
        path,
    )


def _candidate_inputs(root: Path, version: str) -> CandidateInputs:
    root.mkdir(parents=True, exist_ok=True)
    members: list[ToolkitRcMemberV1] = []
    object_sources: dict[str, Path] = {}
    wheel_documents: dict[str, dict[str, Any]] = {}

    for role in ToolkitRcMemberRole:
        is_base = role is ToolkitRcMemberRole.BASE_CONTRACTS_WHEEL
        distribution = "custos-strategy-toolkit" if is_base else "custos-strategy-toolkit-nautilus"
        bindings: dict[str, ImmutableToolkitArtifactBindingV1] = {}
        for field_name in OBJECT_FIELDS:
            content = f"{version}:{role.value}:{field_name}\n".encode()
            binding, path = _binding(
                root=root,
                version=version,
                role=role,
                field_name=field_name,
                content=content,
            )
            bindings[field_name] = binding
            object_sources[binding.coordinate] = path

        wheel = bindings["wheel"]
        requires_dist = (
            ["pydantic==2.12.5"]
            if is_base
            else [
                f"custos-strategy-toolkit=={version}",
                "nautilus-trader==1.230.0",
            ]
        )
        wheel_documents[distribution] = {
            "distribution_name": distribution,
            "version": version,
            "filename": Path(object_sources[wheel.coordinate]).name,
            "coordinate": wheel.coordinate,
            "sha256": wheel.sha256,
            "size_bytes": wheel.size_bytes,
            "requires_python": ">=3.11" if is_base else "<3.13,>=3.12",
            "requires_dist": requires_dist,
            "top_level_modules": ["custos_toolkit" if is_base else "custos_toolkit_nautilus"],
            "sbom_input": {"path": "ephemeral.json", "sha256": "f" * 64},
        }
        dependencies = (
            (
                LockedToolkitDependencyV1(
                    name="pydantic",
                    version="2.12.5",
                    requirement="pydantic==2.12.5",
                ),
            )
            if is_base
            else (
                LockedToolkitDependencyV1(
                    name="custos-strategy-toolkit",
                    version=version,
                    requirement=f"custos-strategy-toolkit=={version}",
                ),
                LockedToolkitDependencyV1(
                    name="nautilus-trader",
                    version="1.230.0",
                    requirement="nautilus-trader==1.230.0",
                ),
            )
        )
        members.append(
            ToolkitRcMemberV1(
                role=role,
                distribution_name=distribution,
                version=version,
                python_requires=">=3.11" if is_base else ">=3.12,<3.13",
                nautilus_version=None if is_base else "1.230.0",
                top_level_modules=("custos_toolkit" if is_base else "custos_toolkit_nautilus",),
                dependencies=dependencies,
                source_repository="https://github.com/the-alephain-guild/custos",
                source_commit=SOURCE_COMMIT,
                **bindings,
            )
        )

    manifest = ToolkitRcReceiptManifestV1(
        candidate_version=version,
        members=tuple(members),
    )
    manifest_path = root / "toolkit-rc-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    build_manifest_path = root / "toolkit-rc-build-manifest-input.json"
    build_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "alephain.custos.toolkit-rc-build-candidate.v1",
                "status": "BUILD_CANDIDATE_ONLY",
                "source_commit": SOURCE_COMMIT,
                "source_date_epoch": SOURCE_DATE_EPOCH,
                "candidate_version": version,
                "builds": {
                    "build-1": wheel_documents,
                    "build-2": wheel_documents,
                },
                "reproducible": True,
                "registry_accessed": False,
                "ready_receipt_created": False,
                "strategy_release_bom_created": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return CandidateInputs(
        manifest_path=manifest_path,
        build_manifest_path=build_manifest_path,
        object_sources=object_sources,
        candidate_version=version,
    )


@dataclass(slots=True)
class FakeOciRegistry:
    registry: str = "registry.example"
    repository: str = "custos/toolkit"
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifests: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    upload_count: int = 0
    fail_upload_at: int | None = None
    drop_commit_response: bool = False
    drift_readback: bool = False

    def resolve_manifest(self, reference: str) -> str | None:
        value = self.manifests.get(reference)
        return None if value is None else value[0]

    def upload_blob(self, descriptor: OciDescriptor, content: bytes) -> None:
        self.upload_count += 1
        if self.fail_upload_at == self.upload_count:
            self.fail_upload_at = None
            raise OciRegistryError("injected blob upload failure")
        if descriptor.digest != sha256_digest(content) or descriptor.size != len(content):
            raise OciRegistryError("descriptor mismatch")
        self.blobs[descriptor.digest] = content

    def put_manifest(self, tag: str, content: bytes) -> str:
        digest = sha256_digest(content)
        self.manifests[tag] = (digest, content)
        self.manifests[digest] = (digest, content)
        if self.drop_commit_response:
            self.drop_commit_response = False
            raise OciCommitUnknownError("injected response loss")
        return digest

    def verify_release(
        self,
        *,
        tag: str,
        manifest_digest: str,
        manifest_content: bytes,
        descriptors: tuple[OciDescriptor, ...],
    ) -> None:
        if self.drift_readback:
            raise OciRegistryError("injected digest drift")
        if self.manifests.get(tag) != (manifest_digest, manifest_content):
            raise OciRegistryError("tag readback differs")
        if self.manifests.get(manifest_digest) != (manifest_digest, manifest_content):
            raise OciRegistryError("digest readback differs")
        for descriptor in descriptors:
            content = self.blobs.get(descriptor.digest)
            if content is None or len(content) != descriptor.size:
                raise OciRegistryError("blob readback differs")


def _publish(
    inputs: CandidateInputs,
    registry: FakeOciRegistry,
    pending_path: Path,
):
    return publish_toolkit_rc_candidate(
        manifest_path=inputs.manifest_path,
        build_manifest_path=inputs.build_manifest_path,
        object_sources=inputs.object_sources,
        registry=registry.registry,
        repository=registry.repository,
        pending_receipt_path=pending_path,
        registry_client=registry,
    )


def test_oci_manifest_is_the_only_atomic_authority_and_tag_cannot_drift(
    tmp_path: Path,
) -> None:
    rc1 = _candidate_inputs(tmp_path / "rc1", "0.1.0rc1")
    rc2 = _candidate_inputs(tmp_path / "rc2", "0.1.0rc2")
    registry = FakeOciRegistry()
    pending_rc1 = tmp_path / "pending-rc1.json"
    evidence = _publish(rc1, registry, pending_rc1)
    receipt = ToolkitRcOciPublicationReceiptV1.model_validate_json(pending_rc1.read_bytes())
    assert evidence.candidate_version == "0.1.0rc1"
    assert receipt.status == "PENDING_PROTECTED_RELEASE"
    assert receipt.ready is False
    assert receipt.publication_atomic is True
    assert receipt.manifest_commit_verified is True
    assert receipt.descriptor_readback_verified is True
    assert receipt.tag_readback_verified is True
    assert receipt.production_credentials_used is False
    assert receipt.oci_coordinate == (
        f"{registry.registry}/{registry.repository}@{evidence.manifest_digest}"
    )
    assert registry.resolve_manifest("0.1.0rc1") == evidence.manifest_digest

    recovered = _publish(rc1, registry, tmp_path / "recovered-same-digest.json")
    assert recovered.commit_recovered_by_digest is True

    registry.manifests["0.1.0rc1"] = ("sha256:" + "f" * 64, b"drift")
    with pytest.raises(ArtifactCoordinateExistsError, match="next RC"):
        _publish(rc1, registry, tmp_path / "must-not-exist.json")
    assert not (tmp_path / "must-not-exist.json").exists()

    rc2_evidence = _publish(rc2, registry, tmp_path / "pending-rc2.json")
    assert rc2_evidence.candidate_version == "0.1.0rc2"


def test_partial_blob_failure_creates_no_manifest_and_same_candidate_retries(
    tmp_path: Path,
) -> None:
    inputs = _candidate_inputs(tmp_path / "inputs", "0.1.0rc1")
    registry = FakeOciRegistry(fail_upload_at=2)
    failed_pending = tmp_path / "failed-pending.json"
    with pytest.raises(ArtifactPublicationError, match="blob staging"):
        _publish(inputs, registry, failed_pending)
    assert registry.resolve_manifest(inputs.candidate_version) is None
    assert not failed_pending.exists()

    retried = _publish(inputs, registry, tmp_path / "retry-pending.json")
    assert retried.candidate_version == "0.1.0rc1"
    assert registry.resolve_manifest(inputs.candidate_version) == retried.manifest_digest


@pytest.mark.parametrize("failure", ["missing_attestation", "digest_drift"])
def test_incomplete_or_unverified_publication_never_writes_pending_or_ready_receipt(
    tmp_path: Path,
    failure: str,
) -> None:
    inputs = _candidate_inputs(tmp_path / failure, "0.1.0rc1")
    registry = FakeOciRegistry(drift_readback=failure == "digest_drift")
    if failure == "missing_attestation":
        attestation = next(
            coordinate
            for coordinate in inputs.object_sources
            if "/sigstore_attestation@sha256:" in coordinate
        )
        del inputs.object_sources[attestation]

    pending_path = tmp_path / f"{failure}-pending.json"
    with pytest.raises(ArtifactPublicationError):
        _publish(inputs, registry, pending_path)
    assert not pending_path.exists()
    assert not (tmp_path / "custos-toolkit-rc-authority-v1.json").exists()


def test_commit_unknown_recovers_only_from_the_expected_manifest_digest(
    tmp_path: Path,
) -> None:
    inputs = _candidate_inputs(tmp_path / "inputs", "0.1.0rc1")
    registry = FakeOciRegistry(drop_commit_response=True)
    pending = tmp_path / "recovered.json"
    evidence = _publish(inputs, registry, pending)
    assert pending.is_file()
    assert evidence.commit_recovered_by_digest is True
    assert registry.resolve_manifest(inputs.candidate_version) == evidence.manifest_digest


def test_descriptor_readback_drift_fails_before_pending_evidence(tmp_path: Path) -> None:
    inputs = _candidate_inputs(tmp_path / "inputs", "0.1.0rc1")
    registry = FakeOciRegistry(drift_readback=True)
    pending = tmp_path / "must-not-exist.json"
    with pytest.raises(ArtifactPublicationError, match="digest readback"):
        _publish(inputs, registry, pending)
    assert not pending.exists()


def test_publication_sources_contain_no_bespoke_artifact_service_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/toolkit_rc_publish.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/release-toolkit-rc.yml").read_text(encoding="utf-8")
    assert "artifact-service" not in source
    assert "ARTIFACT_SERVICE" not in workflow
    assert "OCI Distribution" in source
