"""Custos-owned strategy execution and artifact verification contracts.

These models define the runner's execution ABI and the exact artifact evidence
it accepts. They deliberately do not define StrategyRelease lifecycle or the
canonical StrategyRelease BOM, which remain Crucible and Philosophers-Stone
responsibilities respectively.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Literal, Protocol, Self, TypeAlias, TypeVar, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

STRATEGY_EXECUTION_ABI_V1 = "alephain.strategy_runtime.v1"
STRATEGY_CONTRACT_SCHEMA_VERSION = 1
STRATEGY_CONTRACT_CANONICALIZATION = "sha256-canonical-json-v1"

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
SafeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")]
SourceCommit = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]

JsonScalar: TypeAlias = None | bool | int | Decimal | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
FrozenJsonObject: TypeAlias = Mapping[str, JsonValue]

ConfigT = TypeVar("ConfigT")
StrategyT_co = TypeVar("StrategyT_co", covariant=True)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyExecutionContextV1(_StrictFrozenModel):
    """Identity and provenance presented to a strategy runtime adapter."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="StrategyExecutionContextV1",
        json_schema_extra={
            "$id": "https://custos.the-alephain-guild/contracts/strategy-execution-context-v1.schema.json"
        },
    )

    schema_version: Literal[1] = 1
    engine: Literal["nautilus"]
    trading_mode: Literal["sandbox", "testnet", "live"]
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: Sha256Hex
    effective_config_digest: Sha256Hex
    generation: StrictInt = Field(ge=1)


class StrategyRuntimeAdapterV1(Protocol[ConfigT, StrategyT_co]):
    """Typed adapter implemented by an engine-specific strategy wheel."""

    def build_config(
        self,
        effective_config: FrozenJsonObject,
        execution_context: StrategyExecutionContextV1,
    ) -> ConfigT: ...

    def build_strategy(self, config: ConfigT) -> StrategyT_co: ...


class ArtifactMemberRole(StrEnum):
    BASE_CONTRACTS_WHEEL = "base_contracts_wheel"
    NAUTILUS_WHEEL = "nautilus_wheel"
    STRATEGY_WHEEL = "strategy_wheel"
    STRATEGY_MANIFEST = "strategy_manifest"
    RUNTIME_ARTIFACT = "runtime_artifact"
    ATTESTATION_BUNDLE = "attestation_bundle"
    SBOM = "sbom"
    CONTRACT_SCHEMA = "contract_schema"
    SOURCE_TREE = "source_tree"


class DigestBindingV1(_StrictFrozenModel):
    name: SafeName
    sha256: Sha256Hex

    @field_validator("name")
    @classmethod
    def validate_relative_name(cls, value: str) -> str:
        return _validate_relative_artifact_name(value)


class ArtifactMemberV1(_StrictFrozenModel):
    role: ArtifactMemberRole
    name: SafeName
    media_type: NonEmptyString
    size_bytes: StrictInt = Field(ge=0)
    sha256: Sha256Hex

    @field_validator("name")
    @classmethod
    def validate_relative_name(cls, value: str) -> str:
        return _validate_relative_artifact_name(value)


class StrategyManifestV1(_StrictFrozenModel):
    """Artifact-local compatibility metadata, never release authority."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="StrategyManifestV1",
        json_schema_extra={
            "$id": "https://custos.the-alephain-guild/contracts/strategy-manifest-v1.schema.json"
        },
    )

    schema_version: Literal[1] = 1
    execution_abi: Literal["alephain.strategy_runtime.v1"]
    entry_point_group: Literal["alephain.strategy_runtime.v1"]
    entry_point: NonEmptyString
    engine: Literal["nautilus"]
    engine_version: Literal["1.230.0"]
    requires_python: Literal[">=3.12,<3.13"]
    base_contracts_version: NonEmptyString
    engine_toolkit_version: NonEmptyString
    config_schema_sha256: Sha256Hex
    catalog_alias: NonEmptyString | None = None
    runtime_artifacts: tuple[ArtifactMemberV1, ...] = ()

    @field_validator("entry_point")
    @classmethod
    def validate_entry_point(cls, value: str) -> str:
        module, separator, attribute = value.partition(":")
        valid_module = re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module)
        if not separator or not attribute or not valid_module:
            raise ValueError("entry_point must be a module path and attribute separated by ':'")
        if module.split(".", 1)[0] in {"shared", "pandas_ta"}:
            raise ValueError("legacy top-level toolkit aliases are not valid entry points")
        return value

    @model_validator(mode="after")
    def validate_runtime_artifacts(self) -> Self:
        if any(
            member.role is not ArtifactMemberRole.RUNTIME_ARTIFACT
            for member in self.runtime_artifacts
        ):
            raise ValueError("manifest runtime_artifacts may contain only runtime_artifact members")
        _require_unique_members(self.runtime_artifacts)
        return self


class StrategyArtifactRefV1(_StrictFrozenModel):
    """Sole first-production pre-sign execution identity."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="StrategyArtifactRefV1",
        json_schema_extra={
            "$id": "https://custos.the-alephain-guild/contracts/v1/strategy-artifact-ref-v1.schema.json"
        },
    )

    schema_version: Literal[1] = 1
    artifact_kind: Literal["wheel"]
    artifact_coordinate: NonEmptyString
    artifact_sha256: Sha256Hex
    artifact_size_bytes: StrictInt = Field(gt=0)
    manifest_sha256: Sha256Hex
    manifest_size_bytes: StrictInt = Field(gt=0)
    required_runtime_artifacts: tuple[ArtifactMemberV1, ...] = Field(min_length=1)
    sbom_sha256: Sha256Hex
    contract_schema_sha256: Sha256Hex
    source_repository: NonEmptyString
    source_commit: SourceCommit
    normalized_source_tree_sha256: Sha256Hex
    python_version: Annotated[str, StringConstraints(pattern=r"^3\.12\.[0-9]+$")]
    engine: Literal["nautilus"]
    engine_version: Literal["1.230.0"]
    base_contracts_version: NonEmptyString
    engine_toolkit_version: NonEmptyString
    build_inputs: tuple[DigestBindingV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pre_sign_identity(self) -> Self:
        if any(
            member.role is not ArtifactMemberRole.RUNTIME_ARTIFACT
            for member in self.required_runtime_artifacts
        ):
            raise ValueError("required_runtime_artifacts may contain only runtime_artifact members")
        _require_unique_members(self.required_runtime_artifacts)
        _require_unique_names(self.build_inputs, label="build input")
        if not self.artifact_coordinate.endswith(f"@sha256:{self.artifact_sha256}"):
            raise ValueError("artifact_coordinate must be pinned to artifact_sha256")
        return self


class RunnerLocalArtifactPolicyDecisionV1(_StrictFrozenModel):
    """Custos-owned runner-local decision, independent of Crucible policy."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="RunnerLocalArtifactPolicyDecisionV1",
        json_schema_extra={
            "$id": (
                "https://custos.the-alephain-guild/contracts/v1/"
                "runner-local-artifact-policy-decision-v1.schema.json"
            )
        },
    )

    schema_version: Literal[1] = 1
    authority: Literal["custos-runner-local"]
    policy_id: NonEmptyString
    policy_version: StrictInt = Field(ge=1)
    policy_digest: Sha256Hex
    evaluated_at: datetime
    decision: Literal["accepted"]
    release_bom_digest: Sha256Hex
    artifact_ref_digest: Sha256Hex
    artifact_evidence_digest: Sha256Hex
    artifact_acceptance_receipt_digest: Sha256Hex


class StrategyArtifactPreImportVerificationReceiptV1(_StrictFrozenModel):
    """Contract-consumer proof over producer-owned evidence objects."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="StrategyArtifactPreImportVerificationReceiptV1",
        json_schema_extra={
            "$id": (
                "https://custos.the-alephain-guild/contracts/v1/"
                "strategy-artifact-pre-import-verification-receipt-v1.schema.json"
            )
        },
    )

    schema_version: Literal[1] = 1
    verification_profile: Literal["custos-artifact-pre-import-verification-v1"]
    verified_at: datetime
    release_bom: dict[str, object]
    release_bom_digest: Sha256Hex
    release_statement: dict[str, object]
    release_statement_digest: Sha256Hex
    artifact_ref: StrategyArtifactRefV1
    artifact_ref_digest: Sha256Hex
    detached_attestation_ref: dict[str, object]
    detached_attestation_ref_digest: Sha256Hex
    crucible_artifact_evidence: dict[str, object]
    crucible_artifact_evidence_digest: Sha256Hex
    crucible_artifact_acceptance: dict[str, object]
    crucible_artifact_acceptance_receipt_digest: Sha256Hex
    runner_local_policy_decision: RunnerLocalArtifactPolicyDecisionV1

    @model_validator(mode="after")
    def validate_owner_contract_bindings(self) -> Self:
        if self.release_bom_digest != canonical_json_digest(self.release_bom):
            raise ValueError("release_bom_digest differs from the full producer BOM")
        if self.release_statement_digest != canonical_json_digest(self.release_statement):
            raise ValueError("release_statement_digest differs from the producer statement")
        if self.artifact_ref_digest != canonical_model_digest(self.artifact_ref):
            raise ValueError("artifact_ref_digest differs from StrategyArtifactRefV1")
        if self.detached_attestation_ref_digest != canonical_json_digest(
            self.detached_attestation_ref
        ):
            raise ValueError(
                "detached_attestation_ref_digest differs from the detached producer reference"
            )

        bom_fields = {
            "build_lock_sha256",
            "canonicalization",
            "contract_asset_index_coordinate",
            "contract_asset_index_sha256",
            "engine",
            "engine_version",
            "entry_point_group",
            "entry_point_name",
            "execution_abi_golden_coordinate",
            "execution_abi_golden_sha256",
            "execution_abi_schema_coordinate",
            "execution_abi_schema_sha256",
            "members",
            "producer_commit",
            "producer_repository",
            "python_requires",
            "schema_version",
            "strategy_artifact_coordinate",
            "strategy_artifact_sha256",
            "strategy_coordinate",
            "strategy_manifest_sha256",
            "strategy_sbom_sha256",
            "strategy_source_commit",
            "strategy_source_tree_sha256",
            "toolkit_coordinate",
            "toolkit_sbom_sha256",
            "toolkit_wheel_sha256",
            "zero_rewrite_characterization_sha256",
            "zero_rewrite_semantic_diff_sha256",
        }
        if set(self.release_bom) != bom_fields:
            raise ValueError("producer BOM shape is not exact V1")
        if self.release_bom.get("schema_version") != "alephain.strategy-release-bom.v1":
            raise ValueError("producer BOM schema_version is not exact V1")
        if self.release_bom.get("canonicalization") != "sha256-canonical-json-v1":
            raise ValueError("producer BOM canonicalization differs")

        bom_bindings = {
            "strategy_artifact_coordinate": self.artifact_ref.artifact_coordinate,
            "strategy_artifact_sha256": self.artifact_ref.artifact_sha256,
            "strategy_manifest_sha256": self.artifact_ref.manifest_sha256,
            "strategy_sbom_sha256": self.artifact_ref.sbom_sha256,
            "strategy_source_tree_sha256": self.artifact_ref.normalized_source_tree_sha256,
            "producer_repository": self.artifact_ref.source_repository,
            "strategy_source_commit": self.artifact_ref.source_commit,
            "execution_abi_schema_sha256": self.artifact_ref.contract_schema_sha256,
            "engine": self.artifact_ref.engine,
            "engine_version": self.artifact_ref.engine_version,
        }
        for name, expected in bom_bindings.items():
            if self.release_bom.get(name) != expected:
                raise ValueError(f"producer BOM {name} differs from ArtifactRefV1")

        build_inputs = {item.name: item.sha256 for item in self.artifact_ref.build_inputs}
        if build_inputs.get("uv.lock") != self.release_bom.get("build_lock_sha256"):
            raise ValueError("producer BOM build_lock_sha256 differs from ArtifactRefV1")

        members = self.release_bom.get("members")
        if not isinstance(members, list) or not members:
            raise ValueError("producer BOM members must be a non-empty array")
        member_fields = {"coordinate", "media_type", "name", "role", "sha256", "size_bytes"}
        members_by_role: dict[str, list[Mapping[str, object]]] = {}
        member_names: set[str] = set()
        for bom_member in members:
            if not isinstance(bom_member, Mapping) or set(bom_member) != member_fields:
                raise ValueError("producer BOM member shape is not exact V1")
            role = bom_member.get("role")
            member_name = bom_member.get("name")
            if (
                not isinstance(role, str)
                or not isinstance(member_name, str)
                or member_name in member_names
            ):
                raise ValueError("producer BOM member identity is invalid or duplicated")
            member_names.add(member_name)
            members_by_role.setdefault(role, []).append(bom_member)
        singleton_roles = {
            "base_contracts_wheel",
            "contract_schema",
            "nautilus_wheel",
            "source_tree",
            "strategy_manifest",
            "strategy_sbom",
            "strategy_wheel",
            "toolkit_sbom",
        }
        expected_member_roles = set(singleton_roles)
        if self.artifact_ref.required_runtime_artifacts:
            expected_member_roles.add("runtime_artifact")
        if set(members_by_role) != expected_member_roles:
            raise ValueError("producer BOM member roles are not exact V1")
        if any(len(members_by_role[role]) != 1 for role in singleton_roles):
            raise ValueError("producer BOM singleton member role is duplicated")

        def member_for_role(role: str) -> Mapping[str, object]:
            return members_by_role[role][0]

        member_bindings = {
            "strategy_wheel": (
                self.artifact_ref.artifact_sha256,
                self.artifact_ref.artifact_size_bytes,
            ),
            "strategy_manifest": (
                self.artifact_ref.manifest_sha256,
                self.artifact_ref.manifest_size_bytes,
            ),
            "strategy_sbom": (self.artifact_ref.sbom_sha256, None),
            "source_tree": (self.artifact_ref.normalized_source_tree_sha256, None),
            "contract_schema": (
                self.release_bom.get("contract_asset_index_sha256"),
                None,
            ),
            "nautilus_wheel": (self.release_bom.get("toolkit_wheel_sha256"), None),
            "toolkit_sbom": (self.release_bom.get("toolkit_sbom_sha256"), None),
        }
        for role, (digest, size) in member_bindings.items():
            bom_member = member_for_role(role)
            if bom_member.get("sha256") != digest:
                raise ValueError(f"producer BOM {role} digest differs")
            if size is not None and bom_member.get("size_bytes") != size:
                raise ValueError(f"producer BOM {role} size differs")
        runtime_members = {
            (
                item.get("name"),
                item.get("media_type"),
                item.get("sha256"),
                item.get("size_bytes"),
            )
            for item in members_by_role.get("runtime_artifact", [])
        }
        runtime_refs = {
            (item.name, item.media_type, item.sha256, item.size_bytes)
            for item in self.artifact_ref.required_runtime_artifacts
        }
        if runtime_members != runtime_refs:
            raise ValueError("producer BOM runtime artifacts differ from ArtifactRefV1")

        statement_fields = {"_type", "subject", "predicateType", "predicate"}
        if set(self.release_statement) != statement_fields:
            raise ValueError("producer statement shape is not exact V1")
        if self.release_statement.get("_type") != "https://in-toto.io/Statement/v1":
            raise ValueError("producer statement type is not in-toto V1")
        if (
            self.release_statement.get("predicateType")
            != "https://the-alephain-guild.dev/attestation/strategy-release/v1"
        ):
            raise ValueError("producer statement predicate type differs")
        statement_subjects = self.release_statement.get("subject")
        if not isinstance(statement_subjects, list):
            raise ValueError("producer statement subject must be an array")
        expected_subjects = [
            {"name": "strategy-release-bom-v1", "digest": {"sha256": self.release_bom_digest}},
            {"name": "strategy-artifact", "digest": {"sha256": self.artifact_ref.artifact_sha256}},
            {
                "name": "strategy-manifest-v1",
                "digest": {"sha256": self.artifact_ref.manifest_sha256},
            },
            {"name": "strategy-artifact-ref-v1", "digest": {"sha256": self.artifact_ref_digest}},
        ]
        if statement_subjects != expected_subjects:
            raise ValueError("producer statement subjects differ from BOM and ArtifactRefV1")

        predicate = self.release_statement.get("predicate")
        if not isinstance(predicate, Mapping):
            raise ValueError("producer statement predicate must be an object")
        predicate_fields = {
            "build_lock_sha256",
            "contract_asset_index_sha256",
            "engine",
            "engine_version",
            "entry_point_group",
            "entry_point_name",
            "execution_abi_schema_sha256",
            "producer_commit",
            "producer_repository",
            "python_requires",
            "schema_version",
            "source_date_epoch",
            "strategy_sbom_sha256",
            "strategy_source_tree_sha256",
            "toolkit_sbom_sha256",
            "toolkit_wheel_sha256",
            "workflow_identity",
            "zero_rewrite_characterization_sha256",
            "zero_rewrite_semantic_diff_sha256",
        }
        if set(predicate) != predicate_fields:
            raise ValueError("producer statement predicate shape is not exact V1")
        bom_predicate_bindings = {
            "build_lock_sha256",
            "contract_asset_index_sha256",
            "engine",
            "engine_version",
            "entry_point_group",
            "entry_point_name",
            "execution_abi_schema_sha256",
            "producer_commit",
            "producer_repository",
            "python_requires",
            "strategy_sbom_sha256",
            "strategy_source_tree_sha256",
            "toolkit_sbom_sha256",
            "toolkit_wheel_sha256",
            "zero_rewrite_characterization_sha256",
            "zero_rewrite_semantic_diff_sha256",
        }
        for name in bom_predicate_bindings:
            if predicate.get(name) != self.release_bom.get(name):
                raise ValueError(f"producer statement predicate {name} differs from BOM")

        if self.detached_attestation_ref.get("statement_sha256") != self.release_statement_digest:
            raise ValueError("detached attestation reference differs from producer statement")
        evidence_fields = {
            "schema_version",
            "strategy_release_id",
            "artifact_ref_digest",
            "release_bom_digest",
            "release_statement_digest",
            "detached_attestation_ref_digest",
            "bundle_sha256",
            "signed_producer_claims",
            "sigstore_proof",
            "local_policy_evaluation",
            "composite_evidence_digest",
        }
        if set(self.crucible_artifact_evidence) != evidence_fields:
            raise ValueError("Crucible artifact evidence shape is not exact V1")
        evidence_bindings = {
            "artifact_ref_digest": self.artifact_ref_digest,
            "release_bom_digest": self.release_bom_digest,
            "release_statement_digest": self.release_statement_digest,
            "detached_attestation_ref_digest": self.detached_attestation_ref_digest,
            "bundle_sha256": self.detached_attestation_ref.get("bundle_sha256"),
        }
        for name, expected_evidence in evidence_bindings.items():
            if self.crucible_artifact_evidence.get(name) != expected_evidence:
                raise ValueError(f"Crucible artifact evidence {name} differs")
        signed_claims = self.crucible_artifact_evidence.get("signed_producer_claims")
        if not isinstance(signed_claims, Mapping):
            raise ValueError("Crucible signed producer claims must be an object")
        expected_claims = {
            "schema_version": predicate.get("schema_version"),
            "producer_repository": predicate.get("producer_repository"),
            "producer_commit": predicate.get("producer_commit"),
            "workflow_identity": predicate.get("workflow_identity"),
            "source_date_epoch": predicate.get("source_date_epoch"),
            "strategy_source_tree_sha256": predicate.get("strategy_source_tree_sha256"),
            "artifact_sha256": self.artifact_ref.artifact_sha256,
            "manifest_sha256": self.artifact_ref.manifest_sha256,
            "artifact_ref_digest": self.artifact_ref_digest,
            "release_bom_digest": self.release_bom_digest,
            "execution_abi_schema_sha256": predicate.get("execution_abi_schema_sha256"),
            "contract_asset_index_sha256": predicate.get("contract_asset_index_sha256"),
            "toolkit_wheel_sha256": predicate.get("toolkit_wheel_sha256"),
            "toolkit_sbom_sha256": predicate.get("toolkit_sbom_sha256"),
            "build_lock_sha256": predicate.get("build_lock_sha256"),
            "zero_rewrite_semantic_diff_sha256": predicate.get("zero_rewrite_semantic_diff_sha256"),
            "zero_rewrite_characterization_sha256": predicate.get(
                "zero_rewrite_characterization_sha256"
            ),
            "engine": predicate.get("engine"),
            "engine_version": predicate.get("engine_version"),
            "python_requires": predicate.get("python_requires"),
            "entry_point_group": predicate.get("entry_point_group"),
            "entry_point_name": predicate.get("entry_point_name"),
        }
        if dict(signed_claims) != expected_claims:
            raise ValueError("Crucible signed producer claims differ from exact statement bindings")

        proof = self.crucible_artifact_evidence.get("sigstore_proof")
        proof_fields = (
            "bundle_sha256",
            "statement_sha256",
            "dsse_payload_sha256",
            "dsse_signature_sha256",
            "signing_certificate_sha256",
            "trusted_root_sha256",
            "certificate_issuer",
            "certificate_subject",
            "certificate_not_before",
            "certificate_not_after",
            "sct_log_id",
            "sct_sha256",
            "rekor_log_id",
            "rekor_log_index",
            "rekor_integrated_time",
            "rekor_entry_body_sha256",
            "rekor_signed_entry_timestamp_sha256",
            "rekor_inclusion_proof_sha256",
            "rekor_checkpoint_sha256",
            "rekor_tree_size",
            "rekor_root_hash",
        )
        if not isinstance(proof, Mapping) or set(proof) != set(proof_fields):
            raise ValueError("Crucible Sigstore proof shape is not exact V1")
        if (
            proof.get("bundle_sha256") != self.detached_attestation_ref.get("bundle_sha256")
            or proof.get("statement_sha256") != self.release_statement_digest
            or proof.get("dsse_payload_sha256") != self.release_statement_digest
            or proof.get("certificate_subject") != predicate.get("workflow_identity")
        ):
            raise ValueError("Crucible Sigstore proof differs from producer evidence")

        crucible_policy = self.crucible_artifact_evidence.get("local_policy_evaluation")
        policy_fields = (
            "policy_id",
            "policy_version",
            "policy_digest",
            "evaluated_at",
            "decision",
        )
        if not isinstance(crucible_policy, Mapping) or set(crucible_policy) != set(policy_fields):
            raise ValueError("Crucible local policy evaluation shape is not exact V1")
        if crucible_policy.get("decision") != "accepted":
            raise ValueError("Crucible local policy did not accept the artifact")

        evidence_preimage = {
            "schema_version": self.crucible_artifact_evidence["schema_version"],
            "strategy_release_id": self.crucible_artifact_evidence["strategy_release_id"],
            "artifact_ref_digest": self.crucible_artifact_evidence["artifact_ref_digest"],
            "release_bom_digest": self.crucible_artifact_evidence["release_bom_digest"],
            "release_statement_digest": self.crucible_artifact_evidence["release_statement_digest"],
            "detached_attestation_ref_digest": self.crucible_artifact_evidence[
                "detached_attestation_ref_digest"
            ],
            "bundle_sha256": self.crucible_artifact_evidence["bundle_sha256"],
            "signed_producer_claims": expected_claims,
            "sigstore_proof": {name: proof[name] for name in proof_fields},
            "local_policy_evaluation": {name: crucible_policy[name] for name in policy_fields},
            "composite_evidence_digest": "",
        }
        computed_evidence_digest = hashlib.sha256(
            json.dumps(
                evidence_preimage,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if computed_evidence_digest != self.crucible_artifact_evidence_digest:
            raise ValueError("Crucible artifact evidence composite digest differs")
        if (
            self.crucible_artifact_evidence.get("composite_evidence_digest")
            != self.crucible_artifact_evidence_digest
        ):
            raise ValueError("Crucible artifact evidence digest binding differs")

        snapshot_fields = {
            "release_id",
            "definition_id",
            "lifecycle_version",
            "release_number",
            "artifact_ref_digest",
            "manifest_digest",
            "release_bom_digest",
            "artifact_evidence_digest",
            "validated_at",
            "snapshot_digest",
        }
        if set(self.crucible_artifact_acceptance) != snapshot_fields:
            raise ValueError("Crucible StrategyRelease snapshot shape is not exact V1")
        snapshot_bindings = {
            "artifact_ref_digest": self.artifact_ref_digest,
            "manifest_digest": self.artifact_ref.manifest_sha256,
            "release_bom_digest": self.release_bom_digest,
            "artifact_evidence_digest": self.crucible_artifact_evidence_digest,
            "snapshot_digest": self.crucible_artifact_acceptance_receipt_digest,
        }
        for name, expected_snapshot in snapshot_bindings.items():
            if self.crucible_artifact_acceptance.get(name) != expected_snapshot:
                raise ValueError(f"Crucible StrategyRelease snapshot {name} differs")
        if self.crucible_artifact_acceptance.get(
            "release_id"
        ) != self.crucible_artifact_evidence.get("strategy_release_id"):
            raise ValueError("Crucible StrategyRelease identity binding differs")

        policy = self.runner_local_policy_decision
        policy_bindings = {
            "release_bom_digest": self.release_bom_digest,
            "artifact_ref_digest": self.artifact_ref_digest,
            "artifact_evidence_digest": self.crucible_artifact_evidence_digest,
            "artifact_acceptance_receipt_digest": (
                self.crucible_artifact_acceptance_receipt_digest
            ),
        }
        for name, expected in policy_bindings.items():
            if getattr(policy, name) != expected:
                raise ValueError(f"runner-local policy {name} differs")
        if crucible_policy.get("policy_digest") == policy.policy_digest:
            raise ValueError("runner-local policy must not reuse the Crucible policy digest")
        return self


class DevelopmentSourceRefV1(_StrictFrozenModel):
    """Explicit non-promotable source reference for sandbox development only."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        title="DevelopmentSourceRefV1",
        json_schema_extra={
            "$id": "https://custos.the-alephain-guild/contracts/development-source-ref-v1.schema.json"
        },
    )

    schema_version: Literal[1] = 1
    source_path: NonEmptyString
    source_sha256: Sha256Hex
    trading_mode: Literal["sandbox"]
    promotable: Literal[False] = False


def parse_and_freeze_json_object(raw: str | bytes) -> FrozenJsonObject:
    """Parse JSON with Decimal numbers, reject duplicates, and deep-freeze it."""

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    value = json.loads(
        raw,
        parse_float=Decimal,
        parse_int=int,
        parse_constant=_reject_non_finite_json_number,
        object_pairs_hook=_object_without_duplicate_keys,
    )
    frozen = deep_freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("effective config must be a JSON object")
    return cast(FrozenJsonObject, frozen)


def deep_freeze_json(value: object) -> JsonValue:
    """Convert a JSON-compatible value into immutable recursive containers."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, float):
        raise TypeError("float is forbidden; parse JSON numbers as Decimal")
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = deep_freeze_json(value[key])
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json_bytes(value: JsonValue | list[object] | dict[str, object]) -> bytes:
    """Encode the repository's deterministic canonical JSON subset."""

    return _encode_canonical_json(value).encode("utf-8")


def canonical_json_digest(value: JsonValue | list[object] | dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_model_digest(model: BaseModel) -> str:
    value = model.model_dump(mode="json", exclude_none=False)
    return canonical_json_digest(value)


def verify_effective_config_digest(raw: str | bytes, expected_digest: str) -> FrozenJsonObject:
    frozen = parse_and_freeze_json_object(raw)
    actual_digest = canonical_json_digest(frozen)
    if actual_digest != expected_digest:
        raise ValueError("effective config digest does not match signed command")
    return frozen


def _validate_relative_artifact_name(value: str) -> str:
    if "\\" in value:
        raise ValueError("artifact member names must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.endswith("/"):
        raise ValueError("artifact member names must be safe relative paths")
    return value


def _require_unique_names(values: tuple[DigestBindingV1, ...], *, label: str) -> None:
    names = [value.name for value in values]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate {label} name")


def _require_unique_members(members: tuple[ArtifactMemberV1, ...]) -> None:
    keys = [(member.role, member.name) for member in members]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate artifact member role/name")


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_non_finite_json_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _encode_canonical_json(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON numbers must be finite")
        if value == 0:
            return "0"
        text = format(value.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    if isinstance(value, float):
        raise TypeError("float is forbidden in canonical JSON")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        entries = (
            f"{json.dumps(key, ensure_ascii=False)}:{_encode_canonical_json(value[key])}"
            for key in sorted(value)
        )
        return "{" + ",".join(entries) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_canonical_json(item) for item in value) + "]"
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


__all__ = [
    "ArtifactMemberRole",
    "ArtifactMemberV1",
    "DevelopmentSourceRefV1",
    "DigestBindingV1",
    "FrozenJsonObject",
    "JsonScalar",
    "JsonValue",
    "RunnerLocalArtifactPolicyDecisionV1",
    "STRATEGY_CONTRACT_CANONICALIZATION",
    "STRATEGY_CONTRACT_SCHEMA_VERSION",
    "STRATEGY_EXECUTION_ABI_V1",
    "StrategyArtifactRefV1",
    "StrategyArtifactPreImportVerificationReceiptV1",
    "StrategyExecutionContextV1",
    "StrategyManifestV1",
    "StrategyRuntimeAdapterV1",
    "canonical_json_bytes",
    "canonical_json_digest",
    "canonical_model_digest",
    "deep_freeze_json",
    "parse_and_freeze_json_object",
    "verify_effective_config_digest",
]
