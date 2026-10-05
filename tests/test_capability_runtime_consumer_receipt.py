"""Crucible's acceptance of the capability manifest runtime is vendored and bound.

The runtime object in the capability manifest is a cross-repository contract:
Custos writes it and Crucible stores it as the runner's qualification key. The
consumer's receipt is an append-only record of the producer revision it
accepted. These tests hold the vendored copy to that record without pinning the
current asset bytes to it: today's assets are governed by Git and review.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts/check-authority-docs.py"
VENDORED_PATH = (
    "docs/authority/receipts/vendor/crucible-runner-capability-runtime-v1-consumer-receipt.json"
)
ECOSYSTEM_PATH = "docs/authority/ecosystem-authority.json"
INDEX_PATH = "docs/authority/runner-fact-contract-assets-v1.json"
MANIFEST_PATH = ROOT / "authority-manifest.json"

PRODUCER_ASSET_COMMIT = "8cda48daeb09680a8148ea02fce68468b842abf3"
PRODUCER_RUNTIME_CODE_COMMIT = "3abb7c2490445e0fc860ee844ed51f21e6432d97"
CONSUMER_RECEIPT_COMMIT = "89f91858e28977aa1d1776f15c92c83b3f39cee7"
STATUS = "CUSTOS_CAPABILITY_RUNTIME_V1_CONSUMER_ACCEPTED_RUNTIME_OPEN"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_authority_docs", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    for relative in (VENDORED_PATH, ECOSYSTEM_PATH, INDEX_PATH):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _rewrite(root: Path, relative: str, edit) -> None:
    path = root / relative
    document = _json(path)
    edit(document)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def test_vendored_receipt_is_bound_in_the_ecosystem_snapshot() -> None:
    payload = (ROOT / VENDORED_PATH).read_bytes()
    state = _json(ROOT / ECOSYSTEM_PATH)["runner_fact_contract_v1"]
    assert state["capability_runtime_consumer_receipt"] == {
        "repository": "tesseract-trading/crucible-rust",
        "commit": CONSUMER_RECEIPT_COMMIT,
        "producer_path": (
            "docs/authority/receipts/crucible-runner-capability-runtime-v1-consumer-receipt.json"
        ),
        "path": VENDORED_PATH,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "status": STATUS,
    }
    roles = {entry["role"]: entry["path"] for entry in _json(MANIFEST_PATH)["authority_documents"]}
    assert roles["crucible_runner_capability_runtime_consumer_receipt_v1"] == VENDORED_PATH
    assert VENDORED_PATH in _json(MANIFEST_PATH)["doc_drift"]["paths"]


def test_vendored_receipt_accepts_this_repositorys_runtime_revisions() -> None:
    receipt = _json(ROOT / VENDORED_PATH)
    assert receipt["status"] == STATUS
    producer = receipt["producer"]
    assert producer["repository"] == "tesseract-trading/custos"
    assert producer["commit"] == PRODUCER_ASSET_COMMIT
    assert producer["runtime_code_commit"] == PRODUCER_RUNTIME_CODE_COMMIT
    assert "producer_receipt" not in producer
    assert receipt["consumer"]["repository"] == "tesseract-trading/crucible-rust"
    for field in ("runtime_ready", "live_ready", "production_ready"):
        assert receipt[field] is False
    current_paths = {asset["path"] for asset in _json(ROOT / INDEX_PATH)["assets"]}
    assert producer["asset_index"]["producer_path"] == INDEX_PATH
    assert {asset["producer_path"] for asset in producer["assets"].values()} <= current_paths


def test_gate_accepts_the_vendored_receipt(checker: ModuleType, copy_root: Path) -> None:
    errors: list[str] = []
    checker.verify_runner_capability_runtime_consumer(errors, root=copy_root)
    assert errors == []


def test_gate_refuses_a_missing_receipt(checker: ModuleType, copy_root: Path) -> None:
    (copy_root / VENDORED_PATH).unlink()
    errors: list[str] = []
    checker.verify_runner_capability_runtime_consumer(errors, root=copy_root)
    assert any("capability runtime consumer receipt is missing" in error for error in errors)


def test_gate_refuses_an_edited_copy(checker: ModuleType, copy_root: Path) -> None:
    path = copy_root / VENDORED_PATH
    path.write_bytes(path.read_bytes() + b" ")
    errors: list[str] = []
    checker.verify_runner_capability_runtime_consumer(errors, root=copy_root)
    assert any("binding differs" in error for error in errors)


@pytest.mark.parametrize(
    ("label", "edit", "message"),
    [
        # A historical receipt is held to its shape, not to constants: the
        # commits it records must still be full commit ids.
        (
            "an abbreviated producer revision",
            lambda r: r["producer"].update(commit="8cda48da"),
            "producer commit is not a full commit id",
        ),
        (
            "an abbreviated runtime code revision",
            lambda r: r["producer"].update(runtime_code_commit="3abb7c24"),
            "producer runtime_code_commit is not a full commit id",
        ),
        (
            "a receipt cycle",
            lambda r: r["producer"].update(producer_receipt="x"),
            "receipt cycle",
        ),
        (
            "claims runtime readiness",
            lambda r: r.update(runtime_ready=True),
            "runtime_ready must remain false",
        ),
        (
            "an asset the producer does not publish",
            lambda r: r["producer"]["assets"]["runner_fact_batch_golden"].update(
                producer_path="docs/authority/unknown.json"
            ),
            "names an asset outside the RunnerFact V1 index",
        ),
        (
            "an invalid recorded digest",
            lambda r: r["producer"]["assets"]["runner_fact_batch_golden"].update(sha256="x"),
            "recorded digest is invalid",
        ),
    ],
)
def test_gate_refuses_a_receipt_for_something_else(
    checker: ModuleType, copy_root: Path, label: str, edit, message: str
) -> None:
    _rewrite(copy_root, VENDORED_PATH, edit)
    payload = (copy_root / VENDORED_PATH).read_bytes()

    def rebind(document: dict) -> None:
        binding = document["runner_fact_contract_v1"]["capability_runtime_consumer_receipt"]
        binding.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))

    _rewrite(copy_root, ECOSYSTEM_PATH, rebind)
    errors: list[str] = []
    checker.verify_runner_capability_runtime_consumer(errors, root=copy_root)
    assert any(message in error for error in errors), (label, errors)
