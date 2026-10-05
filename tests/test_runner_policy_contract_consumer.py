from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/authority/receipts/custos-runner-safety-policy-v1-consumer-receipt.json"
PINS = ROOT / "docs/authority/vendor/contract-pins-v1.json"


def test_runner_policy_pins_one_v1_producer_handoff() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    producer = receipt["producer_authority"]

    assert receipt["receipt_version"] == 1
    assert receipt["runner_state_schema_version"] == 1
    assert receipt["receipt_status"] == "AUTHENTICATED_SAME_POLICY_CONSUMED_DEPLOYED_ISSUANCE_OPEN"
    assert receipt["code_commit"] == "81982761bff68b98893ee38f2159ee7c5656293b"
    assert producer["producer_commit"] == "d52bb16cc307ccd784e0a615a997253d0ca07763"
    assert producer["producer_receipt_commit"] == "2a851b210707c9eb86b5f244def4629081d3e3b0"
    assert receipt["validation"]["status"] == "FOCUSED_RUNNER_POLICY_V1_PASS"
    assert receipt["validation"]["passed"] == 22
    assert receipt["producer_handoff_consumed"] is True
    assert receipt["runtime_policy_consumed"] is True
    assert receipt["runner_policy_capability_ready"] is False
    assert receipt["runtime_ready"] is False
    assert receipt["production_ready"] is False


def _pinned_policy_paths() -> set[str]:
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    pin = pins["contracts"]["alephain.crucible.runner_safety_policy.v1"]
    return {asset["local_path"] for asset in pin["assets"]}


def test_vendored_policy_assets_are_pinned_by_contract_revision() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    producer = receipt["producer_authority"]

    # The historical receipt still matches the frozen producer receipt it names;
    # its schema and golden digests prove the revision it accepted, while the
    # current vendored bytes are pinned by contract revision.
    assert (
        producer["producer_receipt_sha256"]
        == hashlib.sha256((ROOT / producer["producer_receipt_path"]).read_bytes()).hexdigest()
    )
    for key in ("schema_sha256", "golden_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", producer[key])
    pinned = _pinned_policy_paths()
    assert "docs/authority/vendor/crucible-runner-safety-policy-v1.schema.json" in pinned
    assert "docs/authority/vendor/crucible-runner-safety-policy-golden-v1.json" in pinned
    assert "docs/authority/vendor/crucible-runner-safety-policy-golden-v1.json.sha256" in pinned


def test_runner_policy_assets_are_single_revision_v1() -> None:
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
    pinned = _pinned_policy_paths()
    for asset in index["producer_assets"]:
        assert (ROOT / asset["path"]).is_file()
        assert asset["path"] in pinned or asset["path"].endswith("producer-receipt-v1.json")
    schema = json.loads(
        (ROOT / "docs/authority/vendor/crucible-runner-safety-policy-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["x-contract-id"] == "alephain.crucible.runner_safety_policy.v1"
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
