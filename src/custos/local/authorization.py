"""Offline Ed25519 issuance and verification against kernel-provisioned public roots."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Self
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from custos_toolkit.contracts.strategy_execution import Sha256Hex
from pydantic import AfterValidator, StrictStr, field_validator, model_validator

from custos.core.runtime_account_guard import PreparedRuntimeClaim
from custos.core.runtime_authority import RuntimeAccountIdentity
from custos.local.manifest import (
    LocalRunManifestV1,
    LocalVersionedDocument,
    Revision,
    Uuid4,
    parse_canonical_document,
    parse_local_manifest,
    uuid4_identity,
)

LOCAL_PERMIT_SIGNING_DOMAIN = b"alephain.custos.local-run-permit.v1\x00"
_NATIVE_MAX_SECONDS = (2**64 - 1) // 1_000_000_000
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _operator_id(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", value):
        raise ValueError("operator ID must be a bounded identifier")
    return value


def _utc_instant(value: str) -> datetime:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value):
        raise ValueError("permit time must be canonical UTC with microseconds")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _utc_text(value: str) -> str:
    _utc_instant(value)
    return value


def _native_seconds(value: str) -> int:
    seconds = (_utc_instant(value) - _UNIX_EPOCH) // timedelta(seconds=1)
    if not 0 < seconds <= _NATIVE_MAX_SECONDS:
        raise ValueError("permit deadline is outside native execution time range")
    return seconds


def local_operator_key_id(key: Ed25519PublicKey) -> str:
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("operator authority requires an Ed25519 public key")
    return "ed25519:" + hashlib.sha256(key.public_bytes_raw()).hexdigest()


class LocalRunPermitV1(LocalVersionedDocument):
    permit_id: Uuid4
    run_id: Uuid4
    manifest_digest: Sha256Hex
    account_scope: Sha256Hex
    control_registry_id: Uuid4
    authorization_epoch: Revision
    operator_id: Annotated[StrictStr, AfterValidator(_operator_id)]
    key_id: StrictStr
    issued_at_utc: Annotated[StrictStr, AfterValidator(_utc_text)]
    not_before_utc: Annotated[StrictStr, AfterValidator(_utc_text)]
    expires_at_utc: Annotated[StrictStr, AfterValidator(_utc_text)]
    exit_until_utc: Annotated[StrictStr, AfterValidator(_utc_text)]

    @field_validator("key_id")
    @classmethod
    def check_key_id(cls, value: str) -> str:
        if not re.fullmatch(r"ed25519:[0-9a-f]{64}", value):
            raise ValueError("permit requires a canonical public-key fingerprint")
        return value

    @model_validator(mode="after")
    def check_validity_interval(self) -> Self:
        if not (
            _utc_instant(self.issued_at_utc)
            <= _utc_instant(self.not_before_utc)
            < _utc_instant(self.expires_at_utc)
            <= _utc_instant(self.exit_until_utc)
        ):
            raise ValueError("permit validity interval is invalid")
        _native_seconds(self.expires_at_utc)
        _native_seconds(self.exit_until_utc)
        return self


class SignedLocalRunPermitV1(LocalVersionedDocument):
    permit: LocalRunPermitV1
    signature_base64: StrictStr

    @field_validator("signature_base64")
    @classmethod
    def canonical_signature(cls, value: str) -> str:
        try:
            signature = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("permit signature is not base64") from None
        if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != value:
            raise ValueError("permit signature must encode exactly 64 bytes")
        return value


def local_permit_digest(raw: bytes) -> str:
    """Identify a canonical full envelope without trusting its signature or contents."""
    parse_canonical_document(raw, SignedLocalRunPermitV1)
    return hashlib.sha256(raw).hexdigest()


def sign_local_permit(permit: LocalRunPermitV1, key: Ed25519PrivateKey) -> bytes:
    """Sign with an explicitly supplied operator key; never load keys or authorize a daemon."""
    if not isinstance(permit, LocalRunPermitV1) or not isinstance(key, Ed25519PrivateKey):
        raise ValueError("local permit signing requires explicit typed material")
    permit = LocalRunPermitV1.model_validate(permit.model_dump(mode="json"))
    if local_operator_key_id(key.public_key()) != permit.key_id:
        raise ValueError("management signing key differs from permit authority")
    signature = key.sign(LOCAL_PERMIT_SIGNING_DOMAIN + permit.canonical_bytes())
    return SignedLocalRunPermitV1(
        schema_version=1,
        permit=permit,
        signature_base64=base64.b64encode(signature).decode("ascii"),
    ).canonical_bytes()


@dataclass(frozen=True)
class LocalOperatorAuthority:
    operator_id: str
    public_key: Ed25519PublicKey

    def __post_init__(self) -> None:
        _operator_id(self.operator_id)
        local_operator_key_id(self.public_key)


@dataclass(frozen=True)
class LocalExecutionWindow:
    risk_increase_until: int
    exit_until: int


@dataclass(frozen=True)
class VerifiedLocalRunAuthorization:
    manifest: LocalRunManifestV1
    permit: LocalRunPermitV1
    permit_digest: str
    checkpoint: PreparedRuntimeClaim

    @property
    def execution_window(self) -> LocalExecutionWindow:
        return LocalExecutionWindow(
            risk_increase_until=_native_seconds(self.permit.expires_at_utc),
            exit_until=_native_seconds(self.permit.exit_until_utc),
        )


@dataclass(frozen=True)
class LocalPermitAuthenticator:
    signature_keys: Mapping[str, LocalOperatorAuthority]

    def __post_init__(self) -> None:
        keys = dict(self.signature_keys)
        if not keys or any(
            not isinstance(authority, LocalOperatorAuthority)
            or key_id != local_operator_key_id(authority.public_key)
            for key_id, authority in keys.items()
        ):
            raise ValueError("trusted local operator keys are invalid")
        object.__setattr__(self, "signature_keys", MappingProxyType(keys))

    def verify(
        self,
        *,
        manifest_bytes: bytes,
        signed_permit_bytes: bytes,
        account: RuntimeAccountIdentity,
        control_registry_id: UUID,
        checkpoint: PreparedRuntimeClaim,
        now: datetime,
    ) -> VerifiedLocalRunAuthorization:
        envelope = parse_canonical_document(signed_permit_bytes, SignedLocalRunPermitV1)
        permit = envelope.permit
        authority = self.signature_keys.get(permit.key_id)
        if authority is None:
            raise ValueError("local permit signing key is not trusted")
        try:
            authority.public_key.verify(
                base64.b64decode(envelope.signature_base64, validate=True),
                LOCAL_PERMIT_SIGNING_DOMAIN + permit.canonical_bytes(),
            )
        except InvalidSignature:
            raise ValueError("local permit signature is invalid") from None
        manifest = parse_local_manifest(manifest_bytes)
        digest = local_permit_digest(signed_permit_bytes)
        if (
            not isinstance(account, RuntimeAccountIdentity)
            or not isinstance(checkpoint, PreparedRuntimeClaim)
            or not isinstance(control_registry_id, UUID)
        ):
            raise ValueError("local authorization requires kernel account and checkpoint")
        uuid4_identity(control_registry_id)
        if (
            authority.operator_id != permit.operator_id
            or permit.manifest_digest != manifest.digest
            or checkpoint.run_id != manifest.run_id
            or permit.run_id != manifest.run_id
            or checkpoint.control_source != manifest.control_source
            or checkpoint.control_source != "local"
            or checkpoint.permit_digest != digest
            or type(checkpoint.authorization_epoch) is not int
            or checkpoint.authorization_epoch != permit.authorization_epoch
            or checkpoint.account_scope != account.scope_digest
            or permit.account_scope != account.scope_digest
            or manifest.account_identity != account
            or checkpoint.control_registry_id != control_registry_id
            or permit.control_registry_id != control_registry_id
        ):
            raise ValueError("local permit differs from prepared runtime authority")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("local authorization clock must be an aware UTC instant")
        if not (
            _utc_instant(permit.issued_at_utc)
            <= _utc_instant(permit.not_before_utc)
            <= now
            < _utc_instant(permit.expires_at_utc)
        ):
            raise ValueError("local permit is not currently valid")
        return VerifiedLocalRunAuthorization(manifest, permit, digest, checkpoint)
