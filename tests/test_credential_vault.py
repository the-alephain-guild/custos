"""CredentialVault decrypt path emits the CredentialDecrypted audit signal.

The vault stub does not implement real sops/age decryption. What we contract
today is the audit signal shape: every successful decrypt MUST emit a
structured CredentialDecrypted event, with its fields actually present in the
rendered output, so the downstream audit writer can pick it up.

These assertions read the real emitted event rather than logger internals. An
earlier version asserted stdlib record attributes, which stayed green while the
rendered line carried the event name and none of its fields.
"""

from __future__ import annotations

import structlog.testing

from custos.core.credential_vault import AuditEvent, CredentialVault


def test_decrypt_returns_mock_credential() -> None:
    vault = CredentialVault(tenant_id="acme", initiator="runner-7")
    cred = vault.decrypt("cred-123")
    assert isinstance(cred, dict)
    assert "credential_id" in cred


def test_decrypt_emits_credential_decrypted_audit_event() -> None:
    vault = CredentialVault(tenant_id="acme", initiator="runner-7")
    with structlog.testing.capture_logs() as records:
        vault.decrypt("cred-123")

    audit = [r for r in records if r.get("audit_event") == AuditEvent.CREDENTIAL_DECRYPTED.value]
    assert len(audit) == 1, "expected exactly one CredentialDecrypted audit event"

    rec = audit[0]
    assert rec["event"] == "credential_decrypted"
    assert rec["credential_id"] == "cred-123"
    assert rec["tenant_id"] == "acme"
    assert rec["initiator"] == "runner-7"
    assert rec["timestamp"]  # truthy ISO-8601 string


def test_audit_event_enum_pins_credential_decrypted_name() -> None:
    # Pinning the event name guards against accidental rename — the downstream
    # audit writer matches on this exact string.
    assert AuditEvent.CREDENTIAL_DECRYPTED.value == "CredentialDecrypted"
