#!/usr/bin/env python3
"""Generate deterministic strategy execution schemas, inventories, and goldens."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from custos_toolkit.contracts.strategy_execution import (
    ArtifactMemberRole,
    ArtifactMemberV1,
    DigestBindingV1,
    RunnerLocalArtifactPolicyDecisionV1,
    StrategyArtifactPreImportVerificationReceiptV1,
    StrategyArtifactRefV1,
    StrategyManifestV1,
    canonical_model_digest,
)
from custos_toolkit.contracts.toolkit_rc import (
    ToolkitRcAuthorityReceiptV1,
    ToolkitRcPendingReceiptV1,
    ToolkitRcReceiptManifestV1,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_MODEL = (
    ROOT / "packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py"
)
INVENTORY_PATH = "docs/authority/strategy-toolkit-inventory-v1.json"
INDEX_PATH = "docs/authority/strategy-contract-assets-v1.json"
ARTIFACT_REF_SCHEMA_PATH = "docs/gateway-contract/v1/strategy_artifact_ref_v1.schema.json"
ARTIFACT_REF_GOLDEN_PATH = "docs/authority/strategy-artifact-ref-v1.golden.json"
PRE_IMPORT_SCHEMA_PATH = (
    "docs/gateway-contract/v1/strategy_artifact_pre_import_verification_receipt_v1.schema.json"
)
PRE_IMPORT_GOLDEN_PATH = "docs/authority/strategy-artifact-pre-import-verification-v1.golden.json"
PRE_IMPORT_NEGATIVE_PATH = (
    "docs/authority/strategy-artifact-pre-import-verification-v1.negative.json"
)
STRATEGY_MANIFEST_SCHEMA_PATH = "docs/gateway-contract/v1/strategy_manifest_v1.schema.json"
CONTRACT_RECEIPT_PATH = (
    "docs/authority/receipts/custos-strategy-contract-nautilus-2-v1-handoff-receipt.json"
)
HISTORICAL_CONTRACT_EVIDENCE_PATHS = {
    "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json",
    "docs/authority/receipts/custos-strategy-contract-nautilus-2-v1-producer-receipt.json",
    CONTRACT_RECEIPT_PATH,
}
RUNNER_COMMAND_CONSUMER_INDEX_PATH = (
    "docs/authority/crucible-runner-command-consumer-assets-nautilus-2-v1.json"
)
RUNNER_COMMAND_CONSUMER_RECEIPT_PATH = (
    "docs/authority/receipts/custos-crucible-runner-command-nautilus-2-v1-consumer-receipt.json"
)
# The runner command consumer index and receipt record what an earlier Custos
# revision accepted from crucible-rust. Custos now pins crucible-rust contracts
# by revision in docs/authority/vendor/contract-pins-v1.json, so they are
# historical evidence and no longer regenerated.
HISTORICAL_CONTRACT_EVIDENCE_PATHS.update(
    {
        "docs/authority/crucible-runner-command-consumer-assets-v1.json",
        "docs/authority/receipts/custos-crucible-runner-command-v1-consumer-receipt.json",
        RUNNER_COMMAND_CONSUMER_INDEX_PATH,
        RUNNER_COMMAND_CONSUMER_RECEIPT_PATH,
    }
)
# Contract revisions are raised only when the wire shape or the meaning of a field
# changes; a change to descriptive text alone keeps the revision.
ARTIFACT_REF_CONTRACT = ("alephain.custos.strategy_artifact_ref.v1", 1)
PRE_IMPORT_CONTRACT = ("alephain.custos.strategy_artifact_pre_import_verification_receipt.v1", 1)

TOOLKIT_RC_SCHEMA_PATH = "docs/gateway-contract/v1/toolkit_rc_receipt_manifest_v1.schema.json"
TOOLKIT_RC_PENDING_SCHEMA_PATH = (
    "docs/gateway-contract/v1/toolkit_rc_pending_receipt_v1.schema.json"
)
TOOLKIT_RC_AUTHORITY_SCHEMA_PATH = (
    "docs/gateway-contract/v1/toolkit_rc_authority_receipt_v1.schema.json"
)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def member(
    role: ArtifactMemberRole, name: str, digest: str, size: int, media_type: str
) -> ArtifactMemberV1:
    return ArtifactMemberV1(
        role=role,
        name=name,
        media_type=media_type,
        size_bytes=size,
        sha256=digest,
    )


def with_contract_revision(schema: dict, contract: tuple[str, int]) -> dict:
    contract_id, revision = contract
    return {**schema, "x-contract-id": contract_id, "x-contract-revision": revision}


def _sidecar(path: str, content: bytes) -> bytes:
    return f"{sha256(content)}  {Path(path).name}\n".encode("ascii")


def _build_artifact_ref_assets() -> dict[str, bytes]:
    artifact_digest = "7" * 64
    runtime_member = member(
        ArtifactMemberRole.RUNTIME_ARTIFACT,
        "resources/config.schema.json",
        "5" * 64,
        512,
        "application/schema+json",
    )
    artifact_ref = StrategyArtifactRefV1(
        artifact_kind="wheel",
        artifact_coordinate=(f"ghcr.io/alephain/strategy/supertrend@sha256:{artifact_digest}"),
        artifact_sha256=artifact_digest,
        artifact_size_bytes=4096,
        manifest_sha256="8" * 64,
        manifest_size_bytes=1024,
        required_runtime_artifacts=(runtime_member,),
        sbom_sha256="1" * 64,
        contract_schema_sha256="2" * 64,
        source_repository="https://github.com/alchymia-labs/philosophers-stone",
        source_commit="a" * 40,
        normalized_source_tree_sha256="3" * 64,
        python_version="3.12.4",
        engine="nautilus",
        engine_version="2.0.0rc5+sodex.2",
        base_contracts_version="1.0.0rc1",
        engine_toolkit_version="1.0.0rc1",
        build_inputs=(DigestBindingV1(name="uv.lock", sha256="9" * 64),),
    )
    golden = json_bytes(
        {
            "fixture_schema_version": 1,
            "canonical_name": "Custos StrategyArtifactRefV1 pre-sign golden",
            "candidate_status": "PRE_SIGN_ABI_ONLY",
            "production_handoff_ready": False,
            "canonicalization": "sha256-canonical-json-v1",
            "artifact_ref": artifact_ref.model_dump(mode="json"),
            "artifact_ref_digest": canonical_model_digest(artifact_ref),
            "scope_ceiling": (
                "No command, BOM, attestation, acceptance, verification, runtime, or "
                "production readiness claim"
            ),
        }
    )
    schema = json_bytes(
        with_contract_revision(
            StrategyArtifactRefV1.model_json_schema(mode="validation"), ARTIFACT_REF_CONTRACT
        )
    )
    return {
        ARTIFACT_REF_SCHEMA_PATH: schema,
        ARTIFACT_REF_GOLDEN_PATH: golden,
        f"{ARTIFACT_REF_GOLDEN_PATH}.sha256": _sidecar(
            ARTIFACT_REF_GOLDEN_PATH,
            golden,
        ),
    }


def build_v1_contract_assets() -> dict[str, bytes]:
    artifact_assets = _build_artifact_ref_assets()
    resolution_golden = json.loads(
        (
            ROOT / "docs/authority/vendor/"
            "crucible-runner-strategy-release-resolution-v1.golden.json"
        ).read_text(encoding="utf-8")
    )
    release_material = resolution_golden["response"]["strategy_release_material"]
    artifact_binding = release_material["artifact_binding"]
    release_bom = json.loads(artifact_binding["release_bom_canonical_json"])
    release_bom_digest = artifact_binding["release_bom_digest"]
    artifact_ref = StrategyArtifactRefV1.model_validate(artifact_binding["artifact_ref"])
    artifact_ref_digest = artifact_binding["artifact_ref_digest"]
    release_statement = artifact_binding["release_statement"]
    release_statement_digest = artifact_binding["release_statement_digest"]
    detached_ref = artifact_binding["detached_attestation_ref"]
    detached_ref_digest = artifact_binding["detached_attestation_ref_digest"]
    evidence = release_material["artifact_evidence"]
    artifact_evidence_digest = evidence["composite_evidence_digest"]
    acceptance = release_material["snapshot"]
    acceptance_receipt_digest = acceptance["snapshot_digest"]

    policy = RunnerLocalArtifactPolicyDecisionV1(
        authority="custos-runner-local",
        policy_id="custos-runner-artifact-live-v1",
        policy_version=1,
        policy_digest="e" * 64,
        evaluated_at="2026-07-15T12:00:01Z",
        decision="accepted",
        release_bom_digest=release_bom_digest,
        artifact_ref_digest=artifact_ref_digest,
        artifact_evidence_digest=artifact_evidence_digest,
        artifact_acceptance_receipt_digest=acceptance_receipt_digest,
    )
    receipt = StrategyArtifactPreImportVerificationReceiptV1(
        verification_profile="custos-artifact-pre-import-verification-v1",
        verified_at="2026-07-15T12:00:01Z",
        release_bom=release_bom,
        release_bom_digest=release_bom_digest,
        release_statement=release_statement,
        release_statement_digest=release_statement_digest,
        artifact_ref=artifact_ref,
        artifact_ref_digest=artifact_ref_digest,
        detached_attestation_ref=detached_ref,
        detached_attestation_ref_digest=detached_ref_digest,
        crucible_artifact_evidence=evidence,
        crucible_artifact_evidence_digest=artifact_evidence_digest,
        crucible_artifact_acceptance=acceptance,
        crucible_artifact_acceptance_receipt_digest=acceptance_receipt_digest,
        runner_local_policy_decision=policy,
    )
    schema = StrategyArtifactPreImportVerificationReceiptV1.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema_bytes = json_bytes(with_contract_revision(schema, PRE_IMPORT_CONTRACT))
    golden = json_bytes(
        {
            "fixture_schema_version": 1,
            "canonical_name": "Custos canonical V1 strategy execution contract golden",
            "status": "CANONICAL_V1_PENDING_CONSUMER_RECEIPTS",
            "contract_consumer_ready": False,
            "command_consumer_ready": False,
            "runtime_ready": False,
            "production_ready": False,
            "receipt": receipt.model_dump(mode="json"),
            "receipt_digest": canonical_model_digest(receipt),
        }
    )
    negative = json_bytes(
        {
            "fixture_schema_version": 1,
            "canonical_name": "Custos canonical V1 pre-import receipt negative mutations",
            "base_golden": PRE_IMPORT_GOLDEN_PATH,
            "base_golden_sha256": sha256(golden),
            "cases": [
                {
                    "name": "artifact_ref_bundle_field_forbidden",
                    "mutation": {
                        "operation": "add",
                        "path": ["artifact_ref", "bundle_sha256"],
                        "value": "1" * 64,
                    },
                },
                {
                    "name": "artifact_ref_policy_field_forbidden",
                    "mutation": {
                        "operation": "add",
                        "path": ["artifact_ref", "policy_digest"],
                        "value": "2" * 64,
                    },
                },
                {
                    "name": "bom_array_forbidden",
                    "mutation": {"operation": "replace", "path": ["release_bom"], "value": []},
                },
                {
                    "name": "bundle_self_reference_forbidden",
                    "mutation": {
                        "operation": "replace",
                        "path": ["detached_attestation_ref", "bundle_sha256"],
                        "value": detached_ref_digest,
                    },
                },
                {
                    "name": "legacy_crucible_manifest_alias_forbidden",
                    "mutation": {
                        "operation": "add",
                        "path": ["crucible_artifact_evidence", "manifest_digest"],
                        "value": artifact_ref.manifest_sha256,
                    },
                },
                {
                    "name": "legacy_crucible_receipt_alias_forbidden",
                    "mutation": {
                        "operation": "add",
                        "path": ["crucible_artifact_acceptance", "receipt_digest"],
                        "value": acceptance_receipt_digest,
                    },
                },
                {
                    "name": "release_bom_members_alias_forbidden",
                    "mutation": {
                        "operation": "add",
                        "path": ["release_bom_members"],
                        "value": [],
                    },
                },
                {
                    "name": "request_selected_policy_forbidden",
                    "mutation": {
                        "operation": "add",
                        "path": ["runner_local_policy_decision", "requested_by_command"],
                        "value": True,
                    },
                },
                {
                    "name": "verified_members_alias_forbidden",
                    "mutation": {"operation": "add", "path": ["verified_members"], "value": []},
                },
                {
                    "name": "release_statement_fourth_subject_required",
                    "mutation": {
                        "operation": "replace",
                        "path": ["release_statement", "subject"],
                        "value": release_statement["subject"][:3],
                    },
                },
                {
                    "name": "release_statement_subject_order_is_exact",
                    "mutation": {
                        "operation": "replace",
                        "path": ["release_statement", "subject"],
                        "value": [
                            release_statement["subject"][1],
                            release_statement["subject"][0],
                            *release_statement["subject"][2:],
                        ],
                    },
                },
                {
                    "name": "release_statement_duplicate_subject_forbidden",
                    "mutation": {
                        "operation": "replace",
                        "path": ["release_statement", "subject"],
                        "value": [
                            *release_statement["subject"][:3],
                            release_statement["subject"][2],
                        ],
                    },
                },
                {
                    "name": "release_statement_artifact_ref_digest_is_exact",
                    "mutation": {
                        "operation": "replace",
                        "path": ["release_statement", "subject"],
                        "value": [
                            *release_statement["subject"][:3],
                            {
                                "name": "strategy-artifact-ref-v1",
                                "digest": {"sha256": "f" * 64},
                            },
                        ],
                    },
                },
                {
                    "name": "artifact_ref_execution_abi_binding_is_exact",
                    "mutation": {
                        "operation": "replace",
                        "path": ["artifact_ref", "contract_schema_sha256"],
                        "value": "f" * 64,
                    },
                },
                {
                    "name": "signed_claim_artifact_ref_binding_is_exact",
                    "mutation": {
                        "operation": "replace",
                        "path": [
                            "crucible_artifact_evidence",
                            "signed_producer_claims",
                            "artifact_ref_digest",
                        ],
                        "value": "f" * 64,
                    },
                },
            ],
        }
    )
    generated = {
        **artifact_assets,
        PRE_IMPORT_SCHEMA_PATH: schema_bytes,
        PRE_IMPORT_GOLDEN_PATH: golden,
        f"{PRE_IMPORT_GOLDEN_PATH}.sha256": _sidecar(PRE_IMPORT_GOLDEN_PATH, golden),
        PRE_IMPORT_NEGATIVE_PATH: negative,
        f"{PRE_IMPORT_NEGATIVE_PATH}.sha256": _sidecar(PRE_IMPORT_NEGATIVE_PATH, negative),
    }
    index_entries = [
        {"path": path, "sha256": sha256(data), "size_bytes": len(data)}
        for path, data in sorted(generated.items())
    ]
    index = json_bytes(
        {
            "asset_index_schema_version": 1,
            "canonical_name": "Custos canonical first-production V1 strategy execution contracts",
            "status": "CANONICAL_V1_CONTRACT_ASSETS_PUBLISHED",
            "current_contracts": {
                "strategy_artifact_ref": {
                    "type": "StrategyArtifactRefV1",
                    "schema_path": ARTIFACT_REF_SCHEMA_PATH,
                    "golden_path": ARTIFACT_REF_GOLDEN_PATH,
                },
                "pre_import_verification_receipt": {
                    "type": "StrategyArtifactPreImportVerificationReceiptV1",
                    "schema_path": PRE_IMPORT_SCHEMA_PATH,
                    "golden_path": PRE_IMPORT_GOLDEN_PATH,
                    "negative_path": PRE_IMPORT_NEGATIVE_PATH,
                },
            },
            "producer": {
                "repository": "tesseract-trading/custos",
                "source_path": str(SOURCE_MODEL.relative_to(ROOT)),
                "source_sha256": sha256(SOURCE_MODEL.read_bytes()),
            },
            "assets": index_entries,
        }
    )
    generated[INDEX_PATH] = index
    generated[STRATEGY_MANIFEST_SCHEMA_PATH] = json_bytes(
        StrategyManifestV1.model_json_schema(mode="validation")
    )
    return generated


def build_toolkit_rc_foundation_assets() -> dict[str, bytes]:
    return {
        TOOLKIT_RC_SCHEMA_PATH: json_bytes(
            ToolkitRcReceiptManifestV1.model_json_schema(mode="validation")
        ),
        TOOLKIT_RC_PENDING_SCHEMA_PATH: json_bytes(
            ToolkitRcPendingReceiptV1.model_json_schema(mode="validation")
        ),
        TOOLKIT_RC_AUTHORITY_SCHEMA_PATH: json_bytes(
            ToolkitRcAuthorityReceiptV1.model_json_schema(mode="validation")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    assets = build_v1_contract_assets()
    assets.update(build_toolkit_rc_foundation_assets())
    managed_assets = {
        relative: expected
        for relative, expected in assets.items()
        if relative not in HISTORICAL_CONTRACT_EVIDENCE_PATHS
    }
    drift: list[str] = []
    for relative, expected in managed_assets.items():
        path = ROOT / relative
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                drift.append(relative)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
    if drift:
        for relative in drift:
            print(f"generated strategy contract asset differs: {relative}")
        return 1
    if not args.check:
        print(f"generated {len(managed_assets)} strategy contract assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
