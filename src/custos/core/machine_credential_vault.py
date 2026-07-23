"""Age-encrypted runner machine identity and credential authority.

The enrollment credential and its Ed25519 private key are one security
principal.  They are therefore persisted in one sops+age document and are
never copied into runner.toml, logs, HTTP bodies, NATS payloads, or readiness
state.  Public metadata is kept separately so startup can fail closed on any
binding drift before opening a transport connection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_WORLD_GROUP_BITS = 0o077
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HTTP_TIMEOUT_SECS = 30
_MAX_RESPONSE_BYTES = 1_048_576
_ENROLLMENT_DOMAIN = "crucible.runner.enrollment.pop.v1"
_ROTATION_DOMAIN = "crucible.runner.credential.rotation.pop.v1"
_REVOCATION_DOMAIN = "crucible.runner.credential.revocation.pop.v1"
_REQUEST_DOMAIN = "crucible.runner.machine.request.v1"
_NATS_DOMAIN = "custos.runner.machine.nats.v1"


class MachineCredentialError(RuntimeError):
    """A machine credential is absent, invalid, expired, or untrusted."""


class MachineCredentialTransportError(MachineCredentialError):
    """The authority endpoint is temporarily unavailable."""


class MachineCredentialRejectedError(MachineCredentialError):
    """The authority explicitly rejected this machine principal."""


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _uuid(value: UUID | str, field: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise MachineCredentialError(f"{field} must be a UUID") from exc
    if parsed.int == 0:
        raise MachineCredentialError(f"{field} must not be nil")
    return parsed


def _timestamp(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MachineCredentialError(f"{field} must be RFC3339") from exc
    else:
        raise MachineCredentialError(f"{field} must be RFC3339")
    if parsed.tzinfo is None:
        raise MachineCredentialError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _key_id(public_key: bytes) -> str:
    return f"ed25519-{_sha256(public_key)[:32]}"


def generate_machine_identity() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, _key_id(public_key)


def canonical_enrollment_proof(
    *,
    enrollment_token: str,
    tenant_id: str,
    runner_id: UUID,
    challenge_nonce: UUID,
    machine_key_id: str,
    public_key: bytes,
) -> bytes:
    return "\n".join(
        (
            _ENROLLMENT_DOMAIN,
            f"tenant_id={tenant_id}",
            f"runner_id={runner_id}",
            f"challenge_nonce={challenge_nonce}",
            f"machine_key_id={machine_key_id}",
            f"public_key_sha256={_sha256(public_key)}",
            f"enrollment_token_sha256={_sha256(enrollment_token.encode('utf-8'))}",
        )
    ).encode("utf-8")


def canonical_rotation_proof(
    *,
    credential: MachineCredential,
    challenge_nonce: UUID,
    correlation_id: UUID,
    new_machine_key_id: str,
    new_public_key: bytes,
    reason: str,
) -> bytes:
    return "\n".join(
        (
            _ROTATION_DOMAIN,
            f"tenant_id={credential.tenant_id}",
            f"runner_id={credential.runner_id}",
            f"credential_id={credential.credential_id}",
            f"credential_version={credential.credential_version}",
            f"challenge_nonce={challenge_nonce}",
            f"correlation_id={correlation_id}",
            f"new_machine_key_id={new_machine_key_id}",
            f"new_public_key_sha256={_sha256(new_public_key)}",
            f"reason_sha256={_sha256(reason.encode('utf-8'))}",
        )
    ).encode("utf-8")


def canonical_revocation_proof(
    *,
    credential: MachineCredential,
    challenge_nonce: UUID,
    correlation_id: UUID,
    reason: str,
) -> bytes:
    return "\n".join(
        (
            _REVOCATION_DOMAIN,
            f"tenant_id={credential.tenant_id}",
            f"runner_id={credential.runner_id}",
            f"credential_id={credential.credential_id}",
            f"credential_version={credential.credential_version}",
            f"challenge_nonce={challenge_nonce}",
            f"correlation_id={correlation_id}",
            f"reason_sha256={_sha256(reason.encode('utf-8'))}",
        )
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MachineCredential:
    """Decrypted machine principal; repr deliberately omits all secrets."""

    tenant_id: str
    runner_id: UUID
    credential_id: UUID
    credential_version: int
    credential_valid_until: datetime
    machine_key_id: str
    machine_credential: str
    private_key_bytes: bytes
    source_path: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.tenant_id):
            raise MachineCredentialError("tenant_id is not a safe authority identifier")
        object.__setattr__(self, "runner_id", _uuid(self.runner_id, "runner_id"))
        object.__setattr__(self, "credential_id", _uuid(self.credential_id, "credential_id"))
        if type(self.credential_version) is not int or self.credential_version < 1:
            raise MachineCredentialError("credential_version must be positive")
        object.__setattr__(
            self,
            "credential_valid_until",
            _timestamp(self.credential_valid_until, "credential_valid_until"),
        )
        if not self.machine_credential.startswith("rkc1."):
            raise MachineCredentialError("machine credential is not an rkc1 credential")
        if len(self.private_key_bytes) != 32:
            raise MachineCredentialError("Ed25519 private key must contain 32 bytes")
        if self.machine_key_id != _key_id(self.public_key_bytes):
            raise MachineCredentialError("machine_key_id does not match the private key")

    def __repr__(self) -> str:
        return (
            "MachineCredential("
            f"tenant_id={self.tenant_id!r}, runner_id={self.runner_id!r}, "
            f"credential_id={self.credential_id!r}, "
            f"credential_version={self.credential_version!r}, "
            f"credential_valid_until={self.credential_valid_until!r}, "
            f"machine_key_id={self.machine_key_id!r})"
        )

    @property
    def private_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.private_key_bytes)

    @property
    def public_key_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self.private_key.sign(payload)).decode("ascii")

    def assert_active(self, *, now: datetime | None = None) -> None:
        if self.source_path is not None and not self.source_path.exists():
            raise MachineCredentialError("machine credential was invalidated locally")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if self.credential_valid_until <= current:
            raise MachineCredentialError("machine credential is expired")

    def assert_binding(self, metadata: Any) -> None:
        self.assert_active()
        expected = {
            "tenant_id": self.tenant_id,
            "runner_id": str(self.runner_id),
            "credential_id": str(self.credential_id),
            "credential_version": self.credential_version,
            "credential_valid_until": _timestamp_text(self.credential_valid_until),
            "machine_key_id": self.machine_key_id,
        }
        for binding_field, value in expected.items():
            if getattr(metadata, binding_field, None) != value:
                raise MachineCredentialError(
                    f"machine credential binding mismatch for {binding_field}"
                )

    def authenticated_headers(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any],
        correlation_id: UUID,
        issued_at: datetime | None = None,
        deployment_instance_id: str | None = None,
        deployment_spec_id: str | None = None,
        deployment_spec_digest: str | None = None,
    ) -> dict[str, str]:
        self.assert_active()
        body_digest = _sha256(_canonical_json_bytes(body))
        request_id = _uuid(correlation_id, "correlation_id")
        request_issued_at = (issued_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        issued_at_text = request_issued_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        proof = "\n".join(
            (
                _REQUEST_DOMAIN,
                f"method={method.upper()}",
                f"path={path}",
                f"tenant_id={self.tenant_id}",
                f"runner_id={self.runner_id}",
                f"credential_id={self.credential_id}",
                f"credential_version={self.credential_version}",
                f"machine_key_id={self.machine_key_id}",
                f"request_id={request_id}",
                f"issued_at={issued_at_text}",
                f"deployment_instance_id={deployment_instance_id or '-'}",
                f"deployment_spec_id={deployment_spec_id or '-'}",
                f"deployment_spec_digest={deployment_spec_digest or '-'}",
                f"body_sha256={body_digest}",
            )
        ).encode("utf-8")
        return {
            "X-Crucible-Credential": self.machine_credential,
            "X-Crucible-Tenant-Id": self.tenant_id,
            "X-Crucible-Runner-Id": str(self.runner_id),
            "X-Crucible-Credential-Id": str(self.credential_id),
            "X-Crucible-Credential-Version": str(self.credential_version),
            "X-Crucible-Machine-Key-Id": self.machine_key_id,
            "X-Crucible-Request-Id": str(request_id),
            "X-Crucible-Issued-At": issued_at_text,
            "X-Crucible-Deployment-Instance-Id": deployment_instance_id or "-",
            "X-Crucible-Deployment-Spec-Id": deployment_spec_id or "-",
            "X-Crucible-Deployment-Spec-Digest": deployment_spec_digest or "-",
            "X-Crucible-Body-Sha256": body_digest,
            "X-Crucible-Machine-Signature": self.sign(proof),
        }

    def nats_authority(
        self,
        *,
        kind: str,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.assert_active()
        deployment_instance_id = _optional_text(payload.get("deployment_instance_id"))
        deployment_spec_id = _optional_text(
            payload.get("deployment_spec_id") or payload.get("spec_id")
        )
        deployment_spec_digest = _optional_text(payload.get("deployment_spec_digest"))
        payload_digest = _sha256(_canonical_json_bytes(payload))
        correlation_id = _optional_text(payload.get("correlation_id")) or event_id
        proof = "\n".join(
            (
                _NATS_DOMAIN,
                f"kind={kind}",
                f"event_id={event_id}",
                f"tenant_id={self.tenant_id}",
                f"runner_id={self.runner_id}",
                f"credential_id={self.credential_id}",
                f"credential_version={self.credential_version}",
                f"machine_key_id={self.machine_key_id}",
                f"correlation_id={correlation_id}",
                f"deployment_instance_id={deployment_instance_id or '-'}",
                f"deployment_spec_id={deployment_spec_id or '-'}",
                f"deployment_spec_digest={deployment_spec_digest or '-'}",
                f"payload_sha256={payload_digest}",
            )
        ).encode("utf-8")
        return {
            "schema_version": 1,
            "tenant_id": self.tenant_id,
            "runner_id": str(self.runner_id),
            "credential_id": str(self.credential_id),
            "credential_version": self.credential_version,
            "machine_key_id": self.machine_key_id,
            "correlation_id": correlation_id,
            "deployment_instance_id": deployment_instance_id,
            "deployment_spec_id": deployment_spec_id,
            "deployment_spec_digest": deployment_spec_digest,
            "payload_sha256": payload_digest,
            "signature_algorithm": "ed25519",
            "signature_base64": self.sign(proof),
        }

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state": "active",
            "tenant_id": self.tenant_id,
            "runner_id": str(self.runner_id),
            "credential_id": str(self.credential_id),
            "credential_version": self.credential_version,
            "credential_valid_until": _timestamp_text(self.credential_valid_until),
            "machine_key_id": self.machine_key_id,
            "machine_credential": self.machine_credential,
            "private_key_base64": base64.b64encode(self.private_key_bytes).decode("ascii"),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> MachineCredential:
        expected = {
            "schema_version",
            "state",
            "tenant_id",
            "runner_id",
            "credential_id",
            "credential_version",
            "credential_valid_until",
            "machine_key_id",
            "machine_credential",
            "private_key_base64",
        }
        if set(document) != expected:
            raise MachineCredentialError("machine vault has an unexpected document shape")
        if document.get("schema_version") != 1 or document.get("state") != "active":
            raise MachineCredentialError("machine vault is not an active v1 credential")
        try:
            private_key_bytes = base64.b64decode(document["private_key_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise MachineCredentialError("machine vault private key is invalid") from exc
        return cls(
            tenant_id=str(document["tenant_id"]),
            runner_id=_uuid(document["runner_id"], "runner_id"),
            credential_id=_uuid(document["credential_id"], "credential_id"),
            credential_version=document["credential_version"],
            credential_valid_until=_timestamp(
                document["credential_valid_until"], "credential_valid_until"
            ),
            machine_key_id=str(document["machine_key_id"]),
            machine_credential=str(document["machine_credential"]),
            private_key_bytes=private_key_bytes,
        )


class MachineCredentialVault:
    """Single-principal sops+age vault with atomic replacement semantics."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def load(self) -> MachineCredential:
        if not self.path.exists():
            raise MachineCredentialError(
                f"machine credential vault {self.path} is missing; run `arx-runner enroll`"
            )
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & _WORLD_GROUP_BITS:
            raise MachineCredentialError(
                f"machine credential vault {self.path} must have mode 0600"
            )
        _require_age_key_file()
        command = (
            "sops",
            "--decrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            str(self.path),
        )
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
            )
        except OSError as exc:
            raise MachineCredentialError("cannot execute sops for machine vault") from exc
        if result.returncode != 0:
            raise MachineCredentialError("cannot decrypt machine credential vault")
        try:
            document = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MachineCredentialError("machine credential vault is not valid JSON") from exc
        if not isinstance(document, dict):
            raise MachineCredentialError("machine credential vault root must be an object")
        credential = replace(
            MachineCredential.from_document(document),
            source_path=self.path,
        )
        credential.assert_active()
        return credential

    def persist(self, credential: MachineCredential, *, age_recipient: str) -> None:
        recipient = age_recipient.strip()
        if not recipient.startswith("age1"):
            raise MachineCredentialError("age recipient must be an age1 public recipient")
        self.path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.path.parent, _DIR_MODE)
        plaintext = _canonical_json_bytes(credential.to_document()) + b"\n"
        command = (
            "sops",
            "--encrypt",
            "--age",
            recipient,
            "--input-type",
            "json",
            "--output-type",
            "json",
            "/dev/stdin",
        )
        try:
            result = subprocess.run(
                command,
                input=plaintext,
                check=False,
                capture_output=True,
            )
        except OSError as exc:
            raise MachineCredentialError("cannot execute sops for machine vault") from exc
        finally:
            plaintext = b""
        if result.returncode != 0 or not result.stdout:
            raise MachineCredentialError("cannot encrypt machine credential vault")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(descriptor, _FILE_MODE)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(result.stdout)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, _FILE_MODE)
            _fsync_directory(self.path.parent)
        finally:
            temp_path.unlink(missing_ok=True)

    def invalidate(self) -> None:
        self.path.unlink(missing_ok=True)
        if self.path.parent.exists():
            _fsync_directory(self.path.parent)


def _machine_authority_rejection_context(error: HTTPError) -> str:
    try:
        raw = error.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return ""
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(document, dict):
        return ""
    fields: list[str] = []
    code = document.get("code")
    if isinstance(code, str) and _SAFE_ID.fullmatch(code):
        fields.append(f"code={code}")
    correlation_id = document.get("correlation_id")
    try:
        fields.append(f"correlation_id={UUID(str(correlation_id))}")
    except (TypeError, ValueError, AttributeError):
        pass
    return f" ({', '.join(fields)})" if fields else ""


class MachineCredentialHttpClient:
    """Typed direct Crucible machine client; error text never includes secrets."""

    def __init__(self, backend_url: str, credential: MachineCredential) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.credential = credential
        _require_secure_backend(self.backend_url)

    def post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        canonical_path: str | None = None,
        correlation_id: UUID,
        deployment_instance_id: str | None = None,
        deployment_spec_id: str | None = None,
        deployment_spec_digest: str | None = None,
    ) -> dict[str, Any]:
        encoded = _canonical_json_bytes(body)
        headers = self.credential.authenticated_headers(
            method="POST",
            path=canonical_path or path,
            body=body,
            correlation_id=correlation_id,
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=deployment_spec_id,
            deployment_spec_digest=deployment_spec_digest,
        )
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.backend_url}{path}", data=encoded, headers=headers, method="POST"
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=_HTTP_TIMEOUT_SECS) as response:
                status = getattr(response, "status", HTTPStatus.OK)
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in {408, 425, 429}:
                raise MachineCredentialRejectedError(
                    f"machine authority rejected request with HTTP {exc.code}"
                    f"{_machine_authority_rejection_context(exc)}"
                ) from exc
            raise MachineCredentialTransportError(
                "machine authority returned a server error"
            ) from exc
        except URLError as exc:
            raise MachineCredentialTransportError("machine authority is unavailable") from exc
        if status >= 300 or len(raw) > _MAX_RESPONSE_BYTES:
            raise MachineCredentialTransportError("machine authority returned an invalid response")
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MachineCredentialTransportError(
                "machine authority returned invalid JSON"
            ) from exc
        if not isinstance(document, dict):
            raise MachineCredentialTransportError(
                "machine authority response root must be an object"
            )
        return document

    def verify_active(self) -> None:
        correlation_id = _new_uuid4()
        body = {
            "tenant_id": self.credential.tenant_id,
            "runner_id": str(self.credential.runner_id),
            "credential_id": str(self.credential.credential_id),
            "credential_version": self.credential.credential_version,
            "correlation_id": str(correlation_id),
        }
        response = self.post(
            "/api/v1/runner-credentials/verify",
            body,
            correlation_id=correlation_id,
        )
        expected = {
            "tenant_id": self.credential.tenant_id,
            "runner_id": str(self.credential.runner_id),
            "credential_id": str(self.credential.credential_id),
            "credential_version": self.credential.credential_version,
            "machine_key_id": self.credential.machine_key_id,
            "state": "active",
        }
        if any(response.get(field) != value for field, value in expected.items()):
            raise MachineCredentialError("machine authority binding is not active")
        valid_until = _timestamp(response.get("credential_valid_until"), "credential_valid_until")
        if valid_until != self.credential.credential_valid_until:
            raise MachineCredentialError("machine authority expiry binding mismatch")
        self.credential.assert_active()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _new_uuid4() -> UUID:
    from uuid import uuid4

    return uuid4()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_secure_backend(backend_url: str) -> None:
    parsed = urllib.parse.urlsplit(backend_url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise MachineCredentialError(
        "machine credentials require HTTPS (HTTP is allowed only for loopback)"
    )


def _require_age_key_file() -> Path:
    configured = os.environ.get("SOPS_AGE_KEY_FILE", "").strip()
    if not configured:
        raise MachineCredentialError("SOPS_AGE_KEY_FILE is required")
    path = Path(configured).expanduser().resolve()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise MachineCredentialError("SOPS_AGE_KEY_FILE is not readable") from exc
    if mode & _WORLD_GROUP_BITS:
        raise MachineCredentialError("SOPS_AGE_KEY_FILE must not be accessible by group or others")
    return path


def resolve_age_recipient(value: str | None) -> str:
    recipient = (value or os.environ.get("SOPS_AGE_RECIPIENT", "")).strip()
    if not recipient:
        raise MachineCredentialError(
            "an age recipient is required via --age-recipient or SOPS_AGE_RECIPIENT"
        )
    if not recipient.startswith("age1"):
        raise MachineCredentialError("age recipient must be an age1 public recipient")
    return recipient


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
