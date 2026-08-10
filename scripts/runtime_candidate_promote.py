#!/usr/bin/env python3
"""Promote the attested Custos runtime candidate without changing OCI bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

CANDIDATE_REPOSITORY = "ghcr.io/the-alephain-guild/custos"
CANDIDATE_PLATFORM = "linux/amd64"
CANDIDATE_SOURCE_REPOSITORY = "the-alephain-guild/custos"
PROMOTION_REPOSITORY = "the-alephain-guild/custos"
PROMOTION_WORKFLOW = ".github/workflows/promote-runtime-candidate.yml"
PROMOTION_IDENTITY = (
    "https://github.com/the-alephain-guild/custos/"
    ".github/workflows/promote-runtime-candidate.yml@refs/heads/main"
)
PROMOTION_ENVIRONMENT = "v1-team-runtime-promotion"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RECEIPT_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]+-V1$")


class RuntimeCandidatePromotionError(ValueError):
    """Raised when immutable promotion evidence is incomplete or inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeCandidatePromotionError(message)


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise RuntimeCandidatePromotionError(f"{label} is unavailable: {path}") from error
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeCandidatePromotionError(f"{label} is not valid UTF-8 JSON") from error
    _require(isinstance(document, dict), f"{label} must be a JSON object")
    return document, payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_timestamp(value: object, label: str) -> str:
    _require(isinstance(value, str), f"{label} must be a string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RuntimeCandidatePromotionError(f"{label} must be RFC3339") from error
    _require(parsed.tzinfo is not None, f"{label} must include a timezone")
    return value


def _validate_publication(
    document: dict[str, Any],
    payload: bytes,
    expected_receipt_sha256: str,
) -> dict[str, str]:
    expected_keys = {
        "schema_version",
        "receipt_id",
        "status",
        "image_key",
        "image",
        "source",
        "workflow",
        "evidence",
        "production_ready",
        "remaining_gate",
    }
    _require(set(document) == expected_keys, "publication receipt shape differs")
    _require(
        _sha256(payload) == expected_receipt_sha256,
        "publication receipt sha256 mismatch",
    )
    _require(document["schema_version"] == 1, "publication schema_version differs")
    _require(
        document["receipt_id"] == "CUSTOS-V1-TEAM-IMAGE-PUBLICATION-V1",
        "publication receipt_id differs",
    )
    _require(
        document["status"] == "IMAGE_PUBLISHED_ATTESTED",
        "publication status differs",
    )
    _require(document["image_key"] == "custos", "publication image_key differs")
    _require(
        document["production_ready"] is False,
        "candidate must not self-claim production",
    )

    image = document["image"]
    _require(isinstance(image, dict), "publication image must be an object")
    _require(
        set(image) == {"repository", "digest", "platform"},
        "publication image shape differs",
    )
    _require(
        image["repository"] == CANDIDATE_REPOSITORY,
        "candidate repository differs",
    )
    _require(
        isinstance(image["digest"], str) and OCI_DIGEST.fullmatch(image["digest"]),
        "candidate digest differs",
    )
    _require(image["platform"] == CANDIDATE_PLATFORM, "candidate platform differs")

    source = document["source"]
    _require(isinstance(source, dict), "publication source must be an object")
    _require(
        set(source) == {"repository", "revision"},
        "publication source shape differs",
    )
    _require(
        source["repository"] == CANDIDATE_SOURCE_REPOSITORY,
        "candidate source repository differs",
    )
    _require(
        isinstance(source["revision"], str) and HEX_40.fullmatch(source["revision"]),
        "candidate source revision differs",
    )

    workflow = document["workflow"]
    _require(isinstance(workflow, dict), "publication workflow must be an object")
    _require(
        workflow.get("repository") == CANDIDATE_SOURCE_REPOSITORY,
        "publication workflow repository differs",
    )
    _require(
        workflow.get("workflow_file") == ".github/workflows/release.yml",
        "publication workflow file differs",
    )
    _require(
        workflow.get("source_ref") == "refs/heads/main",
        "publication source ref differs",
    )
    _require(
        workflow.get("oidc_issuer") == OIDC_ISSUER,
        "publication OIDC issuer differs",
    )
    return {
        "repository": image["repository"],
        "digest": image["digest"],
        "platform": image["platform"],
        "source_revision": source["revision"],
        "receipt_id": document["receipt_id"],
        "receipt_sha256": expected_receipt_sha256,
    }


def _validate_evidence(evidence: object, owner: str) -> None:
    _require(
        isinstance(evidence, dict) and set(evidence) == {"commands", "receipts"},
        f"{owner} evidence shape differs",
    )
    commands = evidence["commands"]
    _require(isinstance(commands, list) and commands, f"{owner} commands are missing")
    for command in commands:
        _require(
            isinstance(command, dict)
            and set(command) == {"command", "result"}
            and isinstance(command["command"], str)
            and bool(command["command"])
            and command["result"] == "passed",
            f"{owner} command evidence differs",
        )
    receipts = evidence["receipts"]
    _require(
        isinstance(receipts, list) and receipts,
        f"{owner} evidence receipts are missing",
    )
    for receipt in receipts:
        _require(
            isinstance(receipt, dict)
            and set(receipt) == {"path", "sha256", "size_bytes"}
            and isinstance(receipt["path"], str)
            and bool(receipt["path"])
            and isinstance(receipt["sha256"], str)
            and SHA256.fullmatch(receipt["sha256"])
            and isinstance(receipt["size_bytes"], int)
            and receipt["size_bytes"] > 0,
            f"{owner} evidence receipt differs",
        )


def _validate_acceptance(
    document: dict[str, Any],
    payload: bytes,
    *,
    expected_owner: str,
    expected_scope: str,
    expected_repository: str,
    candidate: dict[str, str],
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "receipt_id",
        "owner",
        "acceptance_scope",
        "status",
        "candidate",
        "issuer",
        "evidence",
        "accepted_at",
        "invariants",
    }
    _require(set(document) == expected_keys, f"{expected_owner} acceptance shape differs")
    _require(document["schema_version"] == 1, f"{expected_owner} schema_version differs")
    receipt_id = document["receipt_id"]
    _require(
        isinstance(receipt_id, str) and RECEIPT_ID.fullmatch(receipt_id),
        f"{expected_owner} receipt_id differs",
    )
    _require(document["owner"] == expected_owner, f"{expected_owner} owner differs")
    _require(
        document["acceptance_scope"] == expected_scope,
        f"{expected_owner} acceptance scope differs",
    )
    _require(
        document["status"] == "ACCEPTED",
        f"{expected_owner} did not accept candidate",
    )

    expected_candidate = {
        "repository": candidate["repository"],
        "digest": candidate["digest"],
        "source_revision": candidate["source_revision"],
        "publication_receipt_sha256": candidate["receipt_sha256"],
    }
    _require(
        document["candidate"] == expected_candidate,
        f"{expected_owner} candidate binding differs",
    )
    issuer = document["issuer"]
    _require(isinstance(issuer, dict), f"{expected_owner} issuer must be an object")
    _require(
        set(issuer) == {"repository", "revision", "workflow_file", "workflow_run_id"},
        f"{expected_owner} issuer shape differs",
    )
    _require(
        issuer["repository"] == expected_repository,
        f"{expected_owner} issuer repository differs",
    )
    _require(
        isinstance(issuer["revision"], str) and HEX_40.fullmatch(issuer["revision"]),
        f"{expected_owner} issuer revision differs",
    )
    _require(
        isinstance(issuer["workflow_file"], str) and bool(issuer["workflow_file"]),
        f"{expected_owner} workflow file differs",
    )
    _require(
        isinstance(issuer["workflow_run_id"], int) and issuer["workflow_run_id"] > 0,
        f"{expected_owner} workflow run differs",
    )
    _validate_evidence(document["evidence"], expected_owner)
    _validate_timestamp(document["accepted_at"], f"{expected_owner} accepted_at")
    _require(
        document["invariants"]
        == {
            "exact_digest_reverified": True,
            "rebuild_permitted": False,
            "retag_substitute_permitted": False,
        },
        f"{expected_owner} invariants differ",
    )
    return {
        "receipt_id": receipt_id,
        "owner": expected_owner,
        "sha256": _sha256(payload),
        "size_bytes": len(payload),
    }


def promote_runtime_candidate(
    *,
    candidate_receipt_path: Path,
    crucible_acceptance_path: Path,
    strategy_owner_acceptance_path: Path,
    expected_publication_receipt_sha256: str,
    observed_manifest_digest_before: str,
    observed_manifest_digest_after: str,
    workflow_revision: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    promoted_at: str,
    output_path: Path,
) -> dict[str, Any]:
    """Validate both owner decisions and emit an unchanged-digest promotion receipt."""

    _require(
        SHA256.fullmatch(expected_publication_receipt_sha256) is not None,
        "expected publication sha256 differs",
    )
    _require(HEX_40.fullmatch(workflow_revision) is not None, "workflow revision differs")
    _require(workflow_run_id > 0, "workflow run id differs")
    _require(workflow_run_attempt > 0, "workflow run attempt differs")
    _validate_timestamp(promoted_at, "promoted_at")
    _require(not output_path.exists(), f"promotion output already exists: {output_path}")

    publication, publication_bytes = _load_json(
        candidate_receipt_path,
        "candidate publication receipt",
    )
    candidate = _validate_publication(
        publication,
        publication_bytes,
        expected_publication_receipt_sha256,
    )
    _require(
        observed_manifest_digest_before == candidate["digest"],
        "pre-promotion manifest digest differs",
    )
    _require(
        observed_manifest_digest_after == candidate["digest"],
        "post-promotion manifest digest differs",
    )

    crucible, crucible_bytes = _load_json(
        crucible_acceptance_path,
        "Crucible acceptance receipt",
    )
    crucible_input = _validate_acceptance(
        crucible,
        crucible_bytes,
        expected_owner="crucible-rust",
        expected_scope="deployed-runtime-round-trip",
        expected_repository="tesseract-trading-ltd/crucible-rust",
        candidate=candidate,
    )
    strategy_owner, strategy_owner_bytes = _load_json(
        strategy_owner_acceptance_path,
        "strategy owner acceptance receipt",
    )
    strategy_owner_input = _validate_acceptance(
        strategy_owner,
        strategy_owner_bytes,
        expected_owner="philosophers-stone",
        expected_scope="strategy-artifact-runtime-acceptance",
        expected_repository="alchymia-labs/philosophers-stone",
        candidate=candidate,
    )

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": "CUSTOS-RUNTIME-CANDIDATE-PROMOTION-V1",
        "status": "PROMOTED_UNCHANGED",
        "candidate": {
            "repository": candidate["repository"],
            "digest": candidate["digest"],
            "source_revision": candidate["source_revision"],
            "platform": candidate["platform"],
        },
        "inputs": {
            "publication": {
                "receipt_id": candidate["receipt_id"],
                "owner": "custos",
                "sha256": candidate["receipt_sha256"],
                "size_bytes": len(publication_bytes),
            },
            "crucible_acceptance": crucible_input,
            "strategy_owner_acceptance": strategy_owner_input,
        },
        "workflow": {
            "repository": PROMOTION_REPOSITORY,
            "revision": workflow_revision,
            "run_id": workflow_run_id,
            "run_attempt": workflow_run_attempt,
            "workflow_file": PROMOTION_WORKFLOW,
            "workflow_identity": PROMOTION_IDENTITY,
            "environment": PROMOTION_ENVIRONMENT,
            "oidc_issuer": OIDC_ISSUER,
        },
        "invariants": {
            "manifest_digest_before": observed_manifest_digest_before,
            "manifest_digest_after": observed_manifest_digest_after,
            "exact_digest_unchanged": True,
            "image_rebuilt": False,
            "tag_repointed": False,
            "registry_mutated": False,
        },
        "promoted_at": promoted_at,
        "artifact_runtime_ready": True,
        "system_production_ready": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-receipt", type=Path, required=True)
    parser.add_argument("--crucible-acceptance", type=Path, required=True)
    parser.add_argument("--strategy-owner-acceptance", type=Path, required=True)
    parser.add_argument("--expected-publication-receipt-sha256", required=True)
    parser.add_argument("--observed-manifest-digest-before", required=True)
    parser.add_argument("--observed-manifest-digest-after", required=True)
    parser.add_argument("--workflow-revision", required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--promoted-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        promote_runtime_candidate(
            candidate_receipt_path=args.candidate_receipt,
            crucible_acceptance_path=args.crucible_acceptance,
            strategy_owner_acceptance_path=args.strategy_owner_acceptance,
            expected_publication_receipt_sha256=args.expected_publication_receipt_sha256,
            observed_manifest_digest_before=args.observed_manifest_digest_before,
            observed_manifest_digest_after=args.observed_manifest_digest_after,
            workflow_revision=args.workflow_revision,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            promoted_at=args.promoted_at,
            output_path=args.output,
        )
    except RuntimeCandidatePromotionError as error:
        raise SystemExit(f"runtime candidate promotion rejected: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
