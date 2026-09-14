from __future__ import annotations

import json
import re
from pathlib import Path

from custos_toolkit import contracts as strategy_contracts

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs/authority/strategy-contract-assets-v1.json"
RECEIPT = (
    ROOT / "docs/authority/receipts/custos-strategy-contract-nautilus-2-v1-handoff-receipt.json"
)


def test_public_contract_surface_has_one_artifact_and_receipt_version() -> None:
    assert "StrategyArtifactRefV1" in strategy_contracts.__all__
    assert "StrategyArtifactPreImportVerificationReceiptV1" in strategy_contracts.__all__
    assert not any(
        re.search(r"(?:ArtifactRef|VerificationReceipt)V(?:[2-9]|[1-9][0-9]+)$", name)
        for name in strategy_contracts.__all__
    )


def test_asset_index_has_no_predecessor_or_compatibility_track() -> None:
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    serialized = json.dumps(index, sort_keys=True)

    assert index["status"] == "CANONICAL_V1_CONTRACT_ASSETS_PUBLISHED"
    assert "consumer_receipts" not in index
    assert "runtime_ready" not in index
    assert {"legacy_non_production", "predecessor", "superseded"}.isdisjoint(index)
    assert "runtime_fallback_allowed" not in serialized
    assert re.search(r'"(?:[^"]+-)?v(?:[2-9]|[1-9][0-9]+)(?:/|\.json")', serialized) is None


def test_authority_manifest_points_only_to_canonical_v1_assets() -> None:
    manifest = json.loads((ROOT / "authority-manifest.json").read_text(encoding="utf-8"))
    paths = {
        entry["path"]
        for entry in manifest["authority_documents"]
        if isinstance(entry, dict) and "path" in entry
    }

    assert str(INDEX.relative_to(ROOT)) in paths
    assert str(RECEIPT.relative_to(ROOT)) in paths
    assert not any(
        re.search(r"(?:^|/)v(?:[2-9]|[1-9][0-9]+)(?:/|\.json$)", path)
        or re.search(
            r"strategy-contract-assets-v(?:[2-9]|[1-9][0-9]+)\.json$",
            path,
        )
        for path in paths
    )
