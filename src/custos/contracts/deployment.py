"""Crucible-signed DeploymentSpec consumer contract.

ARX authorizes human intent but never signs or relays runner commands. Crucible
publishes an exact-instance command as a signed domain event. Custos verifies the
original event bytes and subject before translating the canonical payload into
the local execution-engine shape.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, TypeAlias
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

DOMAIN_EVENT_SIGNATURE_CONTEXT = b"CRUCIBLE-DOMAIN-EVENT-V1\0"
DOMAIN_EVENT_SIGNATURE_PROFILE = "crucible-domain-event-v1-exact-bytes"
DOMAIN_EVENT_ENCODING = "application/json;base64url"
DEPLOYMENT_SPEC_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
_CANONICAL_DEPLOYMENT_SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_spec_id",
        "tenant_id",
        "trading_mode",
        "strategy_id",
        "artifact_source",
        "execution_config",
        "strategy_product_id",
        "risk_policy_id",
        "risk_policy_version",
        "risk_policy",
        "risk_policy_digest",
        "target_runner_id",
        "engine_binding_id",
        "execution_channel",
        "credential_scope",
        "runner_contract_requirements",
        "venue_source_policy",
        "source_policy_digest",
        "scheduling_policy",
        "scheduling_policy_digest",
        "promotion_id",
        "promotion_evidence_digest",
    }
)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
SafeId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]


class TradingMode(StrEnum):
    SANDBOX = "sandbox"
    TESTNET = "testnet"
    LIVE = "live"


class LifecycleState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ARCHIVED = "archived"


class SandboxExecutionConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starting_balances: list[str] = Field(min_length=1)


class RunnerExecutionConfigV1(BaseModel):
    """Typed engine input carried by the sole first-production command V1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    engine: Annotated[str, StringConstraints(pattern=r"^nautilus$")]
    connector: SafeId
    pairs: list[str] = Field(min_length=1)
    leverage: StrictInt = Field(ge=1)
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    log_level: str = "INFO"
    sandbox: SandboxExecutionConfigV1 | None = None
    nautilus_config: dict[str, Any] = Field(default_factory=dict)


class CredentialScopeRefV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: UUID
    scope_digest: Sha256Hex


class StrategyReleaseSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    definition_id: Annotated[UUID, Field(strict=False)]
    release_id: Annotated[UUID, Field(strict=False)]
    release_version: StrictInt = Field(ge=1)
    artifact_digest: Sha256Hex
    manifest_digest: Sha256Hex
    snapshot_digest: Sha256Hex


class DevelopmentArtifactSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    definition_id: Annotated[UUID, Field(strict=False)]
    source_sha256: Sha256Hex
    publication_receipt_digest: Sha256Hex
    snapshot_digest: Sha256Hex
    promotable: Literal[False]

    @model_validator(mode="after")
    def validate_snapshot_digest(self) -> Self:
        if canonical_development_snapshot_digest(self) != self.snapshot_digest:
            raise ValueError("development artifact snapshot digest differs")
        return self


class StrategyReleaseArtifactSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["strategy_release"]
    snapshot: StrategyReleaseSnapshotV1


class DevelopmentArtifactSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["development_source"]
    snapshot: DevelopmentArtifactSnapshotV1


DeploymentArtifactSourceV1: TypeAlias = Annotated[
    StrategyReleaseArtifactSourceV1 | DevelopmentArtifactSourceV1,
    Field(discriminator="kind"),
]
_ARTIFACT_SOURCE_ADAPTER = TypeAdapter(DeploymentArtifactSourceV1)


class DeploymentSpec(BaseModel):
    """Validated local engine view of a canonical Crucible DeploymentSpec V1."""

    model_config = ConfigDict(extra="forbid", title="DeploymentSpecPayload v1")

    deployment_spec_id: UUID
    deployment_instance_id: UUID
    deployment_spec_digest: Sha256Hex
    strategy_id: UUID
    artifact_source: DeploymentArtifactSourceV1
    generation: StrictInt = Field(ge=1)
    trading_mode: TradingMode
    lifecycle_state: LifecycleState
    credential_scope: CredentialScopeRefV1
    connector: str = Field(min_length=1)
    pairs: list[str] = Field(min_length=1)
    leverage: StrictInt = Field(ge=1)
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    log_level: str = "INFO"
    sandbox: SandboxExecutionConfigV1 | None = None
    nautilus_config: dict[str, Any] = Field(default_factory=dict)
    promotion_id: UUID | None = None
    promotion_evidence_digest: Sha256Hex | None = None

    @model_validator(mode="after")
    def validate_mode_requirements(self) -> Self:
        if self.trading_mode is TradingMode.LIVE:
            if self.promotion_id is None or self.promotion_evidence_digest is None:
                raise ValueError("live deployment requires Crucible-signed promotion evidence")
        if self.trading_mode is TradingMode.SANDBOX and self.sandbox is None:
            raise ValueError("sandbox deployment requires sandbox.starting_balances")
        return self


class _SignedDomainEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    signature_profile: str
    event_encoding: str
    event_bytes: str
    signature_key_id: str
    signature: str


@dataclass(frozen=True, slots=True)
class CrucibleDomainEventVerifier:
    key_id: str
    public_key: Ed25519PublicKey

    @classmethod
    def from_file(cls, path: str | Path, *, key_id: str) -> CrucibleDomainEventVerifier:
        if not key_id.strip():
            raise ValueError("Crucible domain-event key id is required")
        encoded = Path(path).expanduser().read_text(encoding="utf-8").strip()
        try:
            raw = bytes.fromhex(encoded) if len(encoded) == 64 else _decode_base64url(encoded)
            public_key = Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "Crucible domain-event public key must encode 32 Ed25519 bytes"
            ) from exc
        return cls(key_id=key_id, public_key=public_key)

    def verify(self, *, subject: str, data: bytes) -> bytes:
        envelope = _SignedDomainEventEnvelope.model_validate_json(data)
        if envelope.signature_profile != DOMAIN_EVENT_SIGNATURE_PROFILE:
            raise ValueError("unsupported Crucible domain-event signature profile")
        if envelope.event_encoding != DOMAIN_EVENT_ENCODING:
            raise ValueError("unsupported Crucible domain-event encoding")
        if envelope.signature_key_id != self.key_id:
            raise ValueError("Crucible domain-event signing key id does not match authority")
        event_bytes = _decode_base64url(envelope.event_bytes)
        signature = _decode_base64url(envelope.signature)
        if len(signature) != 64:
            raise ValueError("Crucible domain-event signature must be 64 bytes")
        subject_bytes = subject.encode("utf-8")
        framed = b"".join(
            (
                DOMAIN_EVENT_SIGNATURE_CONTEXT,
                len(subject_bytes).to_bytes(4, "big"),
                subject_bytes,
                len(event_bytes).to_bytes(8, "big"),
                event_bytes,
            )
        )
        try:
            self.public_key.verify(signature, framed)
        except InvalidSignature as exc:
            raise ValueError("Crucible domain-event signature verification failed") from exc
        return event_bytes


def runtime_deployment_spec(
    *,
    canonical: dict[str, Any],
    deployment_instance_id: UUID,
    deployment_spec_id: UUID,
    deployment_spec_digest: str,
    generation: int,
    lifecycle_state: str,
) -> DeploymentSpec:
    execution_config = canonical.get("execution_config")
    credential_scope = canonical.get("credential_scope")
    if not isinstance(execution_config, dict):
        raise ValueError("canonical DeploymentSpec execution_config is invalid")
    if not isinstance(credential_scope, dict):
        raise ValueError("canonical DeploymentSpec credential scope is invalid")
    runtime = RunnerExecutionConfigV1.model_validate(execution_config)
    return DeploymentSpec.model_validate(
        {
            "deployment_spec_id": deployment_spec_id,
            "deployment_instance_id": deployment_instance_id,
            "deployment_spec_digest": deployment_spec_digest,
            "strategy_id": canonical.get("strategy_id"),
            "artifact_source": canonical.get("artifact_source"),
            "generation": generation,
            "trading_mode": canonical.get("trading_mode"),
            "lifecycle_state": lifecycle_state,
            "credential_scope": credential_scope,
            "connector": runtime.connector,
            "pairs": runtime.pairs,
            "leverage": runtime.leverage,
            "strategy_config": runtime.strategy_config,
            "log_level": runtime.log_level,
            "sandbox": runtime.sandbox,
            "nautilus_config": runtime.nautilus_config,
            "promotion_id": canonical.get("promotion_id"),
            "promotion_evidence_digest": canonical.get("promotion_evidence_digest"),
        }
    )


def parse_deployment_artifact_source(value: object) -> DeploymentArtifactSourceV1:
    return _ARTIFACT_SOURCE_ADAPTER.validate_python(value)


def canonical_development_snapshot_digest(snapshot: DevelopmentArtifactSnapshotV1) -> str:
    payload = {
        "schema_version": 1,
        "definition_id": str(snapshot.definition_id),
        "source_sha256": snapshot.source_sha256,
        "publication_receipt_digest": snapshot.publication_receipt_digest,
        "promotable": False,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decode_base64url(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("base64url value is empty or contains whitespace")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url value") from exc


def canonical_deployment_spec_digest(canonical: dict[str, Any]) -> str:
    """Match Crucible `sha256-canonical-json-v1` over the canonical spec only.

    The digest field and command envelope are intentionally excluded. Exact
    field-set validation prevents an additive producer change from silently
    changing the cross-language hash contract.
    """
    fields = frozenset(canonical)
    if fields != _CANONICAL_DEPLOYMENT_SPEC_FIELDS:
        missing = sorted(_CANONICAL_DEPLOYMENT_SPEC_FIELDS - fields)
        extra = sorted(fields - _CANONICAL_DEPLOYMENT_SPEC_FIELDS)
        raise ValueError(
            f"canonical DeploymentSpec field set differs: missing={missing}, extra={extra}"
        )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_risk_policy_snapshot(canonical: dict[str, Any]) -> None:
    """Verify the opaque Crucible-owned policy bytes without interpreting them."""

    policy = canonical.get("risk_policy")
    expected = canonical.get("risk_policy_digest")
    if not isinstance(policy, dict) or not policy:
        raise ValueError("canonical DeploymentSpec risk_policy must be an object")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("canonical DeploymentSpec risk_policy_digest is invalid")
    encoded = json.dumps(
        policy,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != expected:
        raise ValueError("canonical DeploymentSpec risk policy digest differs")
