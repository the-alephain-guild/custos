"""The release inputs: the runner from source pinned to the rc, the toolkit as the rc."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/authority/receipts/custos-toolkit-rc10-authority-v1.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_dist", ROOT / "scripts/release_dist.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _load()


def _wheel(path: Path, name: str, version: str, requires: list[str]) -> None:
    metadata = "\n".join(
        ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        + [f"Requires-Dist: {requirement}" for requirement in requires]
    )
    distribution = name.replace("-", "_")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{distribution}-{version}.dist-info/METADATA", metadata + "\n")


def test_both_toolkit_pins_are_rewritten_to_the_rc() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    rewritten = release.rewrite_toolkit_pins(pyproject, "0.1.0rc10")

    assert '"custos-strategy-toolkit==0.1.0rc10"' in rewritten
    assert '"custos-strategy-toolkit-nautilus==0.1.0rc10"' in rewritten
    assert '"custos-strategy-toolkit==0.1.0"' not in rewritten
    assert '"custos-strategy-toolkit-nautilus==0.1.0"' not in rewritten


def test_a_missing_pin_is_refused_rather_than_skipped() -> None:
    with pytest.raises(release.ReleaseDistError, match="exactly once"):
        release.rewrite_toolkit_pins(
            'dependencies = ["custos-strategy-toolkit==0.1.0"]', "0.1.0rc10"
        )


def test_the_receipt_names_both_wheels_with_their_digests() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))

    wheels = release.receipt_wheels(receipt)

    assert wheels["base_contracts_wheel"]["filename"] == (
        "custos_strategy_toolkit-0.1.0rc10-py3-none-any.whl"
    )
    assert wheels["nautilus_wheel"]["filename"] == (
        "custos_strategy_toolkit_nautilus-0.1.0rc10-py3-none-any.whl"
    )
    assert all(len(wheel["sha256"]) == 64 for wheel in wheels.values())


def _fetched(tmp_path: Path) -> dict[str, dict[str, Any]]:
    wheels: dict[str, dict[str, Any]] = {}
    for key, name in (
        ("base_contracts_wheel", "custos_strategy_toolkit-0.1.0rcX-py3-none-any.whl"),
        ("nautilus_wheel", "custos_strategy_toolkit_nautilus-0.1.0rcX-py3-none-any.whl"),
    ):
        content = f"{key} bytes".encode()
        (tmp_path / name).write_bytes(content)
        wheels[key] = {
            "filename": name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    return wheels


def test_fetched_wheels_matching_the_receipt_are_accepted(tmp_path: Path) -> None:
    release.verify_fetched(tmp_path, _fetched(tmp_path))


def test_a_fetched_wheel_whose_bytes_differ_from_the_receipt_is_refused(tmp_path: Path) -> None:
    wheels = _fetched(tmp_path)
    path = tmp_path / wheels["nautilus_wheel"]["filename"]
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(release.ReleaseDistError, match="differs from the receipt digest"):
        release.verify_fetched(tmp_path, wheels)


def _three_wheels(tmp_path: Path, runner_pin: str) -> None:
    _wheel(
        tmp_path / "custos_runner-0.8.0-py3-none-any.whl",
        "custos-runner",
        "0.8.0",
        [
            f"custos-strategy-toolkit=={runner_pin}",
            f"custos-strategy-toolkit-nautilus=={runner_pin}; extra == 'nautilus'",
            "pyyaml>=6",
        ],
    )
    _wheel(
        tmp_path / "custos_strategy_toolkit_nautilus-0.1.0rc10-py3-none-any.whl",
        "custos-strategy-toolkit-nautilus",
        "0.1.0rc10",
        ["custos-strategy-toolkit==0.1.0rc10"],
    )


def test_runner_and_rc_wheels_naming_the_same_toolkit_agree(tmp_path: Path) -> None:
    _three_wheels(tmp_path, "0.1.0rc10")

    release.check_pins_agree(tmp_path, "0.1.0rc10")


def test_a_runner_still_pinned_to_the_source_version_is_refused(tmp_path: Path) -> None:
    _three_wheels(tmp_path, "0.1.0")

    with pytest.raises(release.ReleaseDistError, match="not the rc"):
        release.check_pins_agree(tmp_path, "0.1.0rc10")


def test_the_release_build_pins_fetches_checks_and_records_the_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole sequence with the build and the registry replaced, nothing else."""

    out = tmp_path / "dist"
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    registered = release.receipt_wheels(receipt)
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["uv", "build"]:
            pyproject = (Path(kwargs["cwd"]) / "pyproject.toml").read_text(encoding="utf-8")
            pin = "0.1.0rc10" if '"custos-strategy-toolkit==0.1.0rc10"' in pyproject else "0.1.0"
            _wheel(
                out / "custos_runner-0.8.0-py3-none-any.whl",
                "custos-runner",
                "0.8.0",
                [f"custos-strategy-toolkit=={pin}", f"custos-strategy-toolkit-nautilus=={pin}"],
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        if "custos_strategy_publisher.toolkit_fetch" in args:
            # The registry client would write the receipt's bytes; the fixture can
            # only write bytes of the receipt's size, so patch the digest it expects.
            for key, wheel in registered.items():
                content = b"\0" * wheel["size"]
                if key == "nautilus_wheel":
                    _wheel(
                        out / wheel["filename"],
                        "custos-strategy-toolkit-nautilus",
                        "0.1.0rc10",
                        ["custos-strategy-toolkit==0.1.0rc10"],
                    )
                    content = (out / wheel["filename"]).read_bytes()
                else:
                    (out / wheel["filename"]).write_bytes(content)
                wheel["sha256"] = hashlib.sha256(content).hexdigest()
                wheel["size"] = len(content)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, **kwargs)

    monkeypatch.setattr(release, "receipt_wheels", lambda _receipt: registered)
    # The toolkit comparison has its own tests; here the checkout is taken to be the
    # rc so this test does not depend on whether the branch is ahead of it.
    pin = release._load_pin_check()
    rc = {**pin.authorized_rc(ROOT), "toolkit_matches_rc": True, "differing_files": []}
    monkeypatch.setattr(release, "_load_pin_check", lambda: SimpleNamespace(check=lambda _root: rc))
    monkeypatch.setenv("TOOLKIT_REGISTRY_ACTOR", "test")
    monkeypatch.setenv("TOOLKIT_REGISTRY_TOKEN", "test")
    document = release.build_release_dist(ROOT, out, runner)

    assert document["candidate_version"] == "0.1.0rc10"
    written = json.loads((out / release.AUTHORITY_FILE).read_text(encoding="utf-8"))
    assert written == document
    assert written["wheels"]["nautilus_wheel"]["sha256"] == registered["nautilus_wheel"]["sha256"]
    assert any(call[:2] == ["uv", "build"] for call in calls)
    assert any("custos_strategy_publisher.toolkit_fetch" in call for call in calls)
