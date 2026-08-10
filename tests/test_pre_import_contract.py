from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from custos_toolkit.contracts import strategy_execution
from custos_toolkit.contracts.strategy_execution import (
    StrategyArtifactRefV1,
    StrategyArtifactPreImportVerificationReceiptV1,
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
RECEIPT = ROOT / "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"
CRUCIBLE_RECEIPT = (
    ROOT / "docs/authority/receipts/vendor/"
    "crucible-custos-strategy-contract-v1-consumer-receipt.json"
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
    assert receipt["artifact_ref"]["contract_schema_sha256"] == (
        receipt["release_bom"]["execution_abi_schema_sha256"]
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
    assert crucible_receipt["producer"]["commit"] == (
        "a83e6f6969709316b8f11bcc0618b2f7b32fc19f"
    )
    assert crucible_receipt["consumer"] == "crucible-rust"
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


def test_contract_receipt_stays_pending_until_both_consumers_pin_v1() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))

    assert receipt["status"] == "CANONICAL_V1_PENDING_CONSUMER_RECEIPTS"
    assert receipt["contract_consumer_ready"] is False
    assert receipt["command_consumer_ready"] is False
    assert receipt["runtime_ready"] is False
    assert receipt["production_ready"] is False
    assert receipt["consumers"]["philosophers_stone"]["receipt"] is None
    crucible_pin = receipt["consumers"]["crucible_rust"]["receipt"]
    assert crucible_pin["commit"] == "4abd73eb320ac99bf16e443c5e572e5d1047391d"
    assert crucible_pin["sha256"] == hashlib.sha256(CRUCIBLE_RECEIPT.read_bytes()).hexdigest()
