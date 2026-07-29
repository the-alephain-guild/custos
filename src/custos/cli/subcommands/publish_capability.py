"""Publish a RunnerFact capability revision using the enrolled machine identity."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from custos.core.machine_credential_vault import (
    MachineCredentialError,
    MachineCredentialHttpClient,
    MachineCredentialVault,
)
from custos.core.runner_fact import (
    RunnerFactError,
    RunnerFactIdentity,
    capability_binding_evidence_digest,
    capability_scope_binding_values,
    normalize_capability_scope_bindings,
)
from custos.core.runner_toml import RunnerToml, require_attested

_PUBLICATION_PATH = "/api/v1/runner/capability-publications"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "publish-capability",
        help="Publish the next Runner capability revision with the enrolled machine key.",
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--runner-toml", type=Path, default=Path.home() / ".arx" / "runner.toml")
    parser.add_argument(
        "--authority-path",
        type=Path,
        default=Path.home() / ".arx" / "runner-capability.json",
    )
    parser.add_argument("--idempotency-key", type=_non_nil_uuid, default=None)
    parser.add_argument("--capability-version-id", type=_non_nil_uuid, default=None)
    parser.add_argument("--capability-version", type=_positive_int, default=None)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    try:
        runner = require_attested(
            RunnerToml.read(args.runner_toml), action="publish a capability receipt"
        )
        credential = MachineCredentialVault(Path(runner.machine_vault_path)).load()
        credential.assert_binding(runner)
        MachineCredentialHttpClient(runner.backend_url, credential).verify_active()
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("manifest root must be an object")
        scope_bindings = normalize_capability_scope_bindings(document)
        identity = RunnerFactIdentity.from_private_bytes(
            credential.private_key_bytes, credential.machine_key_id
        )
        capability_version = _next_capability_version(args.authority_path, args.capability_version)
        request_body = identity.publication_request(
            tenant_id=runner.tenant_id,
            runner_id=UUID(runner.runner_id),
            capability_manifest=document,
            capability_version=capability_version,
            capability_version_id=args.capability_version_id,
            idempotency_key=args.idempotency_key,
        )
        response = MachineCredentialHttpClient(runner.backend_url, credential).post(
            _PUBLICATION_PATH,
            request_body.body,
            correlation_id=request_body.idempotency_key,
        )
    except (OSError, ValueError, TypeError, RunnerFactError, MachineCredentialError) as exc:
        print(f"Runner capability publication failed: {exc}", file=sys.stderr)
        return 1

    expected = {
        "runner_id": runner.runner_id,
        "capability_version_id": str(request_body.capability_version_id),
        "capability_version": request_body.capability_version,
        "manifest_digest": request_body.manifest_digest,
        "key_id": identity.key_id,
        "key_version": 1,
        "public_key_digest": hashlib.sha256(
            base64.b64decode(request_body.body["public_key_base64"], validate=True)
        ).hexdigest(),
    }
    if any(response.get(field) != value for field, value in expected.items()):
        print("Runner capability response does not match signed authority", file=sys.stderr)
        return 1
    binding_status = response.get("binding_status")
    binding_evidence_digest = response.get("binding_evidence_digest")
    if (
        not isinstance(response.get("valid_from"), str)
        or not _is_lower_sha256(response.get("key_authority_digest"))
        or not _is_lower_sha256(response.get("proof_digest"))
        or binding_status not in {"validated", "pending_projection"}
        or (binding_status == "validated") != _is_lower_sha256(binding_evidence_digest)
        or type(response.get("restart_required")) is not bool
    ):
        print("Runner capability response authority is invalid", file=sys.stderr)
        return 1
    if (
        binding_status == "validated"
        and binding_evidence_digest
        != capability_binding_evidence_digest(runner.tenant_id, runner.runner_id, scope_bindings)
    ):
        print("Runner capability binding evidence does not match the manifest", file=sys.stderr)
        return 1
    receipt = {
        "schema_version": 1,
        "tenant_id": runner.tenant_id,
        **expected,
        "capability_manifest": document,
        "scope_bindings": capability_scope_binding_values(scope_bindings),
        "algorithm": "ed25519",
        "valid_from": response.get("valid_from"),
        "key_authority_digest": response.get("key_authority_digest"),
        "proof_digest": response.get("proof_digest"),
        "binding_status": binding_status,
        "binding_evidence_digest": binding_evidence_digest,
        "restart_required": response["restart_required"],
    }
    try:
        _write_receipt(args.authority_path, receipt)
    except OSError as exc:
        print(f"Cannot persist Runner capability receipt: {exc}", file=sys.stderr)
        return 1
    print(
        "Runner capability published: "
        f"capability_version_id={expected['capability_version_id']} "
        f"capability_version={expected['capability_version']} "
        f"key_id={identity.key_id} restart_required={receipt['restart_required']}"
    )
    return 0


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _next_capability_version(path: Path, requested: int | None) -> int:
    if requested is not None:
        return requested
    authority_path = path.expanduser().resolve()
    if not authority_path.exists():
        return 1
    document = json.loads(authority_path.read_text(encoding="utf-8"))
    current = document.get("capability_version") if isinstance(document, dict) else None
    if type(current) is not int or current < 1:
        raise ValueError("existing Runner capability receipt has no valid revision")
    return current + 1


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _non_nil_uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a UUID") from exc
    if parsed.int == 0:
        raise argparse.ArgumentTypeError("UUID must not be nil")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed
