from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from example_producer import WORKFLOW_IDENTITY, WORKFLOW_REF

from custos_strategy_publisher.model import (
    ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION,
    DSSE_PAYLOAD_TYPE,
    IN_TOTO_STATEMENT_TYPE,
    STRATEGY_RELEASE_PREDICATE_TYPE,
    ArtifactAttestationRefV1,
    StrategyReleaseStatementV1,
)

ROOT = Path(__file__).resolve().parents[1]
STATEMENT_SCHEMA = ROOT / "contracts/strategy-release-statement-v1.schema.json"
STATEMENT_GOLDEN = ROOT / "contracts/strategy-release-statement-v1.golden.json"
ATTESTATION_SCHEMA = ROOT / "contracts/artifact-attestation-ref-v1.schema.json"
ATTESTATION_GOLDEN = ROOT / "contracts/artifact-attestation-ref-v1.golden.json"


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_statement_contract_freezes_dsse_and_closed_in_toto_shape() -> None:
    schema = _load(STATEMENT_SCHEMA)
    golden = _load(STATEMENT_GOLDEN)
    statement = StrategyReleaseStatementV1.from_mapping(golden)

    assert schema["x-dsse-payloadType"] == DSSE_PAYLOAD_TYPE
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"_type", "subject", "predicateType", "predicate"}
    assert statement.statement_type == IN_TOTO_STATEMENT_TYPE
    assert statement.predicate_type == STRATEGY_RELEASE_PREDICATE_TYPE
    assert statement.predicate.workflow_identity == WORKFLOW_IDENTITY
    assert WORKFLOW_REF == (
        "alchymia-labs/philosophers-stone/.github/workflows/"
        "publish-strategy-artifact.yml@refs/heads/main"
    )
    assert tuple(subject.name for subject in statement.subjects) == (
        "strategy-release-bom-v1",
        "strategy-artifact",
        "strategy-manifest-v1",
        "strategy-artifact-ref-v1",
    )
    assert all(set(subject.digest) == {"sha256"} for subject in statement.subjects)
    assert set(statement.predicate.to_mapping()) == {
        "schema_version",
        "producer_repository",
        "producer_commit",
        "workflow_identity",
        "source_date_epoch",
        "strategy_source_tree_sha256",
        "execution_abi_schema_sha256",
        "contract_asset_index_sha256",
        "toolkit_wheel_sha256",
        "toolkit_sbom_sha256",
        "strategy_sbom_sha256",
        "build_lock_sha256",
        "zero_rewrite_semantic_diff_sha256",
        "zero_rewrite_characterization_sha256",
        "engine",
        "engine_version",
        "python_requires",
        "entry_point_group",
        "entry_point_name",
    }
    assert not ({"bundle_sha256", "trust_policy_id", "trust_policy_digest"} & set(golden))

    unknown = dict(golden)
    unknown["bundle_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        StrategyReleaseStatementV1.from_mapping(unknown)


@pytest.mark.parametrize(
    "mutation", ["subject_order", "digest_key", "predicate_extra", "predicate_missing"]
)
def test_statement_rejects_any_subject_or_predicate_shape_drift(mutation: str) -> None:
    value = _load(STATEMENT_GOLDEN)
    subjects = value["subject"]
    predicate = value["predicate"]
    assert isinstance(subjects, list)
    assert isinstance(predicate, dict)
    if mutation == "subject_order":
        subjects[0], subjects[1] = subjects[1], subjects[0]
    elif mutation == "digest_key":
        digest = subjects[0]["digest"]
        assert isinstance(digest, dict)
        digest["sha512"] = "f" * 128
    elif mutation == "predicate_extra":
        predicate["bundle_sha256"] = "f" * 64
    else:
        del predicate["build_lock_sha256"]

    with pytest.raises(ValueError):
        StrategyReleaseStatementV1.from_mapping(value)


def test_detached_attestation_ref_is_closed_and_not_a_verification_receipt() -> None:
    schema = _load(ATTESTATION_SCHEMA)
    golden = _load(ATTESTATION_GOLDEN)
    reference = ArtifactAttestationRefV1.from_mapping(golden)

    assert reference.schema_version == ARTIFACT_ATTESTATION_REF_SCHEMA_VERSION
    assert schema["additionalProperties"] is False
    assert set(golden) == {
        "schema_version",
        "statement_coordinate",
        "statement_sha256",
        "bundle_coordinate",
        "bundle_sha256",
        "payload_type",
        "predicate_type",
    }
    assert reference.payload_type == DSSE_PAYLOAD_TYPE
    assert reference.predicate_type == STRATEGY_RELEASE_PREDICATE_TYPE
    assert "trust" not in json.dumps(golden)
    assert "verified" not in json.dumps(golden)


@pytest.mark.parametrize(
    "golden",
    [STATEMENT_GOLDEN, ATTESTATION_GOLDEN],
)
def test_canonical_golden_sidecars_bind_exact_bytes(golden: Path) -> None:
    expected = golden.with_name(f"{golden.name}.sha256").read_text(encoding="ascii").strip()
    assert expected == hashlib.sha256(golden.read_bytes()).hexdigest()


def test_contract_only_slice_does_not_fabricate_sigstore_or_trust_root_evidence() -> None:
    assert not (
        ROOT / "docs/authority/fixtures/strategy-release-statement-v1.positive.sigstore.json"
    ).exists()
    assert not (
        ROOT / "docs/authority/fixtures/strategy-release-statement-v1.trusted-root.json"
    ).exists()
