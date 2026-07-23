from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION = ROOT / "docs/authority/strategy-toolkit-extraction-v1.json"
EXTRACTION_RECEIPT = ROOT / "docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"
BASELINE = ROOT / "docs/authority/strategy-toolkit-typing-baseline-v1.json"
CLOSURE = ROOT / "docs/authority/strategy-toolkit-typing-closure-v1.json"
CLOSURE_RECEIPT = ROOT / "docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json"
EXTRACTION_COMMIT = "b5ff7ee9cea0e78f4462a478bafa42f8f6e18805"
TYPING_CLOSURE_COMMIT = "5a19a816d4f6d90e7d3fbde80d39f562decd8c4b"


def _load(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_typing_manifest_extends_immutable_extraction_evidence() -> None:
    extraction = _load(EXTRACTION)
    closure = _load(CLOSURE)
    extraction_files = cast(list[dict[str, object]], extraction["files"])
    closure_files = cast(list[dict[str, object]], closure["files"])
    historical_by_target = {
        cast(str, record["target_path"]): cast(str, record["target_sha256"])
        for record in extraction_files
    }

    assert closure["extracted_implementation_commit"] == EXTRACTION_COMMIT
    assert closure["typed_implementation_commit"] == TYPING_CLOSURE_COMMIT
    assert closure["verification_mode"] == "exact_commit_snapshot"
    assert closure["extraction_manifest_sha256"] == _sha256(EXTRACTION)
    assert closure["extraction_receipt_sha256"] == _sha256(EXTRACTION_RECEIPT)
    assert closure["typing_baseline_sha256"] == _sha256(BASELINE)
    assert len(closure_files) == len(historical_by_target) == 241
    assert {
        cast(str, record["target_path"]): cast(str, record["extracted_target_sha256"])
        for record in closure_files
    } == historical_by_target


def test_typing_candidate_is_strict_zero_without_vendor_rewrite() -> None:
    closure = _load(CLOSURE)
    closure_files = cast(list[dict[str, object]], closure["files"])
    target_counts = cast(dict[str, object], closure["target_error_counts"])
    vendor_records = [record for record in closure_files if record["category"] == "private_vendor"]

    assert target_counts == {"nautilus_adapter": 0, "platform_neutral": 0}
    assert len(vendor_records) == 150
    assert all(record["changed"] is False for record in vendor_records)
    assert closure["public_contract_policy"] == (
        "owned toolkit sources may not import or annotate typing.Any"
    )


def test_receipt_is_ready_only_for_typing_closure_handoff() -> None:
    closure = _load(CLOSURE)
    receipt = _load(CLOSURE_RECEIPT)

    assert receipt["receipt_status"] == "READY_TYPING_CLOSURE"
    assert receipt["handoff_ready"] is True
    assert receipt["handoff_scope"] == "Custos strategy toolkit typing closure only"
    assert receipt["production_ready"] is False
    assert receipt["typed_implementation_commit"] == TYPING_CLOSURE_COMMIT
    assert receipt["verification_checkout"] == {"clean": True, "head": TYPING_CLOSURE_COMMIT}
    assert receipt["open_blockers"] == [
        "public pre-import artifact verifier and attestation contract",
        "immutable toolkit release candidate publication",
    ]
    assert receipt["manifest_sha256"] == _sha256(CLOSURE)
    assert receipt["typed_candidate_tree_sha256"] == closure["typed_candidate_tree_sha256"]
