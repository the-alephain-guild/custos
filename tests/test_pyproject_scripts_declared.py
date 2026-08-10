"""Packaging contract: SEMVER, one CLI, production Sigstore, and LTS tools.

Plan 11 T8 landed the clean-break single console-script entry
(``arx-runner = custos.cli.subcommands:main``) and bumped the version to
0.2.0. Plan 12 T1 layers on top:

- base dependencies include Sigstore because production artifact admission uses it;
- ``[project.optional-dependencies].lts`` retains pytest-docker release tooling.
- ``[tool.hatch.build.hooks.custom]`` to give Plan 12 T8 a hook point for the
  ``SOURCE_DATE_EPOCH`` reproducible-build knob.

The assertions here also lock the Plan 11 legacy removal (no ``custos``
console-script) so a future regression in either plan is caught early.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_project_scripts_single_entry_arx_runner():
    data = _load()
    scripts = data["project"]["scripts"]
    assert scripts.get("arx-runner") == "custos.cli.subcommands:main", scripts
    # Plan 11 clean-break: legacy `custos` script must not reappear.
    assert "custos" not in scripts, f"legacy `custos` script re-appeared: {scripts}"


def test_project_version_is_0_3_0():
    data = _load()
    assert data["project"]["version"] == "0.3.0"


def test_project_runtime_declares_sigstore_and_lts_declares_pytest_docker():
    data = _load()
    extras = data["project"]["optional-dependencies"]
    assert "lts" in extras, f"missing `lts` extra; got extras={list(extras)}"
    joined = " ".join(extras["lts"])
    dependencies = " ".join(data["project"]["dependencies"])
    assert "sigstore>=3.0,<4.0" in dependencies, data["project"]["dependencies"]
    assert "sigstore" not in joined, extras["lts"]
    # pytest-docker for docker-marker test infra (Plan 12 T2).
    assert "pytest-docker" in joined, extras["lts"]


def test_hatch_custom_build_hook_declared():
    """Plan 12 T8 SOURCE_DATE_EPOCH knob requires a custom hatch build hook.

    We only verify the table exists here — the hook body (Python file under
    ``hatch_build.py``) is a T8 deliverable.
    """
    data = _load()
    hooks = data.get("tool", {}).get("hatch", {}).get("build", {}).get("hooks", {})
    assert "custom" in hooks, f"missing [tool.hatch.build.hooks.custom]; got {hooks}"


def test_pytest_markers_cover_release_test_gates():
    """Plan 12 introduces `docker` / `ci_only` / `slow` markers (T2/T3/T8).

    Without registration pytest emits ``PytestUnknownMarkWarning`` (which the
    stricter CI mode escalates to error). Registering here also documents each
    marker's intent for external contributors.
    """
    data = _load()
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    names = {m.split(":", 1)[0].strip() for m in markers}
    assert {"docker", "ci_only", "slow"} <= names, sorted(names)
