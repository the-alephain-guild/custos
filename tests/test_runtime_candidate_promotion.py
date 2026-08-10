from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.runtime_candidate_promote import (
    RuntimeCandidatePromotionError,
    promote_runtime_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION = (
    ROOT / "docs/authority/external/custos/runtime-image-publication-receipt-v1.json"
)
PUBLICATION_SHA256 = "aa0cd73656128059a53f2aa5db21ac2fbd4fa012c31b30c6d07830c1909d7ead"
DIGEST = "sha256:d66c25345c869ed25d93791bcb98357c7d33c44a3330cee2312dfe377c762690"
SOURCE_REVISION = "b25a5f53e676677b234e98a72ccb36d43d83d0d5"


def _acceptance(owner: str) -> dict[str, Any]:
    if owner == "crucible-rust":
        scope = "deployed-runtime-round-trip"
        repository = "tesseract-trading-ltd/crucible-rust"
    else:
        scope = "strategy-artifact-runtime-acceptance"
        repository = "alchymia-labs/philosophers-stone"
    return {
        "schema_version": 1,
        "receipt_id": f"{owner.upper()}-RUNTIME-CANDIDATE-ACCEPTANCE-V1",
        "owner": owner,
        "acceptance_scope": scope,
        "status": "ACCEPTED",
        "candidate": {
            "repository": "ghcr.io/the-alephain-guild/custos",
            "digest": DIGEST,
            "source_revision": SOURCE_REVISION,
            "publication_receipt_sha256": PUBLICATION_SHA256,
        },
        "issuer": {
            "repository": repository,
            "revision": "1" * 40,
            "workflow_file": ".github/workflows/runtime-candidate-acceptance.yml",
            "workflow_run_id": 1234,
        },
        "evidence": {
            "commands": [
                {"command": "make focused-acceptance", "result": "passed"}
            ],
            "receipts": [
                {
                    "path": (
                        "docs/authority/receipts/"
                        "runtime-candidate-acceptance-v1.json"
                    ),
                    "sha256": "a" * 64,
                    "size_bytes": 1024,
                }
            ],
        },
        "accepted_at": "2026-08-10T12:00:00Z",
        "invariants": {
            "exact_digest_reverified": True,
            "rebuild_permitted": False,
            "retag_substitute_permitted": False,
        },
    }


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def _promote(
    tmp_path: Path,
    *,
    mutate_strategy_owner: bool = False,
) -> tuple[dict[str, Any], Path, Path]:
    crucible_path = tmp_path / "crucible.json"
    strategy_path = tmp_path / "strategy-owner.json"
    crucible = _acceptance("crucible-rust")
    strategy = _acceptance("philosophers-stone")
    if mutate_strategy_owner:
        strategy["candidate"]["digest"] = "sha256:" + "f" * 64
    _write(crucible_path, crucible)
    _write(strategy_path, strategy)
    receipt = promote_runtime_candidate(
        candidate_receipt_path=PUBLICATION,
        crucible_acceptance_path=crucible_path,
        strategy_owner_acceptance_path=strategy_path,
        expected_publication_receipt_sha256=PUBLICATION_SHA256,
        observed_manifest_digest_before=DIGEST,
        observed_manifest_digest_after=DIGEST,
        workflow_revision="2" * 40,
        workflow_run_id=5678,
        workflow_run_attempt=1,
        promoted_at="2026-08-10T12:30:00Z",
        output_path=tmp_path / "promotion.json",
    )
    return receipt, crucible_path, strategy_path


def test_both_exact_owner_acceptances_emit_unchanged_digest_receipt(
    tmp_path: Path,
) -> None:
    receipt, crucible_path, strategy_path = _promote(tmp_path)

    assert receipt["status"] == "PROMOTED_UNCHANGED"
    assert receipt["candidate"]["digest"] == DIGEST
    assert receipt["invariants"] == {
        "manifest_digest_before": DIGEST,
        "manifest_digest_after": DIGEST,
        "exact_digest_unchanged": True,
        "image_rebuilt": False,
        "tag_repointed": False,
        "registry_mutated": False,
    }
    assert receipt["inputs"]["publication"]["sha256"] == PUBLICATION_SHA256
    assert receipt["inputs"]["crucible_acceptance"]["sha256"] == hashlib.sha256(
        crucible_path.read_bytes()
    ).hexdigest()
    strategy_sha256 = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    assert receipt["inputs"]["strategy_owner_acceptance"]["sha256"] == strategy_sha256
    assert receipt["artifact_runtime_ready"] is True
    assert receipt["system_production_ready"] is False


def test_mismatched_strategy_owner_digest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeCandidatePromotionError, match="candidate binding differs"):
        _promote(tmp_path, mutate_strategy_owner=True)


def test_missing_crucible_receipt_is_rejected_before_output(tmp_path: Path) -> None:
    strategy_path = tmp_path / "strategy-owner.json"
    _write(strategy_path, _acceptance("philosophers-stone"))
    output = tmp_path / "promotion.json"

    with pytest.raises(
        RuntimeCandidatePromotionError,
        match="Crucible acceptance receipt is unavailable",
    ):
        promote_runtime_candidate(
            candidate_receipt_path=PUBLICATION,
            crucible_acceptance_path=tmp_path / "missing.json",
            strategy_owner_acceptance_path=strategy_path,
            expected_publication_receipt_sha256=PUBLICATION_SHA256,
            observed_manifest_digest_before=DIGEST,
            observed_manifest_digest_after=DIGEST,
            workflow_revision="2" * 40,
            workflow_run_id=5678,
            workflow_run_attempt=1,
            promoted_at="2026-08-10T12:30:00Z",
            output_path=output,
        )
    assert not output.exists()


def test_candidate_publication_exact_bytes_are_pinned(tmp_path: Path) -> None:
    tampered = tmp_path / "publication.json"
    document = json.loads(PUBLICATION.read_text(encoding="utf-8"))
    document["remaining_gate"] = "tampered"
    _write(tampered, document)
    crucible_path = tmp_path / "crucible.json"
    strategy_path = tmp_path / "strategy-owner.json"
    _write(crucible_path, _acceptance("crucible-rust"))
    _write(strategy_path, _acceptance("philosophers-stone"))

    with pytest.raises(
        RuntimeCandidatePromotionError,
        match="publication receipt sha256 mismatch",
    ):
        promote_runtime_candidate(
            candidate_receipt_path=tampered,
            crucible_acceptance_path=crucible_path,
            strategy_owner_acceptance_path=strategy_path,
            expected_publication_receipt_sha256=PUBLICATION_SHA256,
            observed_manifest_digest_before=DIGEST,
            observed_manifest_digest_after=DIGEST,
            workflow_revision="2" * 40,
            workflow_run_id=5678,
            workflow_run_attempt=1,
            promoted_at="2026-08-10T12:30:00Z",
            output_path=tmp_path / "promotion.json",
        )


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "promotion.json"
    output.write_text("operator evidence\n", encoding="utf-8")
    crucible_path = tmp_path / "crucible.json"
    strategy_path = tmp_path / "strategy-owner.json"
    _write(crucible_path, _acceptance("crucible-rust"))
    _write(strategy_path, _acceptance("philosophers-stone"))

    with pytest.raises(
        RuntimeCandidatePromotionError,
        match="promotion output already exists",
    ):
        promote_runtime_candidate(
            candidate_receipt_path=PUBLICATION,
            crucible_acceptance_path=crucible_path,
            strategy_owner_acceptance_path=strategy_path,
            expected_publication_receipt_sha256=PUBLICATION_SHA256,
            observed_manifest_digest_before=DIGEST,
            observed_manifest_digest_after=DIGEST,
            workflow_revision="2" * 40,
            workflow_run_id=5678,
            workflow_run_attempt=1,
            promoted_at="2026-08-10T12:30:00Z",
            output_path=output,
        )
    assert output.read_text(encoding="utf-8") == "operator evidence\n"
