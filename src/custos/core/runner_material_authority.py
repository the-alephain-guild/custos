"""Direct machine-authenticated runner material resolution from Crucible."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from custos_toolkit.contracts.strategy_execution import (
    DevelopmentSourceRefV1,
    StrategyArtifactRefV1,
    canonical_json_bytes,
    canonical_json_digest,
)

from custos.artifacts.runtime import StrategyReleaseArtifactAuthorityV1
from custos.contracts.crucible_runner_command import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import (
    DevelopmentArtifactSourceV1,
    StrategyReleaseArtifactSourceV1,
)
from custos.core.machine_credential_vault import (
    MachineCredential,
    MachineCredentialError,
    MachineCredentialHttpClient,
    MachineCredentialRejectedError,
    MachineCredentialTransportError,
)

RUNNER_MATERIAL_RESOLUTION_PATH = "/api/v1/runner-material/resolve"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESPONSE_FIELDS = {
    "schema_version",
    "tenant_id",
    "runner_id",
    "trading_mode",
    "deployment_instance_id",
    "deployment_spec_id",
    "deployment_spec_digest",
    "generation",
    "command_subject",
    "command_event_sha256",
    "command_fingerprint",
    "stored_command_fingerprint",
    "artifact_binding_digest",
    "artifact_binding",
    "development_source_ref",
    "strategy_release_material",
}
_DEVELOPMENT_BINDING_FIELDS = {
    "schema_version",
    "deployment_instance_id",
    "deployment_spec_id",
    "deployment_spec_digest",
    "generation",
    "effective_config_digest",
    "source_sha256",
    "publication_receipt_digest",
    "development_snapshot_digest",
    "promotable",
}
_DEVELOPMENT_SOURCE_FIELDS = {
    "schema_version",
    "source_path",
    "source_sha256",
    "trading_mode",
    "promotable",
}
_STRATEGY_EXECUTION_BINDING_FIELDS = {
    "schema_version",
    "deployment_instance_id",
    "deployment_spec_id",
    "deployment_spec_digest",
    "generation",
    "strategy_release_id",
    "release_bom_digest",
    "artifact_ref",
    "effective_config_digest",
}
_STRATEGY_RELEASE_MATERIAL_FIELDS = {
    "snapshot",
    "artifact_binding",
    "artifact_evidence",
    "artifact_evidence_digest_input_json",
}
_STRATEGY_RELEASE_SNAPSHOT_FIELDS = {
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
_STRATEGY_RELEASE_ARTIFACT_BINDING_FIELDS = {
    "strategy_release_id",
    "manifest",
    "manifest_digest",
    "manifest_canonical_json",
    "release_bom_digest",
    "release_bom_canonical_json",
    "release_statement_digest",
    "release_statement",
    "release_statement_canonical_json",
    "detached_attestation_ref_digest",
    "detached_attestation_ref",
    "detached_attestation_ref_canonical_json",
    "artifact_ref_digest",
    "artifact_ref",
    "artifact_ref_canonical_json",
}
_ARTIFACT_EVIDENCE_FIELDS = {
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


class RunnerMaterialResolutionError(ValueError):
    """Crucible returned material that differs from the signed command."""


class RunnerMaterialUnavailableError(RuntimeError):
    """Crucible material authority is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class ResolvedDevelopmentRunnerMaterialV1:
    source_ref: DevelopmentSourceRefV1
    publication_receipt_digest: str
    artifact_binding_digest: str
    stored_command_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResolvedStrategyReleaseRunnerMaterialV1:
    release_authority: StrategyReleaseArtifactAuthorityV1
    release_statement_bytes: bytes
    artifact_binding_digest: str
    stored_command_fingerprint: str


class DevelopmentRunnerMaterialResolver(Protocol):
    async def resolve_development(
        self,
        *,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
    ) -> ResolvedDevelopmentRunnerMaterialV1: ...


class StrategyReleaseRunnerMaterialResolver(Protocol):
    async def resolve_strategy_release(
        self,
        *,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
    ) -> ResolvedStrategyReleaseRunnerMaterialV1: ...


class RunnerMaterialAuthorityClient:
    """Resolve only the material bound to one consumed signed command."""

    def __init__(self, crucible_url: str, machine_credential: MachineCredential) -> None:
        self.machine_credential = machine_credential
        self.http = MachineCredentialHttpClient(crucible_url, machine_credential)

    async def resolve_development(
        self,
        *,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
    ) -> ResolvedDevelopmentRunnerMaterialV1:
        response = await self._resolve(command, command_fingerprint)
        return parse_development_material_resolution(
            response,
            command=command,
            command_fingerprint=command_fingerprint,
        )

    async def resolve_strategy_release(
        self,
        *,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
    ) -> ResolvedStrategyReleaseRunnerMaterialV1:
        response = await self._resolve(command, command_fingerprint)
        return parse_strategy_release_material_resolution(
            response,
            command=command,
            command_fingerprint=command_fingerprint,
        )

    async def _resolve(
        self,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
    ) -> Mapping[str, Any]:
        if command.command_fingerprint != command_fingerprint:
            raise RunnerMaterialResolutionError(
                "durable command fingerprint differs from exact signed event bytes"
            )
        correlation_id = uuid4()
        body = {
            "tenant_id": command.tenant_id,
            "runner_id": str(command.runner_id),
            "credential_id": str(self.machine_credential.credential_id),
            "credential_version": self.machine_credential.credential_version,
            "correlation_id": str(correlation_id),
            "trading_mode": command.mode,
            "deployment_instance_id": str(command.deployment_instance_id),
            "deployment_spec_id": str(command.deployment_spec_id),
            "deployment_spec_digest": command.deployment_spec_digest,
            "generation": command.generation,
            "command_fingerprint": command_fingerprint,
        }
        try:
            response = await asyncio.to_thread(
                self.http.post,
                RUNNER_MATERIAL_RESOLUTION_PATH,
                body,
                canonical_path=RUNNER_MATERIAL_RESOLUTION_PATH,
                correlation_id=correlation_id,
                deployment_instance_id=str(command.deployment_instance_id),
                deployment_spec_id=str(command.deployment_spec_id),
                deployment_spec_digest=command.deployment_spec_digest,
            )
        except MachineCredentialTransportError as error:
            raise RunnerMaterialUnavailableError(
                "runner material authority is unavailable"
            ) from error
        except (MachineCredentialRejectedError, MachineCredentialError) as error:
            raise RunnerMaterialResolutionError(
                "runner material authority rejected the signed command binding"
            ) from error
        return response


def parse_development_material_resolution(
    response: Mapping[str, Any],
    *,
    command: CrucibleRunnerDeploymentCommandV1,
    command_fingerprint: str,
) -> ResolvedDevelopmentRunnerMaterialV1:
    stored_fingerprint = _validate_common_resolution(
        response,
        command=command,
        command_fingerprint=command_fingerprint,
    )
    source = command.artifact_source
    if not isinstance(source, DevelopmentArtifactSourceV1):
        raise RunnerMaterialResolutionError("development resolver received StrategyRelease command")
    snapshot = source.snapshot
    effective_config_digest = canonical_json_digest(command.deployment_spec["execution_config"])

    artifact_binding = _exact_object(response.get("artifact_binding"), "artifact_binding")
    if set(artifact_binding) != {"kind", "binding"} or (
        artifact_binding["kind"] != "development_source"
    ):
        raise RunnerMaterialResolutionError(
            "runner material artifact binding is not development_source V1"
        )
    binding = _exact_object(artifact_binding["binding"], "artifact_binding.binding")
    if set(binding) != _DEVELOPMENT_BINDING_FIELDS:
        raise RunnerMaterialResolutionError("development artifact binding shape is not exact V1")
    expected_binding = {
        "schema_version": 1,
        "deployment_instance_id": str(command.deployment_instance_id),
        "deployment_spec_id": str(command.deployment_spec_id),
        "deployment_spec_digest": command.deployment_spec_digest,
        "generation": command.generation,
        "effective_config_digest": effective_config_digest,
        "source_sha256": snapshot.source_sha256,
        "publication_receipt_digest": snapshot.publication_receipt_digest,
        "development_snapshot_digest": snapshot.snapshot_digest,
        "promotable": False,
    }
    if binding != expected_binding:
        raise RunnerMaterialResolutionError(
            "development artifact binding differs from signed command"
        )
    binding_digest = _canonical_digest(artifact_binding)
    if response.get("artifact_binding_digest") != binding_digest:
        raise RunnerMaterialResolutionError("artifact binding digest differs")
    if response.get("strategy_release_material") is not None:
        raise RunnerMaterialResolutionError(
            "development material response contains StrategyRelease authority"
        )

    source_document = _exact_object(
        response.get("development_source_ref"), "development_source_ref"
    )
    if set(source_document) != _DEVELOPMENT_SOURCE_FIELDS:
        raise RunnerMaterialResolutionError("development source ref shape is not exact V1")
    if (
        source_document.get("schema_version") != 1
        or source_document.get("source_sha256") != snapshot.source_sha256
        or source_document.get("trading_mode") != "sandbox"
        or source_document.get("promotable") is not False
        or not isinstance(source_document.get("source_path"), str)
        or not Path(source_document["source_path"]).is_absolute()
    ):
        raise RunnerMaterialResolutionError("development source ref differs from signed command")
    return ResolvedDevelopmentRunnerMaterialV1(
        source_ref=DevelopmentSourceRefV1(**source_document),
        publication_receipt_digest=snapshot.publication_receipt_digest,
        artifact_binding_digest=binding_digest,
        stored_command_fingerprint=stored_fingerprint,
    )


def parse_strategy_release_material_resolution(
    response: Mapping[str, Any],
    *,
    command: CrucibleRunnerDeploymentCommandV1,
    command_fingerprint: str,
) -> ResolvedStrategyReleaseRunnerMaterialV1:
    stored_fingerprint = _validate_common_resolution(
        response,
        command=command,
        command_fingerprint=command_fingerprint,
    )
    source = command.artifact_source
    if not isinstance(source, StrategyReleaseArtifactSourceV1):
        raise RunnerMaterialResolutionError(
            "StrategyRelease resolver received development-source command"
        )
    if response.get("development_source_ref") is not None:
        raise RunnerMaterialResolutionError(
            "StrategyRelease material response contains development source"
        )
    material = _exact_object(
        response.get("strategy_release_material"),
        "strategy_release_material",
    )
    if set(material) != _STRATEGY_RELEASE_MATERIAL_FIELDS:
        raise RunnerMaterialResolutionError("StrategyRelease material shape is not exact V1")
    snapshot = _exact_object(material.get("snapshot"), "strategy_release_material.snapshot")
    if set(snapshot) != _STRATEGY_RELEASE_SNAPSHOT_FIELDS:
        raise RunnerMaterialResolutionError("StrategyRelease snapshot shape is not exact V1")
    release_binding = _exact_object(
        material.get("artifact_binding"),
        "strategy_release_material.artifact_binding",
    )
    if set(release_binding) != _STRATEGY_RELEASE_ARTIFACT_BINDING_FIELDS:
        raise RunnerMaterialResolutionError(
            "StrategyRelease artifact authority shape is not exact V1"
        )

    manifest, _ = _verified_canonical_object(
        release_binding,
        value_field="manifest",
        bytes_field="manifest_canonical_json",
        digest_field="manifest_digest",
        label="strategy manifest",
    )
    release_bom = _verified_canonical_document(
        release_binding,
        bytes_field="release_bom_canonical_json",
        digest_field="release_bom_digest",
        label="release BOM",
    )
    release_statement, release_statement_bytes = _verified_canonical_object(
        release_binding,
        value_field="release_statement",
        bytes_field="release_statement_canonical_json",
        digest_field="release_statement_digest",
        label="release statement",
    )
    detached_attestation_ref, _ = _verified_canonical_object(
        release_binding,
        value_field="detached_attestation_ref",
        bytes_field="detached_attestation_ref_canonical_json",
        digest_field="detached_attestation_ref_digest",
        label="detached attestation reference",
    )
    artifact_ref_document, _ = _verified_canonical_object(
        release_binding,
        value_field="artifact_ref",
        bytes_field="artifact_ref_canonical_json",
        digest_field="artifact_ref_digest",
        label="StrategyArtifactRef",
    )
    try:
        artifact_ref = StrategyArtifactRefV1.model_validate(artifact_ref_document)
    except ValueError as error:
        raise RunnerMaterialResolutionError("StrategyArtifactRef is invalid") from error

    evidence = _exact_object(
        material.get("artifact_evidence"),
        "strategy_release_material.artifact_evidence",
    )
    if set(evidence) != _ARTIFACT_EVIDENCE_FIELDS:
        raise RunnerMaterialResolutionError(
            "StrategyRelease artifact evidence shape is not exact V1"
        )
    evidence_input_json = material.get("artifact_evidence_digest_input_json")
    if not isinstance(evidence_input_json, str) or not evidence_input_json:
        raise RunnerMaterialResolutionError(
            "StrategyRelease artifact evidence digest input is invalid"
        )
    evidence_input_bytes = evidence_input_json.encode("utf-8")

    execution_binding = _exact_object(response.get("artifact_binding"), "artifact_binding")
    if set(execution_binding) != {"kind", "binding"} or (
        execution_binding["kind"] != "strategy_release"
    ):
        raise RunnerMaterialResolutionError(
            "runner material artifact binding is not strategy_release V1"
        )
    binding = _exact_object(execution_binding["binding"], "artifact_binding.binding")
    if set(binding) != _STRATEGY_EXECUTION_BINDING_FIELDS:
        raise RunnerMaterialResolutionError(
            "StrategyRelease execution binding shape is not exact V1"
        )
    effective_config_digest = canonical_json_digest(command.deployment_spec["execution_config"])
    expected_binding = {
        "schema_version": 1,
        "deployment_instance_id": str(command.deployment_instance_id),
        "deployment_spec_id": str(command.deployment_spec_id),
        "deployment_spec_digest": command.deployment_spec_digest,
        "generation": command.generation,
        "strategy_release_id": str(source.snapshot.release_id),
        "release_bom_digest": release_binding["release_bom_digest"],
        "artifact_ref": artifact_ref.model_dump(mode="json"),
        "effective_config_digest": effective_config_digest,
    }
    if binding != expected_binding:
        raise RunnerMaterialResolutionError(
            "StrategyRelease execution binding differs from signed command"
        )
    binding_digest = _canonical_digest(execution_binding)
    if response.get("artifact_binding_digest") != binding_digest:
        raise RunnerMaterialResolutionError("artifact binding digest differs")

    expected_snapshot = {
        "release_id": str(source.snapshot.release_id),
        "definition_id": str(source.snapshot.definition_id),
        "lifecycle_version": source.snapshot.release_version,
        "manifest_digest": source.snapshot.manifest_digest,
        "snapshot_digest": source.snapshot.snapshot_digest,
    }
    if any(snapshot.get(field) != value for field, value in expected_snapshot.items()):
        raise RunnerMaterialResolutionError(
            "StrategyRelease snapshot differs from signed DeploymentSpec"
        )
    if (
        release_binding.get("strategy_release_id") != str(source.snapshot.release_id)
        or release_binding.get("manifest_digest") != source.snapshot.manifest_digest
        or release_binding.get("manifest") != manifest
        or release_binding.get("release_statement") != release_statement
        or snapshot.get("artifact_ref_digest") != release_binding.get("artifact_ref_digest")
        or snapshot.get("release_bom_digest") != release_binding.get("release_bom_digest")
        or snapshot.get("artifact_evidence_digest") != evidence.get("composite_evidence_digest")
        or artifact_ref.artifact_sha256 != source.snapshot.artifact_digest
        or artifact_ref.manifest_sha256 != source.snapshot.manifest_digest
        or evidence.get("strategy_release_id") != str(source.snapshot.release_id)
        or evidence.get("artifact_ref_digest") != release_binding.get("artifact_ref_digest")
        or evidence.get("release_bom_digest") != release_binding.get("release_bom_digest")
        or evidence.get("release_statement_digest")
        != release_binding.get("release_statement_digest")
        or evidence.get("detached_attestation_ref_digest")
        != release_binding.get("detached_attestation_ref_digest")
    ):
        raise RunnerMaterialResolutionError(
            "StrategyRelease authority material is not internally bound"
        )

    authority = StrategyReleaseArtifactAuthorityV1(
        strategy_release_id=source.snapshot.release_id,
        strategy_release_snapshot_bytes=canonical_json_bytes(snapshot),
        release_bom=release_bom,
        release_bom_digest=str(release_binding["release_bom_digest"]),
        artifact_ref=artifact_ref,
        artifact_ref_digest=str(release_binding["artifact_ref_digest"]),
        detached_attestation_ref=detached_attestation_ref,
        crucible_artifact_evidence=evidence,
        crucible_artifact_evidence_digest=str(evidence["composite_evidence_digest"]),
        crucible_artifact_evidence_digest_input_bytes=evidence_input_bytes,
        crucible_artifact_acceptance=snapshot,
        crucible_artifact_acceptance_receipt_digest=str(snapshot["snapshot_digest"]),
    )
    return ResolvedStrategyReleaseRunnerMaterialV1(
        release_authority=authority,
        release_statement_bytes=release_statement_bytes,
        artifact_binding_digest=binding_digest,
        stored_command_fingerprint=stored_fingerprint,
    )


def _validate_common_resolution(
    response: Mapping[str, Any],
    *,
    command: CrucibleRunnerDeploymentCommandV1,
    command_fingerprint: str,
) -> str:
    if set(response) != _RESPONSE_FIELDS:
        raise RunnerMaterialResolutionError("runner material response shape is not exact V1")
    expected_identity = {
        "schema_version": 1,
        "tenant_id": command.tenant_id,
        "runner_id": str(command.runner_id),
        "trading_mode": command.mode,
        "deployment_instance_id": str(command.deployment_instance_id),
        "deployment_spec_id": str(command.deployment_spec_id),
        "deployment_spec_digest": command.deployment_spec_digest,
        "generation": command.generation,
        "command_subject": command.verified_subject,
        "command_event_sha256": hashlib.sha256(command.exact_signed_event_bytes).hexdigest(),
        "command_fingerprint": command_fingerprint,
    }
    if any(response.get(field) != value for field, value in expected_identity.items()):
        raise RunnerMaterialResolutionError(
            "runner material response differs from signed command identity"
        )
    expected_stored_fingerprint = _canonical_digest(
        {
            "schema_version": 1,
            "deployment_instance_id": str(command.deployment_instance_id),
            "deployment_spec_id": str(command.deployment_spec_id),
            "deployment_spec_digest": command.deployment_spec_digest,
            "generation": command.generation,
            "artifact_source_digest": command.artifact_source.snapshot.snapshot_digest,
            "effective_config_digest": canonical_json_digest(
                command.deployment_spec["execution_config"]
            ),
            "desired_lifecycle_state": command.lifecycle_state,
        }
    )
    stored_fingerprint = response.get("stored_command_fingerprint")
    if stored_fingerprint != expected_stored_fingerprint:
        raise RunnerMaterialResolutionError("runner material stored command fingerprint differs")
    if not isinstance(stored_fingerprint, str) or _SHA256.fullmatch(stored_fingerprint) is None:
        raise RunnerMaterialResolutionError("stored command fingerprint is invalid")
    return stored_fingerprint


def _verified_canonical_object(
    container: Mapping[str, Any],
    *,
    value_field: str,
    bytes_field: str,
    digest_field: str,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    value = _exact_object(container.get(value_field), value_field)
    document, payload = _verified_canonical_document_with_bytes(
        container,
        bytes_field=bytes_field,
        digest_field=digest_field,
        label=label,
    )
    if value != document:
        raise RunnerMaterialResolutionError(f"{label} canonical bytes differ from value")
    return document, payload


def _verified_canonical_document(
    container: Mapping[str, Any],
    *,
    bytes_field: str,
    digest_field: str,
    label: str,
) -> dict[str, Any]:
    document, _ = _verified_canonical_document_with_bytes(
        container,
        bytes_field=bytes_field,
        digest_field=digest_field,
        label=label,
    )
    return document


def _verified_canonical_document_with_bytes(
    container: Mapping[str, Any],
    *,
    bytes_field: str,
    digest_field: str,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    raw = container.get(bytes_field)
    digest = container.get(digest_field)
    if (
        not isinstance(raw, str)
        or not raw
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise RunnerMaterialResolutionError(f"{label} canonical authority is invalid")
    payload = raw.encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RunnerMaterialResolutionError(f"{label} canonical digest differs")
    return _strict_json_object(payload, label), payload


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RunnerMaterialResolutionError(f"{label} contains duplicate JSON keys")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number is forbidden: {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RunnerMaterialResolutionError(f"{label} is not strict JSON") from error
    if not isinstance(value, dict):
        raise RunnerMaterialResolutionError(f"{label} must be an object")
    return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunnerMaterialResolutionError(f"{label} must be an object")
    return value
