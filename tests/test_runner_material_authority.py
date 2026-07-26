from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from custos_toolkit.contracts.strategy_execution import (
    canonical_json_bytes,
    canonical_json_digest,
)

from custos.artifacts.runtime import _strategy_release_snapshot_digest
from custos.contracts import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import (
    DevelopmentArtifactSourceV1,
    StrategyReleaseArtifactSourceV1,
)
from custos.core.runner_material_authority import (
    RunnerMaterialResolutionError,
    parse_development_material_resolution,
    parse_strategy_release_material_resolution,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs/authority/runner-deployment-command-golden-v1.json"
ARTIFACT_REF_FIXTURE = ROOT / "docs/authority/strategy-artifact-ref-v1.golden.json"


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


def _command_from_event(
    *,
    case_name: str,
    artifact_source: dict[str, Any] | None = None,
) -> CrucibleRunnerDeploymentCommandV1:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = next(value for value in fixture["cases"] if value["name"] == case_name)
    event = deepcopy(case["event_document"])
    if artifact_source is not None:
        event["payload"]["deployment_spec"]["artifact_source"] = artifact_source
        event["payload"]["deployment_spec_digest"] = _canonical_digest(
            event["payload"]["deployment_spec"]
        )
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
    return CrucibleRunnerDeploymentCommandV1.from_verified_signed_envelope(
        signed_envelope_bytes=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        subject=case["subject"],
    )


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
        "strategy_release_material": None,
    }


def _canonical_document(value: dict[str, Any]) -> tuple[str, str]:
    payload = canonical_json_bytes(value)
    return payload.decode("utf-8"), hashlib.sha256(payload).hexdigest()


def _strategy_material_fixture() -> tuple[
    CrucibleRunnerDeploymentCommandV1,
    dict[str, Any],
]:
    artifact_ref = json.loads(ARTIFACT_REF_FIXTURE.read_text(encoding="utf-8"))["artifact_ref"]
    artifact_payload = b"strategy-wheel"
    manifest = {
        "schema_version": 1,
        "execution_abi": "alephain.strategy_runtime.v1",
        "entry_point_group": "alephain.strategy_runtime.v1",
        "entry_point": "strategy.runtime:build",
        "engine": "nautilus",
        "engine_version": "1.230.0",
        "requires_python": ">=3.12,<3.13",
        "base_contracts_version": "1.0.0rc1",
        "engine_toolkit_version": "1.0.0rc1",
        "config_schema_sha256": "5" * 64,
        "catalog_alias": None,
        "runtime_artifacts": artifact_ref["required_runtime_artifacts"],
    }
    manifest_json, manifest_digest = _canonical_document(manifest)
    artifact_digest = hashlib.sha256(artifact_payload).hexdigest()
    artifact_ref["artifact_coordinate"] = (
        f"ghcr.io/alephain/strategy/supertrend@sha256:{artifact_digest}"
    )
    artifact_ref["artifact_sha256"] = artifact_digest
    artifact_ref["artifact_size_bytes"] = len(artifact_payload)
    artifact_ref["manifest_sha256"] = manifest_digest
    artifact_ref["manifest_size_bytes"] = len(manifest_json.encode("utf-8"))
    artifact_ref_json, artifact_ref_digest = _canonical_document(artifact_ref)

    release_bom = {
        "schema_version": "alephain.strategy-release-bom.v1",
        "entry_point_group": "alephain.strategy_runtime.v1",
        "entry_point_name": "strategy.runtime:build",
        "members": [
            {
                "role": "strategy_wheel",
                "coordinate": artifact_ref["artifact_coordinate"],
                "name": "strategy.whl",
                "media_type": "application/vnd.pypa.wheel",
                "size_bytes": len(artifact_payload),
                "sha256": artifact_digest,
            },
            artifact_ref["required_runtime_artifacts"][0],
        ],
    }
    release_bom_json, release_bom_digest = _canonical_document(release_bom)
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "strategy.whl", "digest": {"sha256": artifact_digest}}],
        "predicateType": "https://alephain.guild/strategy-release/v1",
        "predicate": {"schema_version": "alephain.strategy-release.v1"},
    }
    statement_json, statement_digest = _canonical_document(statement)
    bundle_digest = hashlib.sha256(b"sigstore-bundle").hexdigest()
    detached_attestation_ref = {
        "schema_version": 1,
        "statement_coordinate": f"oci://statement@sha256:{statement_digest}",
        "statement_sha256": statement_digest,
        "bundle_coordinate": f"oci://bundle@sha256:{bundle_digest}",
        "bundle_sha256": bundle_digest,
        "payload_type": "application/vnd.in-toto+json",
        "predicate_type": "https://alephain.guild/strategy-release/v1",
    }
    detached_ref_json, detached_ref_digest = _canonical_document(detached_attestation_ref)

    release_id = "50000000-0000-4000-8000-000000000005"
    definition_id = "40000000-0000-4000-8000-000000000004"
    evidence_input = {
        "schema_version": 1,
        "strategy_release_id": release_id,
        "artifact_ref_digest": artifact_ref_digest,
        "release_bom_digest": release_bom_digest,
        "release_statement_digest": statement_digest,
        "detached_attestation_ref_digest": detached_ref_digest,
        "bundle_sha256": bundle_digest,
        "signed_producer_claims": {"producer_repository": "alchymia-labs/philosophers-stone"},
        "sigstore_proof": {"bundle_sha256": bundle_digest},
        "local_policy_evaluation": {"decision": "accepted"},
        "composite_evidence_digest": "",
    }
    evidence_input_json = json.dumps(
        evidence_input,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    evidence_digest = hashlib.sha256(evidence_input_json.encode("utf-8")).hexdigest()
    evidence = dict(evidence_input)
    evidence["composite_evidence_digest"] = evidence_digest
    snapshot = {
        "release_id": release_id,
        "definition_id": definition_id,
        "lifecycle_version": 2,
        "release_number": 1,
        "artifact_ref_digest": artifact_ref_digest,
        "manifest_digest": manifest_digest,
        "release_bom_digest": release_bom_digest,
        "artifact_evidence_digest": evidence_digest,
        "validated_at": "2026-07-23T01:02:03Z",
        "snapshot_digest": "",
    }
    snapshot["snapshot_digest"] = _strategy_release_snapshot_digest(snapshot)
    command = _command_from_event(
        case_name="deployment_spec_ready_for_runner",
        artifact_source={
            "kind": "strategy_release",
            "snapshot": {
                "definition_id": definition_id,
                "release_id": release_id,
                "release_version": 1,
                "artifact_digest": artifact_digest,
                "manifest_digest": manifest_digest,
                "snapshot_digest": snapshot["snapshot_digest"],
            },
        },
    )
    source = command.artifact_source
    assert isinstance(source, StrategyReleaseArtifactSourceV1)
    effective_config_digest = canonical_json_digest(command.deployment_spec["execution_config"])
    stored_command_fingerprint = _canonical_digest(
        {
            "schema_version": 1,
            "deployment_instance_id": str(command.deployment_instance_id),
            "deployment_spec_id": str(command.deployment_spec_id),
            "deployment_spec_digest": command.deployment_spec_digest,
            "generation": command.generation,
            "artifact_source_digest": source.snapshot.snapshot_digest,
            "effective_config_digest": effective_config_digest,
            "desired_lifecycle_state": command.lifecycle_state,
        }
    )
    execution_binding = {
        "kind": "strategy_release",
        "binding": {
            "schema_version": 1,
            "deployment_instance_id": str(command.deployment_instance_id),
            "deployment_spec_id": str(command.deployment_spec_id),
            "deployment_spec_digest": command.deployment_spec_digest,
            "generation": command.generation,
            "strategy_release_id": release_id,
            "release_bom_digest": release_bom_digest,
            "artifact_ref": artifact_ref,
            "effective_config_digest": effective_config_digest,
        },
    }
    release_binding = {
        "strategy_release_id": release_id,
        "manifest": manifest,
        "manifest_digest": manifest_digest,
        "manifest_canonical_json": manifest_json,
        "release_bom_digest": release_bom_digest,
        "release_bom_canonical_json": release_bom_json,
        "release_statement_digest": statement_digest,
        "release_statement": statement,
        "release_statement_canonical_json": statement_json,
        "detached_attestation_ref_digest": detached_ref_digest,
        "detached_attestation_ref": detached_attestation_ref,
        "detached_attestation_ref_canonical_json": detached_ref_json,
        "artifact_ref_digest": artifact_ref_digest,
        "artifact_ref": artifact_ref,
        "artifact_ref_canonical_json": artifact_ref_json,
    }
    response = {
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
        "artifact_binding_digest": _canonical_digest(execution_binding),
        "artifact_binding": execution_binding,
        "development_source_ref": None,
        "strategy_release_material": {
            "snapshot": snapshot,
            "artifact_binding": release_binding,
            "artifact_evidence": evidence,
            "artifact_evidence_digest_input_json": evidence_input_json,
        },
    }
    return command, response


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


def test_strategy_release_resolution_accepts_exact_owner_material() -> None:
    command, response = _strategy_material_fixture()

    material = parse_strategy_release_material_resolution(
        response,
        command=command,
        command_fingerprint=command.command_fingerprint,
    )

    assert material.release_authority.strategy_release_id == (
        command.artifact_source.snapshot.release_id
    )
    assert material.release_authority.strategy_release_snapshot_digest == (
        command.artifact_source.snapshot.snapshot_digest
    )
    assert material.release_statement_bytes


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("snapshot_digest", "differs from signed DeploymentSpec"),
        ("evidence_input", "artifact evidence digest differs"),
        ("development_ref", "contains development source"),
        ("extra_material_field", "material shape is not exact V1"),
    ],
)
def test_strategy_release_resolution_rejects_authority_drift(
    mutation: str,
    message: str,
) -> None:
    command, response = _strategy_material_fixture()
    candidate = deepcopy(response)
    if mutation == "snapshot_digest":
        candidate["strategy_release_material"]["snapshot"]["snapshot_digest"] = "f" * 64
    elif mutation == "evidence_input":
        candidate["strategy_release_material"]["artifact_evidence_digest_input_json"] += " "
    elif mutation == "development_ref":
        candidate["development_source_ref"] = {}
    else:
        candidate["strategy_release_material"]["unexpected"] = True

    with pytest.raises((RunnerMaterialResolutionError, ValueError), match=message):
        parse_strategy_release_material_resolution(
            candidate,
            command=command,
            command_fingerprint=command.command_fingerprint,
        )
