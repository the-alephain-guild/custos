from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from custos_toolkit.contracts.strategy_execution import canonical_json_digest

from custos.contracts import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import DevelopmentArtifactSourceV1
from custos.core.runner_material_authority import (
    RunnerMaterialResolutionError,
    parse_development_material_resolution,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs/authority/runner-deployment-command-golden-v1.json"


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recursively_sorted(value: object) -> object:
    if isinstance(value, dict):
        return {key: _recursively_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_recursively_sorted(item) for item in value]
    return value


def _command() -> CrucibleRunnerDeploymentCommandV1:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = next(
        value
        for value in fixture["cases"]
        if value["name"] == "deployment_spec_ready_for_runner_development_source"
    )
    event = dict(case["event_document"])
    event["payload"] = _recursively_sorted(event["payload"])
    event_bytes = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = {
        "schema_version": 1,
        "signature_profile": "crucible-domain-event-v1-exact-bytes",
        "event_encoding": "application/json;base64url",
        "event_bytes": base64.urlsafe_b64encode(event_bytes).rstrip(b"=").decode("ascii"),
        "signature_key_id": "fixture-domain-key-v1",
        "signature": base64.urlsafe_b64encode(bytes(64)).rstrip(b"=").decode("ascii"),
    }
    command = CrucibleRunnerDeploymentCommandV1.from_verified_signed_envelope(
        signed_envelope_bytes=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        subject=case["subject"],
    )
    assert command.command_fingerprint == case["command_fingerprint"]
    return command


def _response(command: CrucibleRunnerDeploymentCommandV1) -> dict[str, Any]:
    source = command.artifact_source
    assert isinstance(source, DevelopmentArtifactSourceV1)
    snapshot = source.snapshot
    effective_config_digest = canonical_json_digest(command.deployment_spec["execution_config"])
    stored_command_fingerprint = _canonical_digest(
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
    artifact_binding = {
        "kind": "development_source",
        "binding": {
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
        },
    }
    return {
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
        "command_fingerprint": command.command_fingerprint,
        "stored_command_fingerprint": stored_command_fingerprint,
        "artifact_binding_digest": _canonical_digest(artifact_binding),
        "artifact_binding": artifact_binding,
        "development_source_ref": {
            "schema_version": 1,
            "source_path": (f"/tmp/custos-artifacts/sha256/{snapshot.source_sha256}"),
            "source_sha256": snapshot.source_sha256,
            "trading_mode": "sandbox",
            "promotable": False,
        },
    }


def test_development_material_resolution_accepts_exact_instance_binding() -> None:
    command = _command()

    material = parse_development_material_resolution(
        _response(command),
        command=command,
        command_fingerprint=command.command_fingerprint,
    )

    assert material.source_ref.source_sha256 == command.artifact_source.snapshot.source_sha256
    assert Path(material.source_ref.source_path).is_absolute()
    assert material.publication_receipt_digest == (
        command.artifact_source.snapshot.publication_receipt_digest
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("command_fingerprint", "differs from signed command identity"),
        ("artifact_binding_digest", "artifact binding digest differs"),
        ("relative_source_path", "development source ref differs"),
        ("extra_response_field", "response shape is not exact V1"),
    ],
)
def test_development_material_resolution_rejects_authority_drift(
    mutation: str,
    message: str,
) -> None:
    command = _command()
    response = deepcopy(_response(command))
    if mutation == "command_fingerprint":
        response["command_fingerprint"] = "f" * 64
    elif mutation == "artifact_binding_digest":
        response["artifact_binding_digest"] = "f" * 64
    elif mutation == "relative_source_path":
        response["development_source_ref"]["source_path"] = "sha256/material"
    else:
        response["unexpected"] = True

    with pytest.raises(RunnerMaterialResolutionError, match=message):
        parse_development_material_resolution(
            response,
            command=command,
            command_fingerprint=command.command_fingerprint,
        )
