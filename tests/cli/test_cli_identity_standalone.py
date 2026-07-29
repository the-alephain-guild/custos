"""Generating a runner identity for a lane that answers to no authority.

Enrolment exists to have an authority *attest* an identity, not to create one —
the keypair was always generated locally. What the offline lane needs is the
creation without the attestation, and a document that cannot be mistaken for an
attested one.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from custos.cli.subcommands import main
from custos.core.machine_credential_vault import MachineCredentialVault
from custos.core.runner_toml import RunnerToml, is_attested

TENANT = "local"


def _argv(tmp_path: Path, **overrides: str) -> list[str]:
    argv = {
        "--tenant-id": TENANT,
        "--runner-toml": str(tmp_path / "arx" / "runner.toml"),
        "--machine-vault": str(tmp_path / "arx" / "vault" / "runner-machine.enc"),
        "--age-recipient": "age1test-recipient",
    }
    argv.update(overrides)
    flat = ["identity", "standalone"]
    for flag, value in argv.items():
        flat += [flag, value]
    return flat


@pytest.fixture
def offline_vault(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, str]]:
    """Record what would be encrypted, without requiring sops for every case."""

    persisted: list[tuple[object, str]] = []

    def _persist(vault: MachineCredentialVault, credential: object, *, age_recipient: str) -> None:
        persisted.append((credential, age_recipient))
        vault.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(vault.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(descriptor)

    def _load(vault: MachineCredentialVault) -> object:
        # Hand back exactly what was persisted, so a caller that reads the vault
        # gets past decryption and on to the check under test.
        return persisted[-1][0]

    monkeypatch.setattr(MachineCredentialVault, "persist", _persist)
    monkeypatch.setattr(MachineCredentialVault, "load", _load)
    return persisted


def test_the_generated_document_is_a_valid_v1_authority(
    tmp_path: Path, offline_vault: list[tuple[object, str]]
) -> None:
    assert main(_argv(tmp_path)) == 0

    record = RunnerToml.read(tmp_path / "arx" / "runner.toml")
    assert record.tenant_id == TENANT
    assert record.credential_version == 1


def test_the_generated_document_is_unattested(
    tmp_path: Path, offline_vault: list[tuple[object, str]]
) -> None:
    main(_argv(tmp_path))

    assert not is_attested(RunnerToml.read(tmp_path / "arx" / "runner.toml"))


def test_the_document_is_written_at_0600(
    tmp_path: Path, offline_vault: list[tuple[object, str]]
) -> None:
    main(_argv(tmp_path))

    mode = stat.S_IMODE(os.stat(tmp_path / "arx" / "runner.toml").st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_the_private_key_is_handed_to_the_vault_and_never_to_the_document(
    tmp_path: Path, offline_vault: list[tuple[object, str]]
) -> None:
    """Red line 0.1: the key material goes to sops+age, never to a readable file."""

    main(_argv(tmp_path))

    (credential, recipient) = offline_vault[0]
    assert recipient == "age1test-recipient"
    assert len(credential.private_key_bytes) == 32
    document = (tmp_path / "arx" / "runner.toml").read_text()
    assert credential.machine_credential not in document
    assert credential.machine_credential.startswith("rkc1.")


def test_the_key_material_stays_out_of_the_printed_summary(
    tmp_path: Path,
    offline_vault: list[tuple[object, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(_argv(tmp_path))

    (credential, _) = offline_vault[0]
    printed = capsys.readouterr().out
    assert credential.machine_credential not in printed
    assert credential.machine_key_id in printed


def test_generating_over_an_existing_identity_is_refused(
    tmp_path: Path, offline_vault: list[tuple[object, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The vault material is bound to the document; replacing one orphans the other."""

    assert main(_argv(tmp_path)) == 0
    first = (tmp_path / "arx" / "runner.toml").read_text()

    assert main(_argv(tmp_path)) == 1
    assert (tmp_path / "arx" / "runner.toml").read_text() == first
    assert "already exists" in capsys.readouterr().err


def test_no_backend_is_contacted(
    tmp_path: Path, offline_vault: list[tuple[object, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of this entry point is that there is nothing to contact."""

    import urllib.request

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the standalone path opened a network connection")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)

    assert main(_argv(tmp_path)) == 0


def test_an_age_recipient_is_required(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [flag for flag in _argv(tmp_path) if flag != "age1test-recipient"]
    argv.remove("--age-recipient")

    assert main(argv) == 1
    assert "age recipient" in capsys.readouterr().err


def test_a_standalone_identity_has_no_route_to_live(
    tmp_path: Path, offline_vault: list[tuple[object, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Two independent refusals, and live needs both to fall for it to happen."""

    from custos.offline.mode_guard import OfflineModeRefused, refuse_live

    main(_argv(tmp_path))
    capsys.readouterr()

    signed = main(
        [
            "start",
            "--runner-toml",
            str(tmp_path / "arx" / "runner.toml"),
            "--enabled-mode",
            "live",
        ]
    )

    assert signed == 1
    assert "backend_url" in capsys.readouterr().err
    with pytest.raises(OfflineModeRefused):
        refuse_live("live", source="deployment spec")


def _sops_available() -> bool:
    from shutil import which

    return which("sops") is not None and which("age-keygen") is not None


@pytest.mark.skipif(not _sops_available(), reason="sops and age-keygen are not installed")
def test_the_generated_pair_satisfies_the_binding_start_checks(tmp_path: Path) -> None:
    """The one test that proves startup would accept this, using real sops.

    ``start`` reads the document, loads the vault and calls ``assert_binding``.
    Mocking the vault proves the arguments; only a real round trip proves the two
    halves agree.
    """

    key_file = tmp_path / "age.key"
    generated = subprocess.run(
        ["age-keygen", "-o", str(key_file)], capture_output=True, text=True, check=True
    )
    recipient = generated.stderr.split("Public key: ")[-1].strip()
    os.environ["SOPS_AGE_KEY_FILE"] = str(key_file)
    try:
        assert main(_argv(tmp_path, **{"--age-recipient": recipient})) == 0

        record = RunnerToml.read(tmp_path / "arx" / "runner.toml")
        credential = MachineCredentialVault(Path(record.machine_vault_path)).load()
        credential.assert_binding(record)
    finally:
        os.environ.pop("SOPS_AGE_KEY_FILE", None)
