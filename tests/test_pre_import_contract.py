from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from custos_toolkit.contracts import strategy_execution
from custos_toolkit.contracts.strategy_execution import (
    StrategyArtifactPreImportVerificationReceiptV1,
    StrategyArtifactRefV1,
)
from pydantic import ValidationError as PydanticValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (
    ROOT
    / "docs/gateway-contract/v1/strategy_artifact_pre_import_verification_receipt_v1.schema.json"
)
GOLDEN = ROOT / "docs/authority/strategy-artifact-pre-import-verification-v1.golden.json"
NEGATIVE = ROOT / "docs/authority/strategy-artifact-pre-import-verification-v1.negative.json"
INDEX = ROOT / "docs/authority/strategy-contract-assets-v1.json"
RECEIPT = (
    ROOT / "docs/authority/receipts/custos-strategy-contract-nautilus-2-v1-handoff-receipt.json"
)
CRUCIBLE_RECEIPT = (
    ROOT / "docs/authority/receipts/vendor/"
    "crucible-custos-strategy-contract-trading-scope-v1-consumer-receipt.json"
)
PS_RECEIPT = (
    ROOT / "docs/authority/receipts/vendor/"
    "ps-custos-strategy-contract-trading-scope-v1-consumer-receipt.json"
)


def _validate(document: dict[str, object]) -> None:
    StrategyArtifactPreImportVerificationReceiptV1.model_validate(document)


def _apply_mutation(document: dict[str, object], mutation: dict[str, object]) -> None:
    path = mutation["path"]
    assert isinstance(path, list) and path
    target: object = document
    for segment in path[:-1]:
        assert isinstance(target, dict)
        target = target[segment]
    assert isinstance(target, dict)
    key = path[-1]
    assert isinstance(key, str)
    if mutation["operation"] == "remove":
        del target[key]
    else:
        target[key] = mutation["value"]


def test_schema_golden_and_index_are_the_same_v1_contract() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    index = json.loads(INDEX.read_text(encoding="utf-8"))

    assert schema["title"] == "StrategyArtifactPreImportVerificationReceiptV1"
    assert schema["properties"]["schema_version"]["const"] == 1
    _validate(golden["receipt"])
    receipt_contract = index["current_contracts"]["pre_import_verification_receipt"]
    artifact_ref_contract = index["current_contracts"]["strategy_artifact_ref"]
    assert receipt_contract["type"] == "StrategyArtifactPreImportVerificationReceiptV1"
    assert receipt_contract["schema_path"] == str(SCHEMA.relative_to(ROOT))
    assert artifact_ref_contract["type"] == "StrategyArtifactRefV1"
    assert index["status"] == "CANONICAL_V1_CONTRACT_ASSETS_PUBLISHED"
    assert "consumer_receipts" not in index
    assert "runtime_ready" not in index
    receipt = golden["receipt"]
    assert (
        receipt["artifact_ref"]["contract_schema_sha256"]
        == (receipt["release_bom"]["execution_abi_schema_sha256"])
    )
    assert [subject["name"] for subject in receipt["release_statement"]["subject"]] == [
        "strategy-release-bom-v1",
        "strategy-artifact",
        "strategy-manifest-v1",
        "strategy-artifact-ref-v1",
    ]
    claims = receipt["crucible_artifact_evidence"]["signed_producer_claims"]
    assert claims["artifact_ref_digest"] == receipt["artifact_ref_digest"]
    assert claims["release_bom_digest"] == receipt["release_bom_digest"]
    crucible_receipt = json.loads(CRUCIBLE_RECEIPT.read_text(encoding="utf-8"))
    assert crucible_receipt["producer"]["commit"] == "ffdc693f6180ded18b1c0c1c3bc708ea53cd2220"
    assert crucible_receipt["consumer"] == {
        "accepted_at_commit": "1a2e250e6a7d1fad290302b456b6f098a12dad24",
        "repository": "tesseract-trading/crucible-rust",
    }
    assert crucible_receipt["runtime_ready"] is False
    assert crucible_receipt["production_ready"] is False


def test_all_published_negative_cases_fail_closed() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["receipt"]
    negatives = json.loads(NEGATIVE.read_text(encoding="utf-8"))

    assert negatives["cases"]
    for case in negatives["cases"]:
        invalid = copy.deepcopy(golden)
        _apply_mutation(invalid, case["mutation"])
        with pytest.raises((PydanticValidationError, TypeError, ValueError)):
            _validate(invalid)


def test_build_lock_binding_uses_the_signed_digest_not_a_producer_local_path() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["receipt"]
    artifact_ref = copy.deepcopy(golden["artifact_ref"])
    build_lock_digest = golden["release_bom"]["build_lock_sha256"]
    artifact_ref["build_inputs"][0]["name"] = "resources/build-lock-v1.json"
    parsed = StrategyArtifactRefV1.model_validate(artifact_ref)

    strategy_execution._require_single_build_lock_binding(parsed, build_lock_digest)
    with pytest.raises(ValueError, match="build_lock_sha256 differs"):
        strategy_execution._require_single_build_lock_binding(parsed, "f" * 64)

    artifact_ref["build_inputs"].append(
        {"name": "resources/build-lock-copy-v1.json", "sha256": build_lock_digest}
    )
    duplicated = StrategyArtifactRefV1.model_validate(artifact_ref)
    with pytest.raises(ValueError, match="build_lock_sha256 differs"):
        strategy_execution._require_single_build_lock_binding(duplicated, build_lock_digest)


def test_pre_import_receipt_rejects_non_sigstore_publisher_profile() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["receipt"]
    invalid = copy.deepcopy(golden)
    proof = invalid["crucible_artifact_evidence"]["sigstore_proof"]
    invalid["crucible_artifact_evidence"]["sigstore_proof"] = {
        "publisher_profile": "team_kms",
        "proof": proof,
    }

    with pytest.raises(
        PydanticValidationError,
        match="not exact GitHub OIDC Sigstore evidence",
    ):
        _validate(invalid)


def test_pre_import_receipt_accepts_digest_bound_github_oidc_proof_wrapper() -> None:
    receipt = copy.deepcopy(json.loads(GOLDEN.read_text(encoding="utf-8"))["receipt"])
    evidence = receipt["crucible_artifact_evidence"]
    evidence["sigstore_proof"] = {
        "publisher_profile": "github_oidc",
        "proof": evidence["sigstore_proof"],
    }
    claim_fields = (
        "schema_version",
        "producer_repository",
        "producer_commit",
        "workflow_identity",
        "source_date_epoch",
        "strategy_source_tree_sha256",
        "artifact_sha256",
        "manifest_sha256",
        "artifact_ref_digest",
        "release_bom_digest",
        "execution_abi_schema_sha256",
        "contract_asset_index_sha256",
        "toolkit_wheel_sha256",
        "toolkit_sbom_sha256",
        "build_lock_sha256",
        "zero_rewrite_semantic_diff_sha256",
        "zero_rewrite_characterization_sha256",
        "engine",
        "engine_version",
        "python_requires",
        "entry_point_group",
        "entry_point_name",
    )
    proof_fields = (
        "bundle_sha256",
        "statement_sha256",
        "dsse_payload_sha256",
        "dsse_signature_sha256",
        "signing_certificate_sha256",
        "trusted_root_sha256",
        "certificate_issuer",
        "certificate_subject",
        "certificate_not_before",
        "certificate_not_after",
        "sct_log_id",
        "sct_sha256",
        "rekor_log_id",
        "rekor_log_index",
        "rekor_integrated_time",
        "rekor_entry_body_sha256",
        "rekor_signed_entry_timestamp_sha256",
        "rekor_inclusion_proof_sha256",
        "rekor_checkpoint_sha256",
        "rekor_tree_size",
        "rekor_root_hash",
    )
    policy_fields = (
        "policy_id",
        "policy_version",
        "policy_digest",
        "evaluated_at",
        "decision",
    )
    wrapped = evidence["sigstore_proof"]
    evidence_preimage = {
        "schema_version": evidence["schema_version"],
        "strategy_release_id": evidence["strategy_release_id"],
        "artifact_ref_digest": evidence["artifact_ref_digest"],
        "release_bom_digest": evidence["release_bom_digest"],
        "release_statement_digest": evidence["release_statement_digest"],
        "detached_attestation_ref_digest": evidence["detached_attestation_ref_digest"],
        "bundle_sha256": evidence["bundle_sha256"],
        "signed_producer_claims": {
            name: evidence["signed_producer_claims"][name] for name in claim_fields
        },
        "sigstore_proof": {
            "publisher_profile": "github_oidc",
            "proof": {name: wrapped["proof"][name] for name in proof_fields},
        },
        "local_policy_evaluation": {
            name: evidence["local_policy_evaluation"][name] for name in policy_fields
        },
        "composite_evidence_digest": "",
    }
    digest = hashlib.sha256(
        json.dumps(
            evidence_preimage,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    evidence["composite_evidence_digest"] = digest
    receipt["crucible_artifact_evidence_digest"] = digest
    receipt["crucible_artifact_acceptance"]["artifact_evidence_digest"] = digest
    receipt["runner_local_policy_decision"]["artifact_evidence_digest"] = digest

    _validate(receipt)


def test_contract_receipt_records_both_nautilus_2_consumers() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))

    assert receipt["status"] == "CANONICAL_V1_CONSUMER_HANDOFF_COMPLETE"
    assert receipt["contract_consumer_ready"] is True
    assert receipt["command_consumer_ready"] is True
    assert receipt["runtime_ready"] is False
    assert receipt["production_ready"] is False
    ps_pin = receipt["consumers"]["philosophers_stone"]["receipt"]
    assert ps_pin["commit"] == "c89ce7bd5187ad1c0b148b8554eeb234f63c1deb"
    assert ps_pin["sha256"] == hashlib.sha256(PS_RECEIPT.read_bytes()).hexdigest()
    crucible_pin = receipt["consumers"]["crucible_rust"]["receipt"]
    assert crucible_pin["commit"] == "8e38becf270c55f3676e6467015c2a3fbfd64588"
    assert crucible_pin["sha256"] == hashlib.sha256(CRUCIBLE_RECEIPT.read_bytes()).hexdigest()
