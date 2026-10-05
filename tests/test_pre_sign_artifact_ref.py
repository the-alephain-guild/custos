from __future__ import annotations

import json
from pathlib import Path

import pytest
from custos_toolkit.contracts.strategy_execution import (
    StrategyArtifactRefV1,
    canonical_model_digest,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json"
GOLDEN = ROOT / "docs/authority/strategy-artifact-ref-v1.golden.json"
INDEX = ROOT / "docs/authority/strategy-contract-assets-v1.json"

ALLOWED_FIELDS = {
    "schema_version",
    "artifact_kind",
    "artifact_coordinate",
    "artifact_sha256",
    "artifact_size_bytes",
    "manifest_sha256",
    "manifest_size_bytes",
    "required_runtime_artifacts",
    "sbom_sha256",
    "contract_schema_sha256",
    "source_repository",
    "source_commit",
    "normalized_source_tree_sha256",
    "python_version",
    "engine",
    "engine_version",
    "base_contracts_version",
    "engine_toolkit_version",
    "build_inputs",
}
FORBIDDEN_FIELDS = {
    "attestation",
    "attestation_bundle",
    "bundle_coordinate",
    "bundle_sha256",
    "certificate",
    "certificate_chain",
    "issuer",
    "workflow_identity",
    "sigstore",
    "signer",
    "transparency_log",
    "transparency_log_verified",
    "trust_policy_id",
    "trust_policy_version",
    "trust_policy_digest",
    "trusted_root_digest",
    "verified_subjects",
    "verification_profile",
    "verification_receipt",
}


def _golden() -> dict[str, object]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_v1_is_the_only_pre_sign_execution_identity() -> None:
    document = _golden()
    artifact_ref = document["artifact_ref"]
    parsed = StrategyArtifactRefV1.model_validate(artifact_ref)

    assert set(artifact_ref) == ALLOWED_FIELDS
    assert set(artifact_ref).isdisjoint(FORBIDDEN_FIELDS)
    assert parsed.schema_version == 1
    assert parsed.artifact_kind == "wheel"
    assert parsed.build_inputs
    assert parsed.required_runtime_artifacts
    assert document["artifact_ref_digest"] == canonical_model_digest(parsed)


@pytest.mark.parametrize("field", sorted(FORBIDDEN_FIELDS))
def test_v1_rejects_bundle_policy_and_post_verification_fields(field: str) -> None:
    value = dict(_golden()["artifact_ref"])
    value[field] = "forbidden"

    with pytest.raises(ValidationError):
        StrategyArtifactRefV1.model_validate(value)


def test_schema_and_index_publish_only_canonical_v1() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    index = json.loads(INDEX.read_text(encoding="utf-8"))

    contract_keywords = {
        "x-contract-id": schema.pop("x-contract-id"),
        "x-contract-revision": schema.pop("x-contract-revision"),
    }
    assert contract_keywords == {
        "x-contract-id": "alephain.custos.strategy_artifact_ref.v1",
        "x-contract-revision": 1,
    }
    assert schema == StrategyArtifactRefV1.model_json_schema(mode="validation")
    assert set(schema["properties"]) == ALLOWED_FIELDS
    assert schema["properties"]["schema_version"]["const"] == 1
    assert index["asset_index_schema_version"] == 1
    assert index["current_contracts"]["strategy_artifact_ref"] == {
        "type": "StrategyArtifactRefV1",
        "schema_path": str(SCHEMA.relative_to(ROOT)),
        "golden_path": str(GOLDEN.relative_to(ROOT)),
    }
    assert {"legacy_non_production", "predecessor", "superseded"}.isdisjoint(index)
