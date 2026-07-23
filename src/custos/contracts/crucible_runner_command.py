"""Strict V1 consumer for Crucible-signed DeploymentSpec commands."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StringConstraints, model_validator

from custos.contracts.deployment import (
    DeploymentArtifactSourceV1,
    DeploymentSpec,
    DevelopmentArtifactSourceV1,
    StrategyReleaseArtifactSourceV1,
    canonical_deployment_spec_digest,
    parse_deployment_artifact_source,
    runtime_deployment_spec,
    validate_risk_policy_snapshot,
)

__all__ = ["CrucibleRunnerDeploymentCommandV1"]

_SIGNATURE_PROFILE = "crucible-domain-event-v1-exact-bytes"
_EVENT_ENCODING = "application/json;base64url"
_SUBJECT_PREFIX = "crucible.runner.command.v1"
_EVENT_TYPE_PREFIXES = frozenset(
    {
        "DeploymentSpecReadyForRunner",
        "DeploymentInstanceDesiredStateChanged",
    }
)
_FINGERPRINT_DOMAIN = b"CRUCIBLE-RUNNER-DEPLOYMENT-COMMAND-FINGERPRINT-V1\0"
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_ENVELOPE_FIELDS = (
    "schema_version",
    "signature_profile",
    "event_encoding",
    "event_bytes",
    "signature_key_id",
    "signature",
)
_EVENT_FIELDS = (
    "schema_version",
    "event_id",
    "tenant_id",
    "event_plane",
    "bounded_context",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "event_type",
    "payload",
    "correlation_id",
    "actor_assertion_jti",
    "occurred_at",
)
_COMMAND_FIELDS = (
    "schema_version",
    "tenant_id",
    "mode",
    "runner_id",
    "deployment_instance_id",
    "deployment_spec_id",
    "deployment_spec_digest",
    "generation",
    "lifecycle_state",
    "deployment_spec",
    "issued_at",
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TenantId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class CrucibleRunnerDeploymentCommandV1(BaseModel):
    """The sole Custos command model for the first-production V1 wire."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    tenant_id: TenantId
    mode: Literal["sandbox", "testnet", "live"]
    runner_id: Annotated[UUID, Field(strict=False)]
    deployment_instance_id: Annotated[UUID, Field(strict=False)]
    deployment_spec_id: Annotated[UUID, Field(strict=False)]
    deployment_spec_digest: Sha256Hex
    generation: int = Field(strict=True, ge=1)
    lifecycle_state: Literal["running", "paused", "stopped", "archived"]
    deployment_spec: dict[str, Any]
    issued_at: str = Field(min_length=1)

    _exact_signed_event_bytes: bytes = PrivateAttr(default=b"")
    _verified_subject: str = PrivateAttr(default="")
    _producer_fingerprint: str = PrivateAttr(default="")

    @property
    def trading_mode(self) -> str:
        """Return the explicit trading mode used by local execution state."""

        return self.mode

    @property
    def exact_signed_event_bytes(self) -> bytes:
        return self._exact_signed_event_bytes

    @property
    def verified_subject(self) -> str:
        return self._verified_subject

    @property
    def producer_fingerprint(self) -> str:
        return self._producer_fingerprint

    @property
    def strategy_id(self) -> UUID:
        """Return the canonical strategy identity carried by DeploymentSpec V1."""

        return _required_uuid(
            self.deployment_spec.get("strategy_id"), "deployment_spec.strategy_id"
        )

    @property
    def strategy_release_id(self) -> UUID:
        source = self.artifact_source
        if not isinstance(source, StrategyReleaseArtifactSourceV1):
            raise ValueError("development source has no StrategyRelease identity")
        return source.snapshot.release_id

    @property
    def strategy_artifact_digest(self) -> str:
        return self.artifact_identity_digest

    @property
    def artifact_source(self) -> DeploymentArtifactSourceV1:
        return parse_deployment_artifact_source(self.deployment_spec["artifact_source"])

    @property
    def artifact_identity_digest(self) -> str:
        source = self.artifact_source
        if isinstance(source, StrategyReleaseArtifactSourceV1):
            return source.snapshot.artifact_digest
        return source.snapshot.source_sha256

    @property
    def is_development_source(self) -> bool:
        return isinstance(self.artifact_source, DevelopmentArtifactSourceV1)

    def to_runtime_spec(self) -> DeploymentSpec:
        """Translate the signed canonical spec into the local engine view."""

        return runtime_deployment_spec(
            canonical=self.deployment_spec,
            deployment_instance_id=self.deployment_instance_id,
            deployment_spec_id=self.deployment_spec_id,
            deployment_spec_digest=self.deployment_spec_digest,
            generation=self.generation,
            lifecycle_state=self.lifecycle_state,
        )

    @model_validator(mode="after")
    def validate_spec_binding(self) -> Self:
        spec = self.deployment_spec
        if spec.get("schema_version") != 1:
            raise ValueError("deployment_spec.schema_version must be exactly 1")
        bindings = (
            (spec.get("tenant_id"), self.tenant_id, "tenant_id"),
            (spec.get("trading_mode"), self.mode, "trading_mode"),
            (str(spec.get("target_runner_id")), str(self.runner_id), "target_runner_id"),
            (
                str(spec.get("deployment_spec_id")),
                str(self.deployment_spec_id),
                "deployment_spec_id",
            ),
        )
        for actual, expected, field_name in bindings:
            if actual != expected:
                raise ValueError(f"deployment_spec.{field_name} differs from command authority")
        validate_risk_policy_snapshot(spec)
        if canonical_deployment_spec_digest(spec) != self.deployment_spec_digest:
            raise ValueError("deployment_spec digest differs from command authority")
        strategy_id = _required_uuid(spec.get("strategy_id"), "deployment_spec.strategy_id")
        source = parse_deployment_artifact_source(spec.get("artifact_source"))
        if source.snapshot.definition_id != strategy_id:
            raise ValueError("deployment_spec artifact source differs from strategy_id")
        if isinstance(source, DevelopmentArtifactSourceV1) and (
            self.mode != "sandbox"
            or spec.get("promotion_id") is not None
            or spec.get("promotion_evidence_digest") is not None
        ):
            raise ValueError("development source is sandbox-only and cannot be promoted")
        _required_timestamp(self.issued_at, "issued_at")
        return self

    @classmethod
    def from_verified_signed_envelope(
        cls,
        *,
        subject: str,
        signed_envelope_bytes: bytes,
    ) -> Self:
        """Parse exact bytes after the caller verifies the Ed25519 signature."""

        envelope = _strict_json_object(signed_envelope_bytes, "signed_envelope")
        _require_exact_keys(envelope, _ENVELOPE_FIELDS, "signed_envelope")
        if envelope["schema_version"] != 1:
            raise ValueError("signed envelope schema_version must be exactly 1")
        if envelope["signature_profile"] != _SIGNATURE_PROFILE:
            raise ValueError("signed envelope signature_profile differs")
        if envelope["event_encoding"] != _EVENT_ENCODING:
            raise ValueError("signed envelope event_encoding differs")
        key_id = envelope["signature_key_id"]
        if not isinstance(key_id, str) or _KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise ValueError("signed envelope signature_key_id is invalid")
        event_bytes = _decode_base64url(envelope["event_bytes"], "signed_envelope.event_bytes")
        signature = _decode_base64url(envelope["signature"], "signed_envelope.signature")
        if len(signature) != 64:
            raise ValueError("signed envelope signature must be exactly 64 bytes")

        event = _strict_json_object(event_bytes, "event_document")
        _require_exact_keys(event, _EVENT_FIELDS, "event_document")
        if _json_bytes_preserving_order(event) != event_bytes:
            raise ValueError("event document is not exact compact JSON")
        if event["schema_version"] != 1:
            raise ValueError("event schema_version must be exactly 1")
        _required_uuid(event["event_id"], "event.event_id")
        _required_uuid(event["aggregate_id"], "event.aggregate_id")
        _required_timestamp(event["occurred_at"], "event.occurred_at")
        if (
            not isinstance(event["tenant_id"], str)
            or _SAFE_TOKEN.fullmatch(event["tenant_id"]) is None
        ):
            raise ValueError("event.tenant_id is invalid")

        plane = event["event_plane"]
        if not isinstance(plane, dict) or tuple(plane) != ("kind", "trading_mode"):
            raise ValueError("event_plane must be a closed mode object")
        if plane["kind"] != "mode" or plane["trading_mode"] not in {
            "sandbox",
            "testnet",
            "live",
        }:
            raise ValueError("event_plane must identify one supported trading mode")
        if event["bounded_context"] != "deployment":
            raise ValueError("event bounded_context must be deployment")
        if event["aggregate_type"] != "deployment_instance":
            raise ValueError("event aggregate_type must be deployment_instance")
        if type(event["aggregate_version"]) is not int or event["aggregate_version"] < 1:
            raise ValueError("event aggregate_version must be positive")

        payload = event["payload"]
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        _require_exact_keys(payload, _COMMAND_FIELDS, "command")
        command = cls.model_validate(payload)

        event_type = event["event_type"]
        if not isinstance(event_type, str):
            raise ValueError("event_type must be a string")
        parts = event_type.split(".")
        if (
            len(parts) != 3
            or parts[0] not in _EVENT_TYPE_PREFIXES
            or parts[1] != str(command.runner_id)
            or parts[2] != str(command.deployment_instance_id)
        ):
            raise ValueError("event_type differs from command runner and deployment instance")
        expected_subject = (
            f"{_SUBJECT_PREFIX}.{command.tenant_id}.{command.runner_id}.{command.mode}"
        )
        if subject != expected_subject:
            raise ValueError("NATS subject differs from the signed V1 command")
        if (
            event["tenant_id"] != command.tenant_id
            or plane["trading_mode"] != command.mode
            or str(event["aggregate_id"]) != str(command.deployment_instance_id)
            or event["aggregate_version"] != command.generation
        ):
            raise ValueError("event authority differs from command authority")

        fingerprint = hashlib.sha256(_framed(_FINGERPRINT_DOMAIN, subject, event_bytes)).hexdigest()
        object.__setattr__(command, "_exact_signed_event_bytes", event_bytes)
        object.__setattr__(command, "_verified_subject", subject)
        object.__setattr__(command, "_producer_fingerprint", fingerprint)
        return command


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: dict[str, Any], expected: tuple[str, ...], label: str, *, ordered: bool = False
) -> None:
    actual = tuple(value)
    if (actual != expected) if ordered else (frozenset(actual) != frozenset(expected)):
        raise ValueError(f"{label} has an open or incomplete shape")


def _decode_base64url(value: object, label: str) -> bytes:
    if not isinstance(value, str) or _BASE64URL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is not canonical base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} is invalid base64url") from exc


def _json_bytes_preserving_order(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _required_uuid(value: object, label: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc
    if parsed.int == 0 or str(parsed) != str(value):
        raise ValueError(f"{label} must be a canonical non-nil UUID")
    return parsed


def _required_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _framed(domain: bytes, subject: str, event_bytes: bytes) -> bytes:
    subject_bytes = subject.encode("utf-8")
    return b"".join(
        (
            domain,
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )
