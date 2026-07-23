from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from custos.cli.subcommands import main


def _put_argv(vault_dir: Path, permission_scope: str | None = None) -> list[str]:
    argv = [
        "vault",
        "put",
        "--key-id",
        "binance-paper",
        "--tenant-id",
        "acme",
        "--api-key",
        "public-api-key",
        "--scope-digest",
        "a" * 64,
        "--api-secret",
        "test-secret",
        "--age-recipient",
        "age1exampleexample",
        "--vault-dir",
        str(vault_dir),
    ]
    if permission_scope is not None:
        argv.extend(["--permission-scope", permission_scope])
    return argv


@pytest.fixture
def sops_encrypt(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    run_mock = mock.MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["sops"],
            returncode=0,
            stdout=b"ENCRYPTED",
            stderr=b"",
        )
    )
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    return run_mock


def _encrypted_payload(run_mock: mock.MagicMock) -> dict[str, dict[str, str]]:
    return json.loads(run_mock.call_args.kwargs["input"])


def test_default_permission_scope_is_trade_no_withdraw(
    tmp_path: Path,
    sops_encrypt: mock.MagicMock,
) -> None:
    assert main(_put_argv(tmp_path / "vault")) == 0
    payload = _encrypted_payload(sops_encrypt)
    assert payload["binance-paper"]["permission_scope"] == "trade_no_withdraw"


def test_explicit_trade_no_withdraw_ok(
    tmp_path: Path,
    sops_encrypt: mock.MagicMock,
) -> None:
    assert main(_put_argv(tmp_path / "vault", "trade_no_withdraw")) == 0


def test_illegal_permission_scope_rejected_by_choices(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(_put_argv(tmp_path / "vault", "withdraw"))
    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "invalid choice" in stderr
    assert "trade_no_withdraw" in stderr


def test_permission_scope_written_to_encrypted_payload(
    tmp_path: Path,
    sops_encrypt: mock.MagicMock,
) -> None:
    main(_put_argv(tmp_path / "vault", "trade_no_withdraw"))
    credential = _encrypted_payload(sops_encrypt)["binance-paper"]
    assert credential["permission_scope"] == "trade_no_withdraw"


def test_permission_scope_in_audit_event(
    tmp_path: Path,
    sops_encrypt: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="custos.credential_vault"):
        main(_put_argv(tmp_path / "vault", "trade_no_withdraw"))
    audit_record = next(
        record
        for record in caplog.records
        if getattr(record, "audit_event", None) == "CredentialEncrypted"
    )
    assert audit_record.permission_scope == "trade_no_withdraw"
