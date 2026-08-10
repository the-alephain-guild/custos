"""CLI coverage for nonce-bound runner machine-principal enrollment."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from unittest import mock
from uuid import UUID

import pytest

from custos.cli.subcommands import enroll as enroll_command
from custos.cli.subcommands import main
from custos.core.machine_credential_vault import MachineCredentialError
from custos.core.runner_toml import RunnerToml

_BACKEND = "http://127.0.0.1:8000"
_RUNNER_ID = "22222222-2222-4222-8222-222222222222"
_CREDENTIAL_ID = "33333333-3333-4333-8333-333333333333"


def _authority_response(body: dict[str, object]) -> dict[str, object]:
    return {
        "tenant_id": body["tenant_id"],
        "runner_id": body["runner_id"],
        "machine_key_id": body["machine_key_id"],
        "credential_id": _CREDENTIAL_ID,
        "credential_version": 1,
        "credential_valid_until": "2027-07-14T00:00:00Z",
        "long_term_credential": "rkc1.test-machine-credential",
        "enrolled_at": "2026-07-14T00:00:00Z",
    }


def _base_argv(tmp_path: Path, *, token: str = "one-shot-token") -> list[str]:
    token_file = tmp_path / "enrollment-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    return [
        "enroll",
        "--token-file",
        str(token_file),
        "--backend",
        _BACKEND,
        "--tenant-id",
        "acme",
        "--runner-id",
        _RUNNER_ID,
        "--agent-version",
        "0.3.0",
        "--runner-toml",
        str(tmp_path / "arx" / "runner.toml"),
        "--machine-vault",
        str(tmp_path / "arx" / "vault" / "runner-machine.enc"),
        "--age-recipient",
        "age1test-recipient",
    ]


def _wire_enrollment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    post: mock.MagicMock | None = None,
) -> tuple[mock.MagicMock, list[tuple[object, str]]]:
    if post is None:
        post = mock.MagicMock(side_effect=lambda _backend, body: _authority_response(body))
    persisted: list[tuple[object, str]] = []

    def _persist(_vault, credential, *, age_recipient: str) -> None:
        persisted.append((credential, age_recipient))

    monkeypatch.setattr(enroll_command, "_post_enrollment", post)
    monkeypatch.setattr(enroll_command.MachineCredentialVault, "persist", _persist)
    return post, persisted


def test_enroll_persists_public_metadata_and_machine_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, persisted = _wire_enrollment(monkeypatch)

    assert main(_base_argv(tmp_path)) == 0

    runner_toml = tmp_path / "arx" / "runner.toml"
    loaded = RunnerToml.read(runner_toml)
    assert loaded.tenant_id == "acme"
    assert loaded.runner_id == _RUNNER_ID
    assert loaded.backend_url == _BACKEND
    assert loaded.credential_id == _CREDENTIAL_ID
    assert loaded.credential_version == 1
    assert loaded.machine_vault_path == str(tmp_path / "arx" / "vault" / "runner-machine.enc")
    assert stat.S_IMODE(os.stat(runner_toml).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(runner_toml.parent).st_mode) == 0o700
    assert post.call_count == 1
    assert len(persisted) == 1
    credential, recipient = persisted[0]
    assert credential.runner_id == UUID(_RUNNER_ID)
    assert credential.machine_credential.startswith("rkc1.")
    assert recipient == "age1test-recipient"


def test_enroll_sends_nonce_bound_proof_to_crucible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)

    assert main(_base_argv(tmp_path)) == 0

    backend, body = post.call_args.args
    assert backend == _BACKEND
    assert body["enrollment_token"] == "one-shot-token"
    assert body["tenant_id"] == "acme"
    assert body["runner_id"] == _RUNNER_ID
    assert body["agent_version"] == "0.3.0"
    UUID(str(body["challenge_nonce"]))
    assert str(body["machine_key_id"]).startswith("ed25519-")
    assert len(base64.b64decode(str(body["public_key_base64"]))) == 32
    assert len(base64.b64decode(str(body["proof_signature_base64"]))) == 64


def test_enroll_authority_failure_leaves_no_public_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    post = mock.MagicMock(side_effect=MachineCredentialError("authority unavailable"))
    _wire_enrollment(monkeypatch, post=post)

    assert main(_base_argv(tmp_path)) == 1
    assert not (tmp_path / "arx" / "runner.toml").exists()
    assert not (tmp_path / "arx" / "vault" / "runner-machine.enc").exists()
    assert "authority unavailable" in capsys.readouterr().err


def test_enroll_existing_state_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    runner_toml.parent.mkdir(parents=True)
    runner_toml.write_text("existing")
    post, _persisted = _wire_enrollment(monkeypatch)

    assert main(_base_argv(tmp_path)) == 1
    assert runner_toml.read_text() == "existing"
    post.assert_not_called()


@pytest.mark.parametrize("backend", ["file:///etc/passwd", "gopher://host", "host-only"])
def test_enroll_rejects_non_http_backend_before_handler(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)
    argv = _base_argv(tmp_path)
    argv[argv.index(_BACKEND)] = backend

    with pytest.raises(SystemExit):
        main(argv)
    post.assert_not_called()


def test_enroll_rejects_insecure_non_loopback_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)
    argv = _base_argv(tmp_path)
    argv[argv.index(_BACKEND)] = "http://crucible.internal:8000"

    assert main(argv) == 1
    post.assert_not_called()


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--tenant-id", "../evil"),
        ("--runner-id", "runner-7"),
    ],
)
def test_enroll_rejects_invalid_boundary_values(
    flag: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)
    argv = _base_argv(tmp_path)
    argv[argv.index(flag) + 1] = value

    with pytest.raises(SystemExit):
        main(argv)
    post.assert_not_called()


def test_enroll_never_prints_raw_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire_enrollment(monkeypatch)
    token = "super-secret-token"

    assert main(_base_argv(tmp_path, token=token)) == 0

    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_enroll_rejects_token_argv() -> None:
    with pytest.raises(SystemExit):
        main(["enroll", "--token", "secret"])


def test_enroll_rejects_overexposed_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)
    argv = _base_argv(tmp_path)
    token_file = Path(argv[argv.index("--token-file") + 1])
    os.chmod(token_file, 0o644)

    assert main(argv) == 1
    post.assert_not_called()


def test_enroll_rejects_symlink_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)
    argv = _base_argv(tmp_path)
    token_file = Path(argv[argv.index("--token-file") + 1])
    target = tmp_path / "real-token"
    token_file.replace(target)
    token_file.symlink_to(target)

    assert main(argv) == 1
    post.assert_not_called()


def test_enroll_rejects_multiline_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post, _persisted = _wire_enrollment(monkeypatch)
    argv = _base_argv(tmp_path)
    token_file = Path(argv[argv.index("--token-file") + 1])
    token_file.write_text("first\nsecond\n", encoding="utf-8")
    os.chmod(token_file, 0o600)

    assert main(argv) == 1
    post.assert_not_called()
