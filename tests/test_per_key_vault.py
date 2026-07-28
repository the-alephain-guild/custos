"""Failing-first tests for ``PerKeyVault`` — the reconciler's runtime read path.

Mirrors the ``vault verify`` contract at the reconciler read site:
- missing ``.enc`` file → clear error
- ``permission_scope`` violation → raise before returning
- sops subprocess failure → propagate up (no silent return)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import structlog.testing


def test_per_key_vault_missing_enc_file_clear_error(tmp_path: Path) -> None:
    from custos.core.per_key_vault import PerKeyVault

    vault = PerKeyVault(vault_dir=tmp_path / "vault", tenant_id="acme", initiator="runner-7")
    with pytest.raises(FileNotFoundError, match="arx-runner vault put"):
        vault.decrypt("not-there")


def test_per_key_vault_scope_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from custos.core.per_key_vault import PerKeyVault

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    enc = vault_dir / "binance-paper.enc"
    enc.write_bytes(b"CIPHER")
    import os

    os.chmod(enc, 0o600)

    payload = {
        "binance-paper": {
            "api_key": "pub",
            "api_secret": "sec",
            "permission_scope": "trade_full",
        }
    }
    run_mock = mock.MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["sops"],
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
    )
    monkeypatch.setattr("custos.core.per_key_vault.subprocess.run", run_mock)
    vault = PerKeyVault(vault_dir=vault_dir, tenant_id="acme", initiator="runner-7")
    with pytest.raises(ValueError, match="permission_scope"):
        vault.decrypt("binance-paper")


def test_per_key_vault_sops_fail_no_silent_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custos.core.per_key_vault import PerKeyVault

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    enc = vault_dir / "binance-paper.enc"
    enc.write_bytes(b"CIPHER")
    import os

    os.chmod(enc, 0o600)

    run_mock = mock.MagicMock(
        side_effect=subprocess.CalledProcessError(1, ["sops"], stderr=b"boom")
    )
    monkeypatch.setattr("custos.core.per_key_vault.subprocess.run", run_mock)
    vault = PerKeyVault(vault_dir=vault_dir, tenant_id="acme", initiator="runner-7")
    with pytest.raises(RuntimeError, match="sops"):
        vault.decrypt("binance-paper")


def test_per_key_vault_happy_path_emits_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path returns credential dict + emits CredentialDecrypted audit."""

    from custos.core.credential_vault import AuditEvent
    from custos.core.per_key_vault import PerKeyVault

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    enc = vault_dir / "binance-paper.enc"
    enc.write_bytes(b"CIPHER")
    import os

    os.chmod(enc, 0o600)

    payload = {
        "binance-paper": {
            "api_key": "pub",
            "api_secret": "sec",
            "permission_scope": "trade_no_withdraw",
        }
    }
    run_mock = mock.MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["sops"],
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
    )
    monkeypatch.setattr("custos.core.per_key_vault.subprocess.run", run_mock)
    vault = PerKeyVault(vault_dir=vault_dir, tenant_id="acme", initiator="runner-7")
    with structlog.testing.capture_logs() as records:
        cred = vault.decrypt("binance-paper")
    assert cred["permission_scope"] == "trade_no_withdraw"
    assert run_mock.call_args.args[0] == [
        "sops",
        "--decrypt",
        "--input-type",
        "json",
        "--output-type",
        "json",
        str(enc),
    ]
    audit = [r for r in records if r.get("audit_event") == AuditEvent.CREDENTIAL_DECRYPTED.value]
    assert len(audit) == 1
    assert audit[0]["credential_id"] == "binance-paper"
    assert audit[0]["tenant_id"] == "acme"


def test_cli_verify_and_runtime_share_json_decrypt_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custos.cli.subcommands import main
    from custos.core.per_key_vault import PerKeyVault

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    enc_path = vault_dir / "binance-paper.enc"
    enc_path.write_bytes(b"CIPHER")
    age_key_file = tmp_path / "age.key"

    import os

    os.chmod(enc_path, 0o600)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(age_key_file))
    payload = {
        "binance-paper": {
            "api_key": "pub",
            "api_secret": "sec",
            "permission_scope": "trade_no_withdraw",
        }
    }
    expected_command = [
        "sops",
        "--decrypt",
        "--input-type",
        "json",
        "--output-type",
        "json",
        str(enc_path),
    ]
    command_mock = mock.MagicMock(return_value=expected_command)
    monkeypatch.setattr(
        "custos.core.per_key_vault.sops_json_decrypt_command",
        command_mock,
        raising=False,
    )
    run_mock = mock.MagicMock(
        return_value=subprocess.CompletedProcess(
            args=expected_command,
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
    )
    monkeypatch.setattr("custos.core.per_key_vault.subprocess.run", run_mock)

    cli_exit_code = main(
        [
            "vault",
            "verify",
            "--key-id",
            "binance-paper",
            "--tenant-id",
            "acme",
            "--vault-dir",
            str(vault_dir),
        ]
    )
    runtime_credential = PerKeyVault(
        vault_dir=vault_dir,
        tenant_id="acme",
        initiator="runner-7",
    ).decrypt("binance-paper")

    assert cli_exit_code == 0
    assert runtime_credential["permission_scope"] == "trade_no_withdraw"
    assert command_mock.call_args_list == [mock.call(enc_path), mock.call(enc_path)]
    assert [call.args[0] for call in run_mock.call_args_list] == [
        expected_command,
        expected_command,
    ]
    assert [call.kwargs["env"]["SOPS_AGE_KEY_FILE"] for call in run_mock.call_args_list] == [
        str(age_key_file),
        str(age_key_file),
    ]


def test_per_key_vault_missing_sops_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from custos.core.per_key_vault import PerKeyVault

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    enc = vault_dir / "binance-paper.enc"
    enc.write_bytes(b"CIPHER")
    import os

    os.chmod(enc, 0o600)

    run_mock = mock.MagicMock(side_effect=FileNotFoundError("sops missing"))
    monkeypatch.setattr("custos.core.per_key_vault.subprocess.run", run_mock)
    vault = PerKeyVault(vault_dir=vault_dir, tenant_id="acme", initiator="runner-7")
    with pytest.raises(RuntimeError, match="sops CLI"):
        vault.decrypt("binance-paper")
