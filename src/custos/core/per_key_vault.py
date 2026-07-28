"""Runtime read path for the per-key vault written by ``arx-runner vault put``.

Consumes ``<vault-dir>/<credential_id>.enc`` files that are single-credential
sops+age envelopes (as opposed to the deleted legacy multi-credential JSON
``SopsAgeVault``). Inherits ``_verify_permission_scope`` and
``_emit_decrypt_audit`` from ``_BaseVault`` so every decrypt still refuses
non-``trade_no_withdraw`` scopes and emits the canonical
``CredentialDecrypted`` audit event.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from custos.core.credential_vault import _BaseVault
from custos.core.log import get_logger

_log = get_logger("custos.credential_vault")
_SOPS_TIMEOUT_SECS = 30


def sops_json_decrypt_command(enc_path: Path) -> list[str]:
    """Build the explicit JSON decrypt command for a per-key ``.enc`` file."""
    return [
        "sops",
        "--decrypt",
        "--input-type",
        "json",
        "--output-type",
        "json",
        str(enc_path),
    ]


class PerKeyVault(_BaseVault):
    """Reconciler-facing vault: one ``.enc`` file per credential_id."""

    def __init__(self, *, vault_dir: Path, tenant_id: str, initiator: str) -> None:
        super().__init__(tenant_id=tenant_id, initiator=initiator)
        self._vault_dir = vault_dir

    def decrypt(self, credential_id: str) -> dict:
        enc_path = self._vault_dir / f"{credential_id}.enc"
        if not enc_path.exists():
            raise FileNotFoundError(
                f"vault entry {enc_path} not found; run "
                f"`arx-runner vault put --key-id {credential_id}` first"
            )

        env = dict(os.environ)
        try:
            result = subprocess.run(
                sops_json_decrypt_command(enc_path),
                env=env,
                capture_output=True,
                check=True,
                timeout=_SOPS_TIMEOUT_SECS,
            )
        except FileNotFoundError as exc:
            _log.error("sops_binary_not_found", error=str(exc))
            raise RuntimeError("sops CLI not installed on runner host") from exc
        except subprocess.CalledProcessError as exc:
            # stderr may carry sops error info but never plaintext (sops
            # design). Length only for the audit trail.
            _log.error(
                "sops_decrypt_failed",
                credential_id=credential_id,
                returncode=exc.returncode,
                stderr_len=len(exc.stderr or b""),
            )
            raise RuntimeError("sops decryption failed") from exc
        except subprocess.TimeoutExpired:
            _log.error("sops_decrypt_timeout", credential_id=credential_id)
            raise RuntimeError("sops decryption timed out") from None

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            _log.error(
                "sops_output_parse_failed",
                credential_id=credential_id,
                error=str(exc),
            )
            raise RuntimeError("sops output is not valid JSON") from exc

        cred = payload.get(credential_id)
        if cred is None:
            _log.error(
                "credential_not_in_enc_file",
                credential_id=credential_id,
                vault_dir=str(self._vault_dir),
            )
            raise KeyError(f"credential {credential_id!r} not present in {enc_path.name}")

        self._verify_permission_scope(cred, credential_id)
        self._emit_decrypt_audit(credential_id)
        return cred
