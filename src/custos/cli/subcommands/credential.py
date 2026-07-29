"""Machine credential verification, rotation, and revocation commands."""

from __future__ import annotations

import argparse
import base64
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization

from custos.core.machine_credential_vault import (
    MachineCredential,
    MachineCredentialError,
    MachineCredentialHttpClient,
    MachineCredentialVault,
    canonical_revocation_proof,
    canonical_rotation_proof,
    generate_machine_identity,
    resolve_age_recipient,
)
from custos.core.runner_toml import RunnerToml, require_attested

_DEFAULT_RUNNER_TOML = Path.home() / ".arx" / "runner.toml"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "credential", help="Verify, rotate, or revoke the runner machine credential."
    )
    actions = parser.add_subparsers(dest="action", metavar="{verify,rotate,revoke}")
    verify = actions.add_parser("verify", help="Verify local and server authority binding.")
    _common(verify)
    verify.set_defaults(action_handler=_verify)
    rotate = actions.add_parser("rotate", help="Rotate with old-key proof of possession.")
    _common(rotate)
    rotate.add_argument("--reason", required=True, type=_reason)
    rotate.add_argument("--age-recipient", default=None)
    rotate.set_defaults(action_handler=_rotate)
    revoke = actions.add_parser("revoke", help="Revoke and immediately erase local authority.")
    _common(revoke)
    revoke.add_argument("--reason", required=True, type=_reason)
    revoke.add_argument(
        "--authority-path",
        type=Path,
        default=Path.home() / ".arx" / "runner-capability.json",
    )
    revoke.add_argument(
        "--ready-file",
        type=Path,
        default=Path.home() / ".arx" / "state" / "runner-ready.json",
    )
    revoke.set_defaults(action_handler=_revoke)
    parser.set_defaults(handler=_dispatch)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runner-toml", type=Path, default=_DEFAULT_RUNNER_TOML)


def _dispatch(args: argparse.Namespace) -> int:
    handler = getattr(args, "action_handler", None)
    if handler is None:
        raise SystemExit("credential requires an action (verify, rotate, or revoke)")
    return handler(args)


def _load(args: argparse.Namespace) -> tuple[RunnerToml, MachineCredentialVault, MachineCredential]:
    metadata = require_attested(
        RunnerToml.read(args.runner_toml), action="manage a machine credential"
    )
    vault = MachineCredentialVault(Path(metadata.machine_vault_path))
    credential = vault.load()
    credential.assert_binding(metadata)
    return metadata, vault, credential


def _verify(args: argparse.Namespace) -> int:
    try:
        metadata, _, credential = _load(args)
        MachineCredentialHttpClient(metadata.backend_url, credential).verify_active()
    except (OSError, ValueError, MachineCredentialError) as exc:
        print(f"Machine credential verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Machine credential active: "
        f"credential_id={credential.credential_id} "
        f"credential_version={credential.credential_version} "
        f"machine_key_id={credential.machine_key_id}"
    )
    return 0


def _rotate(args: argparse.Namespace) -> int:
    try:
        metadata, vault, old = _load(args)
        age_recipient = resolve_age_recipient(args.age_recipient)
        new_private_key, new_key_id = generate_machine_identity()
        new_public_key = new_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        challenge_nonce = uuid4()
        correlation_id = uuid4()
        rotation_proof = canonical_rotation_proof(
            credential=old,
            challenge_nonce=challenge_nonce,
            correlation_id=correlation_id,
            new_machine_key_id=new_key_id,
            new_public_key=new_public_key,
            reason=args.reason,
        )
        body = {
            "tenant_id": old.tenant_id,
            "runner_id": str(old.runner_id),
            "credential_id": str(old.credential_id),
            "credential_version": old.credential_version,
            "challenge_nonce": str(challenge_nonce),
            "correlation_id": str(correlation_id),
            "new_machine_key_id": new_key_id,
            "new_public_key_base64": base64.b64encode(new_public_key).decode("ascii"),
            "proof_signature_base64": old.sign(rotation_proof),
            "reason": args.reason,
        }
        response = MachineCredentialHttpClient(metadata.backend_url, old).post(
            "/api/v1/runner-credentials/rotate", body, correlation_id=correlation_id
        )
        new = _rotated_credential(response, old, new_private_key, new_key_id)
        vault.persist(new, age_recipient=age_recipient)
        RunnerToml.write(
            args.runner_toml,
            RunnerToml(
                tenant_id=new.tenant_id,
                runner_id=str(new.runner_id),
                backend_url=metadata.backend_url,
                credential_id=str(new.credential_id),
                credential_version=new.credential_version,
                credential_valid_until=_timestamp_text(new.credential_valid_until),
                machine_key_id=new.machine_key_id,
                machine_vault_path=str(vault.path),
                enrolled_at=metadata.enrolled_at,
            ),
        )
    except (OSError, ValueError, KeyError, MachineCredentialError) as exc:
        print(f"Machine credential rotation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Machine credential rotated: "
        f"credential_id={new.credential_id} credential_version={new.credential_version} "
        f"machine_key_id={new.machine_key_id}"
    )
    return 0


def _revoke(args: argparse.Namespace) -> int:
    try:
        metadata, vault, credential = _load(args)
        challenge_nonce = uuid4()
        correlation_id = uuid4()
        proof = canonical_revocation_proof(
            credential=credential,
            challenge_nonce=challenge_nonce,
            correlation_id=correlation_id,
            reason=args.reason,
        )
        body = {
            "tenant_id": credential.tenant_id,
            "runner_id": str(credential.runner_id),
            "credential_id": str(credential.credential_id),
            "credential_version": credential.credential_version,
            "challenge_nonce": str(challenge_nonce),
            "correlation_id": str(correlation_id),
            "proof_signature_base64": credential.sign(proof),
            "reason": args.reason,
        }
        response = MachineCredentialHttpClient(metadata.backend_url, credential).post(
            "/api/v1/runner-credentials/revoke", body, correlation_id=correlation_id
        )
        if response.get("state") != "revoked":
            raise MachineCredentialError("authority did not confirm revocation")
        vault.invalidate()
        args.runner_toml.expanduser().resolve().unlink(missing_ok=True)
        args.authority_path.expanduser().resolve().unlink(missing_ok=True)
        args.ready_file.expanduser().resolve().unlink(missing_ok=True)
    except (OSError, ValueError, MachineCredentialError) as exc:
        print(f"Machine credential revocation failed: {exc}", file=sys.stderr)
        return 1
    print("Machine credential revoked and local authority erased")
    return 0


def _rotated_credential(
    response: dict[str, object],
    old: MachineCredential,
    new_private_key: object,
    new_key_id: str,
) -> MachineCredential:
    expected = {
        "tenant_id": old.tenant_id,
        "runner_id": str(old.runner_id),
        "credential_version": old.credential_version + 1,
        "machine_key_id": new_key_id,
        "state": "active",
    }
    if any(response.get(field) != value for field, value in expected.items()):
        raise MachineCredentialError("rotation response does not match the signed request")
    valid_until = response.get("credential_valid_until")
    if not isinstance(valid_until, str):
        raise MachineCredentialError("rotation response is missing credential_valid_until")
    try:
        parsed_expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MachineCredentialError("rotation response expiry is invalid") from exc
    private_bytes = new_private_key.private_bytes(  # type: ignore[attr-defined]
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return MachineCredential(
        tenant_id=old.tenant_id,
        runner_id=old.runner_id,
        credential_id=response["credential_id"],  # type: ignore[arg-type]
        credential_version=response["credential_version"],  # type: ignore[arg-type]
        credential_valid_until=parsed_expiry,
        machine_key_id=new_key_id,
        machine_credential=str(response["long_term_credential"]),
        private_key_bytes=private_bytes,
    )


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _reason(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 512 or any(ord(character) < 32 for character in cleaned):
        raise argparse.ArgumentTypeError("reason must contain 1-512 printable characters")
    return cleaned
