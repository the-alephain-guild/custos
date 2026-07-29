from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from custos_toolkit.contracts import ToolkitRcPendingReceiptV1

from scripts.toolkit_rc_release_readiness import (
    ReleaseReadinessError,
    assemble_toolkit_rc_publication_inputs,
    prepare_toolkit_rc_release_readiness,
    resolve_locked_dependencies,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATE_EPOCH = 1_704_067_200
SOURCE_COMMIT = "a" * 40
SCHEMA_PATH = ROOT / "docs/gateway-contract/v1/toolkit_rc_pending_receipt_v1.schema.json"
READY_RECEIPT_PATH = ROOT / "docs/authority/receipts/custos-toolkit-rc-authority-v1.json"


@dataclass(frozen=True, slots=True)
class BuildCandidateFixture:
    manifest_input_path: Path
    source_commit: str = SOURCE_COMMIT
    source_date_epoch: int = SOURCE_DATE_EPOCH


@pytest.fixture(scope="module")
def build_candidate(
    tmp_path_factory: pytest.TempPathFactory,
) -> BuildCandidateFixture:
    build_root = tmp_path_factory.mktemp("toolkit-release-readiness") / "candidate"
    records: dict[str, dict[str, object]] = {}
    policies = {
        "custos-strategy-toolkit": {
            "filename": "custos_strategy_toolkit-0.1.0rc1-py3-none-any.whl",
            "requires_python": ">=3.11",
            "requires_dist": ["pydantic>=2.5", "pyyaml>=6"],
            "top_level_modules": ["custos_toolkit"],
        },
        "custos-strategy-toolkit-nautilus": {
            "filename": ("custos_strategy_toolkit_nautilus-0.1.0rc1-py3-none-any.whl"),
            "requires_python": "<3.13,>=3.12",
            "requires_dist": [
                "custos-strategy-toolkit==0.1.0rc1",
                "nautilus-trader==1.230.0",
                "pyyaml>=6",
                "packaging",
            ],
            "top_level_modules": ["custos_toolkit_nautilus"],
        },
    }
    for distribution, policy in policies.items():
        filename = str(policy["filename"])
        content = f"UNSIGNED TOOLKIT WHEEL FIXTURE {distribution}\n".encode()
        digest = hashlib.sha256(content).hexdigest()
        for build_name in ("build-1", "build-2"):
            path = build_root / build_name / "dist" / distribution / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        records[distribution] = {
            "distribution_name": distribution,
            "version": "0.1.0rc1",
            "filename": filename,
            "coordinate": (
                f"toolkit-rc://custos/{distribution}/0.1.0rc1/{filename}@sha256:{digest}"
            ),
            "sha256": digest,
            "size_bytes": len(content),
            "requires_python": policy["requires_python"],
            "requires_dist": policy["requires_dist"],
            "top_level_modules": policy["top_level_modules"],
            "sbom_input": {"path": "ephemeral.json", "sha256": "f" * 64},
        }
    manifest_path = build_root / "toolkit-rc-build-manifest-input.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "alephain.custos.toolkit-rc-build-candidate.v1",
                "status": "BUILD_CANDIDATE_ONLY",
                "source_commit": SOURCE_COMMIT,
                "source_date_epoch": SOURCE_DATE_EPOCH,
                "candidate_version": "0.1.0rc1",
                "builds": {"build-1": records, "build-2": records},
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
    return BuildCandidateFixture(manifest_input_path=manifest_path)


def _prepare(build_root: Path, output_root: Path):
    return prepare_toolkit_rc_release_readiness(
        repository_root=ROOT,
        build_root=build_root,
        lock_path=ROOT / "uv.lock",
        contract_schema_path=(
            ROOT / "docs/gateway-contract/v1/toolkit_rc_receipt_manifest_v1.schema.json"
        ),
        contract_asset_index_path=(ROOT / "docs/authority/strategy-contract-assets-v1.json"),
        toolkit_extraction_receipt_path=(
            ROOT / "docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"
        ),
        toolkit_typing_closure_receipt_path=(
            ROOT / "docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json"
        ),
        pre_import_verifier_receipt_path=(
            ROOT / "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"
        ),
        output_root=output_root,
    )


def test_formal_cyclonedx_and_lock_evidence_are_exact_and_deterministic(
    build_candidate: BuildCandidateFixture,
    tmp_path: Path,
) -> None:
    first = _prepare(build_candidate.manifest_input_path.parent, tmp_path / "first")
    second = _prepare(build_candidate.manifest_input_path.parent, tmp_path / "second")

    expected_locks = {
        "custos-strategy-toolkit": {
            "pydantic==2.13.4",
            "pyyaml==6.0.3",
        },
        "custos-strategy-toolkit-nautilus": {
            "custos-strategy-toolkit==0.1.0rc1",
            "nautilus-trader==1.230.0",
            "packaging==26.2",
            "pyyaml==6.0.3",
        },
    }
    lock_evidence = json.loads(first.dependency_lock_path.read_text(encoding="utf-8"))
    for distribution, expected in expected_locks.items():
        assert {
            dependency["requirement"] for dependency in lock_evidence["distributions"][distribution]
        } == expected

        first_sbom = first.sbom_paths[distribution]
        second_sbom = second.sbom_paths[distribution]
        assert first_sbom.read_bytes() == second_sbom.read_bytes()
        sbom = json.loads(first_sbom.read_text(encoding="utf-8"))
        assert sbom["bomFormat"] == "CycloneDX"
        assert sbom["specVersion"] == "1.6"
        assert sbom["version"] == 1
        assert "files" not in sbom
        assert {
            f"{component['name']}=={component['version']}" for component in sbom["components"]
        } == expected
        assert sbom["dependencies"][0]["dependsOn"]
        assert sbom["metadata"]["properties"] == [
            {"name": "alephain:source_commit", "value": build_candidate.source_commit},
            {"name": "alephain:source_date_epoch", "value": str(SOURCE_DATE_EPOCH)},
        ]

    assert first.dependency_lock_path.read_bytes() == second.dependency_lock_path.read_bytes()


def test_provenance_binds_every_release_input_without_wall_clock_state(
    build_candidate: BuildCandidateFixture,
    tmp_path: Path,
) -> None:
    first = _prepare(build_candidate.manifest_input_path.parent, tmp_path / "first")
    second = _prepare(build_candidate.manifest_input_path.parent, tmp_path / "second")
    assert first.provenance_path.read_bytes() == second.provenance_path.read_bytes()

    statement = json.loads(first.provenance_path.read_text(encoding="utf-8"))
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1"
    assert statement["predicate"]["buildDefinition"]["buildType"] == (
        "https://custos.the-alephain-guild/build-types/toolkit-rc/v1"
    )
    parameters = statement["predicate"]["buildDefinition"]["externalParameters"]
    assert parameters == {
        "candidate_version": "0.1.0rc1",
        "source_commit": build_candidate.source_commit,
        "source_date_epoch": SOURCE_DATE_EPOCH,
    }
    metadata = statement["predicate"]["runDetails"]["metadata"]
    assert metadata["startedOn"] == "2024-01-01T00:00:00Z"
    assert metadata["finishedOn"] == "2024-01-01T00:00:00Z"
    subject_names = {subject["name"] for subject in statement["subject"]}
    assert {
        "custos_strategy_toolkit-0.1.0rc1-py3-none-any.whl",
        "custos_strategy_toolkit_nautilus-0.1.0rc1-py3-none-any.whl",
        "custos-strategy-toolkit.cdx.json",
        "custos-strategy-toolkit-nautilus.cdx.json",
        "toolkit_rc_receipt_manifest_v1.schema.json",
        "strategy-contract-assets-v1.json",
        "strategy-toolkit-extraction-receipt-v1.json",
        "strategy-toolkit-typing-closure-receipt-v1.json",
        "custos-strategy-contract-v1-producer-receipt.json",
        "toolkit-rc-dependency-locks.json",
    }.issubset(subject_names)
    assert all(set(subject["digest"]) == {"sha256"} for subject in statement["subject"])


def test_pending_contract_is_source_generated_and_has_one_operational_blocker(
    build_candidate: BuildCandidateFixture,
    tmp_path: Path,
) -> None:
    readiness = _prepare(build_candidate.manifest_input_path.parent, tmp_path / "readiness")
    document = json.loads(readiness.pending_receipt_path.read_text(encoding="utf-8"))
    receipt = ToolkitRcPendingReceiptV1.model_validate(document)

    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == (
        ToolkitRcPendingReceiptV1.model_json_schema(mode="validation")
    )
    assert receipt.status == "PENDING_PROTECTED_RELEASE"
    assert receipt.ready is False
    assert receipt.formal_sboms_complete is True
    assert receipt.dependency_locks_complete is True
    assert receipt.provenance_complete is True
    assert receipt.production_credentials_used is False
    assert receipt.production_signature_verified is False
    assert receipt.remote_publication_verified is False
    assert receipt.final_receipt_published is False
    assert receipt.final_blockers == (
        "execute the protected production release runner with credentials and register "
        "its verified remote receipt",
    )
    ready_receipt = json.loads(READY_RECEIPT_PATH.read_text(encoding="utf-8"))
    assert ready_receipt["status"] == "READY_TOOLKIT_RC"
    assert ready_receipt["candidate_version"] == "0.1.0rc6"

    authority = json.loads((ROOT / "authority-manifest.json").read_text(encoding="utf-8"))
    assert {
        "role": "toolkit_rc_pending_receipt_schema_v1_contract_only",
        "path": ("docs/gateway-contract/v1/toolkit_rc_pending_receipt_v1.schema.json"),
        "contract_only": True,
        "ready_receipt_published": True,
    } in authority["authority_documents"]


def test_lock_resolution_fails_closed_when_uv_lock_cannot_satisfy_requirement() -> None:
    with pytest.raises(ReleaseReadinessError, match="not locked"):
        resolve_locked_dependencies(
            raw_requirements=("not-a-real-release-dependency>=1",),
            candidate_version="0.1.0rc1",
            lock_path=ROOT / "uv.lock",
        )


def test_wheel_drift_fails_before_sbom_or_provenance_emission(
    build_candidate: BuildCandidateFixture,
    tmp_path: Path,
) -> None:
    build_root = tmp_path / "tampered-build"
    shutil.copytree(build_candidate.manifest_input_path.parent, build_root)
    wheel = next((build_root / "build-1/dist/custos-strategy-toolkit").glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(ReleaseReadinessError, match=r"wheel (?:bytes|digest)"):
        _prepare(build_root, output_root)
    assert not output_root.exists()


def test_unverified_sigstore_bundle_cannot_assemble_publication_inputs(
    build_candidate: BuildCandidateFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = _prepare(build_candidate.manifest_input_path.parent, tmp_path / "readiness")
    bundle = tmp_path / "production.sigstore.json"
    bundle.write_text(
        json.dumps(
            {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "verificationMaterial": {},
                "messageSignature": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.toolkit_rc_release_readiness.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="untrusted test fixture rejected"
        ),
    )
    output_root = tmp_path / "must-not-assemble"

    with pytest.raises(ReleaseReadinessError, match="Sigstore verification failed"):
        assemble_toolkit_rc_publication_inputs(
            build_root=build_candidate.manifest_input_path.parent,
            readiness_root=readiness.pending_receipt_path.parent,
            contract_schema_path=(
                ROOT / "docs/gateway-contract/v1/toolkit_rc_receipt_manifest_v1.schema.json"
            ),
            contract_asset_index_path=(ROOT / "docs/authority/strategy-contract-assets-v1.json"),
            toolkit_extraction_receipt_path=(
                ROOT / "docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"
            ),
            toolkit_typing_closure_receipt_path=(
                ROOT / "docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json"
            ),
            pre_import_verifier_receipt_path=(
                ROOT / "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"
            ),
            sigstore_bundle_path=bundle,
            output_root=output_root,
        )
    assert not output_root.exists()


def test_dedicated_production_workflow_is_manual_oidc_and_fail_closed() -> None:
    workflow = ROOT / ".github/workflows/release-toolkit-rc.yml"
    assert workflow.is_file()
    source = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in source
    assert "candidate_version:" in source
    assert "push:" not in source
    assert "permissions:\n  contents: read\n  packages: write\n  id-token: write" in source
    assert "environment: toolkit-rc-release" in source
    assert "runs-on: ubuntu-24.04" in source
    assert "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b" in source
    assert "version: '0.11.16'" in source
    assert "uv sync --frozen --extra lts" in source
    assert "SOURCE_DATE_EPOCH: '1704067200'" in source
    assert "python scripts/toolkit_rc_build.py" in source
    assert "python scripts/toolkit_rc_release_readiness.py prepare" in source
    assert "sigstore sign --bundle" in source
    assert "sigstore verify identity" in source
    assert "python scripts/toolkit_rc_release_readiness.py assemble" in source
    assert "python -m scripts.toolkit_rc_publish" in source
    assert "--production-release-runner" in source
    assert "group: toolkit-rc-${{ inputs.candidate_version }}" in source
    assert "oci_coordinate=${{ steps.publish.outputs.oci_coordinate }}" in source
    assert "manifest_digest=${{ steps.publish.outputs.manifest_digest }}" in source
    assert "CUSTOS_TOOLKIT_OCI_REGISTRY: ghcr.io" in source
    assert "CUSTOS_TOOLKIT_OCI_REPOSITORY: the-alephain-guild/custos-strategy-toolkit" in source
    assert "CUSTOS_TOOLKIT_ARTIFACT_SERVICE" not in source
    assert "self-hosted" not in source
    assert "actions/upload-artifact" not in source
    assert "softprops/action-gh-release" not in source
    assert "skip-existing" not in source
    assert "release.yml" not in source
    assert "READY_TOOLKIT_RC" not in source
