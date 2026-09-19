"""OKX passphrases follow the encrypted credential and redaction boundary."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from custos.cli.subcommands import main
from custos.core.runtime_log_fact import RuntimeLogRedactor


def arguments(path):
    return [
        "vault",
        "put",
        "--key-id",
        "okx-demo",
        "--tenant-id",
        "acme",
        "--api-key",
        "key",
        "--api-secret",
        "secret",
        "--scope-digest",
        "a" * 64,
        "--age-recipient",
        "age1test",
        "--vault-dir",
        str(path),
        "--api-passphrase-env",
        "TEST_OKX_PASSPHRASE",
    ]


def test_passphrase_is_encrypted_via_stdin_and_never_printed(tmp_path, monkeypatch, capsys):
    phrase = "private-passphrase-sentinel"
    monkeypatch.setenv("TEST_OKX_PASSPHRASE", phrase)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=b"ciphertext", stderr=b""))
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run)
    assert main(arguments(tmp_path / "vault")) == 0
    assert json.loads(run.call_args.kwargs["input"])["okx-demo"]["api_passphrase"] == phrase
    assert phrase not in str(run.call_args.args)
    assert phrase not in str(capsys.readouterr())
    assert (tmp_path / "vault/okx-demo.enc").read_bytes() == b"ciphertext"


@pytest.mark.parametrize("value", [None, ""])
def test_missing_passphrase_never_creates_a_partial_credential(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TEST_OKX_PASSPHRASE", raising=False)
    else:
        monkeypatch.setenv("TEST_OKX_PASSPHRASE", value)
    run = Mock()
    monkeypatch.setattr("custos.cli.subcommands.vault.subprocess.run", run)
    assert main(arguments(tmp_path / "vault")) == 1
    run.assert_not_called()


def test_passphrase_redaction_covers_structured_and_text_messages():
    redactor = RuntimeLogRedactor()
    assert "private-sentinel" not in str(redactor.fields({"api_passphrase": "private-sentinel"}))
    assert "private-sentinel" not in redactor.message("api_passphrase=private-sentinel")
