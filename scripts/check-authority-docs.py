#!/usr/bin/env python3
"""Validate self-contained Custos authority and optional workspace alignment."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "authority-manifest.json"
STRATEGY_CONTRACT_RECEIPT_PATH = (
    "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"
)
CANONICAL_INDEX_PATH = "docs/authority/strategy-contract-assets-v1.json"
CANONICAL_ARTIFACT_REF_SCHEMA_PATH = "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json"
CANONICAL_ARTIFACT_REF_GOLDEN_PATH = "docs/authority/strategy-artifact-ref-v1.golden.json"
CANONICAL_PRE_IMPORT_SCHEMA_PATH = (
    "docs/gateway-contract/v1/strategy_artifact_pre_import_verification_receipt_v1.schema.json"
)
CANONICAL_PRE_IMPORT_GOLDEN_PATH = (
    "docs/authority/strategy-artifact-pre-import-verification-v1.golden.json"
)
CANONICAL_PRE_IMPORT_NEGATIVE_PATH = (
    "docs/authority/strategy-artifact-pre-import-verification-v1.negative.json"
)
RUNNER_COMMAND_CONSUMER_INDEX_PATH = (
    "docs/authority/crucible-runner-command-consumer-assets-v1.json"
)
RUNNER_COMMAND_CONSUMER_RECEIPT_PATH = (
    "docs/authority/receipts/custos-crucible-runner-command-v1-consumer-receipt.json"
)
RUNNER_FACT_DURABLE_STATE_RECEIPT_PATH = (
    "docs/authority/receipts/custos-runner-durable-state-v1-receipt.json"
)
ARTIFACT_RUNTIME_RECEIPT_PATH = (
    "docs/authority/receipts/custos-strategy-artifact-runtime-v1-receipt.json"
)
ARTIFACT_RUNTIME_SOURCE = "src/custos/artifacts/runtime.py"
ENGINE_LIFECYCLE_RECEIPT_PATH = "docs/authority/receipts/custos-engine-lifecycle-v1-receipt.json"
ENGINE_LIFECYCLE_SOURCE = "src/custos/core/engine_lifecycle.py"
PORTFOLIO_SEMANTICS_RECEIPT_PATH = (
    "docs/authority/receipts/custos-runner-portfolio-semantics-v1-receipt.json"
)
RUNNER_POLICY_CONSUMER_INDEX_PATH = (
    "docs/authority/crucible-runner-safety-policy-consumer-assets-v1.json"
)
RUNNER_POLICY_CONSUMER_SOURCE = "src/custos/contracts/crucible_runner_safety_policy.py"
RUNNER_POLICY_RECEIPT_PATH = (
    "docs/authority/receipts/custos-runner-safety-policy-v1-consumer-receipt.json"
)
RUNNER_POLICY_FAILURE_MATRIX_RECEIPT_PATH = (
    "docs/authority/receipts/custos-runner-safety-policy-failure-matrix-v1.json"
)
RUNNER_NATS_TRANSPORT_CONSUMER_RECEIPT_PATH = (
    "docs/authority/receipts/custos-runner-nats-transport-v1-consumer-receipt.json"
)
RUNNER_FACT_CONTRACT_INDEX_PATH = "docs/authority/runner-fact-contract-assets-v1.json"
RUNNER_FACT_CONTRACT_RECEIPT_PATH = (
    "docs/authority/receipts/custos-runner-fact-v1-producer-receipt.json"
)
RUNNER_COMMAND_CONSUMER_SOURCE = "src/custos/contracts/crucible_runner_command.py"
RUNNER_COMMAND_GOLDEN_PATH = "docs/authority/runner-deployment-command-golden-v1.json"
RUNNER_COMMAND_PRODUCER_COMMIT = "57783973375055cd8af7cc4e681314a0b633f6fa"
REVIEW_VENDOR_ROOT = "docs/authority/receipts/vendor"
CURRENT_STRATEGY_CONTRACT_SOURCE = (
    "packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py"
)
EXPECTED_CONTRACT_SUMMARY = {
    "canonicalization": "sha256-canonical-json-v1",
    "execution_abi": "alephain.strategy_runtime.v1",
    "execution_abi_version": 1,
    "contract_schema_version": 1,
    "entry_point_group": "alephain.strategy_runtime.v1",
}


def resolve(path: str, *, root: Path = ROOT) -> Path:
    return (root / path).resolve()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"authority JSON unreadable at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"authority JSON must be an object: {path}")
    return value


def _nested(value: object, path: tuple[str, ...]) -> object:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _load_gate_json(path: Path, *, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        return load_json(path)
    except SystemExit as exc:
        errors.append(f"{label} is not valid JSON: {exc}")
        return None


def _resolve_local_file(
    value: object,
    *,
    label: str,
    root: Path,
    errors: list[str],
    required_parent: str | None = None,
) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{label} requires a non-empty repository-relative path")
        return None
    relative = Path(value)
    if relative.is_absolute():
        errors.append(f"{label} must be repository-relative")
        return None
    repository_root = root.resolve()
    path = (repository_root / relative).resolve()
    if not path.is_relative_to(repository_root):
        errors.append(f"{label} escapes the repository root")
        return None
    if required_parent is not None:
        parent = (repository_root / required_parent).resolve()
        if not path.is_relative_to(parent):
            errors.append(f"{label} must stay under {required_parent}")
            return None
    if not path.is_file():
        errors.append(f"missing {label}: {path}")
        return None
    return path


def _asset_table(
    entries: object, *, label: str, errors: list[str]
) -> dict[str, tuple[str, int]] | None:
    if not isinstance(entries, list):
        errors.append(f"{label} must be an asset list")
        return None
    result: dict[str, tuple[str, int]] = {}
    valid = True
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"{label} contains a non-object asset")
            valid = False
            continue
        path = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("size_bytes")
        if not isinstance(path, str) or not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
            errors.append(f"{label} contains an invalid path or SHA-256")
            valid = False
            continue
        if type(size) is not int or size < 0:
            errors.append(f"{label} contains an invalid size for {path}")
            valid = False
            continue
        if path in result:
            errors.append(f"{label} contains duplicate asset path {path}")
            valid = False
            continue
        result[path] = (str(digest), size)
    return result if valid else None


def _validate_contract_summary(receipt: dict[str, Any], errors: list[str]) -> None:
    summary = receipt.get("contract_summary")
    if not isinstance(summary, dict):
        errors.append("strategy contract producer receipt lacks a structured contract summary")
        return
    for field, expected in EXPECTED_CONTRACT_SUMMARY.items():
        if summary.get(field) != expected:
            errors.append(f"strategy contract producer contract summary {field} differs")


def _validate_requirement_review(
    name: str,
    slot: object,
    *,
    profile: dict[str, Any],
    receipt: dict[str, Any],
    asset_table: dict[str, tuple[str, int]],
    root: Path,
    errors: list[str],
) -> None:
    if not isinstance(slot, dict):
        errors.append(f"{name} requirements review must be a structured object")
        return
    if slot.get("status") != "ACCEPTED_REQUIREMENTS_REVIEW":
        errors.append(f"{name} requirements review decision is not accepted")
        return
    if slot.get("required_receipt_name") != profile["canonical_name"]:
        errors.append(f"{name} requirements review canonical name differs")
    evidence = slot.get("receipt")
    if not isinstance(evidence, dict):
        errors.append(f"{name} review evidence must be a structured receipt object")
        return
    for field in ("source_repository", "source_path", "source_commit", "sha256", "vendored_path"):
        if evidence.get(field) != profile[field]:
            errors.append(f"{name} review evidence {field} differs")
    vendored_path = _resolve_local_file(
        evidence.get("vendored_path"),
        label=f"{name} vendored requirements review",
        root=root,
        errors=errors,
        required_parent=REVIEW_VENDOR_ROOT,
    )
    if vendored_path is None:
        return
    actual_digest = hashlib.sha256(vendored_path.read_bytes()).hexdigest()
    if actual_digest != evidence.get("sha256"):
        errors.append(f"{name} vendored requirements review byte digest differs")
    review = _load_gate_json(vendored_path, label=f"{name} vendored review", errors=errors)
    if review is None:
        return
    if _nested(review, profile["decision_path"]) != "ACCEPTED_REQUIREMENTS_REVIEW":
        errors.append(f"{name} vendored review decision is not ACCEPTED_REQUIREMENTS_REVIEW")
    if _nested(review, profile["review_schema_path"]) != profile["review_schema_value"]:
        errors.append(f"{name} vendored review schema version differs")

    producer = receipt.get("producer")
    asset_ref = receipt.get("contract_asset_index")
    if not isinstance(producer, dict) or not isinstance(asset_ref, dict):
        errors.append(f"{name} cannot bind an invalid producer receipt")
        return
    reviewed_producer = _nested(review, profile["producer_path"])
    if not isinstance(reviewed_producer, dict):
        errors.append(f"{name} vendored review lacks a producer snapshot")
        return
    expected_commit = producer.get("commit") or producer.get("candidate_commit")
    if reviewed_producer.get("repository") != producer.get("repository"):
        errors.append(f"{name} reviewed producer repository differs")
    if reviewed_producer.get("commit") != expected_commit:
        errors.append(f"{name} reviewed producer commit differs")
    reviewed_source = reviewed_producer.get("source")
    if not isinstance(reviewed_source, dict) or reviewed_source != {
        "path": producer.get("source"),
        "sha256": producer.get("source_sha256"),
    }:
        errors.append(f"{name} reviewed producer source differs")
    reviewed_index = reviewed_producer.get("contract_asset_index")
    if not isinstance(reviewed_index, dict) or reviewed_index != {
        "path": asset_ref.get("path"),
        "sha256": asset_ref.get("sha256"),
    }:
        errors.append(f"{name} reviewed asset index differs")

    summary = receipt.get("contract_summary", {})
    if _nested(review, profile["canonicalization_path"]) != summary.get("canonicalization"):
        errors.append(f"{name} reviewed canonicalization differs")
    if _nested(review, profile["execution_abi_path"]) != summary.get("execution_abi"):
        errors.append(f"{name} reviewed execution ABI differs")
    if _nested(review, profile["entry_point_group_path"]) != summary.get("entry_point_group"):
        errors.append(f"{name} reviewed entry-point group differs")
    reviewed_assets = _asset_table(
        _nested(review, profile["assets_path"]),
        label=f"{name} reviewed assets",
        errors=errors,
    )
    if reviewed_assets is not None and reviewed_assets != asset_table:
        errors.append(f"{name} reviewed schema asset digest set differs")


def verify_strategy_contract_assets(errors: list[str]) -> None:
    index_path = resolve("docs/authority/strategy-contract-assets-v1.json")
    if not index_path.is_file():
        errors.append(f"missing strategy contract asset index: {index_path}")
        return
    index = load_json(index_path)
    for entry in index.get("assets", []):
        path = resolve(str(entry.get("path") or ""))
        if not path.is_file():
            errors.append(f"missing generated strategy contract asset: {path}")
            continue
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != entry.get("sha256"):
            errors.append(f"strategy contract asset digest differs: {path}")
        if path.stat().st_size != entry.get("size_bytes"):
            errors.append(f"strategy contract asset size differs: {path}")


def verify_strategy_contract_authority(errors: list[str]) -> None:
    """Verify the sole first-production strategy contract asset set."""
    import hashlib

    required_paths = {
        "index": resolve(CANONICAL_INDEX_PATH),
        "artifact_schema": resolve(CANONICAL_ARTIFACT_REF_SCHEMA_PATH),
        "artifact_golden": resolve(CANONICAL_ARTIFACT_REF_GOLDEN_PATH),
        "pre_import_schema": resolve(CANONICAL_PRE_IMPORT_SCHEMA_PATH),
        "pre_import_golden": resolve(CANONICAL_PRE_IMPORT_GOLDEN_PATH),
        "pre_import_negative": resolve(CANONICAL_PRE_IMPORT_NEGATIVE_PATH),
        "receipt": resolve(STRATEGY_CONTRACT_RECEIPT_PATH),
        "crucible_consumer_receipt": resolve(
            "docs/authority/receipts/vendor/"
            "crucible-custos-strategy-contract-v1-consumer-receipt.json"
        ),
    }
    missing = [str(path) for path in required_paths.values() if not path.is_file()]
    if missing:
        errors.append("canonical V1 strategy contract assets are missing: " + ", ".join(missing))
        return

    try:
        index = json.loads(required_paths["index"].read_text(encoding="utf-8"))
        artifact_schema = json.loads(required_paths["artifact_schema"].read_text(encoding="utf-8"))
        pre_import_schema = json.loads(
            required_paths["pre_import_schema"].read_text(encoding="utf-8")
        )
        receipt = json.loads(required_paths["receipt"].read_text(encoding="utf-8"))
        crucible_consumer_receipt = json.loads(
            required_paths["crucible_consumer_receipt"].read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"canonical V1 strategy contract assets are unreadable: {exc}")
        return

    if index.get("asset_index_schema_version") != 1:
        errors.append("strategy-contract-assets-v1.json must use asset_index_schema_version=1")
    if index.get("status") != "CANONICAL_V1_CONTRACT_ASSETS_PUBLISHED":
        errors.append("strategy-contract-assets-v1.json must describe immutable published assets")
    forbidden_handoff_keys = {
        "consumer_receipts",
        "contract_consumer_ready",
        "command_consumer_ready",
        "runtime_ready",
        "production_ready",
    }
    present_handoff_keys = forbidden_handoff_keys.intersection(index)
    if present_handoff_keys:
        errors.append(
            "immutable contract asset index must not contain handoff state: "
            + ", ".join(sorted(present_handoff_keys))
        )
    forbidden_index_keys = {"legacy_non_production", "predecessor", "superseded"}
    present_forbidden = forbidden_index_keys.intersection(index)
    if present_forbidden:
        errors.append(
            "canonical V1 asset index must not expose predecessor tracks: "
            + ", ".join(sorted(present_forbidden))
        )

    current = index.get("current_contracts")
    if not isinstance(current, dict):
        errors.append("strategy-contract-assets-v1.json current_contracts must be an object")
    else:
        artifact = current.get("strategy_artifact_ref")
        pre_import = current.get("pre_import_verification_receipt")
        expected_artifact = {
            "type": "StrategyArtifactRefV1",
            "schema_path": str(CANONICAL_ARTIFACT_REF_SCHEMA_PATH),
            "golden_path": str(CANONICAL_ARTIFACT_REF_GOLDEN_PATH),
        }
        expected_pre_import = {
            "type": "StrategyArtifactPreImportVerificationReceiptV1",
            "schema_path": str(CANONICAL_PRE_IMPORT_SCHEMA_PATH),
            "golden_path": str(CANONICAL_PRE_IMPORT_GOLDEN_PATH),
            "negative_path": str(CANONICAL_PRE_IMPORT_NEGATIVE_PATH),
        }
        for label, actual, expected in (
            ("strategy_artifact_ref", artifact, expected_artifact),
            ("pre_import_verification_receipt", pre_import, expected_pre_import),
        ):
            if not isinstance(actual, dict):
                errors.append(f"canonical V1 {label} entry must be an object")
                continue
            for key, value in expected.items():
                if actual.get(key) != value:
                    errors.append(
                        f"canonical V1 {label}.{key} must be {value!r}, got {actual.get(key)!r}"
                    )

    assets = index.get("assets")
    if not isinstance(assets, list) or not assets:
        errors.append("strategy-contract-assets-v1.json assets must be a non-empty list")
    else:
        for position, asset in enumerate(assets):
            if not isinstance(asset, dict):
                errors.append(f"canonical V1 asset entry {position} must be an object")
                continue
            asset_path_value = asset.get("path")
            if not isinstance(asset_path_value, str):
                errors.append(f"canonical V1 asset entry {position} path must be a string")
                continue
            asset_path = ROOT / asset_path_value
            if not asset_path.is_file():
                errors.append(f"canonical V1 indexed asset is missing: {asset_path_value}")
                continue
            payload = asset_path.read_bytes()
            actual_digest = hashlib.sha256(payload).hexdigest()
            if asset.get("sha256") != actual_digest:
                errors.append(f"canonical V1 indexed digest drift: {asset_path_value}")
            if asset.get("size_bytes") != len(payload):
                errors.append(f"canonical V1 indexed size drift: {asset_path_value}")

    if artifact_schema.get("title") != "StrategyArtifactRefV1":
        errors.append("strategy_artifact_ref_v1.schema.json must describe StrategyArtifactRefV1")
    artifact_properties = artifact_schema.get("properties", {})
    schema_version = artifact_properties.get("schema_version", {})
    if schema_version.get("const") != 1:
        errors.append("StrategyArtifactRefV1 schema_version must be const 1")
    forbidden_artifact_fields = {
        "attestation",
        "attestation_bundle",
        "trust_policy",
        "verification_receipt",
    }
    present_fields = forbidden_artifact_fields.intersection(artifact_properties)
    if present_fields:
        errors.append(
            "StrategyArtifactRefV1 must remain a pre-sign execution identity; found "
            + ", ".join(sorted(present_fields))
        )

    if pre_import_schema.get("title") != "StrategyArtifactPreImportVerificationReceiptV1":
        errors.append(
            "strategy_artifact_pre_import_verification_receipt_v1.schema.json must describe the canonical receipt"
        )
    pre_import_version = pre_import_schema.get("properties", {}).get("schema_version", {})
    if pre_import_version.get("const") != 1:
        errors.append(
            "StrategyArtifactPreImportVerificationReceiptV1 schema_version must be const 1"
        )

    if receipt.get("status") != "CANONICAL_V1_PENDING_CONSUMER_RECEIPTS":
        errors.append("canonical V1 contract receipt must remain pending consumer receipts")
    readiness_fields = (
        "contract_consumer_ready",
        "command_consumer_ready",
        "runtime_ready",
        "production_ready",
    )
    if any(receipt.get(field) is not False for field in readiness_fields):
        errors.append(
            "canonical V1 readiness flags must remain false until the coordinated reset is pinned"
        )

    crucible_receipt_pin = {
        "repository": "tesseract-trading/crucible-rust",
        "commit": "a30a62bb2e4115adaad9c036386ebb2b731e0526",
        "path": (
            "docs/authority/receipts/crucible-custos-strategy-contract-v1-consumer-receipt.json"
        ),
        "vendored_path": (
            "docs/authority/receipts/vendor/"
            "crucible-custos-strategy-contract-v1-consumer-receipt.json"
        ),
        "sha256": hashlib.sha256(
            required_paths["crucible_consumer_receipt"].read_bytes()
        ).hexdigest(),
    }
    receipt_consumers = receipt.get("consumers", {})
    if receipt_consumers.get("philosophers_stone", {}).get("receipt") is not None:
        errors.append("Custos receipt must not fabricate the pending PS consumer receipt")
    if receipt_consumers.get("crucible_rust", {}).get("receipt") != crucible_receipt_pin:
        errors.append("Custos producer receipt does not pin the exact Crucible consumer receipt")
    expected_consumer_keys = {
        "canonical_name",
        "consumer",
        "producer",
        "production_ready",
        "receipt_schema_version",
        "runtime_ready",
        "status",
    }
    if set(crucible_consumer_receipt) != expected_consumer_keys:
        errors.append("Crucible consumer receipt must contain one producer-scoped shape")
    if (
        crucible_consumer_receipt.get("producer", {}).get("commit")
        != "39493964e165afbd688beac7205ed4d6943c2c49"
    ):
        errors.append("vendored Crucible receipt does not consume the canonical Custos V1 commit")
    if crucible_consumer_receipt.get("consumer") != "crucible-rust":
        errors.append("vendored Crucible receipt consumer identity differs")
    if (
        crucible_consumer_receipt.get("status")
        != "EXACT_CUSTOS_V1_CONTRACT_PINNED_PENDING_PRODUCER_HANDOFF"
    ):
        errors.append("vendored Crucible receipt status differs")
    if (
        crucible_consumer_receipt.get("runtime_ready") is not False
        or crucible_consumer_receipt.get("production_ready") is not False
    ):
        errors.append("vendored Crucible contract receipt must remain fail closed")


def verify_runner_command_consumer(errors: list[str]) -> None:
    index_path = resolve(RUNNER_COMMAND_CONSUMER_INDEX_PATH)
    receipt_path = resolve(RUNNER_COMMAND_CONSUMER_RECEIPT_PATH)
    source_path = resolve(RUNNER_COMMAND_CONSUMER_SOURCE)
    fixture_path = resolve(RUNNER_COMMAND_GOLDEN_PATH)
    if not all(path.is_file() for path in (index_path, receipt_path, source_path, fixture_path)):
        errors.append("runner command consumer V1 command consumer inventory is incomplete")
        return

    index = load_json(index_path)
    expected_code_status = "READY_DEVELOPMENT_RUNTIME_PENDING_REAL_COMMAND_RECEIPT"
    if index.get("status") != expected_code_status:
        errors.append("runner command consumer code status differs")
    model = index.get("consumer_model")
    if not isinstance(model, dict) or model.get("path") != RUNNER_COMMAND_CONSUMER_SOURCE:
        errors.append("runner command consumer consumer model differs")
    else:
        if model.get("sha256") != hashlib.sha256(source_path.read_bytes()).hexdigest():
            errors.append("runner command consumer consumer model digest differs")
        if model.get("size_bytes") != source_path.stat().st_size:
            errors.append("runner command consumer consumer model size differs")
    consumer_assets = index.get("consumer_assets")
    if not isinstance(consumer_assets, list):
        errors.append("runner command consumer assets must be a list")
        consumer_assets = []
    for asset in consumer_assets:
        if not isinstance(asset, dict):
            errors.append("runner command consumer asset entry must be an object")
            continue
        asset_path = resolve(str(asset.get("path") or ""))
        if not asset_path.is_file():
            errors.append(f"runner command consumer asset is missing: {asset_path}")
            continue
        asset_bytes = asset_path.read_bytes()
        if asset.get("sha256") != hashlib.sha256(asset_bytes).hexdigest() or asset.get(
            "size_bytes"
        ) != len(asset_bytes):
            errors.append(f"runner command consumer asset digest differs: {asset_path}")
    fixture = (
        next(
            (
                asset
                for asset in consumer_assets
                if isinstance(asset, dict) and asset.get("path") == RUNNER_COMMAND_GOLDEN_PATH
            ),
            None,
        )
        if isinstance(consumer_assets, list)
        else None
    )
    if (
        not isinstance(fixture, dict)
        or fixture.get("sha256") != hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    ):
        errors.append("runner command consumer V1 fixture digest differs")
    producer = index.get("producer_authority")
    if not isinstance(producer, dict):
        errors.append("runner command consumer producer authority is missing")
    elif producer.get("receipt") is not None:
        errors.append("runner command consumer cannot pin an unpublished producer receipt")
    elif (
        producer.get("contract") != "CrucibleRunnerDeploymentCommandV1"
        or producer.get("status") != "CONTRACT_V1_PINNED_RUNTIME_RECEIPT_PENDING"
        or producer.get("producer_commit") != RUNNER_COMMAND_PRODUCER_COMMIT
        or producer.get("subject_template") != "crucible.runner.command.v1.<tenant>.<runner>.<mode>"
    ):
        errors.append("runner command consumer V1 producer contract differs")
    if index.get("command_contains_deployment_spec_only") is not True:
        errors.append("runner command consumer command must contain DeploymentSpec only")
    if index.get("command_contract_consumer_ready") is not True:
        errors.append("runner command consumer exact command contract must be ready")
    if index.get("development_material_resolution_ready") is not True:
        errors.append("runner command consumer development material resolution must be ready")
    if index.get("consumer_scope") != [
        "deployment_spec_command",
        "durable_runner_intake",
        "development_material_resolution",
    ]:
        errors.append("runner command consumer scope differs")

    receipt = load_json(receipt_path)
    if receipt.get("receipt_status") != expected_code_status:
        errors.append("runner command consumer receipt status differs")
    bound_index = receipt.get("contract_asset_index")
    if (
        not isinstance(bound_index, dict)
        or bound_index.get("sha256") != hashlib.sha256(index_path.read_bytes()).hexdigest()
    ):
        errors.append("runner command consumer receipt does not bind the current index")
    if receipt.get("runtime_ready") is not False or receipt.get("production_ready") is not False:
        errors.append("runner command consumer cannot claim runtime or production readiness")
    if receipt.get("development_material_resolution_ready") is not True:
        errors.append("runner command receipt development material resolution differs")

    source = source_path.read_text(encoding="utf-8")
    required = (
        "CrucibleRunnerDeploymentCommandV1",
        "DeploymentSpecReadyForRunner",
        "DeploymentInstanceDesiredStateChanged",
        "signed envelope schema_version must be exactly 1",
    )
    if any(marker not in source for marker in required):
        errors.append("runner command consumer sole V1 parser markers are incomplete")
    forbidden = (
        r"RunnerDeploymentCommandV(?:[2-9]|[1-9][0-9]+)",
        r"schema_version must be exactly (?:[2-9]|[1-9][0-9]+)",
        r"ArtifactRefV(?:[2-9]|[1-9][0-9]+)",
    )
    if any(re.search(pattern, source) for pattern in forbidden):
        errors.append("runner command consumer retains a superseded command parser")


def verify_strategy_contract_canonical_source(
    errors: list[str],
    *,
    root: Path = ROOT,
) -> None:
    """Require one package source and no compatibility shim or predecessor receipt."""

    source = resolve(CURRENT_STRATEGY_CONTRACT_SOURCE, root=root)
    if not source.is_file():
        errors.append(f"missing canonical strategy contract source: {source}")
        return
    legacy_shim = resolve("src/custos/contracts/strategy_execution.py", root=root)
    if legacy_shim.exists():
        errors.append(f"legacy strategy contract shim must be deleted: {legacy_shim}")

    receipt_path = resolve(STRATEGY_CONTRACT_RECEIPT_PATH, root=root)
    if not receipt_path.is_file():
        errors.append(f"missing canonical V1 contract receipt: {receipt_path}")
        return
    receipt = load_json(receipt_path)
    producer = receipt.get("producer")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not isinstance(producer, dict):
        errors.append("canonical V1 contract receipt producer must be an object")
    else:
        if producer.get("source_path") != CURRENT_STRATEGY_CONTRACT_SOURCE:
            errors.append("canonical V1 contract receipt source path differs")
        if producer.get("source_sha256") != digest:
            errors.append("canonical V1 contract receipt source digest differs")
    if "predecessor" in receipt or "historical_task_2_source" in receipt:
        errors.append("canonical V1 contract receipt must not retain predecessor authority")


def verify_toolkit_rc_release_authority(manifest: dict[str, object], errors: list[str]) -> None:
    expected_entries = (
        {
            "role": "toolkit_rc_pending_receipt_schema_v1_contract_only",
            "path": ("docs/gateway-contract/v1/toolkit_rc_pending_receipt_v1.schema.json"),
            "contract_only": True,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_authority_receipt_schema_v1",
            "path": "docs/gateway-contract/v1/toolkit_rc_authority_receipt_v1.schema.json",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_oci_distribution_client",
            "path": "scripts/toolkit_rc_oci.py",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_promotion_runner",
            "path": "scripts/toolkit_rc_promote.py",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_independent_promotion_workflow",
            "path": ".github/workflows/promote-toolkit-rc.yml",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_release_readiness_runner",
            "path": "scripts/toolkit_rc_release_readiness.py",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_production_release_workflow",
            "path": ".github/workflows/release-toolkit-rc.yml",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_authority_receipt_v1",
            "path": "docs/authority/receipts/custos-toolkit-rc-authority-v1.json",
            "contract_only": False,
            "ready_receipt_published": True,
        },
        {
            "role": "toolkit_rc_authority_receipt_v1_sha256",
            "path": "docs/authority/receipts/custos-toolkit-rc-authority-v1.json.sha256",
            "contract_only": False,
            "ready_receipt_published": True,
        },
    )
    entries = manifest.get("authority_documents", [])
    for entry in expected_entries:
        if entry not in entries:
            errors.append(f"authority manifest lacks Toolkit RC authority entry {entry['role']}")

    pending_schema_path = resolve(
        "docs/gateway-contract/v1/toolkit_rc_pending_receipt_v1.schema.json"
    )
    if not pending_schema_path.is_file():
        errors.append(f"missing canonical Toolkit RC pending schema: {pending_schema_path}")
    schema = ROOT / "docs/gateway-contract/v1/toolkit_rc_authority_receipt_v1.schema.json"
    if not schema.is_file():
        errors.append(f"missing canonical Toolkit RC V1 authority schema: {schema}")

    ecosystem_path = ROOT / "docs/authority/ecosystem-authority.json"
    if not ecosystem_path.is_file():
        errors.append(f"missing ecosystem authority snapshot: {ecosystem_path}")
        return
    release = load_json(ecosystem_path).get("toolkit_rc_release")
    if not isinstance(release, dict):
        errors.append("ecosystem toolkit_rc_release must be an object")
        return
    expected = {
        "authority_schema": "docs/gateway-contract/v1/toolkit_rc_authority_receipt_v1.schema.json",
        "receipt_status": "READY_TOOLKIT_RC",
        "receipt_present": True,
        "receipt_path": "docs/authority/receipts/custos-toolkit-rc-authority-v1.json",
        "receipt_sha256": "570f69cbb9c796bfa82a57543ba163a4f3586d4fd57ca08a4174ed52ee713702",
        "handoff_ready": True,
        "publication_protocol": "OCI_DISTRIBUTION_V1",
        "runtime_ready": False,
        "production_ready": False,
    }
    for key, value in expected.items():
        if release.get(key) != value:
            errors.append(
                f"canonical Toolkit RC release {key} must be {value!r}, got {release.get(key)!r}"
            )
    blockers = release.get("open_blockers")
    if not isinstance(blockers, list) or not blockers:
        errors.append("canonical Toolkit RC release must remain fail-closed with explicit blockers")
    _verify_toolkit_rc_receipt(errors)


def _verify_toolkit_rc_receipt(errors: list[str]) -> None:
    ecosystem_path = resolve("docs/authority/ecosystem-authority.json")
    ecosystem = load_json(ecosystem_path) if ecosystem_path.is_file() else {}
    release = ecosystem.get("toolkit_rc_release", {})
    if not isinstance(release, dict):
        errors.append("ecosystem toolkit_rc_release must be an object")
        return
    receipt_path = ROOT / "docs/authority/receipts/custos-toolkit-rc-authority-v1.json"
    sidecar_path = receipt_path.with_suffix(f"{receipt_path.suffix}.sha256")
    if not receipt_path.is_file() or not sidecar_path.is_file():
        errors.append("canonical Toolkit RC READY receipt or sidecar is missing")
        return
    receipt_bytes = receipt_path.read_bytes()
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    expected_sidecar = f"{receipt_digest}  {receipt_path.name}\n"
    if sidecar_path.read_text(encoding="ascii") != expected_sidecar:
        errors.append("canonical Toolkit RC READY receipt sidecar differs")
    receipt = load_json(receipt_path)
    expected_receipt = {
        "candidate_version": "0.1.0rc5",
        "source_commit": "a3fd88c4e7e25433b508b2fece94be876630f380",
        "status": "READY_TOOLKIT_RC",
        "ready": True,
        "handoff_ready": True,
        "remote_publication_verified": True,
        "production_credentials_used": True,
        "production_signature_verified": True,
        "runtime_ready": False,
        "production_ready": False,
    }
    for key, value in expected_receipt.items():
        if receipt.get(key) != value:
            errors.append(f"canonical Toolkit RC READY receipt {key} differs")
    if receipt.get("final_blockers") != []:
        errors.append("canonical Toolkit RC READY receipt retains publication blockers")
    publication = receipt.get("publication_receipt")
    if not isinstance(publication, dict):
        errors.append("canonical Toolkit RC READY receipt lacks publication evidence")
    else:
        expected_publication = {
            "manifest_digest": "sha256:670a50cf3725e03744bc2e9efeff74230db934bd83d10a927839254f3d6283d5",
            "workflow_run_id": 29880238705,
            "release_environment": "toolkit-rc-release",
            "tag_readback_verified": True,
            "manifest_commit_verified": True,
        }
        for key, value in expected_publication.items():
            if publication.get(key) != value:
                errors.append(f"canonical Toolkit RC publication {key} differs")
    expected_release = {
        "promotion_run_id": 29883298628,
        "promotion_artifact_digest": "sha256:940bd28ca11b5a8d92f814a0cf39113d93afa89d571648041c5bf002ad418acb",
        "receipt_sha256": receipt_digest,
    }
    for key, value in expected_release.items():
        if release.get(key) != value:
            errors.append(f"ecosystem Toolkit RC release {key} differs")
    expected_policy = {
        "environment_id": 18436102486,
        "required_reviewer": "wukai9203",
        "prevent_self_review": False,
        "can_admins_bypass": False,
        "deployment_branch": "main",
        "verified_at": "2026-07-21T16:00:38Z",
    }
    if release.get("release_environment_policy") != expected_policy:
        errors.append("ecosystem Toolkit RC environment protection evidence differs")

    for relative, required_symbol in (
        (
            "packages/custos-strategy-toolkit/src/custos_toolkit/contracts/toolkit_rc.py",
            "ToolkitRcAuthorityReceiptV1",
        ),
        ("scripts/toolkit_rc_promote.py", "ToolkitRcAuthorityReadyReceiptV1"),
        (".github/workflows/promote-toolkit-rc.yml", "ToolkitRcAuthorityReceiptV1"),
    ):
        source = ROOT / relative
        if not source.is_file():
            errors.append(f"missing canonical Toolkit RC authority source: {source}")
            continue
        value = source.read_text(encoding="utf-8")
        if required_symbol not in value:
            errors.append(f"{relative} does not consume {required_symbol}")
        if "predecessor_oci_manifest" in value:
            errors.append(f"{relative} retains a superseded Toolkit RC authority contract")


def verify_runner_fact_durable_state(manifest: dict[str, Any], errors: list[str]) -> None:
    receipt_path = resolve(RUNNER_FACT_DURABLE_STATE_RECEIPT_PATH)
    if not receipt_path.is_file():
        errors.append("missing RunnerFact durable state durable-state receipt")
        return
    receipt = load_json(receipt_path)
    expected = {
        "receipt_status": "READY_DURABLE_STATE_STORE_ONLY",
        "single_database": True,
        "single_outbox": "runner_fact_outbox",
        "runtime_identity": "deployment_instance_id",
        "durability_port_implemented": True,
        "applied_lifecycle_atomic": True,
        "engine_apply_wired": False,
        "daemon_wired": False,
        "runtime_ready": False,
        "production_ready": False,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(f"RunnerFact durable state receipt {key} differs")
    if receipt.get("stream_key_fields") != [
        "tenant_id",
        "trading_mode",
        "runner_id",
        "deployment_instance_id",
    ]:
        errors.append("RunnerFact durable state receipt stream identity differs")
    if receipt.get("signed_fencing_fields") != [
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
    ]:
        errors.append("RunnerFact durable state receipt signed fencing differs")
    initialization = receipt.get("stream_initialization")
    if not isinstance(initialization, dict) or initialization != {
        "first_production_contract": True,
        "initial_sequence": 1,
        "generation_resets_sequence": False,
        "legacy_stream_compatibility": False,
    }:
        errors.append("RunnerFact durable state receipt stream initialization differs")

    registrations = [
        entry
        for entry in manifest.get("authority_documents", [])
        if entry.get("role") == "runner_fact_durable_state_receipt"
    ]
    if (
        len(registrations) != 1
        or registrations[0].get("path") != RUNNER_FACT_DURABLE_STATE_RECEIPT_PATH
    ):
        errors.append("RunnerFact durable state receipt manifest registration differs")

    snapshot = load_json(resolve("docs/authority/ecosystem-authority.json"))
    state = snapshot.get("runner_state_store")
    if not isinstance(state, dict):
        errors.append("ecosystem authority lacks runner_state_store")
    else:
        for key, value in expected.items():
            if key in state and state.get(key) != value:
                errors.append(f"runner_state_store {key} differs")
        if state.get("status") != "READY_DURABLE_STATE_STORE_ONLY":
            errors.append("runner_state_store status differs")
        if state.get("receipt") != RUNNER_FACT_DURABLE_STATE_RECEIPT_PATH:
            errors.append("runner_state_store receipt path differs")
        if state.get("stream_key_fields") != receipt.get("stream_key_fields"):
            errors.append("runner_state_store stream identity differs")
        if state.get("signed_fencing_fields") != receipt.get("signed_fencing_fields"):
            errors.append("runner_state_store signed fencing differs")

    source = resolve("src/custos/core/runner_fact.py").read_text(encoding="utf-8")
    markers = (
        "class RunnerStateStore",
        "commit_applied_and_enqueue_lifecycle",
        "commit_verified_command_outcome_and_enqueue_fact",
        "CREATE TABLE IF NOT EXISTS desired_deployments",
        '"generation": authority.generation',
    )
    for marker in markers:
        if marker not in source:
            errors.append(f"RunnerFact durable state implementation lacks {marker!r}")
    for forbidden in (
        "runner_" + "stream_cutover",
        "RunnerFact" + "StreamCutover",
        "freeze_" + "stream_cutover",
        "legacy " + "spec-keyed",
    ):
        if forbidden in source:
            errors.append(
                f"RunnerFact durable state retains pre-production compatibility: {forbidden!r}"
            )
    if source.count("CREATE TABLE IF NOT EXISTS runner_fact_outbox") != 1:
        errors.append("RunnerFact durable state must retain exactly one RunnerFact outbox table")


def verify_artifact_runtime(manifest: dict[str, Any], errors: list[str]) -> None:
    """Keep the sole V1 artifact runtime and its unresolved wiring truthful."""

    receipt_path = resolve(ARTIFACT_RUNTIME_RECEIPT_PATH)
    source_path = resolve(ARTIFACT_RUNTIME_SOURCE)
    if not receipt_path.is_file() or not source_path.is_file():
        errors.append("missing artifact runtime V1 artifact runtime authority assets")
        return
    receipt = load_json(receipt_path)
    if receipt.get("receipt_status") != "READY_V1_CODE_PENDING_STRATEGY_RELEASE_RESOLVER":
        errors.append("artifact runtime receipt status differs")
    source = source_path.read_text(encoding="utf-8")
    required = (
        "class StrategyArtifactRuntimeV1",
        "class StrategyReleaseArtifactAuthorityV1",
        "class ArtifactRuntimeCapabilityV1",
        "StrategyArtifactPreImportVerificationReceiptV1",
        "release_authority.assert_command_binding(command)",
    )
    if any(marker not in source for marker in required):
        errors.append("artifact runtime V1 runtime markers are incomplete")
    forbidden = (
        "command.artifact_evidence",
        "command.release_bom",
        "command.artifact_ref",
    )
    if any(marker in source for marker in forbidden):
        errors.append("artifact runtime runtime retains superseded command-owned artifact state")

    ecosystem_path = ROOT / "docs/authority/ecosystem-authority.json"
    runtime = load_json(ecosystem_path).get("strategy_artifact_runtime")
    if not isinstance(runtime, dict):
        errors.append("ecosystem strategy_artifact_runtime must be an object")
        return
    if runtime.get("canonical_v1_only") is not True:
        errors.append("strategy artifact runtime must declare canonical_v1_only=true")
    if runtime.get("compatibility_fallback_allowed") is not False:
        errors.append("strategy artifact runtime compatibility fallback must be disabled")


def verify_engine_lifecycle(manifest: dict[str, Any], errors: list[str]) -> None:
    """Validate additive lifecycle readiness without promoting blocked runtime."""

    receipt_path = resolve(ENGINE_LIFECYCLE_RECEIPT_PATH)
    lifecycle_path = resolve(ENGINE_LIFECYCLE_SOURCE)
    if not receipt_path.is_file() or not lifecycle_path.is_file():
        errors.append("missing engine lifecycle engine lifecycle authority assets")
        return
    receipt = load_json(receipt_path)
    expected = {
        "receipt_status": "PREPARED_BLOCKED_ARTIFACT_RUNTIME_CAPABILITY",
        "engine_adapter_ready": True,
        "protocol_change": "canonical_v1",
        "runtime_identity": "deployment_instance_id",
        "single_database": True,
        "single_outbox": "runner_fact_outbox",
        "runner_state_schema_version": 1,
        "restart_no_duplicate_deploy": True,
        "bounded_restart_budget": True,
        "exponential_backoff": True,
        "timeout_terminal_and_zombie_share_quarantine_path": True,
        "applied_and_terminal_lifecycle_atomic": True,
        "daemon_long_task_supervision": True,
        "artifact_runtime_capability_ready": False,
        "team_daemon_enabled": False,
        "live_ready": False,
        "runtime_ready": False,
        "production_ready": False,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(f"engine lifecycle receipt {key} differs")
    if receipt.get("readiness_checks") != [
        "node_task_alive",
        "data_connectivity_ready",
        "execution_connectivity_ready",
        "portfolio_initialized",
        "reconciliation_initialized",
        "strategy_accepting_lifecycle",
        "mandatory_capabilities_active",
    ]:
        errors.append("engine lifecycle readiness evidence set differs")
    if receipt.get("shutdown_order") != [
        "stop intake and sibling tasks",
        "stop deployments",
        "flush RunnerFact outbox",
        "close publisher and NATS",
    ]:
        errors.append("engine lifecycle shutdown order differs")

    registrations = [
        entry
        for entry in manifest.get("authority_documents", [])
        if entry.get("role") == "engine_lifecycle_receipt"
    ]
    if len(registrations) != 1 or registrations[0].get("path") != ENGINE_LIFECYCLE_RECEIPT_PATH:
        errors.append("engine lifecycle receipt manifest registration differs")

    snapshot = load_json(resolve("docs/authority/ecosystem-authority.json"))
    state = snapshot.get("engine_lifecycle")
    if not isinstance(state, dict):
        errors.append("ecosystem authority lacks engine_lifecycle")
    else:
        if state.get("status") != expected["receipt_status"]:
            errors.append("engine_lifecycle status differs")
        if state.get("receipt") != ENGINE_LIFECYCLE_RECEIPT_PATH:
            errors.append("engine_lifecycle receipt path differs")
        for key in (
            "engine_adapter_ready",
            "protocol_change",
            "runtime_identity",
            "single_database",
            "single_outbox",
            "restart_no_duplicate_deploy",
            "bounded_restart_budget",
            "timeout_terminal_and_zombie_share_quarantine_path",
            "daemon_long_task_supervision",
            "artifact_runtime_capability_ready",
            "team_daemon_enabled",
            "live_ready",
            "runtime_ready",
            "production_ready",
        ):
            if state.get(key) != receipt.get(key):
                errors.append(f"engine_lifecycle {key} differs")
        if state.get("runner_state_schema_version") != 1:
            errors.append("engine_lifecycle current runner_state_schema_version differs")
    artifact_runtime = snapshot.get("strategy_artifact_runtime")
    if not isinstance(artifact_runtime, dict) or any(
        artifact_runtime.get(key) is not False
        for key in ("capability_ready", "daemon_ready", "live_ready", "runtime_ready")
    ):
        errors.append("engine lifecycle must not promote the blocked artifact runtime")

    lifecycle_source = lifecycle_path.read_text(encoding="utf-8")
    protocol_source = resolve("src/custos/core/engine_protocol.py").read_text(encoding="utf-8")
    store_source = resolve("src/custos/core/runner_fact.py").read_text(encoding="utf-8")
    daemon_source = resolve("src/custos/cli/_daemon.py").read_text(encoding="utf-8")
    markers = {
        "engine lifecycle": (
            "class EngineLifecycleSupervisor",
            "ArtifactRuntimeCapabilityV1",
            "commit_applied_and_enqueue_lifecycle",
            "commit_verified_command_outcome_and_enqueue_fact",
            "record_engine_restart",
            "live engine lifecycle remains fail closed",
        ),
        "engine protocol": (
            "class EngineReadyReceipt",
            "class EngineTerminalEvent",
            "async def wait_ready",
            "async def wait_terminal",
        ),
        "runner state store": (
            "RUNNER_STATE_SCHEMA_VERSION: Final = 1",
            "restart_count INTEGER NOT NULL DEFAULT 0",
            "class EngineLifecycleDurableState",
            "load_engine_lifecycle_state",
            "record_engine_restart",
        ),
        "daemon": (
            "_supervise_long_running_tasks",
            "asyncio.FIRST_COMPLETED",
            "_shutdown_in_order",
            "runner_fact_shutdown_flush_incomplete",
        ),
    }
    sources = {
        "engine lifecycle": lifecycle_source,
        "engine protocol": protocol_source,
        "runner state store": store_source,
        "daemon": daemon_source,
    }
    for label, required in markers.items():
        for marker in required:
            if marker not in sources[label]:
                errors.append(f"engine lifecycle {label} lacks {marker!r}")
    for forbidden in ("sqlite3", "CREATE TABLE", "runner_fact_outbox"):
        if forbidden in lifecycle_source:
            errors.append(
                f"engine lifecycle lifecycle creates a forbidden persistence seam {forbidden!r}"
            )
    if store_source.count("CREATE TABLE IF NOT EXISTS runner_fact_outbox") != 1:
        errors.append("engine lifecycle must retain exactly one RunnerFact outbox table")


def verify_portfolio_semantics(manifest: dict[str, Any], errors: list[str]) -> None:
    receipt_path = resolve(PORTFOLIO_SEMANTICS_RECEIPT_PATH)
    if not receipt_path.is_file():
        errors.append("missing portfolio semantics portfolio semantics receipt")
        return
    receipt = load_json(receipt_path)
    expected: dict[str, Any] = {
        "receipt_status": "READY_RELIABLE_PORTFOLIO_SEMANTICS_ONLY",
        "portfolio_semantics_ready": True,
        "single_snapshot_provider": "NautilusPortfolioSnapshotProvider",
        "equity_source": "Nautilus portfolio.equity(venue)",
        "position_pnl_source": "Nautilus position.unrealized_pnl(trusted_mark_price)",
        "open_notional_source": ("absolute quantity multiplied by trusted current mark price"),
        "missing_mark_unreliable": True,
        "missing_equity_unreliable": True,
        "status_breaker_runner_fact_shared_provider": True,
        "breaker_single_snapshot_per_tick": True,
        "breaker_fail_closed_on_unreliable": True,
        "safety_supervisor": "EngineSafetySupervisor",
        "risk_increasing_order_freeze_ready": True,
        "safety_runtime_composed": False,
        "runtime_identity": "deployment_instance_id",
        "runner_safety_policy_ready": False,
        "team_daemon_enabled": False,
        "live_ready": False,
        "runtime_ready": False,
        "production_ready": False,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(f"portfolio semantics receipt {key} differs")
    focused = receipt.get("focused_verification")
    if not isinstance(focused, dict) or focused.get("result") != "20 passed":
        errors.append("portfolio semantics focused verification differs")

    registrations = [
        entry
        for entry in manifest.get("authority_documents", [])
        if entry.get("role") == "portfolio_semantics_receipt"
    ]
    if len(registrations) != 1 or registrations[0].get("path") != PORTFOLIO_SEMANTICS_RECEIPT_PATH:
        errors.append("portfolio semantics receipt manifest registration differs")

    snapshot = load_json(resolve("docs/authority/ecosystem-authority.json"))
    state = snapshot.get("portfolio_semantics")
    if not isinstance(state, dict):
        errors.append("ecosystem authority lacks portfolio_semantics")
    else:
        if state.get("status") != expected["receipt_status"]:
            errors.append("portfolio_semantics status differs")
        if state.get("receipt") != PORTFOLIO_SEMANTICS_RECEIPT_PATH:
            errors.append("portfolio_semantics receipt path differs")
        for key in expected:
            if key == "receipt_status":
                continue
            if state.get(key) != receipt.get(key):
                errors.append(f"portfolio_semantics {key} differs")

    provider_path = resolve("src/custos/engines/nautilus/portfolio_snapshot.py")
    host_path = resolve("src/custos/engines/nautilus/host.py")
    protocol_path = resolve("src/custos/core/engine_protocol.py")
    breaker_path = resolve("src/custos/core/fallback_breaker.py")
    safety_path = resolve("src/custos/core/engine_safety.py")
    source_paths = (provider_path, host_path, protocol_path, breaker_path, safety_path)
    if not all(path.is_file() for path in source_paths):
        errors.append("portfolio semantics source inventory is incomplete")
        return
    provider_source = provider_path.read_text(encoding="utf-8")
    host_source = host_path.read_text(encoding="utf-8")
    protocol_source = protocol_path.read_text(encoding="utf-8")
    breaker_source = breaker_path.read_text(encoding="utf-8")
    safety_source = safety_path.read_text(encoding="utf-8")

    provider_markers = (
        "class NautilusPortfolioSnapshotProvider",
        "portfolio.equity(venue)",
        "position.unrealized_pnl(mark)",
        "mark_price_unavailable:",
        "portfolio_equity_missing:",
    )
    for marker in provider_markers:
        if marker not in provider_source:
            errors.append(f"portfolio semantics provider lacks {marker!r}")
    provider_definitions = sum(
        path.read_text(encoding="utf-8").count("class NautilusPortfolioSnapshotProvider")
        for path in resolve("src/custos").rglob("*.py")
    )
    if provider_definitions != 1:
        errors.append("portfolio semantics must have exactly one portfolio snapshot provider")

    for marker in (
        "self._portfolio_snapshot_provider.snapshot",
        "snapshot.runner_fact_rows()",
        "snapshot.engine_positions()",
        "reliable=False",
        'unreliable_reason="deployment_not_active"',
    ):
        if marker not in host_source:
            errors.append(f"portfolio semantics host lacks {marker!r}")
    for forbidden in (
        "open_notional + unrealized_total",
        'getattr(position, "unrealized_pnl"',
        "kernel.portfolio.equity",
    ):
        if forbidden in host_source:
            errors.append(f"portfolio semantics host retains proxy valuation {forbidden!r}")
    for marker in ("reliable: bool = True", "unreliable_reason: str | None = None"):
        if marker not in protocol_source:
            errors.append(f"portfolio semantics EngineStatus lacks {marker!r}")
    if "def fail_closed(" not in breaker_source:
        errors.append("portfolio semantics breaker lacks fail_closed")

    for marker in (
        "class EngineSafetySupervisor",
        "status = await self._engine.get_engine_status",
        "self._breaker.fail_closed",
        "self._breaker.evaluate",
        "self._engine.flatten_positions",
    ):
        if marker not in safety_source:
            errors.append(f"portfolio semantics safety supervisor lacks {marker!r}")
    if "get_open_notional" in safety_source:
        errors.append("portfolio semantics breaker tick reads a second portfolio snapshot")

    for section_name in ("engine_lifecycle", "strategy_artifact_runtime"):
        section = snapshot.get(section_name)
        if not isinstance(section, dict) or any(
            section.get(key) is not False
            for key in ("team_daemon_enabled", "live_ready", "runtime_ready", "production_ready")
            if key in section
        ):
            errors.append(f"portfolio semantics must not promote {section_name}")


def verify_runner_policy_consumer(manifest: dict[str, Any], errors: list[str]) -> None:
    index_path = resolve(RUNNER_POLICY_CONSUMER_INDEX_PATH)
    receipt_path = resolve(RUNNER_POLICY_RECEIPT_PATH)
    consumer_path = resolve(RUNNER_POLICY_CONSUMER_SOURCE)
    if not all(path.is_file() for path in (index_path, receipt_path, consumer_path)):
        errors.append("runner policy consumer runner policy V1 consumer inventory is incomplete")
        return

    index = load_json(index_path)
    expected_status = "READY_PRODUCER_HANDOFF_PENDING_RUNTIME_PUBLICATION_RECEIPT"
    if index.get("schema_version") != 1 or index.get("status") != expected_status:
        errors.append("runner policy consumer asset index status differs")
    expected_assets = {
        "docs/authority/vendor/crucible-runner-safety-policy-v1.schema.json",
        "docs/authority/vendor/crucible-runner-safety-policy-golden-v1.json",
        "docs/authority/vendor/crucible-runner-safety-policy-golden-v1.json.sha256",
        "docs/authority/vendor/crucible-runner-safety-policy-producer-receipt-v1.json",
    }
    assets = _asset_table(
        index.get("producer_assets"), label="runner policy producer assets", errors=errors
    )
    if assets is not None:
        if set(assets) != expected_assets:
            errors.append("runner policy producer asset paths differ")
        for path, (digest, size) in assets.items():
            local = resolve(path)
            if not local.is_file():
                errors.append(f"missing runner policy producer asset: {local}")
            elif hashlib.sha256(local.read_bytes()).hexdigest() != digest:
                errors.append(f"runner policy producer asset digest differs: {path}")
            elif local.stat().st_size != size:
                errors.append(f"runner policy producer asset size differs: {path}")
    model = index.get("consumer_model")
    if not isinstance(model, dict) or model.get("path") != RUNNER_POLICY_CONSUMER_SOURCE:
        errors.append("runner policy consumer model differs")
    else:
        if model.get("sha256") != hashlib.sha256(consumer_path.read_bytes()).hexdigest():
            errors.append("runner policy consumer model digest differs")
        if model.get("size_bytes") != consumer_path.stat().st_size:
            errors.append("runner policy consumer model size differs")
    producer = index.get("producer_authority")
    if not isinstance(producer, dict):
        errors.append("runner policy producer authority is missing")
    elif (
        producer.get("contract") != "RunnerAggregateCapPolicyV1"
        or producer.get("authority_coordinate") != "crucible.runner-aggregate-cap-policy.v1"
        or producer.get("subject_template") != "crucible.runner.policy.v1.<tenant>.<runner>.<mode>"
        or not re.fullmatch(r"[0-9a-f]{40}", str(producer.get("producer_commit") or ""))
        or not re.fullmatch(r"[0-9a-f]{40}", str(producer.get("producer_receipt_commit") or ""))
        or producer.get("producer_receipt")
        != "docs/authority/vendor/crucible-runner-safety-policy-producer-receipt-v1.json"
        or producer.get("runtime_receipt") is not None
    ):
        errors.append("runner policy producer authority binding differs")
    if index.get("policy_revision_axis") != "revision":
        errors.append("runner policy must have one revision axis")
    if index.get("legacy_policy_version_or_generation_allowed") is not False:
        errors.append("runner policy cannot retain version or generation aliases")

    receipt = load_json(receipt_path)
    if receipt.get("receipt_version") != 1:
        errors.append("runner policy receipt_version must be 1")
    if receipt.get("runner_state_schema_version") != 1:
        errors.append("runner policy must use canonical runner state schema V1")
    if receipt.get("receipt_status") != expected_status:
        errors.append("runner policy contract status differs from the canonical V1 evidence")
    bound_index = receipt.get("contract_asset_index")
    if (
        not isinstance(bound_index, dict)
        or bound_index.get("path") != RUNNER_POLICY_CONSUMER_INDEX_PATH
        or bound_index.get("sha256") != hashlib.sha256(index_path.read_bytes()).hexdigest()
        or bound_index.get("size_bytes") != index_path.stat().st_size
    ):
        errors.append("runner policy receipt does not bind the current asset index")
    if receipt.get("producer_authority", {}).get("producer_commit") != producer.get(
        "producer_commit"
    ):
        errors.append("runner policy receipt producer commit differs from asset index")
    if receipt.get("producer_authority", {}).get("producer_receipt_commit") != producer.get(
        "producer_receipt_commit"
    ):
        errors.append("runner policy receipt producer handoff commit differs from asset index")
    if (ROOT / "docs/authority/vendor/crucible-plan-99").exists():
        errors.append("superseded Crucible runner policy vendor pins must be deleted")


def verify_runner_policy_runtime(manifest: dict[str, Any], errors: list[str]) -> None:
    receipt_path = (
        ROOT / "docs/authority/receipts/custos-runner-safety-policy-v1-consumer-receipt.json"
    )
    if not receipt_path.is_file():
        errors.append(f"missing canonical runner policy V1 receipt: {receipt_path}")
        return
    receipt = load_json(receipt_path)
    if receipt.get("validation") != {
        "command": "uv run pytest -q tests/test_runner_policy_runtime.py "
        "tests/test_runner_fact_store.py "
        "tests/test_order_reservation.py",
        "passed": 20,
        "required_before_runtime_ready": True,
        "status": "FOCUSED_RUNNER_POLICY_PRODUCER_HANDOFF_PASS",
    }:
        errors.append("runner policy V1 focused validation evidence differs")
    if receipt.get("runtime_policy_consumed") is not False:
        errors.append("runner policy V1 must remain fail-closed before owner policy consumption")
    if receipt.get("runtime_ready") is not False or receipt.get("production_ready") is not False:
        errors.append("runner policy V1 cannot claim runtime or production readiness")

    matrix_path = resolve(RUNNER_POLICY_FAILURE_MATRIX_RECEIPT_PATH)
    if not matrix_path.is_file():
        errors.append("runner policy local failure-matrix receipt is missing")
        return
    matrix = load_json(matrix_path)
    if (
        matrix.get("schema_version") != 1
        or matrix.get("status") != "LOCAL_FAILURE_MATRIX_VERIFIED_RUNTIME_ACCEPTANCE_OPEN"
        or matrix.get("runtime_policy_consumed") is not False
        or matrix.get("runtime_ready") is not False
        or matrix.get("live_ready") is not False
        or matrix.get("production_ready") is not False
    ):
        errors.append("runner policy local failure-matrix readiness boundary differs")
    if matrix.get("validation") != {
        "command": "uv run pytest -q tests/test_runner_policy_runtime.py "
        "tests/test_runner_fact_store.py "
        "tests/test_order_reservation.py",
        "passed": 22,
        "status": "FOCUSED_RUNNER_POLICY_LOCAL_FAILURE_MATRIX_PASS",
    }:
        errors.append("runner policy local failure-matrix validation differs")
    source = matrix.get("source")
    source_path = resolve(str(source.get("path") if isinstance(source, dict) else ""))
    if (
        not isinstance(source, dict)
        or not source_path.is_file()
        or source.get("sha256") != hashlib.sha256(source_path.read_bytes()).hexdigest()
        or source.get("size_bytes") != source_path.stat().st_size
    ):
        errors.append("runner policy local failure-matrix source binding differs")


def verify_runner_nats_transport(manifest: dict[str, Any], errors: list[str]) -> None:
    receipt_path = resolve(RUNNER_NATS_TRANSPORT_CONSUMER_RECEIPT_PATH)
    source_paths = {
        "transport": resolve("src/custos/core/nats_transport.py"),
        "authority_client": resolve("src/custos/core/runner_nats_authority.py"),
        "consumer": resolve("src/custos/core/nats_client.py"),
        "publisher": resolve("src/custos/core/runner_fact.py"),
        "acceptance": resolve("tests/integration/runner_command_lifecycle_process.py"),
        "daemon": resolve("src/custos/cli/_daemon.py"),
        "cli": resolve("src/custos/cli/subcommands/nats_transport.py"),
    }
    if not receipt_path.is_file() or not all(path.is_file() for path in source_paths.values()):
        errors.append("runner NATS transport V1 inventory is incomplete")
        return

    receipt = load_json(receipt_path)
    if receipt.get("receipt_status") != "CLONE_LOCAL_CONSUMER_VERIFIED":
        errors.append("runner NATS transport clone-local status differs")
    producer = receipt.get("producer_authority")
    if not isinstance(producer, dict) or producer.get("authority_ready") is not True:
        errors.append("runner NATS transport producer authority truth differs")
    elif (
        producer.get("producer_commit") != "e2499bb"
        or producer.get("authority_receipt")
        != "tesseract-trading/crucible-rust/docs/authority/receipts/"
        "crucible-runner-nats-broker-authority-v1.json"
        or producer.get("http_contract_receipt")
        != "tesseract-trading/crucible-rust/docs/authority/receipts/"
        "crucible-runner-nats-transport-http-v1.json"
    ):
        errors.append("runner NATS transport producer handoff receipt differs")
    if producer.get("control_streams") != {
        "sim": "CRUCIBLE_RUNNER_CONTROL_SIM_V1",
        "live": "CRUCIBLE_RUNNER_CONTROL_LIVE_V1",
    }:
        errors.append("runner NATS transport control stream domains differ")
    if producer.get("command_subject_prefix") != "crucible.runner.command.v1":
        errors.append("runner NATS transport command subject prefix differs")
    if producer.get("policy_subject_prefix") != "crucible.runner.policy.v1":
        errors.append("runner NATS transport policy subject prefix differs")
    assets = producer.get("contract_assets")
    if not isinstance(assets, dict):
        errors.append("runner NATS transport producer contract assets are missing")
    else:
        for path_key, digest_key in (("schema", "schema_sha256"), ("golden", "golden_sha256")):
            asset_path = assets.get(path_key)
            expected_digest = assets.get(digest_key)
            if not isinstance(asset_path, str) or not isinstance(expected_digest, str):
                errors.append(f"runner NATS transport {path_key} asset pin is invalid")
                continue
            resolved = resolve(asset_path)
            if (
                not resolved.is_file()
                or hashlib.sha256(resolved.read_bytes()).hexdigest() != expected_digest
            ):
                errors.append(f"runner NATS transport {path_key} asset digest differs")

    contract = receipt.get("v1_contract")
    if not isinstance(contract, dict):
        errors.append("runner NATS transport V1 contract is missing")
    else:
        expected = {
            "authority_coordinate": "crucible.runner-nats-transport.v1",
            "durable_name_prefix": "custos-control-v1-",
            "delivery_subject_prefix": "custos.runner.control.v1.delivery.",
            "control_filter_order": [
                "crucible.runner.command.v1.<tenant>.<runner>.<mode>",
                "crucible.runner.policy.v1.<tenant>.<runner>.<mode>",
            ],
            "runner_fact_subject_prefix": "crucible.runner.fact.v1.",
            "one_authority_per_mode": True,
            "one_encrypted_vault_per_mode": True,
            "multi_mode_jwt_allowed": False,
            "exact_tenant_runner_mode_filters_required": True,
            "legacy_subject_fallback_allowed": False,
            "legacy_credential_parser_allowed": False,
            "operation_begin_endpoint": "POST /api/v1/runner-nats/transport-operations",
            "operation_result_endpoint": ("POST /api/v1/runner-nats/transport-operation-results"),
            "arx_authorization_intent_required": True,
            "user_nkey_possession_proof_required": True,
            "pending_operation_persisted_before_network": True,
            "restart_reuses_operation_id_and_seed": True,
            "pending_result_exposes_user_jwt": False,
            "compatibility_lifecycle_routes_present": False,
        }
        for key, value in expected.items():
            if contract.get(key) != value:
                errors.append(f"runner NATS transport V1 contract {key} differs")

    truth = receipt.get("truth")
    if not isinstance(truth, dict):
        errors.append("runner NATS transport truth is missing")
    else:
        if truth.get("local_real_nats_revocation_gate") != {
            "command": "make verify-nats-revocation",
            "code_commit": "ba562a91a3d160293f3f0631c47a954164766662",
            "image": "nats:2.10-alpine",
            "image_id": ("sha256:dcadf8f23b60edaaafbe901db7773e2c07947f269c475d8d33d3b46a18b0a7f9"),
            "status": "PASS",
            "tests_passed": 1,
            "admin_preprovisioned_runner_fact_stream": True,
            "runner_stream_admin_authority": False,
            "launched_authenticated_publisher_process": True,
            "jetstream_puback_observed": True,
            "nats_msg_id_equals_batch_id": True,
            "sqlite_outbox_empty_after_puback": True,
            "signed_command_verified": True,
            "durable_desired_state_written": True,
            "artifact_activation_durable_before_apply": True,
            "sandbox_engine_ready": True,
            "applied_lifecycle_outbox_committed_before_command_ack": True,
            "command_ack_observed": True,
            "command_delivered_via_jetstream": True,
            "immutable_artifact_materialization": False,
            "crucible_projection_observed": False,
        }:
            errors.append("runner NATS transport local real-NATS revocation evidence differs")
        if truth.get("authenticated_runtime_projection_gate") != {
            "command": (
                "CRUCIBLE_REPO=<crucible-rust> "
                "make verify-authenticated-runtime-projection"
            ),
            "custos_code_commit": "ba562a91a3d160293f3f0631c47a954164766662",
            "crucible_code_commit": "79b5acb9a97bf9639b46ad8be4c25e03412f8092",
            "status": "PASS",
            "tests_passed": 1,
            "canonical_runner_fact_stream": "CRUCIBLE_RUNNER_FACT_V1",
            "canonical_runner_fact_stream_subjects": "crucible.runner.fact.v1.*.*.*",
            "runner_publish_acl_remained_exact": True,
            "command_delivered_via_existing_durable": True,
            "sandbox_engine_ready": True,
            "command_ack_after_lifecycle_commit": True,
            "runner_fact_puback_observed": True,
            "same_batch_accepted_by_crucible": True,
            "postgresql_projection_work_count": 1,
            "dynamic_capability_authority_matched": True,
            "immutable_artifact_materialization": False,
            "production_services_launched": False,
        }:
            errors.append("authenticated runtime projection evidence differs")
        for key in (
            "begin_poll_consumer_implemented",
            "restart_safe_pending_operation_implemented",
            "broker_receipt_required_for_rotate_or_revoke",
            "jwt_signature_and_acl_verification_implemented",
            "tls_ca_and_exact_server_name_required",
        ):
            if truth.get(key) is not True:
                errors.append(f"runner NATS transport implemented truth {key} differs")
        for key in (
            "production_transport_credential_provisioned",
            "production_durable_verified",
            "real_nats_integration_passed",
            "team_daemon_enabled",
            "runtime_ready",
            "live_ready",
            "production_ready",
        ):
            if truth.get(key) is not False:
                errors.append(f"runner NATS transport pending truth {key} differs")

    transport = source_paths["transport"].read_text(encoding="utf-8")
    authority_client = source_paths["authority_client"].read_text(encoding="utf-8")
    cli = source_paths["cli"].read_text(encoding="utf-8")
    required_markers = (
        "custos-control-v1-",
        "custos.runner.control.v1.delivery",
        "crucible.runner.command.v1",
        "crucible.runner.policy.v1",
        "crucible.runner.fact.v1",
        "CRUCIBLE_RUNNER_CONTROL_SIM_V1",
        "CRUCIBLE_RUNNER_CONTROL_LIVE_V1",
        "runner_nats_transport_domain",
    )
    if any(marker not in transport for marker in required_markers):
        errors.append(
            "runner NATS transport transport does not implement the sole V1 subject profile"
        )
    if any(
        marker in transport
        for marker in (
            "CRUCIBLE_RUNNER_COMMAND_SIM_V1",
            "CRUCIBLE_RUNNER_COMMAND_LIVE_V1",
            "custos.runner.command.v1.delivery",
            "custos-v1-",
        )
    ):
        errors.append("runner NATS transport transport retains the superseded command-only profile")
    required_authority_markers = (
        "/api/v1/runner-nats/transport-operations",
        "/api/v1/runner-nats/transport-operation-results",
        "crucible.runner.nats-key-possession.v1",
        "authorization_intent_id",
        "user_key_possession_signature_base64",
    )
    if any(marker not in authority_client for marker in required_authority_markers):
        errors.append("runner NATS transport authority client contract differs")
    combined_lifecycle = "\n".join((transport, authority_client, cli))
    if any(
        marker in combined_lifecycle
        for marker in (
            "runner-nats-transport/enroll",
            "runner-nats-transport/rotate",
            "runner-nats-transport/activate",
            "revoke-superseded",
            "revocation-challenge",
            "revocation-evidence",
            "RunnerNatsRevocationChallenge",
            "RunnerNatsRevocationObservation",
            "from_issued_response",
        )
    ):
        errors.append("runner NATS transport retains a superseded lifecycle contract")
    if re.search(
        r"(?:custos-v|runner_command_v|RunnerDeploymentCommandV)"
        r"(?:[2-9]|[1-9][0-9]+)",
        transport,
    ):
        errors.append("runner NATS transport transport retains a superseded subject profile")

    ecosystem_path = ROOT / "docs/authority/ecosystem-authority.json"
    ecosystem = load_json(ecosystem_path) if ecosystem_path.is_file() else {}
    snapshot = ecosystem.get("runner_nats_transport_consumer")
    if not isinstance(snapshot, dict):
        errors.append("runner NATS transport ecosystem snapshot is missing")
    elif (
        snapshot.get("status") != "CLONE_LOCAL_CONSUMER_VERIFIED"
        or snapshot.get("producer_commit") != "e2499bb"
        or snapshot.get("user_nkey_possession_proof") is not True
        or snapshot.get("pending_operation_persisted_before_network") is not True
        or snapshot.get("restart_reuses_operation_id_and_seed") is not True
        or snapshot.get("compatibility_lifecycle_routes_present") is not False
        or snapshot.get("local_authenticated_runner_fact_puback_passed") is not True
        or snapshot.get("admin_preprovisioned_runner_fact_stream") is not True
        or snapshot.get("runner_stream_admin_authority") is not False
        or snapshot.get("launched_authenticated_publisher_process") is not True
        or snapshot.get("local_signed_command_engine_puback_passed") is not True
        or snapshot.get("command_delivered_via_jetstream") is not True
        or snapshot.get("authenticated_runtime_projection_gate_passed") is not True
        or snapshot.get("same_batch_accepted_by_crucible_postgresql") is not True
        or snapshot.get("immutable_artifact_materialization_in_gate") is not False
        or snapshot.get("crucible_projection_in_authenticated_gate") is not True
        or snapshot.get("sqlite_outbox_empty_after_puback") is not True
    ):
        errors.append("runner NATS transport ecosystem status differs")


def verify_runner_fact_contract(manifest: dict[str, Any], errors: list[str]) -> None:
    index_path = resolve(RUNNER_FACT_CONTRACT_INDEX_PATH)
    receipt_path = resolve(RUNNER_FACT_CONTRACT_RECEIPT_PATH)
    consumer_receipt_path = resolve(
        "docs/authority/receipts/vendor/"
        "crucible-runner-fact-v1-consumer-receipt.json"
    )
    if not index_path.is_file() or not receipt_path.is_file():
        errors.append("canonical RunnerFact V1 index or receipt is missing")
        return
    if not consumer_receipt_path.is_file():
        errors.append("Crucible RunnerFact V1 consumer receipt is missing")
        return
    index = load_json(index_path)
    receipt = load_json(receipt_path)
    consumer_receipt = load_json(consumer_receipt_path)
    if index.get("authority_coordinate") != "custos.runner-fact.v1":
        errors.append("RunnerFact authority coordinate must be custos.runner-fact.v1")
    if index.get("status") != "CANONICAL_V1_PENDING_RUNTIME_RECEIPTS":
        errors.append("RunnerFact V1 must remain pending runtime receipts")
    if "supersedes_candidate_coordinate" in index or "superseded_candidate_status" in index:
        errors.append("RunnerFact V1 index must not retain candidate lineage")
    assets = index.get("assets")
    if not isinstance(assets, list) or not assets:
        errors.append("RunnerFact V1 assets must be a non-empty list")
    else:
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("path"), str):
                errors.append("RunnerFact V1 asset entry is malformed")
                continue
            asset_path = ROOT / asset["path"]
            if not asset_path.is_file():
                errors.append(f"missing RunnerFact V1 asset: {asset_path}")
                continue
            payload = asset_path.read_bytes()
            if asset.get("sha256") != hashlib.sha256(payload).hexdigest():
                errors.append(f"RunnerFact V1 asset digest drift: {asset_path}")
            if asset.get("size_bytes") != len(payload):
                errors.append(f"RunnerFact V1 asset size drift: {asset_path}")
    index_payload = index_path.read_bytes()
    if receipt.get("receipt_schema_version") != 1:
        errors.append("RunnerFact V1 producer receipt schema differs")
    if receipt.get("status") != "PHASE_A_CONSUMER_ACCEPTED_RUNTIME_OPEN":
        errors.append("RunnerFact V1 producer receipt status differs")
    if receipt.get("producer_commit") != "cce76931884de36c9606db02d94cf4124e7164b5":
        errors.append("RunnerFact V1 producer receipt does not pin the immutable asset commit")
    expected_index_binding = {
        "path": RUNNER_FACT_CONTRACT_INDEX_PATH,
        "sha256": hashlib.sha256(index_payload).hexdigest(),
        "size_bytes": len(index_payload),
    }
    if receipt.get("asset_index") != expected_index_binding:
        errors.append("RunnerFact V1 producer receipt index binding differs")
    consumer_payload = consumer_receipt_path.read_bytes()
    expected_consumer_binding = {
        "crucible_rust": {
            "repository": "tesseract-trading/crucible-rust",
            "commit": "818242c5708d055b2c4b1e5dc9d6a9e5570667fe",
            "producer_path": (
                "docs/authority/receipts/"
                "crucible-runner-fact-v1-consumer-receipt.json"
            ),
            "local_path": (
                "docs/authority/receipts/vendor/"
                "crucible-runner-fact-v1-consumer-receipt.json"
            ),
            "sha256": hashlib.sha256(consumer_payload).hexdigest(),
            "size_bytes": len(consumer_payload),
            "status": "EXACT_CUSTOS_RUNNER_FACT_V1_ACCEPTED_RUNTIME_OPEN",
        }
    }
    if receipt.get("consumer_receipts") != expected_consumer_binding:
        errors.append("RunnerFact V1 external consumer receipt binding differs")
    producer = consumer_receipt.get("producer")
    consumer = consumer_receipt.get("consumer")
    if not isinstance(producer, dict) or (
        producer.get("asset_commit")
        != "cce76931884de36c9606db02d94cf4124e7164b5"
    ):
        errors.append("Crucible RunnerFact V1 receipt producer asset commit differs")
    if isinstance(producer, dict) and "producer_receipt" in producer:
        errors.append("Crucible RunnerFact V1 receipt must not create a receipt cycle")
    if not isinstance(consumer, dict) or (
        consumer.get("code_commit")
        != "506afcdcc6b5342f84810668b0367e983f406857"
    ):
        errors.append("Crucible RunnerFact V1 receipt consumer code commit differs")
    for field in ("runtime_ready", "live_ready", "production_ready"):
        if consumer_receipt.get(field) is not False:
            errors.append(f"Crucible RunnerFact V1 consumer receipt {field} must remain false")
    for field in (
        "runtime_rc",
        "real_runtime_round_trip_ready",
        "live_ready",
        "runtime_ready",
        "production_ready",
    ):
        if receipt.get(field) is not False:
            errors.append(f"RunnerFact V1 producer receipt {field} must remain false")
    local_publication_path = resolve(
        "docs/authority/receipts/custos-runner-fact-local-publication-v1.json"
    )
    if not local_publication_path.is_file():
        errors.append("RunnerFact V1 development-local publication receipt is missing")
        return
    local_publication = load_json(local_publication_path)
    expected_source = {
        "repository": "tesseract-trading/custos",
        "code_commit": "aeca1feb636a2335f54f2772ac1b439ac2ca6e3d",
        "publisher_process": "tests/integration/runner_fact_publication_process.py",
        "publisher_process_sha256": (
            "51795a4a0d1c467c1ff04317462eea035b658a692ef7b3598c9712e981b297f6"
        ),
        "publisher_process_size_bytes": 5667,
        "test": "tests/integration/test_runner_fact_publication.py",
        "test_sha256": "092f9aebbf2b55bbc1fd2bbaa8be17cde4ada8b7df7d8f7f78a6be374a1d0b6b",
        "test_size_bytes": 5496,
        "command": "make verify-runner-fact-publication",
        "verified_at": "2026-07-26",
        "tests_passed": 1,
    }
    if (
        local_publication.get("status")
        != "DEVELOPMENT_LOCAL_PUBLICATION_ACCEPTED_RUNTIME_OPEN"
        or local_publication.get("source") != expected_source
        or local_publication.get("authority_coordinate") != "custos.runner-fact.v1"
        or local_publication.get("development_only") is not True
    ):
        errors.append("RunnerFact V1 development-local publication authority differs")
    for path_field, digest_field, size_field in (
        ("publisher_process", "publisher_process_sha256", "publisher_process_size_bytes"),
        ("test", "test_sha256", "test_size_bytes"),
    ):
        source_path = resolve(str(expected_source[path_field]))
        payload = source_path.read_bytes() if source_path.is_file() else b""
        if (
            hashlib.sha256(payload).hexdigest() != expected_source[digest_field]
            or len(payload) != expected_source[size_field]
        ):
            errors.append(
                f"RunnerFact V1 development-local publication source drift: {source_path}"
            )
    evidence = local_publication.get("evidence", {})
    for field in (
        "production_runner_fact_outbox_used",
        "production_runner_fact_signer_used",
        "production_jetstream_publisher_used",
        "publisher_process_launched",
        "subject_scope_enforced",
        "producer_puback_received",
        "durable_puback_receipt_committed",
        "puback_broker_stream_sequence_recorded",
        "puback_payload_digest_recorded",
        "puback_receipt_survives_sqlite_reopen",
        "outbox_deleted_only_after_puback",
        "nats_message_id_equals_batch_id",
    ):
        if evidence.get(field) is not True:
            errors.append(
                f"RunnerFact V1 development-local publication evidence {field} differs"
            )
    for field in (
        "custos_daemon_launched",
        "engine_command_applied",
        "crucible_projection_observed",
        "runtime_round_trip_ready",
        "runtime_rc",
        "live_ready",
        "runtime_ready",
        "production_ready",
    ):
        if local_publication.get(field) is not False:
            errors.append(
                f"RunnerFact V1 development-local publication {field} must remain false"
            )


def verify_runner_fact_authority(errors: list[str]) -> None:
    ecosystem = load_json(ROOT / "docs/authority/ecosystem-authority.json")
    state = ecosystem.get("runner_fact_contract_v1")
    if not isinstance(state, dict):
        errors.append("ecosystem runner_fact_contract_v1 must be an object")
        return
    if state.get("authority_coordinate") != "custos.runner-fact.v1":
        errors.append("ecosystem RunnerFact V1 authority coordinate differs")
    if state.get("status") != "PHASE_A_CONSUMER_ACCEPTED_RUNTIME_OPEN":
        errors.append("ecosystem RunnerFact V1 status differs")
    if state.get("receipt") != (
        "docs/authority/receipts/custos-runner-fact-v1-producer-receipt.json"
    ):
        errors.append("ecosystem RunnerFact V1 producer receipt path differs")
    consumer_path = (
        ROOT
        / "docs/authority/receipts/vendor/"
        "crucible-runner-fact-v1-consumer-receipt.json"
    )
    consumer_payload = consumer_path.read_bytes() if consumer_path.is_file() else b""
    if state.get("consumer_receipt") != {
        "repository": "tesseract-trading/crucible-rust",
        "commit": "818242c5708d055b2c4b1e5dc9d6a9e5570667fe",
        "path": (
            "docs/authority/receipts/vendor/"
            "crucible-runner-fact-v1-consumer-receipt.json"
        ),
        "sha256": hashlib.sha256(consumer_payload).hexdigest(),
        "size_bytes": len(consumer_payload),
    }:
        errors.append("ecosystem RunnerFact V1 consumer receipt binding differs")
    if state.get("local_publication_receipt") != {
        "path": "docs/authority/receipts/custos-runner-fact-local-publication-v1.json",
        "sha256": "78473e28cbc6db66cee96163749f4de21ac9387916fc108c4f6986cdff6542bf",
        "size_bytes": 2182,
        "status": "DEVELOPMENT_LOCAL_PUBLICATION_ACCEPTED_RUNTIME_OPEN",
    }:
        errors.append("ecosystem RunnerFact V1 local publication receipt binding differs")


def main() -> int:
    manifest = load_json(MANIFEST_PATH)
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("authority manifest schema_version must be 1")

    expected_evolution_policy = {
        "current_production_version": 1,
        "feature_changes": "IN_PLACE_V1",
        "runtime_compatibility_layers_allowed": False,
        "v2_requires_external_production_consumer": True,
        "v2_requires_explicit_migration_window": True,
    }
    if manifest.get("contract_evolution_policy") != expected_evolution_policy:
        errors.append("first-production V1 contract evolution policy differs")
    for entry in manifest.get("authority_documents", []):
        path = resolve(entry["path"])
        if not path.is_file():
            errors.append(f"missing {entry['role']}: {path}")
    verify_toolkit_rc_release_authority(manifest, errors)
    snapshot_path = resolve("docs/authority/ecosystem-authority.json")
    if snapshot_path.is_file():
        snapshot = load_json(snapshot_path)
        if snapshot.get("migration_heads") != manifest.get("expected_migration_heads"):
            errors.append("authority snapshot migration heads differ from manifest")
        fixture = snapshot.get("runner_command_golden_fixture")
        if not isinstance(fixture, dict):
            errors.append("authority snapshot lacks runner command golden fixture")
        else:
            fixture_path = resolve(str(fixture.get("path") or ""))
            if not fixture_path.is_file():
                errors.append(f"missing runner command golden fixture: {fixture_path}")
            else:
                fixture_bytes = fixture_path.read_bytes()
                actual_digest = hashlib.sha256(fixture_bytes).hexdigest()
                if actual_digest != fixture.get("sha256"):
                    errors.append("runner command golden fixture sha256 differs from snapshot")
                sidecar_path = resolve(str(fixture.get("sha256_sidecar") or ""))
                expected_sidecar = f"{actual_digest}  {fixture_path.name}\n"
                if not sidecar_path.is_file():
                    errors.append(f"missing runner command golden sidecar: {sidecar_path}")
                elif sidecar_path.read_text(encoding="ascii") != expected_sidecar:
                    errors.append("runner command golden sidecar differs from fixture")
                sibling_value = fixture.get("optional_sibling_path")
                if isinstance(sibling_value, str) and sibling_value:
                    sibling_path = resolve(sibling_value)
                    if sibling_path.is_file() and sibling_path.read_bytes() != fixture_bytes:
                        errors.append(
                            f"runner command golden differs from optional sibling: {sibling_path}"
                        )
    verify_strategy_contract_assets(errors)
    verify_strategy_contract_authority(errors)
    verify_runner_command_consumer(errors)
    verify_artifact_runtime(manifest, errors)
    verify_runner_fact_durable_state(manifest, errors)
    verify_engine_lifecycle(manifest, errors)
    verify_portfolio_semantics(manifest, errors)
    verify_runner_policy_consumer(manifest, errors)
    verify_runner_policy_runtime(manifest, errors)
    verify_runner_nats_transport(manifest, errors)
    verify_runner_fact_contract(manifest, errors)
    verify_runner_fact_authority(errors)
    verify_strategy_contract_canonical_source(errors)
    for entry in manifest.get("external_optional_documents", []):
        path = resolve(entry["path"])
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in entry.get("must_contain", []):
            if phrase not in text:
                errors.append(f"optional workspace authority {path} lacks {phrase!r}")
    drift = manifest.get("doc_drift", {})
    patterns = [re.compile(value, re.IGNORECASE) for value in drift.get("forbidden_regex", [])]
    for value in drift.get("paths", []):
        path = resolve(value)
        if not path.is_file():
            errors.append(f"missing drift-scanned file: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                errors.append(f"forbidden topology residue in {path}: {match.group(0)!r}")
    for claim in manifest.get("required_claims", []):
        path = resolve(claim["path"])
        if not path.is_file():
            errors.append(f"missing required-claim file: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in claim.get("must_contain", []):
            if phrase not in text:
                errors.append(f"{path} lacks required claim {phrase!r}")
    if errors:
        print("authority gate failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("authority gate passed for standalone custos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
