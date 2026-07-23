"""``arx-runner vault {put,verify,list}`` handlers.

Each per-key credential lives at ``~/.arx/vault/<key-id>.enc`` — a sops+age
single-file per credential replacing the legacy multi-credential JSON
``SopsAgeVault``.

The audit event contract mirrors the decrypt path in
``custos.core.credential_vault``: put emits ``CredentialEncrypted`` via
stdlib ``logging.getLogger("custos.credential_vault")``. Plaintext api
secrets never appear in log records, only the key_id / tenant_id
reference.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from custos.cli.validators import validate_id
from custos.core import per_key_vault
from custos.core.credential_vault import AuditEvent

DEFAULT_VAULT_DIR = Path.home() / ".arx" / "vault"

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_SOPS_TIMEOUT_SECS = 30
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Stdlib logger — matches the decrypt audit event sink so caplog +
# downstream audit-writer pattern-match code stays uniform.
_log = logging.getLogger("custos.credential_vault")


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("vault", help="Per-key credential vault (sops+age).")
    actions = parser.add_subparsers(dest="action", metavar="{put,verify,list}")
    _register_put(actions)
    _register_verify(actions)
    _register_list(actions)
    parser.set_defaults(handler=_dispatch)


def _register_put(actions: argparse._SubParsersAction) -> None:
    p = actions.add_parser(
        "put",
        help="Encrypt an exchange API credential with sops+age.",
    )
    p.add_argument(
        "--key-id",
        required=True,
        type=lambda v: validate_id("key_id", v),
    )
    p.add_argument(
        "--tenant-id",
        required=True,
        type=lambda v: validate_id("tenant_id", v),
    )
    p.add_argument("--api-key", required=True)
    p.add_argument(
        "--scope-digest",
        required=True,
        type=lambda value: _scope_digest(value),
        help="Exact lowercase SHA-256 bound by DeploymentSpec credential_scope.",
    )
    secret = p.add_mutually_exclusive_group(required=True)
    secret.add_argument(
        "--api-secret-stdin",
        action="store_true",
        help="Read the api secret from stdin (recommended for scripts).",
    )
    secret.add_argument(
        "--api-secret-env",
        metavar="ENV_NAME",
        help="Read the api secret from this environment variable.",
    )
    secret.add_argument(
        "--api-secret",
        help="Api secret as an argv (demo only; prints ps aux warning).",
    )
    p.add_argument(
        "--age-recipient",
        default=None,
        help="Age public key recipient (or SOPS_AGE_RECIPIENT env).",
    )
    p.add_argument(
        "--permission-scope",
        choices=["trade_no_withdraw"],
        default="trade_no_withdraw",
        help="Credential permission boundary (0.2.0+: trade_no_withdraw only).",
    )
    p.add_argument("--vault-dir", type=Path, default=DEFAULT_VAULT_DIR)
    p.set_defaults(action_handler=_put)


def _register_verify(actions: argparse._SubParsersAction) -> None:
    p = actions.add_parser(
        "verify",
        help="Decrypt a vault entry and check the permission_scope invariant.",
    )
    p.add_argument(
        "--key-id",
        required=True,
        type=lambda v: validate_id("key_id", v),
    )
    p.add_argument(
        "--tenant-id",
        required=True,
        type=lambda v: validate_id("tenant_id", v),
    )
    p.add_argument("--vault-dir", type=Path, default=DEFAULT_VAULT_DIR)
    p.add_argument(
        "--age-key-file",
        type=Path,
        default=None,
        help="Age private key file (or SOPS_AGE_KEY_FILE env).",
    )
    p.set_defaults(action_handler=_verify)


def _register_list(actions: argparse._SubParsersAction) -> None:
    p = actions.add_parser(
        "list",
        help="List all key-ids currently present in the vault directory.",
    )
    p.add_argument("--vault-dir", type=Path, default=DEFAULT_VAULT_DIR)
    p.set_defaults(action_handler=_list)


def _dispatch(args: argparse.Namespace) -> int:
    action_handler = getattr(args, "action_handler", None)
    if action_handler is None:
        raise SystemExit("vault requires an action ({put,verify,list})")
    return action_handler(args)


def _scope_digest(value: str) -> str:
    if not _LOWER_SHA256.fullmatch(value):
        raise argparse.ArgumentTypeError("scope digest must be lowercase SHA-256")
    return value


def _put(args: argparse.Namespace) -> int:
    """Encrypt one credential to ``<vault-dir>/<key-id>.enc`` via sops+age.

    Contract:
    - Refuses to overwrite an existing ``.enc``.
    - Passes the plaintext payload to sops via ``subprocess.run(input=...)``
      so no shell buffer holds it.
    - Emits a ``CredentialEncrypted`` audit event on success; the log
      record never contains the api_secret plaintext.
    """
    api_secret = _resolve_api_secret(args)
    enc_path = args.vault_dir / f"{args.key_id}.enc"
    if enc_path.exists():
        print(
            f"vault entry {enc_path} already exists; delete before re-put (no --force in v1)",
            file=sys.stderr,
        )
        return 1

    recipient = args.age_recipient or os.environ.get("SOPS_AGE_RECIPIENT")
    if not recipient:
        print(
            "age recipient not set (pass --age-recipient or SOPS_AGE_RECIPIENT)",
            file=sys.stderr,
        )
        return 1

    payload = {
        args.key_id: {
            "api_key": args.api_key,
            "api_secret": api_secret,
            "permission_scope": args.permission_scope,
            "scope_digest": args.scope_digest,
        }
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    try:
        result = subprocess.run(
            [
                "sops",
                "--encrypt",
                "--age",
                recipient,
                "--input-type",
                "json",
                "--output-type",
                "json",
                "/dev/stdin",
            ],
            input=payload_bytes,
            capture_output=True,
            check=True,
            timeout=_SOPS_TIMEOUT_SECS,
        )
    except FileNotFoundError as exc:
        print(
            "sops CLI not installed on runner host; install sops from "
            "https://github.com/getsops/sops/releases",
            file=sys.stderr,
        )
        _log.error("sops_binary_not_found", extra={"error": str(exc)})
        return 1
    except subprocess.CalledProcessError as exc:
        # stderr from sops is safe to surface: sops does not echo plaintext
        # to stderr on encrypt failure.
        stderr_text = (exc.stderr or b"").decode("utf-8", errors="replace")
        print(f"sops encrypt failed: {stderr_text}", file=sys.stderr)
        _log.error(
            "sops_encrypt_failed",
            extra={"key_id": args.key_id, "returncode": exc.returncode},
        )
        return 1

    args.vault_dir.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(args.vault_dir, _DIR_MODE)
    # Also ensure ~/.arx/ itself is 0700 when we just created the vault
    # subdir underneath it — arx dir shared with runner.toml.
    arx_dir = args.vault_dir.parent
    if arx_dir.exists():
        os.chmod(arx_dir, _DIR_MODE)

    tmp = args.vault_dir / f".{args.key_id}.enc.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        try:
            os.write(fd, result.stdout)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, _FILE_MODE)
        os.rename(tmp, enc_path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise

    _emit_encrypt_audit(args.key_id, args.tenant_id, args.permission_scope)
    print(f"credential encrypted to {enc_path}")
    return 0


def _resolve_api_secret(args: argparse.Namespace) -> str:
    """Pick the api secret source with defence-in-depth guard rails.

    Priority: stdin → env → argv. The argv path prints a red-flag stderr
    warning about ps aux / /proc/<pid>/cmdline exposure. Empty strings are
    rejected because sops+age would happily encrypt an empty secret and
    obscure the misuse until a live venue call fails.
    """
    if args.api_secret_stdin:
        secret = sys.stdin.readline().rstrip("\n")
    elif args.api_secret_env:
        try:
            secret = os.environ[args.api_secret_env]
        except KeyError as exc:
            raise SystemExit(
                f"--api-secret-env {args.api_secret_env!r} is not set in environment"
            ) from exc
    else:
        assert args.api_secret is not None  # argparse mutex guarantees this
        print(
            "WARNING: --api-secret via argv is visible in ps aux / /proc/<pid>/cmdline; "
            "prefer --api-secret-stdin or --api-secret-env for production",
            file=sys.stderr,
        )
        secret = args.api_secret
    if not secret:
        raise SystemExit("api secret is empty; refusing to write vault entry")
    return secret


def _emit_encrypt_audit(key_id: str, tenant_id: str, permission_scope: str) -> None:
    timestamp = datetime.now(UTC).isoformat()
    _log.info(
        "credential_encrypted",
        extra={
            "audit_event": AuditEvent.CREDENTIAL_ENCRYPTED.value,
            "key_id": key_id,
            "tenant_id": tenant_id,
            "permission_scope": permission_scope,
            "timestamp": timestamp,
        },
    )


def _verify(args: argparse.Namespace) -> int:
    """Decrypt one vault entry and check the permission_scope invariant.

    Failure paths (all non-zero exit, never prints ``OK``):
    - missing ``.enc`` file
    - world/group-readable ``.enc`` (0o644, etc)
    - sops CalledProcessError (bad passphrase, corrupted file, missing age key)
    - permission_scope != trade_no_withdraw
    """
    from custos.core.credential_vault import _BaseVault  # noqa: PLC0415 — shared invariant

    enc_path = args.vault_dir / f"{args.key_id}.enc"
    if not enc_path.exists():
        print(
            f"vault entry {enc_path} not found; run `arx-runner vault put "
            f"--key-id {args.key_id}` first",
            file=sys.stderr,
        )
        return 1
    mode = stat.S_IMODE(os.stat(enc_path).st_mode)
    if mode & 0o077:
        print(
            f"vault entry {enc_path} mode {oct(mode)} is world/group-readable; "
            "expected 0600 (chmod 600 <path>)",
            file=sys.stderr,
        )
        return 1

    env = dict(os.environ)
    if args.age_key_file is not None:
        env["SOPS_AGE_KEY_FILE"] = str(args.age_key_file)
    try:
        result = subprocess.run(
            per_key_vault.sops_json_decrypt_command(enc_path),
            env=env,
            capture_output=True,
            check=True,
            timeout=_SOPS_TIMEOUT_SECS,
        )
    except FileNotFoundError:
        print(
            "sops CLI not installed on runner host; install sops from "
            "https://github.com/getsops/sops/releases",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        stderr_text = (exc.stderr or b"").decode("utf-8", errors="replace")
        print(f"sops decrypt failed: {stderr_text}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("sops output was not valid JSON", file=sys.stderr)
        return 1
    cred = payload.get(args.key_id)
    if cred is None:
        print(
            f"key_id {args.key_id!r} not present in decrypted payload",
            file=sys.stderr,
        )
        return 1
    try:
        _BaseVault._verify_permission_scope(cred, args.key_id)
    except ValueError as exc:
        print(f"vault entry rejected: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {args.key_id} decrypts and passes permission_scope invariant")
    return 0


def _list(args: argparse.Namespace) -> int:
    """List all key-ids present in the vault directory + emit mode warnings."""
    if not args.vault_dir.exists():
        print(
            "no keys found; run `arx-runner vault put --key-id <id>` to add one",
        )
        return 0
    entries = sorted(args.vault_dir.glob("*.enc"))
    if not entries:
        print(
            "no keys found; run `arx-runner vault put --key-id <id>` to add one",
        )
        return 0
    for enc in entries:
        mode = stat.S_IMODE(os.stat(enc).st_mode)
        if mode & 0o077:
            print(
                f"WARNING: {enc.name} mode {oct(mode)} is world/group-readable (expected 0600)",
                file=sys.stderr,
            )
        print(enc.stem)
    return 0
