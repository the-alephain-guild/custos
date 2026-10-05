from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET_INDEX = ROOT / "docs/authority/crucible-runner-machine-request-consumer-assets-v1.json"
RECEIPT = ROOT / "docs/authority/receipts/custos-runner-machine-request-v1-consumer-receipt.json"
PINS = ROOT / "docs/authority/vendor/contract-pins-v1.json"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_machine_request_consumer_index_is_historical_evidence() -> None:
    index = _load(ASSET_INDEX)

    assert index["producer"]["repository"] == "tesseract-trading/crucible-rust"
    assert index["consumer"]["repository"] == "tesseract-trading/custos"
    for side in ("producer", "consumer"):
        assert re.fullmatch(r"[0-9a-f]{40}", index[side]["commit"])
    # The recorded digests prove the revision that was accepted; they do not pin
    # today's consumer source or vendored bytes.
    for asset in [*index["consumer_assets"], *index["producer_assets"]]:
        assert isinstance(asset["path"], str)
        assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
        assert isinstance(asset["size_bytes"], int)


def test_machine_request_producer_assets_are_pinned_by_contract_revision() -> None:
    pins = _load(PINS)["contracts"]["alephain.crucible.runner_machine_request.v1"]
    pinned = {asset["local_path"] for asset in pins["assets"]}
    for asset in _load(ASSET_INDEX)["producer_assets"]:
        vendored = f"docs/authority/vendor/crucible-{Path(asset['path']).name}"
        assert vendored in pinned
        assert (ROOT / vendored).is_file()


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
