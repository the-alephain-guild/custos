"""Failing-first tests for ``arx-runner vault {put,verify,list}``.

Every sops subprocess call is mocked; the tests are contract tests, not
end-to-end sops fixtures.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from custos.cli.subcommands import main


def _put_argv(vault_dir: Path, key_id: str = "binance-paper") -> list[str]:
    return [
        "vault",
        "put",
        "--key-id",
        key_id,
        "--tenant-id",
        "acme",
        "--api-key",
        "public-api-key",
        "--scope-digest",
        "a" * 64,
        "--api-secret",
        "super-secret-value",
        "--age-recipient",
        "age1exampleexample",
        "--vault-dir",
        str(vault_dir),
    ]


def _fake_sops_encrypt(returned_ciphertext: bytes = b"ENCRYPTED-BYTES") -> mock.MagicMock:
    return mock.MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["sops"], returncode=0, stdout=returned_ciphertext, stderr=b""
        )
    )


def _fake_sops_decrypt(payload: dict) -> mock.MagicMock:
    return mock.MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["sops"],
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
    )


# ---- vault put -----------------------------------------------------------


def test_vault_put_happy_path_writes_enc_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_put_argv(vault_dir))
    assert exit_code == 0
    enc = vault_dir / "binance-paper.enc"
    assert enc.exists()
    assert enc.read_bytes() == b"ENCRYPTED-BYTES"
    assert stat.S_IMODE(os.stat(enc).st_mode) == 0o600


def test_vault_put_rejects_existing_keyid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "binance-paper.enc").write_bytes(b"previous")
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_put_argv(vault_dir))
    assert exit_code != 0
    assert (vault_dir / "binance-paper.enc").read_bytes() == b"previous"
    run_mock.assert_not_called()


def test_vault_put_missing_sops_binary_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    run_mock = mock.MagicMock(side_effect=FileNotFoundError("sops binary missing"))
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_put_argv(vault_dir))
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "sops" in err.lower()


def test_vault_put_rejects_keyid_traversal(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    with pytest.raises(SystemExit):
        main(_put_argv(vault_dir, key_id="../evil"))


def test_vault_put_writes_permission_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    main(_put_argv(vault_dir))
    call = run_mock.call_args
    payload = json.loads(call.kwargs["input"])
    assert payload["binance-paper"]["permission_scope"] == "trade_no_withdraw"
    assert payload["binance-paper"]["api_key"] == "public-api-key"
    assert payload["binance-paper"]["api_secret"] == "super-secret-value"
    assert payload["binance-paper"]["scope_digest"] == "a" * 64


def test_vault_put_never_logs_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    with caplog.at_level(logging.DEBUG):
        main(_put_argv(vault_dir))
    for rec in caplog.records:
        message = rec.getMessage()
        assert "super-secret-value" not in message
        extra_repr = " ".join(str(v) for k, v in vars(rec).items() if k != "msg")
        assert "super-secret-value" not in extra_repr

    # Structlog defence-in-depth (secondary sink): capture and re-scan.
    import structlog.testing

    with structlog.testing.capture_logs() as structlog_records:
        main(
            [
                "vault",
                "put",
                "--key-id",
                "binance-paper-2",
                "--tenant-id",
                "acme",
                "--api-key",
                "public-api-key",
                "--scope-digest",
                "a" * 64,
                "--api-secret",
                "super-secret-value",
                "--age-recipient",
                "age1x",
                "--vault-dir",
                str(vault_dir),
            ]
        )
    for rec in structlog_records:
        assert "super-secret-value" not in json.dumps(rec)


def test_vault_put_emits_credential_encrypted_audit_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The put path must emit an audit-writer event mirroring the decrypt
    signal at credential_vault.py:64-81, but never with plaintext."""
    from custos.core.credential_vault import AuditEvent

    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    with caplog.at_level(logging.INFO, logger="custos.credential_vault"):
        main(_put_argv(vault_dir))
    audit_records = [
        r
        for r in caplog.records
        if getattr(r, "audit_event", None) == AuditEvent.CREDENTIAL_ENCRYPTED.value
    ]
    assert len(audit_records) == 1
    rec = audit_records[0]
    assert rec.key_id == "binance-paper"
    assert rec.tenant_id == "acme"
    for v in vars(rec).values():
        assert "super-secret-value" not in str(v)


def test_vault_put_secret_stdin_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Secret via stdin — cross-check with monkeypatched stdin."""
    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    monkeypatch.setattr("sys.stdin", mock.MagicMock())
    import sys

    sys.stdin.readline.return_value = "stdin-secret\n"
    argv = [
        "vault",
        "put",
        "--key-id",
        "kraken-live",
        "--tenant-id",
        "acme",
        "--api-key",
        "public-api-key",
        "--scope-digest",
        "a" * 64,
        "--api-secret-stdin",
        "--age-recipient",
        "age1x",
        "--vault-dir",
        str(vault_dir),
    ]
    exit_code = main(argv)
    assert exit_code == 0
    call = run_mock.call_args
    payload = json.loads(call.kwargs["input"])
    assert payload["kraken-live"]["api_secret"] == "stdin-secret"


def test_vault_put_secret_env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    monkeypatch.setenv("MY_ENV_SECRET", "env-secret-value")
    argv = [
        "vault",
        "put",
        "--key-id",
        "kraken-live",
        "--tenant-id",
        "acme",
        "--api-key",
        "public-api-key",
        "--scope-digest",
        "a" * 64,
        "--api-secret-env",
        "MY_ENV_SECRET",
        "--age-recipient",
        "age1x",
        "--vault-dir",
        str(vault_dir),
    ]
    exit_code = main(argv)
    assert exit_code == 0
    call = run_mock.call_args
    payload = json.loads(call.kwargs["input"])
    assert payload["kraken-live"]["api_secret"] == "env-secret-value"


def test_vault_put_prefers_stdin_and_warns_on_cmdline_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--api-secret <value>` is demo-only and must warn about ps aux exposure."""
    vault_dir = tmp_path / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_put_argv(vault_dir))
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "ps aux" in err or "warning" in err.lower()


# ---- vault verify --------------------------------------------------------


def _seed_enc(vault_dir: Path, key_id: str = "binance-paper") -> Path:
    vault_dir.mkdir(mode=0o700, exist_ok=True)
    enc = vault_dir / f"{key_id}.enc"
    enc.write_bytes(b"CIPHERTEXT")
    os.chmod(enc, 0o600)
    return enc


def _verify_argv(vault_dir: Path, key_id: str = "binance-paper") -> list[str]:
    return [
        "vault",
        "verify",
        "--key-id",
        key_id,
        "--tenant-id",
        "acme",
        "--vault-dir",
        str(vault_dir),
    ]


def test_vault_verify_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    _seed_enc(vault_dir)
    payload = {
        "binance-paper": {
            "api_key": "pub",
            "api_secret": "sec",
            "permission_scope": "trade_no_withdraw",
        }
    }
    run_mock = _fake_sops_decrypt(payload)
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_verify_argv(vault_dir))
    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


def test_vault_verify_uses_explicit_json_sops_types_for_enc_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_dir = tmp_path / "vault"
    enc_path = _seed_enc(vault_dir)
    age_key_file = tmp_path / "age.key"
    payload = {
        "binance-paper": {
            "api_key": "pub",
            "api_secret": "sec",
            "permission_scope": "trade_no_withdraw",
        }
    }
    run_mock = _fake_sops_decrypt(payload)
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)

    assert main([*_verify_argv(vault_dir), "--age-key-file", str(age_key_file)]) == 0
    call = run_mock.call_args
    assert call.args[0] == [
        "sops",
        "--decrypt",
        "--input-type",
        "json",
        "--output-type",
        "json",
        str(enc_path),
    ]
    assert call.kwargs["env"]["SOPS_AGE_KEY_FILE"] == str(age_key_file)
    assert call.kwargs["capture_output"] is True
    assert call.kwargs["check"] is True
    assert call.kwargs["timeout"] == 30


def test_vault_verify_sops_fail_no_silent_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    _seed_enc(vault_dir)
    run_mock = mock.MagicMock(
        side_effect=subprocess.CalledProcessError(
            returncode=1, cmd=["sops"], stderr=b"decrypt failed"
        )
    )
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_verify_argv(vault_dir))
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "OK" not in captured.out
    assert "decrypt" in captured.err.lower() or "fail" in captured.err.lower()


def test_vault_verify_rejects_scope_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    _seed_enc(vault_dir)
    payload = {
        "binance-paper": {
            "api_key": "pub",
            "api_secret": "sec",
            "permission_scope": "trade_full",
        }
    }
    run_mock = _fake_sops_decrypt(payload)
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_verify_argv(vault_dir))
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "scope" in (captured.out + captured.err).lower()


def test_vault_verify_missing_file_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    run_mock = mock.MagicMock()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_verify_argv(vault_dir, key_id="not-there"))
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "vault put" in err
    run_mock.assert_not_called()


def test_vault_verify_rejects_world_readable_enc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    enc = _seed_enc(vault_dir)
    os.chmod(enc, 0o644)
    run_mock = mock.MagicMock()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    exit_code = main(_verify_argv(vault_dir))
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "0600" in err
    run_mock.assert_not_called()


# ---- vault list ----------------------------------------------------------


def test_vault_list_shows_all_key_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    for name in ("binance-paper", "kraken-live", "okx-testnet"):
        p = vault_dir / f"{name}.enc"
        p.write_bytes(b"x")
        os.chmod(p, 0o600)
    exit_code = main(["vault", "list", "--vault-dir", str(vault_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    for name in ("binance-paper", "kraken-live", "okx-testnet"):
        assert name in out


def test_vault_list_empty_vault_prints_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    exit_code = main(["vault", "list", "--vault-dir", str(vault_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "arx-runner vault put" in out or "no key" in out.lower()


def test_vault_list_rejects_world_readable_enc(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(mode=0o700)
    p = vault_dir / "leaky.enc"
    p.write_bytes(b"x")
    os.chmod(p, 0o644)
    exit_code = main(["vault", "list", "--vault-dir", str(vault_dir)])
    assert exit_code == 0  # list is diagnostic; warns but lists
    captured = capsys.readouterr()
    assert "leaky" in captured.out
    assert "0644" in captured.err or "world" in captured.err.lower()


def test_vault_put_reuses_arx_dir_0700(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After `vault put`, ~/.arx/ mode is exactly 0o700 (shared with runner.toml)."""
    arx_dir = tmp_path / ".arx"
    vault_dir = arx_dir / "vault"
    run_mock = _fake_sops_encrypt()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run_mock)
    main(_put_argv(vault_dir))
    assert stat.S_IMODE(os.stat(arx_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(vault_dir).st_mode) == 0o700
