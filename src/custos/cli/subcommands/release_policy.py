"""Issue the runner-local StrategyRelease trust policy owned by Custos."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from custos.artifacts.policy import (
    ArchiveLimitsV1,
    ReleaseTrustPolicyV1,
    SigstoreIdentityV1,
    canonical_policy_bytes,
    sign_release_policy,
)
from custos_toolkit.contracts.strategy_execution import canonical_json_bytes

_PRIVATE_MODE = 0o600
_PUBLIC_MODE = 0o644
_DIRECTORY_MODE = 0o700


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "release-policy",
        help="Manage Custos-owned runner-local StrategyRelease trust policy artifacts.",
    )
    actions = parser.add_subparsers(dest="release_policy_action", required=True)

    development = actions.add_parser(
        "generate-development-authority",
        help="Generate a local-only Ed25519 policy authority; never use it as production evidence.",
    )
    development.add_argument("--private-key-output", required=True, type=Path)
    development.add_argument("--public-key-output", required=True, type=Path)
    development.add_argument("--receipt-output", required=True, type=Path)
    development.set_defaults(handler=_generate_development_authority)

    issue = actions.add_parser(
        "issue",
        help="Sign one exact ReleaseTrustPolicyV1 with an explicit policy authority.",
    )
    issue.add_argument("--authority-private-key", required=True, type=Path)
    issue.add_argument("--authority-public-key", required=True, type=Path)
    issue.add_argument("--sigstore-trusted-root", required=True, type=Path)
    issue.add_argument("--policy-id", required=True)
    issue.add_argument("--version", required=True, type=int)
    issue.add_argument("--not-before", required=True, type=_aware_datetime)
    issue.add_argument("--expires-at", required=True, type=_aware_datetime)
    issue.add_argument("--issuer", required=True)
    issue.add_argument("--workflow-identity", required=True)
    issue.add_argument("--source-repository", required=True)
    issue.add_argument("--envelope-output", required=True, type=Path)
    issue.add_argument("--receipt-output", required=True, type=Path)
    issue.add_argument("--environment-output", required=True, type=Path)
    issue.set_defaults(handler=_issue)


def _generate_development_authority(args: argparse.Namespace) -> int:
    outputs = (
        _output_path(args.private_key_output),
        _output_path(args.public_key_output),
        _output_path(args.receipt_output),
    )
    try:
        _require_new_outputs(outputs)
        private_key = Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        key_id = _key_id(public_bytes)
        receipt = {
            "schema_version": 1,
            "receipt_id": "CUSTOS-DEVELOPMENT-RELEASE-POLICY-AUTHORITY-V1",
            "status": "DEVELOPMENT_ONLY_NOT_PRODUCTION_APPROVED",
            "key_id": key_id,
            "public_key_sha256": hashlib.sha256(public_bytes).hexdigest(),
            "private_key_storage": "canonical_base64_raw_ed25519_0600",
            "production_approved": False,
        }
        _write_group(
            (
                (outputs[0], _canonical_base64(private_bytes) + b"\n", _PRIVATE_MODE),
                (outputs[1], _canonical_base64(public_bytes) + b"\n", _PUBLIC_MODE),
                (outputs[2], canonical_json_bytes(receipt) + b"\n", _PRIVATE_MODE),
            )
        )
    except (OSError, ValueError) as error:
        print(f"release-policy development authority generation failed: {error}", file=sys.stderr)
        return 1
    print(f"development release-policy authority generated: key_id={key_id}")
    return 0


def _issue(args: argparse.Namespace) -> int:
    outputs = (
        _output_path(args.envelope_output),
        _output_path(args.receipt_output),
        _output_path(args.environment_output),
    )
    try:
        _require_new_outputs(outputs)
        private_key = _load_private_key(args.authority_private_key)
        public_key_path = args.authority_public_key.expanduser().resolve(strict=True)
        public_bytes = _load_public_key_bytes(public_key_path)
        derived_public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if derived_public_bytes != public_bytes:
            raise ValueError("authority private and public keys do not match")
        trusted_root_path = args.sigstore_trusted_root.expanduser().resolve(strict=True)
        trusted_root_bytes = trusted_root_path.read_bytes()
        key_id = _key_id(public_bytes)
        policy = ReleaseTrustPolicyV1(
            policy_id=args.policy_id,
            version=args.version,
            not_before=args.not_before,
            expires_at=args.expires_at,
            sigstore_trusted_root_sha256=hashlib.sha256(trusted_root_bytes).hexdigest(),
            accepted_identities=(
                SigstoreIdentityV1(
                    issuer=args.issuer,
                    workflow_identity=args.workflow_identity,
                    source_repository=args.source_repository,
                ),
            ),
            require_transparency_log=True,
            archive_limits=ArchiveLimitsV1(),
        )
        policy_bytes = canonical_policy_bytes(policy)
        envelope_bytes = sign_release_policy(
            policy,
            authority_key_id=key_id,
            authority_private_key=private_key,
        )
        receipt = {
            "schema_version": 1,
            "receipt_id": "CUSTOS-RELEASE-TRUST-POLICY-ISSUANCE-V1",
            "status": "SIGNED_RELEASE_TRUST_POLICY_ISSUED",
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
            "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
            "envelope_sha256": hashlib.sha256(envelope_bytes).hexdigest(),
            "authority_key_id": key_id,
            "authority_public_key_sha256": hashlib.sha256(public_bytes).hexdigest(),
            "sigstore_trusted_root_sha256": policy.sigstore_trusted_root_sha256,
            "not_before": _timestamp(policy.not_before),
            "expires_at": _timestamp(policy.expires_at),
            "private_key_persisted_by_issue_command": False,
        }
        environment = _environment_bytes(
            envelope_path=outputs[0],
            key_id=key_id,
            public_key_path=public_key_path,
            trusted_root_path=trusted_root_path,
        )
        _write_group(
            (
                (outputs[0], envelope_bytes, _PUBLIC_MODE),
                (outputs[1], canonical_json_bytes(receipt) + b"\n", _PUBLIC_MODE),
                (outputs[2], environment, _PRIVATE_MODE),
            )
        )
    except (OSError, ValueError, ValidationError) as error:
        print(f"release-policy issuance failed: {error}", file=sys.stderr)
        return 1
    print(
        "release trust policy issued: "
        f"policy_id={policy.policy_id} version={policy.version} key_id={key_id}"
    )
    return 0


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include an offset")
    return parsed.astimezone(UTC)


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    raw = _load_canonical_base64(path, label="authority private key")
    if len(raw) != 32:
        raise ValueError("authority private key must encode exactly 32 Ed25519 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _load_public_key_bytes(path: Path) -> bytes:
    raw = _load_canonical_base64(path, label="authority public key")
    if len(raw) != 32:
        raise ValueError("authority public key must encode exactly 32 Ed25519 bytes")
    return raw


def _load_canonical_base64(path: Path, *, label: str) -> bytes:
    encoded = path.expanduser().resolve(strict=True).read_text(encoding="ascii").strip()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError(f"{label} is not canonical base64") from error
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError(f"{label} is not canonical base64")
    return raw


def _key_id(public_bytes: bytes) -> str:
    return f"ed25519-{hashlib.sha256(public_bytes).hexdigest()[:32]}"


def _canonical_base64(value: bytes) -> bytes:
    return base64.b64encode(value)


def _output_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _require_new_outputs(paths: tuple[Path, ...]) -> None:
    duplicates = sorted({str(path) for path in paths if paths.count(path) > 1})
    if duplicates:
        raise ValueError("output paths must be distinct: " + ", ".join(duplicates))
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise ValueError("refusing to overwrite existing output: " + ", ".join(existing))


def _write_group(entries: tuple[tuple[Path, bytes, int], ...]) -> None:
    created: list[Path] = []
    try:
        for path, payload, mode in entries:
            path.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, mode)
            created.append(path)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def _environment_bytes(
    *,
    envelope_path: Path,
    key_id: str,
    public_key_path: Path,
    trusted_root_path: Path,
) -> bytes:
    values: dict[str, Any] = {
        "CUSTOS_ARTIFACT_RELEASE_POLICY_ENVELOPE": envelope_path,
        "CUSTOS_ARTIFACT_RELEASE_POLICY_KEY_ID": key_id,
        "CUSTOS_ARTIFACT_RELEASE_POLICY_PUBLIC_KEY": public_key_path,
        "CUSTOS_ARTIFACT_SIGSTORE_TRUSTED_ROOT": trusted_root_path,
    }
    lines = []
    for name, value in values.items():
        text = str(value)
        if "\n" in text or "\r" in text:
            raise ValueError(f"{name} contains a newline")
        lines.append(f"{name}={text}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
