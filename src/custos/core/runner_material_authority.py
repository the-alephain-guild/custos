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
    canonical_json_digest,
)

from custos.contracts.crucible_runner_command import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import DevelopmentArtifactSourceV1
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


class DevelopmentRunnerMaterialResolver(Protocol):
    async def resolve_development(
        self,
        *,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
    ) -> ResolvedDevelopmentRunnerMaterialV1: ...


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
        return parse_development_material_resolution(
            response,
            command=command,
            command_fingerprint=command_fingerprint,
        )


def parse_development_material_resolution(
    response: Mapping[str, Any],
    *,
    command: CrucibleRunnerDeploymentCommandV1,
    command_fingerprint: str,
) -> ResolvedDevelopmentRunnerMaterialV1:
    if set(response) != _RESPONSE_FIELDS:
        raise RunnerMaterialResolutionError("runner material response shape is not exact V1")
    source = command.artifact_source
    if not isinstance(source, DevelopmentArtifactSourceV1):
        raise RunnerMaterialResolutionError("development resolver received StrategyRelease command")
    snapshot = source.snapshot
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
    effective_config_digest = canonical_json_digest(command.deployment_spec["execution_config"])
    expected_stored_fingerprint = _canonical_digest(
        {
            "schema_version": 1,
            "deployment_instance_id": str(command.deployment_instance_id),
            "deployment_spec_id": str(command.deployment_spec_id),
            "deployment_spec_digest": command.deployment_spec_digest,
            "generation": command.generation,
            "artifact_source_digest": snapshot.snapshot_digest,
            "effective_config_digest": effective_config_digest,
            "desired_lifecycle_state": command.lifecycle_state,
        }
    )
    if response.get("stored_command_fingerprint") != expected_stored_fingerprint:
        raise RunnerMaterialResolutionError("runner material stored command fingerprint differs")

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
    stored_fingerprint = response.get("stored_command_fingerprint")
    if not isinstance(stored_fingerprint, str) or _SHA256.fullmatch(stored_fingerprint) is None:
        raise RunnerMaterialResolutionError("stored command fingerprint is invalid")
    return ResolvedDevelopmentRunnerMaterialV1(
        source_ref=DevelopmentSourceRefV1(**source_document),
        publication_receipt_digest=snapshot.publication_receipt_digest,
        artifact_binding_digest=binding_digest,
        stored_command_fingerprint=stored_fingerprint,
    )


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
