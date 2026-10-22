"""The image's toolkit identity comes from the installed bytes, checked against the rc."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "custos_strategy_toolkit-0.1.0rcX-py3-none-any.whl"
ADAPTER = "custos_strategy_toolkit_nautilus-0.1.0rcX-py3-none-any.whl"


def _load() -> ModuleType:
    path = ROOT / "docker" / "write-toolkit-identity.py"
    spec = importlib.util.spec_from_file_location("write_toolkit_identity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


writer = _load()


def _rc_version(_distribution: str) -> str:
    return "0.1.0rcX"


def _wheels(directory: Path, *, registered_digests: dict[str, str] | None = None) -> dict[str, str]:
    digests = {}
    for key, name in (("base_contracts_wheel", BASE), ("nautilus_wheel", ADAPTER)):
        content = f"{key} wheel bytes".encode()
        (directory / name).write_bytes(content)
        digests[key] = hashlib.sha256(content).hexdigest()
    registered = registered_digests or digests
    authority = {
        "schema_version": 1,
        "candidate_version": "0.1.0rcX",
        "manifest_digest": "sha256:" + "a" * 64,
        "receipt_sha256": "b" * 64,
        "source_commit": "c" * 40,
        "receipt_path": "docs/authority/receipts/x.json",
        "authority_commit": "d" * 40,
        "wheels": {
            "base_contracts_wheel": {
                "filename": BASE,
                "sha256": registered["base_contracts_wheel"],
            },
            "nautilus_wheel": {"filename": ADAPTER, "sha256": registered["nautilus_wheel"]},
        },
    }
    (directory / writer.AUTHORITY_FILE).write_text(json.dumps(authority), encoding="utf-8")
    return digests


def test_a_source_build_gets_no_identity(tmp_path: Path) -> None:
    wheels, out = tmp_path / "wheels", tmp_path / "out"
    wheels.mkdir()
    (wheels / BASE).write_bytes(b"source build")

    assert writer.write_identity(wheels, out, _rc_version) is None
    assert not (out / writer.IDENTITY_FILE).exists()


def test_the_identity_records_the_digests_of_the_installed_wheels(tmp_path: Path) -> None:
    wheels, out = tmp_path / "wheels", tmp_path / "out"
    wheels.mkdir()
    digests = _wheels(wheels)

    identity = writer.write_identity(wheels, out, _rc_version)

    written = json.loads((out / writer.IDENTITY_FILE).read_text(encoding="utf-8"))
    assert written == identity
    assert written["candidate_version"] == "0.1.0rcX"
    assert written["base_contracts_wheel_sha256"] == digests["base_contracts_wheel"]
    assert written["nautilus_wheel_sha256"] == digests["nautilus_wheel"]
    assert stat.S_IMODE((out / writer.IDENTITY_FILE).stat().st_mode) == 0o444


def test_an_installed_wheel_that_is_not_the_rcs_bytes_fails_the_build(tmp_path: Path) -> None:
    wheels, out = tmp_path / "wheels", tmp_path / "out"
    wheels.mkdir()
    _wheels(wheels)
    (wheels / ADAPTER).write_bytes(b"other bytes under the rc's name")

    with pytest.raises(writer.ToolkitIdentityError, match="differs from the rc's receipt digest"):
        writer.write_identity(wheels, out, _rc_version)
    assert not (out / writer.IDENTITY_FILE).exists()


def test_an_authority_file_naming_other_digests_fails_rather_than_being_copied(
    tmp_path: Path,
) -> None:
    wheels, out = tmp_path / "wheels", tmp_path / "out"
    wheels.mkdir()
    _wheels(
        wheels, registered_digests={"base_contracts_wheel": "0" * 64, "nautilus_wheel": "1" * 64}
    )

    with pytest.raises(writer.ToolkitIdentityError, match="receipt digest"):
        writer.write_identity(wheels, out, _rc_version)


def test_an_installed_distribution_reporting_another_version_fails_the_build(
    tmp_path: Path,
) -> None:
    wheels, out = tmp_path / "wheels", tmp_path / "out"
    wheels.mkdir()
    _wheels(wheels)

    with pytest.raises(writer.ToolkitIdentityError, match="installed as 0.1.0"):
        writer.write_identity(wheels, out, lambda _distribution: "0.1.0")


def test_a_wheel_under_another_name_fails_the_build(tmp_path: Path) -> None:
    wheels, out = tmp_path / "wheels", tmp_path / "out"
    wheels.mkdir()
    _wheels(wheels)
    (wheels / BASE).rename(wheels / "custos_strategy_toolkit-0.1.0-py3-none-any.whl")

    with pytest.raises(writer.ToolkitIdentityError, match="not the rc's wheel"):
        writer.write_identity(wheels, out, _rc_version)
