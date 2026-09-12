from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "docs/authority/strategy-toolkit-inventory-v1.json"
EXTRACTION_PATH = ROOT / "docs/authority/strategy-toolkit-extraction-v1.json"
AUTHORITY_MANIFEST_PATH = ROOT / "authority-manifest.json"
EXTRACTION_RECEIPT_PATH = (
    ROOT / "docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"
)
BASE_ROOT = ROOT / "packages/custos-strategy-toolkit/src"
NAUTILUS_ROOT = ROOT / "packages/custos-strategy-toolkit-nautilus/src"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _target(relative: str) -> Path:
    path = Path(relative)
    if path.parts[0] == "custos_toolkit":
        return BASE_ROOT / path
    if path.parts[0] == "custos_toolkit_nautilus":
        return NAUTILUS_ROOT / path
    raise AssertionError(f"unsupported target namespace: {relative}")


# Targets retired after the extraction snapshot.
#
# The inventory and the extraction manifest record what was moved at commit
# b5ff7ee9 and are pinned byte-for-byte by the typing-closure receipt, so they do
# not get rewritten when the toolkit later drops a module -- `authority-docs.md`
# is explicit that a receipt attests its own revision and must not permanently fix
# the current tree. What the assertion below can honestly check is that every
# recorded target is either still here or is named here as deliberately retired.
#
# Adding a line is the deliberate act. Say why.
_RETIRED_SINCE_EXTRACTION = {
    # The msgbus -> Redis -> sidecar -> Crucible SSE lane it fed is retired, and 2.0
    # removed the python message bus it published through. The signal-id linkage it
    # carried lives on in adapter/signal_correlation.py.
    "custos_toolkit_nautilus/adapter/event_publisher.py",
}


def test_frozen_inventory_cardinality_and_classification_are_immutable() -> None:
    inventory = _load(INVENTORY_PATH)
    files = inventory["files"]

    assert inventory["inventory_schema_version"] == 1
    assert inventory["file_count"] == len(files) == 241
    assert inventory["category_counts"]["platform_neutral"] == 36
    assert inventory["category_counts"]["nautilus_specific"] == 55
    assert inventory["category_counts"]["private_vendor"] == 150
    assert {entry["migration_action"] for entry in files} == {"extract_zero_rewrite"}


def test_inventory_maps_each_legacy_path_to_one_physical_target() -> None:
    files = _load(INVENTORY_PATH)["files"]
    legacy_paths = [entry["legacy_path"] for entry in files]
    targets = [entry["target_path"] for entry in files]

    assert len(set(legacy_paths)) == len(legacy_paths)
    assert len(set(targets)) == len(targets)
    assert all(not (ROOT / path).exists() for path in legacy_paths)
    missing = [p for p in targets if not _target(p).is_file()]
    assert set(missing) <= _RETIRED_SINCE_EXTRACTION, (
        f"extracted target vanished without being recorded as retired: "
        f"{sorted(set(missing) - _RETIRED_SINCE_EXTRACTION)}"
    )
    assert _RETIRED_SINCE_EXTRACTION <= set(targets), (
        "a path listed as retired was never in the inventory to begin with"
    )


def test_extraction_manifest_has_the_same_one_to_one_mapping() -> None:
    inventory = _load(INVENTORY_PATH)
    extraction = _load(EXTRACTION_PATH)

    expected = {
        (entry["legacy_path"], entry["target_path"], entry["category"])
        for entry in inventory["files"]
    }
    actual = {
        (entry["legacy_path"], entry["target_path"], entry["category"])
        for entry in extraction["files"]
    }

    assert extraction["extraction_schema_version"] == 1
    assert extraction["file_count"] == 241
    assert actual == expected


def test_repository_manifest_registers_the_inventory_as_authority() -> None:
    manifest = _load(AUTHORITY_MANIFEST_PATH)
    records = [
        entry
        for entry in manifest["authority_documents"]
        if entry["role"] == "strategy_toolkit_inventory"
    ]

    assert records == [
        {
            "role": "strategy_toolkit_inventory",
            "path": "docs/authority/strategy-toolkit-inventory-v1.json",
        }
    ]


def test_extraction_receipt_is_scoped_and_reports_open_blockers() -> None:
    receipt = _load(EXTRACTION_RECEIPT_PATH)
    extraction = _load(EXTRACTION_PATH)

    assert receipt["receipt_status"] == "VERIFIED_EXTRACTION_ONLY"
    assert receipt["handoff_ready"] is False
    assert receipt["implementation"]["implementation_commit"] == (
        "b5ff7ee9cea0e78f4462a478bafa42f8f6e18805"
    )
    assert receipt["implementation"]["worktree_clean"] is True
    assert receipt["extraction"]["status"] == "PASS_241_OF_241_ZERO_REWRITE"
    assert (
        receipt["extraction"]["sha256"] == hashlib.sha256(EXTRACTION_PATH.read_bytes()).hexdigest()
    )
    assert receipt["parity"]["oracle_source_commit"] == extraction["source_commit"]
    assert receipt["typing"]["extracted_implementation"] == ("ACK_EXACT_BASELINE_NOT_STRICT")
    assert receipt["typing"]["production_ready"] is False
    assert receipt["typing"]["closure_requirement"] == "strict extracted-source typing closure"
    assert [blocker["capability"] for blocker in receipt["blockers"]] == [
        "strict extracted-source typing closure",
        "public pre-import verifier and attestation",
    ]
