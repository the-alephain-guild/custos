"""Contract for the complete official Docker runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from custos.cli.subcommands import _build_parser

IMAGE = os.environ.get("CUSTOS_TEST_IMAGE", "custos-runner:test")
EXPECTED_REVISION = os.environ.get("CUSTOS_EXPECTED_REVISION")
_REVISION_FORMAT = '{{index .Config.Labels "org.opencontainers.image.revision"}}'


def _git_head() -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _require_image() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH")
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE, "--format", _REVISION_FORMAT],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0:
        pytest.skip(f"image {IMAGE} not present; run `make docker-build` first")

    # An image built from another revision answers questions about that
    # revision, not this one. A stale local image passed the old hard-coded
    # command matrix for weeks after the CLI it described had been replaced --
    # the image and the list were simply the same age. When a revision is
    # supplied explicitly, the dedicated label test fails instead of skipping.
    revision = inspect.stdout.strip()
    head = _git_head()
    if EXPECTED_REVISION is None and revision and head and revision != head:
        pytest.skip(
            f"image {IMAGE} was built from {revision[:12]}, HEAD is {head[:12]}; "
            "rebuild with `make docker-build` before trusting this contract"
        )


def _run_image(*args: str, entrypoint: str | None = None) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm"]
    if entrypoint is not None:
        command.extend(["--entrypoint", entrypoint])
    command.extend([IMAGE, *args])
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _command_matrix() -> list[tuple[str, ...]]:
    """Every real subcommand, plus one nested command and the bare entrypoint.

    Derived from the parser rather than listed here. A hard-coded list drifted
    once already: it required the image to expose `nats bootstrap` and
    `deployment publish` for months after both were removed from the CLI, and
    said nothing about the three commands that replaced them. Because these
    tests only run against a built image, nothing made the drift visible.
    """
    parser = _build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    names = sorted(actions[0].choices)
    return [("--help",), ("vault", "put", "--help")] + [(name, "--help") for name in names]


@pytest.mark.docker
@pytest.mark.parametrize("command", _command_matrix(), ids=lambda c: " ".join(c))
def test_official_image_exposes_command_matrix(command: tuple[str, ...]) -> None:
    _require_image()

    proc = _run_image(*command)

    assert proc.returncode == 0, (
        f"expected `docker run --rm {IMAGE} {' '.join(command)}` exit 0; "
        f"got rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


@pytest.mark.docker
def test_official_image_has_clean_entrypoint_cmd_and_healthcheck() -> None:
    _require_image()
    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}|{{json .Config.Healthcheck.Test}}",
            IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    entrypoint, command, healthcheck = inspect.stdout.strip().split("|")

    assert json.loads(entrypoint) == ["arx-runner"]
    assert json.loads(command) == ["start"]
    assert json.loads(healthcheck) == ["CMD", "arx-runner", "health"]


@pytest.mark.docker
def test_official_image_contains_nautilus_and_yaml() -> None:
    _require_image()

    proc = _run_image(
        "-c",
        "import nautilus_trader, yaml",
        entrypoint="python",
    )

    assert proc.returncode == 0, (
        f"official image must import NautilusTrader and PyYAML; "
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


@pytest.mark.docker
def test_official_image_contains_v030_distribution() -> None:
    _require_image()

    proc = _run_image(
        "-c",
        "from importlib.metadata import version; print(version('custos-runner'))",
        entrypoint="python",
    )

    assert proc.returncode == 0, (
        "official image must contain the custos-runner distribution; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == "0.3.0"


@pytest.mark.docker
def test_official_image_has_source_revision_label() -> None:
    _require_image()
    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    revision = inspect.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    if EXPECTED_REVISION is not None:
        assert revision == EXPECTED_REVISION


@pytest.mark.docker
@pytest.mark.parametrize("binary", ["sops", "age"])
def test_official_image_contains_vault_toolchain(binary: str) -> None:
    _require_image()

    proc = _run_image("--version", entrypoint=binary)

    assert proc.returncode == 0, (
        f"official image must provide `{binary} --version`; "
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
