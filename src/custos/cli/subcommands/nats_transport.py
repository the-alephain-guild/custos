"""Runner-control User NKey/JWT transport enrollment, rotation and revocation."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from custos.cli.subcommands.start import DEFAULT_RUNNER_TOML
from custos.cli.validators import validate_backend_url
from custos.core.machine_credential_vault import (
    MachineCredentialError,
    MachineCredentialTransportError,
    MachineCredentialVault,
    resolve_age_recipient,
)
from custos.core.nats_transport import (
    RunnerNatsTransportBundle,
    RunnerNatsTransportConnectionProfile,
    RunnerNatsTransportError,
    RunnerNatsTransportVault,
    assert_old_generation_reconnect_denied,
)
from custos.core.runner_nats_authority import (
    RunnerNatsTransportAuthorityClient,
    RunnerNatsTransportOperationCompletion,
)
from custos.core.runner_toml import RunnerToml, require_attested

DEFAULT_TRANSPORT_VAULT_DIR = Path.home() / ".arx" / "vault" / "runner-nats-transport"
DEFAULT_NATS_CA = Path.home() / ".arx" / "certs" / "crucible-nats-ca.pem"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "nats-transport",
        help="Issue, rotate, revoke, resume or verify runner-control NATS authority.",
    )
    actions = parser.add_subparsers(
        dest="transport_action",
        metavar="{enroll,rotate,revoke,resume,verify}",
    )
    for action in ("enroll", "rotate", "revoke"):
        child = actions.add_parser(action)
        _add_authority_arguments(child, require_intent=True)
        child.set_defaults(handler=run)
    resume = actions.add_parser("resume")
    _add_authority_arguments(resume, require_intent=False)
    resume.set_defaults(handler=run)
    verify = actions.add_parser("verify")
    _add_local_arguments(verify)
    verify.set_defaults(handler=run)


def _add_authority_arguments(
    parser: argparse.ArgumentParser,
    *,
    require_intent: bool,
) -> None:
    _add_identity_arguments(parser)
    _add_nats_connection_arguments(parser)
    parser.add_argument("--crucible-url", required=True, type=validate_backend_url)
    if require_intent:
        parser.add_argument(
            "--authorization-intent-id",
            required=True,
            type=UUID,
            help="ARX-approved runner NATS transport authorization intent UUID.",
        )
    parser.add_argument(
        "--operation-timeout-secs",
        type=float,
        default=300.0,
        help="Bounded wait for the Crucible authority operation.",
    )
    parser.add_argument(
        "--age-recipient",
        default=None,
        help="age public recipient; defaults to SOPS_AGE_RECIPIENT.",
    )


def _add_local_arguments(parser: argparse.ArgumentParser) -> None:
    _add_identity_arguments(parser)
    _add_nats_connection_arguments(parser)


def _add_nats_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--nats-ca", type=Path, default=DEFAULT_NATS_CA)
    parser.add_argument("--nats-server-name", required=True)
    parser.add_argument(
        "--verification-timeout-secs",
        type=float,
        default=30.0,
        help="Bounded wait for explicit old-JWT authorization denial.",
    )
    parser.add_argument(
        "--issuer-public-key",
        default=os.environ.get("CRUCIBLE_NATS_ISSUER_ACCOUNT_NKEY", ""),
        help="Optional pin override; persisted authority remains canonical.",
    )


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runner-toml", type=Path, default=DEFAULT_RUNNER_TOML)
    parser.add_argument(
        "--machine-vault",
        type=Path,
        default=None,
        help="Optional exact override; must equal runner.toml machine_vault_path.",
    )
    parser.add_argument(
        "--transport-vault-dir",
        type=Path,
        default=DEFAULT_TRANSPORT_VAULT_DIR,
    )
    parser.add_argument(
        "--trading-mode",
        choices=("sandbox", "testnet", "live"),
        required=True,
        help="Exact mode authority to operate or verify.",
    )


def run(args: argparse.Namespace) -> int:
    try:
        metadata = require_attested(
            RunnerToml.read(args.runner_toml), action="load a transport authority"
        )
        machine_vault_path = Path(metadata.machine_vault_path).expanduser().resolve()
        if (
            args.machine_vault is not None
            and args.machine_vault.expanduser().resolve() != machine_vault_path
        ):
            raise RunnerNatsTransportError(
                "--machine-vault differs from runner.toml authority binding"
            )
        machine_credential = MachineCredentialVault(machine_vault_path).load()
        machine_credential.assert_binding(metadata)
        vault = RunnerNatsTransportVault(args.transport_vault_dir, args.trading_mode)

        if args.transport_action == "verify":
            bundle = vault.load()
            if bundle.active is None:
                raise RunnerNatsTransportError("NATS transport has no active generation")
            if bundle.pending_operation is not None:
                raise RunnerNatsTransportError("NATS transport has a pending operation; run resume")
            issuer = _bound_issuer(args.issuer_public_key, bundle)
            RunnerNatsTransportConnectionProfile(
                credential=bundle.active,
                nats_url=args.nats_url,
                ca_path=args.nats_ca,
                server_name=args.nats_server_name,
                pinned_issuer_public_key=issuer,
            )
            print(
                "NATS transport verified: "
                f"tenant_id={bundle.active.tenant_id} "
                f"runner_id={bundle.active.runner_id} "
                f"trading_mode={bundle.active.trading_mode} "
                f"generation={bundle.active.credential_generation}"
            )
            return 0

        age_recipient = resolve_age_recipient(args.age_recipient)
        authority = RunnerNatsTransportAuthorityClient(
            args.crucible_url,
            machine_credential,
        )
        if args.transport_action == "enroll":
            if vault.path.exists():
                raise RunnerNatsTransportError("NATS transport vault already exists")
            operation = authority.prepare_initial(
                authorization_intent_id=args.authorization_intent_id,
                trading_mode=args.trading_mode,
                expected_issuer_public_key=_required_issuer(args.issuer_public_key),
            )
            bundle = RunnerNatsTransportBundle(
                active=None,
                pending_operation=operation,
            )
            vault.persist(bundle, age_recipient=age_recipient)
        elif args.transport_action in {"rotate", "revoke"}:
            bundle = vault.load()
            if bundle.active is None:
                raise RunnerNatsTransportError(
                    f"cannot {args.transport_action} without active authority"
                )
            if bundle.pending_operation is not None:
                raise RunnerNatsTransportError(
                    "pending NATS operation must be resumed before another operation"
                )
            _bound_issuer(args.issuer_public_key, bundle)
            if args.transport_action == "rotate":
                operation = authority.prepare_rotation(
                    bundle.active,
                    authorization_intent_id=args.authorization_intent_id,
                )
            else:
                operation = authority.prepare_revocation(
                    bundle.active,
                    authorization_intent_id=args.authorization_intent_id,
                )
            bundle = bundle.with_pending_operation(operation)
            vault.persist(bundle, age_recipient=age_recipient)
        elif args.transport_action == "resume":
            bundle = vault.load()
            if bundle.pending_operation is None:
                raise RunnerNatsTransportError("NATS transport has no pending operation")
            _bound_issuer(args.issuer_public_key, bundle)
        else:
            raise RunnerNatsTransportError("a nats-transport action is required")

        assert bundle.pending_operation is not None
        completion = authority.execute(
            bundle.pending_operation,
            timeout_seconds=float(args.operation_timeout_secs),
        )
        completed = asyncio.run(
            _verify_and_commit(
                args=args,
                vault=vault,
                bundle=bundle,
                completion=completion,
                age_recipient=age_recipient,
            )
        )
        if completed is None:
            print(
                "NATS transport revoked: "
                f"operation_id={completion.operation_id} "
                f"receipt={completion.revocation_receipt_digest}"
            )
            return 0
        assert completed.active is not None
        print(
            "NATS transport active: "
            f"tenant_id={completed.active.tenant_id} "
            f"runner_id={completed.active.runner_id} "
            f"trading_mode={completed.active.trading_mode} "
            f"generation={completed.active.credential_generation}"
        )
        return 0
    except (
        MachineCredentialError,
        MachineCredentialTransportError,
        RunnerNatsTransportError,
        OSError,
        ValueError,
    ) as exc:
        print(f"NATS transport operation failed closed: {exc}", file=sys.stderr)
        return 1


async def _verify_and_commit(
    *,
    args: argparse.Namespace,
    vault: RunnerNatsTransportVault,
    bundle: RunnerNatsTransportBundle,
    completion: RunnerNatsTransportOperationCompletion,
    age_recipient: str,
) -> RunnerNatsTransportBundle | None:
    operation = bundle.pending_operation
    if operation is None or completion.operation_id != operation.operation_id:
        raise RunnerNatsTransportError("NATS completion has no matching pending operation")
    issuer = operation.expected_issuer_public_key
    if operation.operation_kind in {"issue", "rotate"}:
        credential = completion.credential
        if credential is None:
            raise RunnerNatsTransportError("completed NATS operation has no credential")
        profile = _connection_profile(args, credential, issuer)
        connection = await _connect(
            profile,
            name=f"custos-transport-activate-{credential.runner_id}",
            allow_reconnect=False,
            max_reconnect_attempts=0,
        )
        await _close(connection)
        if bundle.active is not None:
            await _assert_denied(args, bundle.active, issuer)
        completed = bundle.complete_with(credential)
        vault.persist(completed, age_recipient=age_recipient)
        return completed

    if completion.credential is not None or completion.revocation_receipt_digest is None:
        raise RunnerNatsTransportError("completed NATS revocation result is invalid")
    if bundle.active is None:
        raise RunnerNatsTransportError("NATS revocation has no active local authority")
    await _assert_denied(args, bundle.active, issuer)
    vault.delete()
    return None


async def _assert_denied(
    args: argparse.Namespace,
    credential: Any,
    issuer: str,
) -> None:
    timeout = float(args.verification_timeout_secs)
    if timeout <= 0:
        raise RunnerNatsTransportError("NATS verification timeout must be positive")
    await assert_old_generation_reconnect_denied(
        _connection_profile(args, credential, issuer),
        name=f"custos-transport-denial-{credential.runner_id}",
        timeout_seconds=timeout,
    )


def _connection_profile(
    args: argparse.Namespace,
    credential: Any,
    issuer: str,
) -> RunnerNatsTransportConnectionProfile:
    return RunnerNatsTransportConnectionProfile(
        credential=credential,
        nats_url=args.nats_url,
        ca_path=args.nats_ca,
        server_name=args.nats_server_name,
        pinned_issuer_public_key=issuer,
    )


async def _connect(
    profile: RunnerNatsTransportConnectionProfile,
    *,
    name: str,
    allow_reconnect: bool,
    max_reconnect_attempts: int,
) -> Any:
    try:
        return await profile.connect(
            name=name,
            allow_reconnect=allow_reconnect,
            max_reconnect_attempts=max_reconnect_attempts,
        )
    except RunnerNatsTransportError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize NATS client errors
        raise RunnerNatsTransportError("cannot establish pinned runner NATS session") from exc


async def _close(connection: Any | None) -> None:
    if connection is not None and not connection.is_closed:
        await connection.close()


def _bound_issuer(value: str, bundle: RunnerNatsTransportBundle) -> str:
    expected = (
        bundle.pending_operation.expected_issuer_public_key
        if bundle.pending_operation is not None
        else bundle.active.issuer_public_key
        if bundle.active is not None
        else ""
    )
    supplied = value.strip()
    if supplied and supplied != expected:
        raise RunnerNatsTransportError("issuer pin differs from persisted authority")
    return _required_issuer(expected)


def _required_issuer(value: str) -> str:
    issuer = value.strip()
    if not issuer:
        raise RunnerNatsTransportError(
            "--issuer-public-key or CRUCIBLE_NATS_ISSUER_ACCOUNT_NKEY is required"
        )
    return issuer
