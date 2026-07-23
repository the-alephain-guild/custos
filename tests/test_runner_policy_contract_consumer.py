from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/authority/receipts/custos-runner-safety-policy-v1-consumer-receipt.json"


def test_runner_policy_pins_one_v1_producer_handoff() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))

    assert receipt["receipt_version"] == 1
    assert receipt["runner_state_schema_version"] == 1
    assert receipt["receipt_status"] == "READY_PRODUCER_HANDOFF_PENDING_RUNTIME_PUBLICATION_RECEIPT"
    assert receipt["code_commit"] == "ab5038cf11a7dff6ca79264e6cb91b32758c1357"
    assert receipt["producer_authority"]["producer_commit"] == (
        "d52bb16cc307ccd784e0a615a997253d0ca07763"
    )
    assert receipt["producer_authority"]["producer_receipt_commit"] == (
        "fe930088ea158f8fee057da79aa900674e48b2d3"
    )
    assert receipt["validation"]["status"] == "FOCUSED_RUNNER_POLICY_PRODUCER_HANDOFF_PASS"
    assert receipt["validation"]["passed"] == 20
    assert receipt["producer_handoff_consumed"] is True
    assert receipt["runtime_policy_consumed"] is False
    assert receipt["runner_policy_capability_ready"] is False
    assert receipt["runtime_ready"] is False
    assert receipt["production_ready"] is False


def test_runner_policy_assets_are_exact_and_single_revision_v1() -> None:
    index_path = ROOT / "docs/authority/crucible-runner-safety-policy-consumer-assets-v1.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))

    assert receipt["contract_asset_index"] == {
        "path": str(index_path.relative_to(ROOT)),
        "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "size_bytes": index_path.stat().st_size,
    }
    assert index["policy_revision_axis"] == "revision"
    assert index["legacy_policy_version_or_generation_allowed"] is False
    for asset in index["producer_assets"]:
        path = ROOT / asset["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]
        assert path.stat().st_size == asset["size_bytes"]
    schema = json.loads(
        (ROOT / "docs/authority/vendor/crucible-runner-safety-policy-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "revision" in schema["required"]
    assert "policy_version" not in schema["properties"]
    assert "generation" not in schema["properties"]
    assert schema["properties"]["status"]["enum"] == ["active", "revoked", "expired"]


def test_old_policy_producer_and_slice_receipts_are_not_authority() -> None:
    manifest = json.loads((ROOT / "authority-manifest.json").read_text(encoding="utf-8"))
    paths = {
        entry["path"]
        for entry in manifest["authority_documents"]
        if isinstance(entry, dict) and "path" in entry
    }

    assert str(RECEIPT.relative_to(ROOT)) in paths
    assert "docs/authority/crucible-runner-safety-policy-consumer-assets-v1.json" in paths
    assert "docs/authority/vendor/crucible-runner-safety-policy-v1.schema.json" in paths
    assert "docs/authority/vendor/crucible-runner-safety-policy-golden-v1.json" in paths
    assert "docs/authority/vendor/crucible-runner-safety-policy-golden-v1.json.sha256" in paths
    assert "docs/authority/vendor/crucible-runner-safety-policy-producer-receipt-v1.json" in paths
    assert not any("runner-policy-reservation-v2" in path for path in paths)
    assert not any("runner-policy-native-interception-v3" in path for path in paths)
    assert not any("runner-policy-daemon-composition-v4" in path for path in paths)
    assert not (ROOT / "docs/authority/vendor/crucible-plan-99").exists()
