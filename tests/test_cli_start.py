"""Fail-closed tests for ``arx-runner start`` machine authority loading."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from custos.cli.subcommands import main
from custos.cli.subcommands import start as start_command
from custos.core.runner_toml import RunnerToml

_RUNNER_ID = "22222222-2222-4222-8222-222222222222"


def _seed_runner_toml(target: Path) -> Path:
    machine_vault = target.parent / "vault" / "runner-machine.enc"
    RunnerToml.write(
        target,
        RunnerToml(
            tenant_id="acme",
            runner_id=_RUNNER_ID,
            backend_url="https://crucible.example",
            credential_id="33333333-3333-4333-8333-333333333333",
            credential_version=1,
            credential_valid_until="2027-07-14T00:00:00Z",
            machine_key_id="ed25519-test-key",
            machine_vault_path=str(machine_vault),
            enrolled_at="2026-07-14T00:00:00Z",
        ),
    )
    return machine_vault


def _run_start(
    argv: list[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, mock.MagicMock, mock.MagicMock]:
    run_daemon = mock.MagicMock()
    credential = mock.MagicMock(spec=["assert_binding"])

    async def _fake(*args, **kwargs):
        run_daemon(*args, **kwargs)
        return 0

    monkeypatch.setattr(
        start_command.MachineCredentialVault, "load", mock.MagicMock(return_value=credential)
    )
    monkeypatch.setattr("custos.cli._daemon.run_daemon", _fake)
    return main(["start", "--enabled-mode", "sandbox", *argv]), run_daemon, credential


def _production_state_arguments(root: Path) -> list[str]:
    return [
        "--production-state-root",
        str(root),
        "--runner-toml",
        str(root / "runner.toml"),
        "--vault-dir",
        str(root / "vault"),
        "--nats-transport-vault-dir",
        str(root / "vault" / "runner-nats-transport"),
        "--ready-file",
        str(root / "state" / "runner-ready.json"),
        "--runner-capability",
        str(root / "runner-capability.json"),
        "--runner-fact-outbox",
        str(root / "state" / "runner-fact-outbox.db"),
        "--development-artifact-root",
        str(root / "artifacts" / "development"),
        "--artifact-quarantine-dir",
        str(root / "artifacts" / "quarantine"),
        "--artifact-activation-dir",
        str(root / "artifacts" / "activation"),
        "--artifact-cache-dir",
        str(root / "artifacts" / "cache"),
    ]


def test_start_loads_bound_machine_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    machine_vault = _seed_runner_toml(runner_toml)

    exit_code, run_daemon, credential = _run_start(
        ["--runner-toml", str(runner_toml)], monkeypatch=monkeypatch
    )

    assert exit_code == 0
    namespace = run_daemon.call_args.args[0]
    assert namespace.tenant_id == "acme"
    assert namespace.runner_id == _RUNNER_ID
    assert namespace.machine_vault == machine_vault
    credential.assert_binding.assert_called_once()


def test_start_missing_runner_toml_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, run_daemon, _credential = _run_start(
        ["--runner-toml", str(tmp_path / "missing.toml")], monkeypatch=monkeypatch
    )
    assert exit_code == 1
    assert "arx-runner enroll" in capsys.readouterr().err
    run_daemon.assert_not_called()


def test_start_partial_runner_toml_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "runner.toml"
    runner_toml.write_text('tenant_id = "acme"\nrunner_id = "r"\n')
    os.chmod(runner_toml, 0o600)

    exit_code, run_daemon, _credential = _run_start(
        ["--runner-toml", str(runner_toml)], monkeypatch=monkeypatch
    )

    assert exit_code == 1
    run_daemon.assert_not_called()


def test_start_rejects_world_readable_runner_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner_toml = tmp_path / "runner.toml"
    runner_toml.write_text('tenant_id = "acme"\n')
    os.chmod(runner_toml, 0o644)

    exit_code, run_daemon, _credential = _run_start(
        ["--runner-toml", str(runner_toml)], monkeypatch=monkeypatch
    )

    assert exit_code == 1
    assert "0600" in capsys.readouterr().err
    run_daemon.assert_not_called()


def test_start_preserves_engine_and_fact_outbox_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    _seed_runner_toml(runner_toml)
    outbox = tmp_path / "runner-facts.db"
    vault_dir = tmp_path / "venue-vault"

    exit_code, run_daemon, _credential = _run_start(
        [
            "--runner-toml",
            str(runner_toml),
            "--engine",
            "nautilus",
            "--runner-fact-outbox",
            str(outbox),
            "--vault-dir",
            str(vault_dir),
        ],
        monkeypatch=monkeypatch,
    )

    assert exit_code == 0
    namespace = run_daemon.call_args.args[0]
    assert namespace.engine == "nautilus"
    assert namespace.runner_fact_outbox == outbox
    assert namespace.vault_dir == vault_dir


def test_start_preserves_explicit_development_local_nats_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    _seed_runner_toml(runner_toml)

    exit_code, run_daemon, _credential = _run_start(
        [
            "--runner-toml",
            str(runner_toml),
            "--development-local-nats-url",
            "nats://127.0.0.1:4222",
        ],
        monkeypatch=monkeypatch,
    )

    assert exit_code == 0
    assert run_daemon.call_args.args[0].development_local_nats_url == "nats://127.0.0.1:4222"


def test_start_rejects_machine_vault_override_binding_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    _seed_runner_toml(runner_toml)

    exit_code, run_daemon, _credential = _run_start(
        [
            "--runner-toml",
            str(runner_toml),
            "--machine-vault",
            str(tmp_path / "other.enc"),
        ],
        monkeypatch=monkeypatch,
    )

    assert exit_code == 1
    run_daemon.assert_not_called()


def test_engine_nautilus_selects_real_host() -> None:
    from argparse import Namespace

    from custos.cli._daemon import _build_host
    from custos.engines.nautilus.host import NtTradingNodeHost

    host = _build_host(Namespace(engine="nautilus", tenant_id="acme", runner_id=_RUNNER_ID))
    assert isinstance(host, NtTradingNodeHost)


def test_engine_noop_selects_noop_host() -> None:
    from argparse import Namespace

    from custos.cli._daemon import _build_host
    from custos.engines.nautilus.host import SandboxSimulationHost

    host = _build_host(Namespace(engine="sandbox-sim", tenant_id="acme", runner_id=_RUNNER_ID))
    assert isinstance(host, SandboxSimulationHost)


def test_use_nt_host_flag_is_removed() -> None:
    with pytest.raises(SystemExit):
        main(["start", "--enabled-mode", "sandbox", "--use-nt-host"])


def test_start_preserves_ready_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    _seed_runner_toml(runner_toml)
    ready_file = tmp_path / "runner-ready.json"

    exit_code, run_daemon, _credential = _run_start(
        ["--runner-toml", str(runner_toml), "--ready-file", str(ready_file)],
        monkeypatch=monkeypatch,
    )

    assert exit_code == 0
    assert run_daemon.call_args.args[0].ready_file == ready_file


def test_start_default_paths_target_arx_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_toml = tmp_path / "arx" / "runner.toml"
    _seed_runner_toml(runner_toml)

    exit_code, run_daemon, _credential = _run_start(
        ["--runner-toml", str(runner_toml)], monkeypatch=monkeypatch
    )

    assert exit_code == 0
    namespace = run_daemon.call_args.args[0]
    for path in (
        namespace.vault_dir,
        namespace.ready_file,
        namespace.runner_capability,
        namespace.runner_fact_outbox,
        namespace.crucible_domain_public_key,
        namespace.artifact_cache_dir,
    ):
        assert ".custos" not in str(path)
        assert ".arx" in str(path)


def test_start_binds_every_mutable_production_path_to_one_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "arx"
    _seed_runner_toml(root / "runner.toml")

    exit_code, run_daemon, _credential = _run_start(
        _production_state_arguments(root), monkeypatch=monkeypatch
    )

    assert exit_code == 0
    namespace = run_daemon.call_args.args[0]
    assert namespace.production_state_root == root.resolve()
    for path in (
        namespace.machine_vault,
        namespace.vault_dir,
        namespace.nats_transport_vault_dir,
        namespace.ready_file,
        namespace.runner_capability,
        namespace.runner_fact_outbox,
        namespace.development_artifact_root,
        namespace.artifact_quarantine_dir,
        namespace.artifact_activation_dir,
        namespace.artifact_cache_dir,
    ):
        assert root.resolve() in Path(path).resolve().parents


def test_start_rejects_split_production_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "arx"
    _seed_runner_toml(root / "runner.toml")
    argv = _production_state_arguments(root)
    argv[argv.index("--runner-fact-outbox") + 1] = str(tmp_path / "split.db")

    exit_code, run_daemon, _credential = _run_start(argv, monkeypatch=monkeypatch)

    assert exit_code == 1
    run_daemon.assert_not_called()
