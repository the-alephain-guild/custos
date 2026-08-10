"""Nonce-bound enrollment of an age-encrypted runner machine principal."""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization

from custos.cli.validators import validate_backend_url, validate_id
from custos.core.machine_credential_vault import (
    MachineCredential,
    MachineCredentialError,
    MachineCredentialVault,
    canonical_enrollment_proof,
    generate_machine_identity,
    resolve_age_recipient,
)
from custos.core.runner_toml import RunnerToml

_ENROLLMENT_PATH = "/api/v1/runner-enrollments"
_HTTP_TIMEOUT_SECS = 30
_MAX_RESPONSE_BYTES = 1_048_576


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "enroll",
        help="Enroll a runner with nonce-bound Ed25519 proof of possession.",
    )
    parser.add_argument(
        "--token-file",
        required=True,
        type=Path,
        help=(
            "Path to the one-shot enrollment token. The file must be regular, "
            "must not be a symlink, and must not grant group or other permissions."
        ),
    )
    parser.add_argument("--backend", required=True, type=validate_backend_url)
    parser.add_argument(
        "--tenant-id", required=True, type=lambda value: validate_id("tenant_id", value)
    )
    parser.add_argument("--runner-id", required=True, type=_non_nil_uuid)
    parser.add_argument("--agent-version", default="")
    parser.add_argument(
        "--runner-toml",
        type=Path,
        default=Path.home() / ".arx" / "runner.toml",
    )
    parser.add_argument(
        "--machine-vault",
        type=Path,
        default=Path.home() / ".arx" / "vault" / "runner-machine.enc",
    )
    parser.add_argument(
        "--age-recipient",
        default=None,
        help="age public recipient; defaults to SOPS_AGE_RECIPIENT.",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    vault = MachineCredentialVault(args.machine_vault)
    if vault.path.exists() or args.runner_toml.expanduser().exists():
        print(
            "Runner enrollment state already exists; rotate or revoke the existing credential",
            file=sys.stderr,
        )
        return 1
    try:
        enrollment_token = _read_enrollment_token(args.token_file)
        age_recipient = resolve_age_recipient(args.age_recipient)
        _require_secure_backend(args.backend)
        private_key, machine_key_id = generate_machine_identity()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        challenge_nonce = uuid4()
        proof = canonical_enrollment_proof(
            enrollment_token=enrollment_token,
            tenant_id=args.tenant_id,
            runner_id=args.runner_id,
            challenge_nonce=challenge_nonce,
            machine_key_id=machine_key_id,
            public_key=public_key,
        )
        body = {
            "enrollment_token": enrollment_token,
            "tenant_id": args.tenant_id,
            "runner_id": str(args.runner_id),
            "agent_version": args.agent_version,
            "challenge_nonce": str(challenge_nonce),
            "machine_key_id": machine_key_id,
            "public_key_base64": base64.b64encode(public_key).decode("ascii"),
            "proof_signature_base64": base64.b64encode(private_key.sign(proof)).decode("ascii"),
        }
        response = _post_enrollment(args.backend, body)
        credential = _credential_from_response(
            response,
            tenant_id=args.tenant_id,
            runner_id=args.runner_id,
            machine_key_id=machine_key_id,
            private_key=private_key,
        )
        vault.persist(credential, age_recipient=age_recipient)
        enrolled_at = _required_timestamp(response, "enrolled_at")
        metadata = RunnerToml(
            tenant_id=credential.tenant_id,
            runner_id=str(credential.runner_id),
            backend_url=args.backend.rstrip("/"),
            credential_id=str(credential.credential_id),
            credential_version=credential.credential_version,
            credential_valid_until=_timestamp_text(credential.credential_valid_until),
            machine_key_id=credential.machine_key_id,
            machine_vault_path=str(vault.path),
            enrolled_at=_timestamp_text(enrolled_at),
        )
        RunnerToml.write(args.runner_toml, metadata)
    except (MachineCredentialError, OSError, ValueError) as exc:
        vault.invalidate()
        print(f"Runner enrollment failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Runner enrolled: "
        f"tenant_id={args.tenant_id} runner_id={args.runner_id} "
        f"credential_id={credential.credential_id} "
        f"credential_version={credential.credential_version} "
        f"machine_key_id={credential.machine_key_id}"
    )
    return 0


def _read_enrollment_token(path: Path) -> str:
    source = path.expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise MachineCredentialError("enrollment token file is not securely readable") from exc
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise MachineCredentialError("enrollment token file must be a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise MachineCredentialError(
                "enrollment token file must not grant group or other permissions"
            )
        payload = handle.read(4098)
    if len(payload) > 4097:
        raise MachineCredentialError("enrollment token file is too large")
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if b"\n" in payload or b"\r" in payload:
        raise MachineCredentialError("enrollment token file must contain exactly one token")
    try:
        token = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MachineCredentialError("enrollment token file must be UTF-8") from exc
    try:
        return _non_empty_secret(token)
    except argparse.ArgumentTypeError as exc:
        raise MachineCredentialError("enrollment token file contains an invalid token") from exc


def _post_enrollment(backend: str, body: dict[str, object]) -> dict[str, object]:
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str):
        raise MachineCredentialError("enrollment tenant_id is invalid")
    request = urllib.request.Request(
        f"{backend.rstrip('/')}{_ENROLLMENT_PATH}",
        data=json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Crucible-Tenant-Id": tenant_id,
            "X-Crucible-Request-Id": str(uuid4()),
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=_HTTP_TIMEOUT_SECS) as response:
            status = getattr(response, "status", HTTPStatus.OK)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise MachineCredentialError(
            f"enrollment authority rejected request with HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise MachineCredentialError("enrollment authority is unavailable") from exc
    if status >= 300 or len(raw) > _MAX_RESPONSE_BYTES:
        raise MachineCredentialError("enrollment authority returned an invalid response")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MachineCredentialError("enrollment authority returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MachineCredentialError("enrollment response root must be an object")
    return payload


def _credential_from_response(
    response: dict[str, object],
    *,
    tenant_id: str,
    runner_id: UUID,
    machine_key_id: str,
    private_key: object,
) -> MachineCredential:
    expected = {
        "tenant_id": tenant_id,
        "runner_id": str(runner_id),
        "machine_key_id": machine_key_id,
        "credential_version": 1,
    }
    if any(response.get(field) != value for field, value in expected.items()):
        raise MachineCredentialError("enrollment response does not match signed authority")
    raw_private_key = private_key.private_bytes(  # type: ignore[attr-defined]
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        return MachineCredential(
            tenant_id=tenant_id,
            runner_id=runner_id,
            credential_id=UUID(str(response["credential_id"])),
            credential_version=response["credential_version"],  # type: ignore[arg-type]
            credential_valid_until=_required_timestamp(response, "credential_valid_until"),
            machine_key_id=machine_key_id,
            machine_credential=str(response["long_term_credential"]),
            private_key_bytes=raw_private_key,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MachineCredentialError("enrollment response is incomplete") from exc


def _required_timestamp(document: dict[str, object], field: str) -> datetime:
    value = document.get(field)
    if not isinstance(value, str):
        raise MachineCredentialError(f"enrollment response is missing {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MachineCredentialError(f"enrollment response {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise MachineCredentialError(f"enrollment response {field} has no timezone")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_secure_backend(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise MachineCredentialError("enrollment requires HTTPS outside loopback")


def _non_nil_uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--runner-id must be a UUID") from exc
    if parsed.int == 0:
        raise argparse.ArgumentTypeError("--runner-id must not be nil")
    return parsed


def _non_empty_secret(value: str) -> str:
    if not value or any(ord(character) < 32 for character in value):
        raise argparse.ArgumentTypeError("token must be non-empty and contain no controls")
    return value
