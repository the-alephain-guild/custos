from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET_INDEX = ROOT / "docs/authority/crucible-runner-machine-request-consumer-assets-v1.json"
RECEIPT = ROOT / "docs/authority/receipts/custos-runner-machine-request-v1-consumer-receipt.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_machine_request_consumer_assets_are_exactly_pinned() -> None:
    index = _load(ASSET_INDEX)

    assert index["producer"] == {
        "repository": "tesseract-trading/crucible-rust",
        "plan": 100,
        "commit": "d9df47501f7a871c5b0691b8daf6d83fc3cd82c0",
        "status": "CONTRACT_READY_RUNTIME_PENDING",
    }
    assert index["consumer"] == {
        "repository": "tesseract-trading/custos",
        "plan": 19,
        "commit": "71f3f8d5493e22d5ba8c4c0ff3d7c1c83106e592",
        "status": "DIRECT_CREDENTIAL_CLIENT_READY_NATS_TRANSPORT_PENDING",
    }

    for asset in index["consumer_assets"]:
        path = ROOT / asset["path"]
        assert path.stat().st_size == asset["size_bytes"]
        assert _sha256(path) == asset["sha256"]

    producer_assets = {asset["path"]: asset for asset in index["producer_assets"]}
    for relative_path, asset in producer_assets.items():
        vendored = ROOT / "docs/authority/vendor" / f"crucible-{Path(relative_path).name}"
        assert vendored.stat().st_size == asset["size_bytes"]
        assert _sha256(vendored) == asset["sha256"]


def test_machine_request_receipt_binds_index_and_keeps_runtime_claims_false() -> None:
    receipt = _load(RECEIPT)
    index_pin = receipt["asset_index"]

    assert index_pin["path"] == str(ASSET_INDEX.relative_to(ROOT))
    assert index_pin["size_bytes"] == ASSET_INDEX.stat().st_size
    assert index_pin["sha256"] == _sha256(ASSET_INDEX)
    assert receipt["claims"] == {
        "exact_cross_language_golden_verified": True,
        "direct_enrollment_and_credential_client_ready": True,
        "arx_machine_relay_absent": True,
        "durable_request_replay_ledger_verified": False,
        "per_mode_nats_issuance_verified": False,
        "production_transport_ready": False,
    }
    assert receipt["open_blockers"]
