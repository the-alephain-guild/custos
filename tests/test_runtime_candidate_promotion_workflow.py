from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/promote-runtime-candidate.yml"


def test_runtime_candidate_promotion_is_environment_gated_and_read_only() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in source
    assert "environment: v1-team-runtime-promotion" in source
    assert "contents: read" in source
    assert "packages: read" in source
    assert "id-token: write" in source
    assert "packages: write" not in source
    assert "contents: write" not in source
    for mutation in (
        "docker build ",
        "docker push ",
        "docker tag ",
        "imagetools create",
        "crane tag ",
    ):
        assert mutation not in source


def test_runtime_candidate_promotion_requires_both_fixed_owner_receipts() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "docs/authority/external/crucible-rust/"
        "runtime-candidate-phase-b-acceptance-v1.json"
    ) in source
    assert (
        "docs/authority/external/philosophers-stone/"
        "runtime-candidate-acceptance-v1.json"
    ) in source
    assert 'test -s "$CRUCIBLE_ACCEPTANCE"' in source
    assert 'test -s "$STRATEGY_OWNER_ACCEPTANCE"' in source
    assert "python3 scripts/runtime_candidate_promote.py" in source


def test_candidate_is_reverified_and_promotion_receipt_is_signed() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert source.count("cosign verify ") == 2
    assert "cosign sign-blob --yes --bundle" in source
    assert "cosign verify-blob" in source
    assert '--observed-manifest-digest-before "$CANDIDATE_DIGEST"' in source
    assert '--observed-manifest-digest-after "$CANDIDATE_DIGEST"' in source
    assert "if-no-files-found: error" in source
