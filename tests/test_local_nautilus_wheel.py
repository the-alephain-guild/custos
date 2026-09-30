from __future__ import annotations

import hashlib
import importlib.metadata
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = runpy.run_path(str(ROOT / "docker/install-local-nautilus-wheel.py"))["install"]
VERSION = "2.0.0rc5+sodex.2"


@pytest.fixture
def installer(monkeypatch):
    calls = []
    monkeypatch.setattr(importlib.metadata, "version", lambda name: VERSION)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    return calls


def wheel(tmp_path: Path, version: str = VERSION) -> tuple[str, str]:
    name = f"nautilus_trader-{VERSION}-cp312-cp312-linux_aarch64.whl"
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"nautilus_trader-{VERSION}.dist-info/METADATA",
            f"Name: nautilus-trader\nVersion: {version}\n",
        )
    return name, hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_build_without_an_override_does_not_install_again(tmp_path, installer):
    INSTALL(tmp_path, "", "")
    assert installer == []


def test_local_wheel_is_installed_without_resolving_other_dependencies(tmp_path, installer):
    name, digest = wheel(tmp_path)
    INSTALL(tmp_path, name, digest)
    assert installer == [
        (
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--force-reinstall",
                str(tmp_path / name),
            ],
            {"check": True},
        ),
        ([sys.executable, "-m", "pip", "check"], {"check": True}),
    ]


def test_corrupt_local_wheel_is_rejected_before_installation(tmp_path, installer):
    name, digest = wheel(tmp_path)
    with (tmp_path / name).open("ab") as stream:
        stream.write(b"changed after selection")
    with pytest.raises(ValueError, match="digest does not match"):
        INSTALL(tmp_path, name, digest)
    assert installer == []


def test_local_wheel_cannot_change_the_locked_version(tmp_path, installer):
    name, digest = wheel(tmp_path, "2.0.0rc5+sodex.3")
    with pytest.raises(ValueError, match="preserve the locked engine version"):
        INSTALL(tmp_path, name, digest)
    assert installer == []


@pytest.mark.parametrize(
    ("name", "digest", "error"),
    [
        ("", "f" * 64, "requires a wheel name"),
        ("../nautilus_trader-a.whl", "f" * 64, "wheel basename"),
        ("other-a.whl", "f" * 64, "wheel basename"),
        ("nautilus_trader-a.whl", "", "requires a SHA-256"),
        ("nautilus_trader-a.whl", "z" * 64, "requires a SHA-256"),
    ],
)
def test_invalid_selection_is_rejected_before_reading_or_installing(
    tmp_path, installer, name, digest, error
):
    with pytest.raises(ValueError, match=error):
        INSTALL(tmp_path, name, digest)
    assert installer == []
