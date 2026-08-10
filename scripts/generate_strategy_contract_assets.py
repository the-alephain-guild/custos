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
CONTRACT_RECEIPT_PATH = "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"
CRUCIBLE_CONSUMER_RECEIPT_VENDOR_PATH = (
    "docs/authority/receipts/vendor/"
    "crucible-custos-strategy-contract-v1-consumer-receipt.json"
)
CUSTOS_STRATEGY_CONTRACT_COMMIT = "91c5a648c4db4e16f8dde11f48c8e60b30e0a813"
CRUCIBLE_CONSUMER_RECEIPT_COMMIT = "0a9aa68c78f717f19fdcc9eada9f942980fcf422"
RUNNER_COMMAND_CONSUMER_INDEX_PATH = (
    "docs/authority/crucible-runner-command-consumer-assets-v1.json"
)
RUNNER_COMMAND_CONSUMER_RECEIPT_PATH = (
    "docs/authority/receipts/custos-crucible-runner-command-v1-consumer-receipt.json"
)
RUNNER_COMMAND_CONSUMER_SOURCE = "src/custos/contracts/crucible_runner_command.py"
RUNNER_COMMAND_CONSUMER_TEST = "tests/test_runner_deployment_command_golden.py"
RUNNER_MATERIAL_AUTHORITY_SOURCE = "src/custos/core/runner_material_authority.py"
RUNNER_MATERIAL_AUTHORITY_TEST = "tests/test_runner_material_authority.py"
DEVELOPMENT_ARTIFACT_RUNTIME_SOURCE = "src/custos/artifacts/development_runtime.py"
RUNNER_COMMAND_GOLDEN_PATH = "docs/authority/runner-deployment-command-golden-v1.json"
RUNNER_COMMAND_GOLDEN_SIDECAR_PATH = (
    "docs/authority/runner-deployment-command-golden-v1.json.sha256"
)
RUNNER_COMMAND_FINGERPRINT_VECTOR_PATH = (
    "tests/fixtures/runner_command/runner_command_fingerprint_v1.json"
)
RUNNER_COMMAND_PRODUCER_COMMIT = "8c2c4eff20ae1ba38bbab54cdf7844ef25e1187d"
RUNNER_COMMAND_PRODUCER_RECEIPT_COMMIT = "72c0ba90623e61641e14a3ede8168f3e379e6f4c"
RUNNER_COMMAND_PRODUCER_RECEIPT_VENDOR_PATH = (
    "docs/authority/receipts/vendor/crucible-runner-command-publication-v1.json"
)
RUNNER_COMMAND_PRODUCER_RECEIPT_STATUS = (
    "LOCAL_AUTHENTICATED_PG_JETSTREAM_PUBACK_RECOVERY_VERIFIED_DEPLOYED_ACCEPTANCE_OPEN"
)
RUNNER_COMMAND_CONTRACT_ASSETS = (
    (
        "runner_command_golden",
        RUNNER_COMMAND_GOLDEN_PATH,
        RUNNER_COMMAND_GOLDEN_PATH,
    ),
    (
        "runner_command_golden_sha256",
        RUNNER_COMMAND_GOLDEN_SIDECAR_PATH,
        RUNNER_COMMAND_GOLDEN_SIDECAR_PATH,
    ),
)
RUNNER_COMMAND_SUBJECT_TEMPLATE = "crucible.runner.command.v1.<tenant>.<runner>.<mode>"
RUNNER_STRATEGY_RESOLUTION_RECEIPT_VENDOR_PATH = (
    "docs/authority/receipts/vendor/crucible-runner-strategy-resolution-v1.json"
)
RUNNER_STRATEGY_RESOLUTION_PRODUCER_COMMIT = "34e0f139c64b5e1ed8fe5136a432e7b00aeea9df"
RUNNER_STRATEGY_RESOLUTION_CONTRACT_COMMIT = "a4972de"
RUNNER_STRATEGY_RESOLUTION_RECEIPT_COMMIT = "5a531c2bbc8c9ec08a631d51ebedae5650fc05a0"
RUNNER_STRATEGY_RESOLUTION_CONTRACT_ASSETS = (
    (
        "docs/authority/runner-strategy-release-resolution-v1.schema.json",
        "docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.schema.json",
    ),
    (
        "docs/authority/runner-strategy-release-resolution-v1.schema.json.sha256",
        "docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.schema.json.sha256",
    ),
    (
        "docs/authority/runner-strategy-release-resolution-v1.golden.json",
        "docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json",
    ),
    (
        "docs/authority/runner-strategy-release-resolution-v1.golden.json.sha256",
        "docs/authority/vendor/crucible-runner-strategy-release-resolution-v1.golden.json.sha256",
    ),
)
RUNNER_COMMAND_CONSUMER_STATUS = (
    "LOCAL_STRATEGY_RESOLUTION_CONTRACT_CONSUMED_RUNTIME_ACCEPTANCE_OPEN"
)

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
        engine_version="1.230.0",
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
    schema = json_bytes(StrategyArtifactRefV1.model_json_schema(mode="validation"))
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
    schema_bytes = json_bytes(schema)
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
    crucible_receipt_bytes = (ROOT / CRUCIBLE_CONSUMER_RECEIPT_VENDOR_PATH).read_bytes()
    crucible_receipt_document = json.loads(crucible_receipt_bytes)
    expected_consumer_assets = {
        "artifact_ref_golden": {
            "local_path": "docs/authority/custos-strategy-artifact-ref-v1.golden.json",
            "producer_path": ARTIFACT_REF_GOLDEN_PATH,
            "sha256": sha256(generated[ARTIFACT_REF_GOLDEN_PATH]),
        },
        "artifact_ref_schema": {
            "local_path": "docs/authority/custos-strategy-artifact-ref-v1.schema.json",
            "producer_path": ARTIFACT_REF_SCHEMA_PATH,
            "sha256": sha256(generated[ARTIFACT_REF_SCHEMA_PATH]),
        },
        "contract_asset_index": {
            "local_path": "docs/authority/custos-strategy-contract-assets-v1.json",
            "producer_path": INDEX_PATH,
            "sha256": sha256(index),
        },
        "pre_import_receipt_schema": {
            "local_path": (
                "docs/authority/"
                "custos-strategy-artifact-pre-import-verification-receipt-v1.schema.json"
            ),
            "producer_path": PRE_IMPORT_SCHEMA_PATH,
            "sha256": sha256(generated[PRE_IMPORT_SCHEMA_PATH]),
        },
    }
    if (
        set(crucible_receipt_document)
        != {
            "canonical_name",
            "consumer",
            "producer",
            "production_ready",
            "receipt_schema_version",
            "runtime_ready",
            "status",
        }
        or crucible_receipt_document.get("consumer") != "crucible-rust"
        or crucible_receipt_document.get("producer")
        != {
            "assets": expected_consumer_assets,
            "commit": CUSTOS_STRATEGY_CONTRACT_COMMIT,
            "repository": "tesseract-trading/custos",
        }
        or crucible_receipt_document.get("status")
        != "EXACT_CUSTOS_V1_CONTRACT_PINNED_PENDING_PRODUCER_HANDOFF"
        or crucible_receipt_document.get("runtime_ready") is not False
        or crucible_receipt_document.get("production_ready") is not False
    ):
        raise ValueError("Crucible consumer receipt does not pin the corrected Custos V1")
    crucible_receipt_pin = {
        "repository": "tesseract-trading/crucible-rust",
        "commit": CRUCIBLE_CONSUMER_RECEIPT_COMMIT,
        "path": (
            "docs/authority/receipts/"
            "crucible-custos-strategy-contract-v1-consumer-receipt.json"
        ),
        "vendored_path": CRUCIBLE_CONSUMER_RECEIPT_VENDOR_PATH,
        "sha256": sha256(crucible_receipt_bytes),
    }
    generated[CONTRACT_RECEIPT_PATH] = json_bytes(
        {
            "receipt_schema_version": 1,
            "canonical_name": "Custos strategy contract V1 receipt",
            "status": "CANONICAL_V1_PENDING_CONSUMER_RECEIPTS",
            "producer": {
                "repository": "tesseract-trading/custos",
                "source_path": str(SOURCE_MODEL.relative_to(ROOT)),
                "source_sha256": sha256(SOURCE_MODEL.read_bytes()),
            },
            "contract_asset_index": {
                "path": INDEX_PATH,
                "sha256": sha256(index),
                "size_bytes": len(index),
            },
            "consumers": {
                "philosophers_stone": {
                    "repository": "alchymia-labs/philosophers-stone",
                    "receipt": None,
                },
                "crucible_rust": {
                    "repository": "tesseract-trading/crucible-rust",
                    "receipt": crucible_receipt_pin,
                },
            },
            "policy_boundary": {
                "crucible_local_policy_decision_reused": False,
                "runner_local_policy_decision_required": True,
            },
            "strategy_artifact_pre_import_verification_receipt_v1_published": True,
            "contract_consumer_ready": False,
            "command_consumer_ready": False,
            "runtime_ready": False,
            "production_ready": False,
            "open_blockers": [
                "Philosophers Stone canonical V1 consumer receipt",
                "final exact-byte relock after the remaining PS consumer receipt",
            ],
        }
    )
    return generated


def build_runner_command_consumer_assets() -> dict[str, bytes]:
    command_receipt_bytes = (ROOT / RUNNER_COMMAND_PRODUCER_RECEIPT_VENDOR_PATH).read_bytes()
    command_receipt = json.loads(command_receipt_bytes)
    command_contract_assets = []
    producer_command_contract_assets = []
    for role, producer_path, consumer_path in RUNNER_COMMAND_CONTRACT_ASSETS:
        data = (ROOT / consumer_path).read_bytes()
        pin = {"sha256": sha256(data), "size_bytes": len(data)}
        command_contract_assets.append(
            {
                "role": role,
                "producer_path": producer_path,
                "path": consumer_path,
                **pin,
            }
        )
        producer_command_contract_assets.append({"role": role, "path": producer_path, **pin})
    if (
        command_receipt.get("receipt_id") != "CRUCIBLE-RUNNER-COMMAND-PUBLICATION-V1"
        or command_receipt.get("owner") != "crucible-rust"
        or command_receipt.get("status") != RUNNER_COMMAND_PRODUCER_RECEIPT_STATUS
        or command_receipt.get("contract_assets") != producer_command_contract_assets
        or command_receipt.get("contract_asset_authority", {}).get("contract_commit")
        != RUNNER_COMMAND_PRODUCER_COMMIT
        or command_receipt.get("contract_asset_authority", {}).get("risk_policy_owner")
        != "crucible-rust"
        or command_receipt.get("contract_asset_authority", {}).get(
            "consumer_interpretation_allowed"
        )
        is not False
        or command_receipt.get("contract_evolution")
        != {
            "current_production_version": 1,
            "feature_changes": "IN_PLACE_V1",
            "runtime_compatibility_layers_allowed": False,
            "v2_requires_external_production_consumer": True,
            "v2_requires_explicit_migration_window": True,
        }
        or command_receipt.get("runtime_ready") is not False
        or command_receipt.get("production_ready") is not False
    ):
        raise ValueError("Crucible runner command receipt is not the canonical V1 pin")

    resolution_receipt_bytes = (ROOT / RUNNER_STRATEGY_RESOLUTION_RECEIPT_VENDOR_PATH).read_bytes()
    resolution_receipt = json.loads(resolution_receipt_bytes)
    resolution_contract_assets = []
    producer_contract_assets = []
    for producer_path, vendor_path in RUNNER_STRATEGY_RESOLUTION_CONTRACT_ASSETS:
        data = (ROOT / vendor_path).read_bytes()
        pin = {"sha256": sha256(data), "size_bytes": len(data)}
        resolution_contract_assets.append(
            {"producer_path": producer_path, "path": vendor_path, **pin}
        )
        producer_contract_assets.append({"path": producer_path, **pin})
    if (
        resolution_receipt.get("receipt_id") != "CRUCIBLE-RUNNER-STRATEGY-RESOLUTION-V1"
        or resolution_receipt.get("owner") != "crucible-rust"
        or resolution_receipt.get("consumer") != "custos"
        or resolution_receipt.get("producer_commit") != RUNNER_STRATEGY_RESOLUTION_PRODUCER_COMMIT
        or resolution_receipt.get("runtime_code_commit")
        != RUNNER_STRATEGY_RESOLUTION_PRODUCER_COMMIT
        or resolution_receipt.get("contract_commit") != RUNNER_STRATEGY_RESOLUTION_CONTRACT_COMMIT
        or resolution_receipt.get("contract_assets") != producer_contract_assets
        or resolution_receipt.get("authority_coordinate")
        != "crucible.runner-strategy-resolution.v1"
        or resolution_receipt.get("strategy_release_binding", {}).get(
            "request_accepts_strategy_release_id"
        )
        is not False
        or resolution_receipt.get("strategy_release_binding", {}).get(
            "release_derived_from_persisted_deployment_spec"
        )
        is not True
        or resolution_receipt.get("runtime_ready") is not False
        or resolution_receipt.get("production_ready") is not False
    ):
        raise ValueError("Crucible runner strategy resolution receipt is not the canonical V1 pin")
    consumer_assets = []
    for relative in (
        RUNNER_COMMAND_CONSUMER_SOURCE,
        RUNNER_COMMAND_CONSUMER_TEST,
        RUNNER_MATERIAL_AUTHORITY_SOURCE,
        RUNNER_MATERIAL_AUTHORITY_TEST,
        DEVELOPMENT_ARTIFACT_RUNTIME_SOURCE,
        RUNNER_COMMAND_GOLDEN_PATH,
        RUNNER_COMMAND_GOLDEN_SIDECAR_PATH,
        RUNNER_COMMAND_FINGERPRINT_VECTOR_PATH,
        RUNNER_COMMAND_PRODUCER_RECEIPT_VENDOR_PATH,
        RUNNER_STRATEGY_RESOLUTION_RECEIPT_VENDOR_PATH,
        *(vendor_path for _, vendor_path in RUNNER_STRATEGY_RESOLUTION_CONTRACT_ASSETS),
    ):
        data = (ROOT / relative).read_bytes()
        consumer_assets.append({"path": relative, "sha256": sha256(data), "size_bytes": len(data)})
    index = json_bytes(
        {
            "asset_index_schema_version": 1,
            "canonical_name": "Custos canonical V1 Crucible DeploymentSpec consumer assets",
            "status": RUNNER_COMMAND_CONSUMER_STATUS,
            "consumer_scope": [
                "deployment_spec_command",
                "durable_runner_intake",
                "development_material_resolution",
                "strategy_release_material_resolution",
            ],
            "producer_authority": {
                "repository": "tesseract-trading/crucible-rust",
                "contract": "CrucibleRunnerDeploymentCommandV1",
                "producer_commit": RUNNER_COMMAND_PRODUCER_COMMIT,
                "producer_receipt_commit": RUNNER_COMMAND_PRODUCER_RECEIPT_COMMIT,
                "subject_template": RUNNER_COMMAND_SUBJECT_TEMPLATE,
                "producer_receipt": {
                    "path": RUNNER_COMMAND_PRODUCER_RECEIPT_VENDOR_PATH,
                    "sha256": sha256(command_receipt_bytes),
                    "size_bytes": len(command_receipt_bytes),
                    "status": command_receipt["status"],
                    "contract_assets": command_contract_assets,
                },
                "strategy_resolution_receipt": {
                    "producer_commit": RUNNER_STRATEGY_RESOLUTION_PRODUCER_COMMIT,
                    "contract_commit": RUNNER_STRATEGY_RESOLUTION_CONTRACT_COMMIT,
                    "receipt_commit": RUNNER_STRATEGY_RESOLUTION_RECEIPT_COMMIT,
                    "path": RUNNER_STRATEGY_RESOLUTION_RECEIPT_VENDOR_PATH,
                    "sha256": sha256(resolution_receipt_bytes),
                    "size_bytes": len(resolution_receipt_bytes),
                    "status": resolution_receipt["status"],
                    "contract_assets": resolution_contract_assets,
                },
                "status": "COMMAND_AND_LOCAL_STRATEGY_RESOLUTION_CONTRACT_PINNED",
            },
            "consumer_model": {
                "path": RUNNER_COMMAND_CONSUMER_SOURCE,
                "public_export": "CrucibleRunnerDeploymentCommandV1",
                "sha256": consumer_assets[0]["sha256"],
                "size_bytes": consumer_assets[0]["size_bytes"],
            },
            "consumer_assets": consumer_assets,
            "custos_publishes_command_schema": False,
            "exact_signed_event_bytes_retained": True,
            "signature_bytes_in_fingerprint": False,
            "exact_event_command_fingerprint_pinned": True,
            "command_contains_deployment_spec_only": True,
            "strategy_release_authority_resolution": (
                "sole V1 schema, golden and local HTTP/PG producer receipt consumed; "
                "daemon activation remains open"
            ),
            "consumer_code_ready": True,
            "command_contract_consumer_ready": True,
            "runner_command_producer_receipt_consumed": True,
            "development_material_resolution_ready": True,
            "strategy_release_resolution_contract_ready": True,
            "runtime_ready": False,
            "production_ready": False,
        }
    )
    receipt = json_bytes(
        {
            "receipt_schema_version": 1,
            "canonical_name": "Custos canonical V1 DeploymentSpec consumer receipt",
            "receipt_status": RUNNER_COMMAND_CONSUMER_STATUS,
            "contract_asset_index": {
                "path": RUNNER_COMMAND_CONSUMER_INDEX_PATH,
                "sha256": sha256(index),
                "size_bytes": len(index),
            },
            "crucible_producer": {
                "repository": "tesseract-trading/crucible-rust",
                "contract": "CrucibleRunnerDeploymentCommandV1",
                "producer_commit": RUNNER_COMMAND_PRODUCER_COMMIT,
                "producer_receipt_commit": RUNNER_COMMAND_PRODUCER_RECEIPT_COMMIT,
                "subject_template": RUNNER_COMMAND_SUBJECT_TEMPLATE,
                "producer_receipt": {
                    "path": RUNNER_COMMAND_PRODUCER_RECEIPT_VENDOR_PATH,
                    "sha256": sha256(command_receipt_bytes),
                    "size_bytes": len(command_receipt_bytes),
                    "status": command_receipt["status"],
                    "contract_assets": command_contract_assets,
                },
                "strategy_resolution_receipt": {
                    "producer_commit": RUNNER_STRATEGY_RESOLUTION_PRODUCER_COMMIT,
                    "contract_commit": RUNNER_STRATEGY_RESOLUTION_CONTRACT_COMMIT,
                    "receipt_commit": RUNNER_STRATEGY_RESOLUTION_RECEIPT_COMMIT,
                    "path": RUNNER_STRATEGY_RESOLUTION_RECEIPT_VENDOR_PATH,
                    "sha256": sha256(resolution_receipt_bytes),
                    "size_bytes": len(resolution_receipt_bytes),
                    "status": resolution_receipt["status"],
                    "contract_assets": resolution_contract_assets,
                },
                "status": "COMMAND_AND_LOCAL_STRATEGY_RESOLUTION_CONTRACT_PINNED",
            },
            "consumer_model": {
                "path": RUNNER_COMMAND_CONSUMER_SOURCE,
                "public_export": "CrucibleRunnerDeploymentCommandV1",
                "sha256": consumer_assets[0]["sha256"],
                "size_bytes": consumer_assets[0]["size_bytes"],
            },
            "contract_guards": {
                "deployment_spec_only": True,
                "exact_signed_event_bytes_retained": True,
                "signature_bytes_in_fingerprint": False,
                "exact_event_command_fingerprint_pinned": True,
                "compatibility_fallback": False,
                "second_bom_authority_allowed": False,
                "command_selected_root_policy_issuer_workflow": False,
                "custos_command_schema_published": False,
                "strategy_release_material_in_command": False,
            },
            "consumer_code_ready": True,
            "command_contract_consumer_ready": True,
            "runner_command_producer_receipt_consumed": True,
            "development_material_resolution_ready": True,
            "strategy_release_resolution_contract_ready": True,
            "runtime_ready": False,
            "production_ready": False,
            "open_blockers": [
                "protected PS OCI publication and immutable daemon materialization",
                "same-event NATS command to engine to RunnerFact acceptance with StrategyRelease material",
                "deployed testnet and live acceptance",
            ],
        }
    )
    return {
        RUNNER_COMMAND_CONSUMER_INDEX_PATH: index,
        RUNNER_COMMAND_CONSUMER_RECEIPT_PATH: receipt,
    }


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
    assets.update(build_runner_command_consumer_assets())
    assets.update(build_toolkit_rc_foundation_assets())
    drift: list[str] = []
    for relative, expected in assets.items():
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
        print(f"generated {len(assets)} strategy contract assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
