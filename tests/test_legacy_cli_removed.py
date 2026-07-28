"""Contract tests for the clean-break removal of the legacy CLI.

Contract:
- ``python -m custos ...`` prints a stderr pointer at ``arx-runner start``
  and exits with code 2. No DeprecationWarning bridge, no partial flag
  delegation.
- After ``uv sync``, ``shutil.which("custos")`` returns ``None`` — only
  ``arx-runner`` is registered as a console script.
- All default paths in the subcommand modules resolve under ``~/.arx/``
  (no ``.custos`` substring anywhere).
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from custos.cli.subcommands import start


def test_python_m_custos_exits_nonzero_with_pointer(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "custos", "--tenant-id", "t", "--runner-id", "r"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}: {result.stderr}"
    assert "arx-runner start" in result.stderr
    # The pointer must be reachable by the user running the command: a help
    # flag on the shipped binary and a public URL, never a doc in another
    # repository they have no access to.
    assert "arx-runner --help" in result.stderr
    assert "custos.alephain.com" in result.stderr
    assert "team-self-hosted-lifecycle" not in result.stderr
    assert "DeprecationWarning" not in result.stderr
    assert "DeprecationWarning" not in result.stdout


def test_no_custos_console_script_registered() -> None:
    # After uv sync only arx-runner is registered. `shutil.which('custos')`
    # can be None even in dev because the console script is the only path
    # that would put it on PATH; verify the negative.
    assert shutil.which("custos") is None, "legacy `custos` entry point should be gone"


def test_default_paths_target_arx_namespace() -> None:
    for attr in (
        "DEFAULT_RUNNER_TOML",
        "DEFAULT_VAULT_DIR",
        "DEFAULT_READY_FILE",
        "DEFAULT_RUNNER_CAPABILITY",
        "DEFAULT_RUNNER_FACT_OUTBOX",
        "DEFAULT_CRUCIBLE_DOMAIN_PUBLIC_KEY",
    ):
        path = getattr(start, attr)
        assert ".custos" not in str(path), f"{attr} still references ~/.custos: {path}"
        assert ".arx" in str(path), f"{attr} missing ~/.arx: {path}"


def test_arx_runner_console_script_registered() -> None:
    # Positive control: after uv sync the arx-runner binary should be
    # discoverable. If it is not, T8 pyproject.toml update did not take.
    assert shutil.which("arx-runner") is not None, (
        "arx-runner console script missing — did pyproject.toml [project.scripts] update?"
    )
