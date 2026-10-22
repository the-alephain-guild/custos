"""Contract for the complete official Docker runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
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
    assert json.loads(command) == [
        "start",
        "--production-state-root",
        "/home/custos/.arx",
    ]
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
    assert proc.stdout.strip() == "0.8.0"


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


def _locked_engine_version() -> str:
    """The NautilusTrader version uv.lock resolves to.

    Read straight out of the lock rather than through any helper, so that it
    keeps answering across a change of source form: the version is recorded
    the same way whether the engine resolves from a git commit or from a
    published wheel.
    """
    lock = tomllib.loads((Path(__file__).resolve().parent.parent / "uv.lock").read_text())
    versions = {p["version"] for p in lock["package"] if p["name"] == "nautilus-trader"}
    assert len(versions) == 1, f"uv.lock pins more than one engine version: {sorted(versions)}"
    return versions.pop()


@pytest.mark.docker
def test_the_installed_engine_is_the_version_the_lock_pins() -> None:
    """An image outlives the tree that produced it.

    Nothing else in the image disagrees with a lock that has since moved on,
    so without this the only way to learn which engine an image carries is to
    unpack it -- which is how a stale artifact once kept a contract green for
    weeks (historical-lessons C7).
    """
    _require_image()

    proc = _run_image(
        "-c",
        "from importlib.metadata import version; print(version('nautilus_trader'))",
        entrypoint="python",
    )

    assert proc.returncode == 0, (
        f"official image must carry an installed NautilusTrader; "
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == _locked_engine_version()


@pytest.mark.docker
@pytest.mark.parametrize("binary", ["sops", "age"])
def test_official_image_contains_vault_toolchain(binary: str) -> None:
    _require_image()

    proc = _run_image("--version", entrypoint=binary)

    assert proc.returncode == 0, (
        f"official image must provide `{binary} --version`; "
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


# Which kind of image this run is checking: "1" for an image built from release
# inputs (make test-docker-release), "0" for a source build (make test-docker).
# Unset, the toolkit identity tests skip rather than guess.
EXPECT_TOOLKIT_IDENTITY = os.environ.get("CUSTOS_EXPECT_TOOLKIT_IDENTITY")
TOOLKIT_IDENTITY = "/usr/local/share/custos/toolkit-identity.json"


def _authorized_rc() -> dict[str, object]:
    root = Path(__file__).resolve().parent.parent
    authority = json.loads((root / "docs/authority/ecosystem-authority.json").read_text())
    release = authority["toolkit_rc_release"]
    receipt = json.loads((root / release["receipt_path"]).read_text())
    wheels = {
        layer["role"]: layer["digest"].removeprefix("sha256:")
        for layer in receipt["publication_receipt"]["layers"]
        if layer["role"] in ("base_contracts_wheel.wheel", "nautilus_wheel.wheel")
    }
    # The identity is the receipt's; the record only locates it and pins its bytes.
    return {
        "candidate_version": receipt["candidate_version"],
        "source_commit": receipt["source_commit"],
        "manifest_digest": receipt["publication_receipt"]["manifest_digest"],
        "receipt_sha256": release["receipt_sha256"],
        "base": wheels["base_contracts_wheel.wheel"],
        "nautilus": wheels["nautilus_wheel.wheel"],
    }


@pytest.mark.docker
def test_a_release_image_carries_the_authorized_rc_and_its_identity() -> None:
    if EXPECT_TOOLKIT_IDENTITY != "1":
        pytest.skip("set CUSTOS_EXPECT_TOOLKIT_IDENTITY=1 for an image built from release inputs")
    _require_image()
    rc = _authorized_rc()

    identity = _run_image(TOOLKIT_IDENTITY, entrypoint="cat")
    assert identity.returncode == 0, f"release image has no toolkit identity: {identity.stderr!r}"
    document = json.loads(identity.stdout)
    assert document["candidate_version"] == rc["candidate_version"]
    assert document["manifest_digest"] == rc["manifest_digest"]
    assert document["source_commit"] == rc["source_commit"]
    assert document["receipt_sha256"] == rc["receipt_sha256"]
    assert document["base_contracts_wheel_sha256"] == rc["base"]
    assert document["nautilus_wheel_sha256"] == rc["nautilus"]

    mode = _run_image("-c", "%a", TOOLKIT_IDENTITY, entrypoint="stat")
    assert mode.stdout.strip() == "444"

    versions = _run_image(
        "-c",
        "from importlib.metadata import version; "
        "print(version('custos-strategy-toolkit'), version('custos-strategy-toolkit-nautilus'))",
        entrypoint="python",
    )
    assert versions.stdout.split() == [rc["candidate_version"], rc["candidate_version"]]


@pytest.mark.docker
def test_a_source_built_image_has_no_toolkit_identity() -> None:
    if EXPECT_TOOLKIT_IDENTITY != "0":
        pytest.skip("set CUSTOS_EXPECT_TOOLKIT_IDENTITY=0 for an image built from source")
    _require_image()

    probe = _run_image("-e", TOOLKIT_IDENTITY, entrypoint="test")

    assert probe.returncode != 0, "a source-built image must not claim a toolkit identity"
