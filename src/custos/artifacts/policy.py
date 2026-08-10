from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from custos_toolkit.contracts.strategy_execution import canonical_json_bytes
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from custos.artifacts.errors import ArtifactVerificationCode, ArtifactVerificationError

RELEASE_POLICY_SIGNATURE_CONTEXT = b"CUSTOS-RELEASE-TRUST-POLICY-V1\0"
RELEASE_POLICY_SIGNATURE_PROFILE = "custos-release-trust-policy-ed25519-v1-exact-bytes"
RELEASE_POLICY_PAYLOAD_ENCODING = "application/json;base64url"

NonEmptyString = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Base64Url = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$")]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArchiveLimitsV1(_StrictFrozenModel):
    max_members: StrictInt = Field(default=4096, ge=1, le=100_000)
    max_member_bytes: StrictInt = Field(default=128 * 1024 * 1024, ge=1)
    max_total_uncompressed_bytes: StrictInt = Field(default=512 * 1024 * 1024, ge=1)
    max_compression_ratio: StrictInt = Field(default=200, ge=1, le=10_000)
    max_path_bytes: StrictInt = Field(default=512, ge=32, le=4096)


class SigstoreIdentityV1(_StrictFrozenModel):
    issuer: NonEmptyString
    workflow_identity: NonEmptyString
    source_repository: NonEmptyString


class ReleaseTrustPolicyV1(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    policy_id: NonEmptyString
    version: StrictInt = Field(ge=1)
    not_before: AwareDatetime
    expires_at: AwareDatetime
    sigstore_trusted_root_sha256: Sha256Hex
    accepted_identities: tuple[SigstoreIdentityV1, ...] = Field(min_length=1)
    require_transparency_log: Literal[True]
    archive_limits: ArchiveLimitsV1

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.expires_at <= self.not_before:
            raise ValueError("policy expires_at must be after not_before")
        identities = {
            (identity.issuer, identity.workflow_identity, identity.source_repository)
            for identity in self.accepted_identities
        }
        if len(identities) != len(self.accepted_identities):
            raise ValueError("accepted Sigstore identities must be unique")
        return self


class SignedReleaseTrustPolicyEnvelopeV1(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    signature_profile: Literal["custos-release-trust-policy-ed25519-v1-exact-bytes"] = (
        "custos-release-trust-policy-ed25519-v1-exact-bytes"
    )
    payload_encoding: Literal["application/json;base64url"] = "application/json;base64url"
    policy_bytes: Base64Url
    signature_key_id: NonEmptyString
    signature: Base64Url


@dataclass(frozen=True, slots=True)
class VerifiedReleaseTrustPolicy:
    policy: ReleaseTrustPolicyV1
    policy_bytes: bytes
    policy_digest: str
    authority_key_id: str


def canonical_policy_bytes(policy: ReleaseTrustPolicyV1) -> bytes:
    return canonical_json_bytes(policy.model_dump(mode="json"))


def release_policy_signature_message(policy_bytes: bytes) -> bytes:
    return RELEASE_POLICY_SIGNATURE_CONTEXT + len(policy_bytes).to_bytes(8, "big") + policy_bytes


def sign_release_policy(
    policy: ReleaseTrustPolicyV1,
    *,
    authority_key_id: str,
    authority_private_key: Ed25519PrivateKey,
) -> bytes:
    """Issue the sole V1 envelope over exact canonical policy bytes."""

    policy_bytes = canonical_policy_bytes(policy)
    envelope = SignedReleaseTrustPolicyEnvelopeV1(
        policy_bytes=base64.urlsafe_b64encode(policy_bytes).rstrip(b"=").decode("ascii"),
        signature_key_id=authority_key_id,
        signature=base64.urlsafe_b64encode(
            authority_private_key.sign(release_policy_signature_message(policy_bytes))
        )
        .rstrip(b"=")
        .decode("ascii"),
    )
    return canonical_json_bytes(envelope.model_dump(mode="json"))


def _decode_base64url(value: str, label: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_ENVELOPE_INVALID,
            f"{label} is not canonical unpadded base64url",
        )
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_ENVELOPE_INVALID,
            f"{label} cannot be decoded",
        ) from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode()
    if canonical != value:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_ENVELOPE_INVALID,
            f"{label} is not canonical unpadded base64url",
        )
    return decoded


def verify_signed_release_policy(
    envelope_bytes: bytes,
    *,
    authority_key_id: str,
    authority_public_key: Ed25519PublicKey,
    sigstore_trusted_root_bytes: bytes,
    now: datetime,
) -> VerifiedReleaseTrustPolicy:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_INVALID,
            "verification time must be timezone-aware",
        )
    try:
        envelope = SignedReleaseTrustPolicyEnvelopeV1.model_validate_json(envelope_bytes)
    except (ValidationError, ValueError) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_ENVELOPE_INVALID,
            "signed release policy envelope is invalid",
        ) from error
    if envelope.signature_key_id != authority_key_id:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_AUTHORITY_MISMATCH,
            "release policy signing key id does not match bootstrap authority",
        )

    policy_bytes = _decode_base64url(envelope.policy_bytes, "policy_bytes")
    signature = _decode_base64url(envelope.signature, "signature")
    if len(signature) != 64:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_SIGNATURE_INVALID,
            "release policy signature must be 64 Ed25519 bytes",
        )
    try:
        authority_public_key.verify(signature, release_policy_signature_message(policy_bytes))
    except InvalidSignature as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_SIGNATURE_INVALID,
            "release policy signature verification failed",
        ) from error

    try:
        policy = ReleaseTrustPolicyV1.model_validate_json(policy_bytes)
    except (ValidationError, ValueError) as error:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_INVALID,
            "signed release policy payload is invalid",
        ) from error

    trusted_root_digest = hashlib.sha256(sigstore_trusted_root_bytes).hexdigest()
    if trusted_root_digest != policy.sigstore_trusted_root_sha256:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.TRUST_ROOT_DIGEST_MISMATCH,
            "local Sigstore trusted-root bytes do not match signed policy",
        )
    if now < policy.not_before:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_NOT_YET_VALID,
            "release trust policy is not yet valid",
        )
    if now >= policy.expires_at:
        raise ArtifactVerificationError(
            ArtifactVerificationCode.POLICY_EXPIRED,
            "release trust policy has expired",
        )
    return VerifiedReleaseTrustPolicy(
        policy=policy,
        policy_bytes=policy_bytes,
        policy_digest=hashlib.sha256(policy_bytes).hexdigest(),
        authority_key_id=authority_key_id,
    )
