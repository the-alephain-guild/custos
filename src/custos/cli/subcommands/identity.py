"""Generate a runner identity locally, for a lane that answers to no authority.

Enrolment never needed the network to *create* an identity — the keypair has
always been generated in this process. What the backend adds is attestation: it
issues the credential and vouches that this runner is who it says it is. The
offline lane has no authority to ask, so it takes the creation and leaves the
attestation, and marks the result plainly as unattested.

This is deliberately not a flag on ``enroll``. A command whose purpose is
answering to an authority is the wrong door for a lane that answers to none.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization

from custos.cli.validators import validate_id
from custos.core.machine_credential_vault import (
    MachineCredential,
    MachineCredentialError,
    MachineCredentialVault,
    generate_machine_identity,
    resolve_age_recipient,
)
from custos.core.runner_toml import STANDALONE_BACKEND_URL, RunnerToml

DEFAULT_RUNNER_TOML = Path.home() / ".arx" / "runner.toml"
DEFAULT_MACHINE_VAULT = Path.home() / ".arx" / "vault" / "runner-machine.enc"

_DEFAULT_VALID_DAYS = 365
_CREDENTIAL_PREFIX = "rkc1."
_CREDENTIAL_ENTROPY_BYTES = 32


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "identity", help="Manage this runner's local authority document."
    )
    actions = parser.add_subparsers(dest="identity_action", required=True)
    standalone = actions.add_parser(
        "standalone",
        help="Generate an unattested identity for the offline lane; no backend is contacted.",
    )
    standalone.add_argument(
        "--tenant-id", required=True, type=lambda value: validate_id("tenant_id", value)
    )
    standalone.add_argument("--runner-toml", type=Path, default=DEFAULT_RUNNER_TOML)
    standalone.add_argument("--machine-vault", type=Path, default=DEFAULT_MACHINE_VAULT)
    standalone.add_argument(
        "--age-recipient",
        default=None,
        help="age public recipient; defaults to SOPS_AGE_RECIPIENT.",
    )
    standalone.add_argument("--valid-days", type=int, default=_DEFAULT_VALID_DAYS)
    standalone.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    vault = MachineCredentialVault(args.machine_vault)
    if vault.path.exists() or args.runner_toml.expanduser().exists():
        print(
            "Runner identity already exists; the vault material is bound to that document, "
            "so remove both deliberately before generating another",
            file=sys.stderr,
        )
        return 1
    try:
        age_recipient = resolve_age_recipient(args.age_recipient)
        if args.valid_days < 1:
            raise ValueError("--valid-days must be at least one day")
        private_key, machine_key_id = generate_machine_identity()
        issued_at = datetime.now(UTC)
        credential = MachineCredential(
            tenant_id=args.tenant_id,
            runner_id=uuid4(),
            credential_id=uuid4(),
            credential_version=1,
            credential_valid_until=issued_at + timedelta(days=args.valid_days),
            machine_key_id=machine_key_id,
            machine_credential=_local_credential(),
            private_key_bytes=private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
        vault.persist(credential, age_recipient=age_recipient)
        RunnerToml.write(
            args.runner_toml,
            RunnerToml(
                tenant_id=credential.tenant_id,
                runner_id=str(credential.runner_id),
                backend_url=STANDALONE_BACKEND_URL,
                credential_id=str(credential.credential_id),
                credential_version=credential.credential_version,
                credential_valid_until=_timestamp_text(credential.credential_valid_until),
                machine_key_id=credential.machine_key_id,
                machine_vault_path=str(vault.path),
                enrolled_at=_timestamp_text(issued_at),
            ),
        )
    except (MachineCredentialError, OSError, ValueError) as exc:
        vault.invalidate()
        print(f"Standalone identity generation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Standalone runner identity generated (unattested): "
        f"tenant_id={credential.tenant_id} runner_id={credential.runner_id} "
        f"credential_id={credential.credential_id} "
        f"machine_key_id={credential.machine_key_id} "
        f"backend_url={STANDALONE_BACKEND_URL}"
    )
    return 0


def _local_credential() -> str:
    """Mint the opaque credential a backend would otherwise issue.

    Its only checked property is the ``rkc1.`` prefix, and the charset stays
    inside what the runtime-log redactor recognises, so it cannot leak through a
    log line that quotes it.
    """

    return _CREDENTIAL_PREFIX + secrets.token_urlsafe(_CREDENTIAL_ENTROPY_BYTES)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
