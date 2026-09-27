"""Static contract tests for the PS-owned canonical BOM source model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custos_strategy_publisher.model import (
    StrategyReleaseBomReceiptV1,
    StrategyReleaseBomV1,
    bom_json_schema,
    canonical_json_bytes,
    receipt_json_schema,
    sha256_hex,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = REPO_ROOT / "contracts"


def _bom_mapping() -> dict[str, object]:
    return json.loads((AUTHORITY / "strategy-release-bom-v1.golden.json").read_text())


def _receipt_mapping() -> dict[str, object]:
    return json.loads((AUTHORITY / "strategy-release-bom-receipt-v1.golden.json").read_text())


def test_generated_schemas_have_the_source_model_shape() -> None:
    assert json.loads((AUTHORITY / "strategy-release-bom-v1.schema.json").read_text()) == (
        bom_json_schema()
    )
    assert (
        json.loads((AUTHORITY / "strategy-release-bom-receipt-v1.schema.json").read_text())
        == receipt_json_schema()
    )


def test_contract_goldens_are_canonical_and_detached() -> None:
    bom = StrategyReleaseBomV1.from_mapping(_bom_mapping())
    receipt = StrategyReleaseBomReceiptV1.from_mapping(_receipt_mapping())
    receipt.assert_matches(bom)
    assert "attestation_bundle_sha256" not in bom.to_mapping()
    assert "receipt_sha256" not in bom.to_mapping()
    assert "receipt_sha256" not in receipt.to_mapping()
    assert (AUTHORITY / "strategy-release-bom-v1.golden.json").read_bytes() == (
        canonical_json_bytes(bom.to_mapping())
    )


def test_golden_digest_sidecars_bind_exact_bytes() -> None:
    for name in (
        "strategy-release-bom-v1.golden.json",
        "strategy-release-bom-receipt-v1.golden.json",
    ):
        data = (AUTHORITY / name).read_bytes()
        recorded = (AUTHORITY / f"{name}.sha256").read_text().split()[0]
        assert recorded == sha256_hex(data)


def test_bom_rejects_unknown_self_reference_and_uppercase_hash() -> None:
    unknown = _bom_mapping()
    unknown["attestation_bundle_sha256"] = "7" * 64
    with pytest.raises(ValueError, match="unknown"):
        StrategyReleaseBomV1.from_mapping(unknown)

    uppercase = _bom_mapping()
    uppercase["strategy_artifact_sha256"] = "A" * 64
    with pytest.raises(ValueError, match="lowercase sha256"):
        StrategyReleaseBomV1.from_mapping(uppercase)
