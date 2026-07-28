"""Local exchange-credential vault base helpers.

Hard rules (architecture §1 trust boundary):
- KEK (age private key) never leaves the runner host.
- ``permission_scope`` forbids withdrawal (must be ``trade_no_withdraw``).
- Every decrypt MUST emit a ``CredentialDecrypted`` audit event so the
  audit writer can build the immutable chain. The cloud product surface
  schema never persists keys.
- Every encrypt MUST emit a ``CredentialEncrypted`` audit event with the
  same discipline (only key_id + tenant_id, no plaintext).

Implementations:
- ``CredentialVault``: mock vault returning a placeholder credential
  dict; retained for tests and dev harnesses.
- ``PerKeyVault`` (in ``custos.core.per_key_vault``): production runtime
  reader for the per-key ``~/.arx/vault/<key-id>.enc`` files written by
  ``arx-runner vault put``. Inherits both invariants from ``_BaseVault``.

Future: Hashicorp Vault provider for team tier — the KEK / Vault token
still stays on the runner host.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Protocol

from custos.core.log import get_logger

_log = get_logger("custos.credential_vault")


class AuditEvent(enum.Enum):
    """Audit event names consumed by the downstream audit log writer.

    Pinned as a closed enum so a rename can't silently break the writer's
    pattern match.
    """

    CREDENTIAL_DECRYPTED = "CredentialDecrypted"
    CREDENTIAL_ENCRYPTED = "CredentialEncrypted"


class CredentialVaultProtocol(Protocol):
    """Vault interface. Implementations must guarantee the audit event emit
    on every successful decrypt and must verify permission_scope is non-
    withdrawal before returning."""

    def decrypt(self, credential_id: str) -> dict: ...


class _BaseVault:
    """Shared audit-emit + permission-scope verification logic."""

    def __init__(self, *, tenant_id: str, initiator: str) -> None:
        self._tenant_id = tenant_id
        self._initiator = initiator

    def _emit_decrypt_audit(self, credential_id: str) -> None:
        """Emit canonical CredentialDecrypted audit event.

        Plaintext credentials NEVER appear in audit logs — only the
        credential_id reference. The downstream audit writer consumes the
        structured log event and chains it.
        """
        timestamp = datetime.now(UTC).isoformat()
        _log.info(
            "credential_decrypted",
            audit_event=AuditEvent.CREDENTIAL_DECRYPTED.value,
            credential_id=credential_id,
            tenant_id=self._tenant_id,
            initiator=self._initiator,
            timestamp=timestamp,
        )

    @staticmethod
    def _verify_permission_scope(cred: dict, credential_id: str) -> None:
        """Reject creds that allow withdrawal (CLAUDE.md red line + security.md)."""
        scope = cred.get("permission_scope")
        if scope != "trade_no_withdraw":
            _log.error(
                "credential_scope_violation",
                credential_id=credential_id,
                got_scope=scope,
            )
            raise ValueError(
                f"credential {credential_id!r} has unsafe permission_scope "
                f"(expected 'trade_no_withdraw', got {scope!r})"
            )


class CredentialVault(_BaseVault):
    """Mock vault: returns a placeholder credential dict and emits the
    canonical `CredentialDecrypted` audit signal. Used by tests + dev.

    Kept name `CredentialVault` for backward compat with existing call
    sites (`from custos.core.credential_vault import CredentialVault`).
    """

    def decrypt(self, credential_id: str) -> dict:
        cred = {
            "credential_id": credential_id,
            "tenant_id": self._tenant_id,
            "permission_scope": "trade_no_withdraw",
            "secret": "<mock>",
        }
        self._verify_permission_scope(cred, credential_id)
        self._emit_decrypt_audit(credential_id)
        return cred
