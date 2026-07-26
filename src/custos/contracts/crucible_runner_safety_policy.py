"""Strict consumer for the Crucible-owned runner aggregate-cap policy.

The signed event preserves Rust struct-order compact JSON while the embedded
policy digest uses the runner-safety policy recursively key-sorted canonical JSON profile. This
consumer validates both representations, the subject framing, fingerprint,
Ed25519 signature, and runner scope before returning a typed policy.
DeploymentSpec is deliberately absent from this API.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

SIGNATURE_CONTEXT = b"CRUCIBLE-DOMAIN-EVENT-V1\0"
FINGERPRINT_CONTEXT = b"CRUCIBLE-RUNNER-SAFETY-POLICY-V1\0"
SIGNATURE_PROFILE = "crucible-domain-event-v1-exact-bytes"
EVENT_ENCODING = "application/json;base64url"
SUBJECT_PREFIX = "crucible.runner.policy.v1"

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
_POLICY_FIELDS = (
    "schema_version",
    "authority_coordinate",
    "policy_id",
    "tenant_id",
    "runner_id",
    "trading_mode",
    "revision",
    "settlement_currency",
    "max_order_notional",
    "max_total_notional",
    "exposure_model",
    "breach_action",
    "risk_reducing_orders",
    "effective_at",
    "expires_at",
    "status",
    "previous",
    "policy_digest",
)
_OUTBOX_PAYLOAD_FIELDS = (
    "policy",
    "policy_id",
    "revision",
    "policy_digest",
    "exact_subject",
)
_POLICY_REF_FIELDS = ("policy_id", "revision", "policy_digest")
_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{3,12}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_POSITIVE_DECIMAL_PATTERN = re.compile(r"^(?:[1-9][0-9]*)(?:\.[0-9]*[1-9])?$|^0\.[0-9]*[1-9]$")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TRADING_MODES = frozenset({"live", "sandbox", "testnet"})


TenantId = Annotated[str, Field(pattern=_TENANT_PATTERN.pattern)]
Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN.pattern)]
PositiveCanonicalDecimal = Annotated[str, Field(pattern=_POSITIVE_DECIMAL_PATTERN.pattern)]


class RunnerSafetyPolicyVerificationReason(StrEnum):
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_SCHEMA = "invalid_schema"
    WRONG_SCOPE = "wrong_scope"


class RunnerSafetyPolicyVerificationError(ValueError):
    """Fail-closed verification error with a stable sanitized reason code."""

    def __init__(self, reason_code: RunnerSafetyPolicyVerificationReason, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class RunnerAggregateCapPolicyRefV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: UUID
    revision: StrictInt = Field(ge=1)
    policy_digest: Digest

    @model_validator(mode="after")
    def reject_nil(self) -> Self:
        if self.policy_id.int == 0:
            raise ValueError("previous policy_id must not be nil")
        return self


class RunnerAggregateCapPolicyV1(BaseModel):
    """Closed runner-safety policy body after exact-byte and digest verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    authority_coordinate: Literal["crucible.runner-aggregate-cap-policy.v1"]
    policy_id: UUID
    tenant_id: TenantId
    runner_id: UUID
    trading_mode: Literal["live", "sandbox", "testnet"]
    revision: StrictInt = Field(ge=1)
    settlement_currency: Annotated[str, Field(pattern=_CURRENCY_PATTERN.pattern)]
    max_order_notional: PositiveCanonicalDecimal
    max_total_notional: PositiveCanonicalDecimal
    exposure_model: Literal["filled_plus_active_reservations"]
    breach_action: Literal["freeze_risk_increasing"]
    risk_reducing_orders: Literal["always_permitted"]
    effective_at: datetime
    expires_at: datetime
    status: Literal["active", "revoked", "expired"]
    previous: RunnerAggregateCapPolicyRefV1 | None
    policy_digest: Digest

    @property
    def max_order_notional_decimal(self) -> Decimal:
        return Decimal(self.max_order_notional)

    @property
    def max_total_notional_decimal(self) -> Decimal:
        return Decimal(self.max_total_notional)

    @model_validator(mode="after")
    def validate_closed_contract(self) -> Self:
        if self.policy_id.int == 0 or self.runner_id.int == 0:
            raise ValueError("policy and runner identities must not be nil")
        if self.max_order_notional_decimal > self.max_total_notional_decimal:
            raise ValueError("max_order_notional must not exceed max_total_notional")
        if self.effective_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("policy effective and expiry timestamps must be timezone aware")
        if self.expires_at <= self.effective_at:
            raise ValueError("policy expiry must be after its effective time")
        if self.previous is None:
            if self.revision != 1:
                raise ValueError("initial policy must use revision 1")
        else:
            previous = self.previous
            if previous.policy_id == self.policy_id or self.revision != previous.revision + 1:
                raise ValueError("successor policy fence is invalid")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedRunnerSafetyPolicy:
    policy: RunnerAggregateCapPolicyV1
    exact_subject: str
    exact_event_bytes: bytes
    exact_signed_envelope_bytes: bytes
    signature_key_id: str
    fingerprint: str
    verified_event_bytes_sha256: str


@dataclass(frozen=True, slots=True)
class CrucibleRunnerSafetyPolicyAuthenticator:
    """Verify one exact runner-safety policy envelope against runner-local authority keys."""

    expected_tenant_id: str
    expected_runner_id: UUID
    allowed_trading_modes: frozenset[str]
    signature_keys: Mapping[str, Ed25519PublicKey]

    def __post_init__(self) -> None:
        if not _TENANT_PATTERN.fullmatch(self.expected_tenant_id):
            raise ValueError("expected tenant id is invalid")
        if self.expected_runner_id.int == 0:
            raise ValueError("expected runner id must not be nil")
        modes = frozenset(self.allowed_trading_modes)
        if not modes or not modes <= _TRADING_MODES:
            raise ValueError("allowed trading modes must be a non-empty known subset")
        keys = dict(self.signature_keys)
        if not keys or any(
            not key_id.strip() or not isinstance(key, Ed25519PublicKey)
            for key_id, key in keys.items()
        ):
            raise ValueError("at least one named Ed25519 policy authority key is required")
        object.__setattr__(self, "allowed_trading_modes", modes)
        object.__setattr__(self, "signature_keys", MappingProxyType(keys))

    def verify(self, *, subject: str, signed_envelope_bytes: bytes) -> VerifiedRunnerSafetyPolicy:
        try:
            envelope = _strict_json_object(signed_envelope_bytes, "signed envelope")
            _require_exact_keys(envelope, _ENVELOPE_FIELDS, "signed envelope")
            material = _parse_envelope(envelope, subject)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.INVALID_SCHEMA,
                f"runner safety policy envelope is invalid: {exc}",
            ) from exc

        authority_key = self.signature_keys.get(material.signature_key_id)
        if authority_key is None:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.INVALID_SIGNATURE,
                "runner safety policy signature key is not trusted",
            )
        try:
            authority_key.verify(material.signature, material.signature_input)
        except InvalidSignature as exc:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.INVALID_SIGNATURE,
                "runner safety policy signature verification failed",
            ) from exc

        try:
            policy = _parse_exact_event(
                subject=material.subject,
                event_bytes=material.event_bytes,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.INVALID_SCHEMA,
                f"runner safety policy schema, digest, or binding is invalid: {exc}",
            ) from exc

        if policy.tenant_id != self.expected_tenant_id:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.WRONG_SCOPE,
                "runner safety policy tenant differs from runner authority",
            )
        if policy.runner_id != self.expected_runner_id:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.WRONG_SCOPE,
                "runner safety policy runner differs from runner authority",
            )
        if policy.trading_mode not in self.allowed_trading_modes:
            raise RunnerSafetyPolicyVerificationError(
                RunnerSafetyPolicyVerificationReason.WRONG_SCOPE,
                "runner safety policy trading mode is not enabled by this runner",
            )
        return VerifiedRunnerSafetyPolicy(
            policy=policy,
            exact_subject=material.subject,
            exact_event_bytes=material.event_bytes,
            exact_signed_envelope_bytes=signed_envelope_bytes,
            signature_key_id=material.signature_key_id,
            fingerprint=hashlib.sha256(
                _frame(FINGERPRINT_CONTEXT, material.subject, material.event_bytes)
            ).hexdigest(),
            verified_event_bytes_sha256=hashlib.sha256(material.event_bytes).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class _EnvelopeMaterial:
    subject: str
    event_bytes: bytes
    signature_input: bytes
    signature_key_id: str
    signature: bytes


def _parse_envelope(envelope: dict[str, Any], subject: str) -> _EnvelopeMaterial:
    if envelope["schema_version"] != 1:
        raise ValueError("envelope schema_version must be exactly 1")
    if envelope["signature_profile"] != SIGNATURE_PROFILE:
        raise ValueError("signature_profile differs from the runner-safety policy contract")
    if envelope["event_encoding"] != EVENT_ENCODING:
        raise ValueError("event_encoding differs from the runner-safety policy contract")
    key_id = envelope["signature_key_id"]
    if not isinstance(subject, str) or not subject:
        raise ValueError("subject must be non-empty")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("signature_key_id must be non-empty")
    event_bytes = _decode_base64url(envelope["event_bytes"], "event bytes")
    signature_input = _frame(SIGNATURE_CONTEXT, subject, event_bytes)
    signature = _decode_base64url(envelope["signature"], "signature")
    if len(signature) != 64:
        raise ValueError("signature must contain exactly 64 Ed25519 bytes")
    return _EnvelopeMaterial(
        subject=subject,
        event_bytes=event_bytes,
        signature_input=signature_input,
        signature_key_id=key_id,
        signature=signature,
    )


def _parse_exact_event(*, subject: str, event_bytes: bytes) -> RunnerAggregateCapPolicyV1:
    event = _strict_json_object(event_bytes, "event document")
    _require_exact_keys(event, _EVENT_FIELDS, "event document", ordered=True)
    if _compact_json_bytes(event) != event_bytes:
        raise ValueError("event document is not exact compact JSON")
    if event["schema_version"] != 1:
        raise ValueError("event schema_version must be exactly 1")
    for field in ("event_id", "correlation_id"):
        _require_canonical_uuid(event[field], f"event.{field}")
    _require_timestamp(event["occurred_at"], "event.occurred_at")
    event_plane = event["event_plane"]
    if not isinstance(event_plane, dict):
        raise ValueError("event_plane must be an object")
    _require_exact_keys(event_plane, ("kind", "trading_mode"), "event_plane", ordered=True)

    outbox_payload = event["payload"]
    if not isinstance(outbox_payload, dict):
        raise ValueError("event payload must be an object")
    _require_exact_keys(outbox_payload, _OUTBOX_PAYLOAD_FIELDS, "event payload")
    payload = outbox_payload["policy"]
    if not isinstance(payload, dict):
        raise ValueError("event payload policy must be an object")
    _require_exact_keys(payload, _POLICY_FIELDS, "policy")
    previous = payload["previous"]
    if previous is not None:
        if not isinstance(previous, dict):
            raise ValueError("previous must be an object or null")
        _require_exact_keys(previous, _POLICY_REF_FIELDS, "previous", ordered=True)
    for field in ("policy_id", "runner_id"):
        _require_canonical_uuid(payload[field], f"policy.{field}")
    _require_timestamp(payload["effective_at"], "policy.effective_at")
    _require_timestamp(payload["expires_at"], "policy.expires_at")
    for field in ("max_order_notional", "max_total_notional"):
        _require_positive_canonical_decimal(payload[field], f"policy.{field}")

    body = {key: payload[key] for key in _POLICY_FIELDS[:-1]}
    actual_digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if payload["policy_digest"] != actual_digest:
        raise ValueError("policy digest differs from runner-safety policy canonical JSON")
    policy = RunnerAggregateCapPolicyV1.model_validate(payload)
    actor_assertion_jti = event["actor_assertion_jti"]
    if policy.status == "expired":
        if actor_assertion_jti is not None:
            raise ValueError("system-expired policy must not carry an actor assertion JTI")
    else:
        _require_canonical_uuid(actor_assertion_jti, "event.actor_assertion_jti")

    expected_subject = (
        f"{SUBJECT_PREFIX}.{policy.tenant_id}.{policy.runner_id}.{policy.trading_mode}"
    )
    expected_event_type = {
        "active": "RunnerSafetyPolicyPublished",
        "revoked": "RunnerSafetyPolicyRevoked",
        "expired": "RunnerSafetyPolicyExpired",
    }[policy.status]
    if subject != expected_subject:
        raise ValueError("subject differs from policy tenant and trading mode")
    if (
        outbox_payload["policy_id"] != str(policy.policy_id)
        or outbox_payload["revision"] != policy.revision
        or outbox_payload["policy_digest"] != policy.policy_digest
        or outbox_payload["exact_subject"] != expected_subject
        or event["tenant_id"] != policy.tenant_id
        or event_plane != {"kind": "mode", "trading_mode": policy.trading_mode}
        or event["bounded_context"] != "risk"
        or event["aggregate_type"] != "runner_safety_policy"
        or event["aggregate_id"] != str(policy.policy_id)
        or event["aggregate_version"] != policy.revision
        or event["event_type"] != expected_event_type
    ):
        raise ValueError("event target differs from policy scope or revision")
    return policy


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    if type(payload) is not bytes or not payload:
        raise TypeError(f"{label} bytes are required")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be an object")
    return parsed


def _require_exact_keys(
    value: dict[str, Any],
    expected: tuple[str, ...] | frozenset[str],
    label: str,
    *,
    ordered: bool = False,
) -> None:
    if set(value) != set(expected):
        raise ValueError(f"{label} fields differ from the closed runner-safety policy contract")
    if ordered and tuple(value) != tuple(expected):
        raise ValueError(f"{label} fields are not in Rust struct order")


def _require_canonical_uuid(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a canonical non-nil UUID")


def _require_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a date-time string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")


def _require_positive_canonical_decimal(value: object, label: str) -> None:
    if not isinstance(value, str) or not _POSITIVE_DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a positive canonical decimal string")
    try:
        if Decimal(value) <= 0:
            raise ValueError(f"{label} must be positive")
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a decimal") from exc


def _decode_base64url(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value or not _BASE64URL_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be unpadded base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} is invalid base64url") from exc


def _compact_json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _frame(context: bytes, subject: str, event_bytes: bytes) -> bytes:
    subject_bytes = subject.encode("utf-8")
    return (
        context
        + len(subject_bytes).to_bytes(4, "big")
        + subject_bytes
        + len(event_bytes).to_bytes(8, "big")
        + event_bytes
    )
