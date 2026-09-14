"""The released Nautilus fork is consumed as exact platform wheels."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.0.0rc5+sodex.1"
EXPECTED_HASHES = {
    "0a06c389f63e4c7d9825c2b68e8a225db2db6e92561bcf623eb6d81b2693af97",
    "d1e56ae74547712441eea7d3a46783655a59489a5c4f9dbd297db7b9f1aff754",
    "b5d0f1e483c534e39dfa8215a6816381baf3b873cfb686dea141dc3fbc9849d5",
}


def test_three_released_platform_wheels_replace_the_git_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sources = project["tool"]["uv"]["sources"]["nautilus-trader"]

    assert isinstance(sources, list)
    assert len(sources) == 3
    assert {source["marker"] for source in sources} == {
        "sys_platform == 'darwin' and platform_machine == 'arm64'",
        "sys_platform == 'linux' and platform_machine == 'aarch64'",
        "sys_platform == 'linux' and platform_machine == 'x86_64'",
    }
    assert all(source["url"].startswith("https://github.com/") for source in sources)
    assert all(VERSION in source["url"].replace("%2B", "+") for source in sources)


def test_lock_binds_all_three_release_asset_hashes() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    entries = [item for item in lock["package"] if item["name"] == "nautilus-trader"]

    assert len(entries) == 3
    assert {
        wheel["hash"].removeprefix("sha256:") for item in entries for wheel in item["wheels"]
    } == (EXPECTED_HASHES)
    assert all("url" in item["source"] and "git" not in item["source"] for item in entries)


def test_git_source_build_scaffolding_is_retired() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    runtime_lock = (ROOT / "docker/runtime-requirements.lock").read_text(encoding="utf-8")

    assert "--no-emit-package nautilus-trader" not in makefile
    assert "nautilus-wheel:" not in makefile
    assert not (ROOT / "docker/nautilus-wheel.dockerfile").exists()
    assert not (ROOT / "scripts/nautilus_source_pin.py").exists()
    assert "nautilus-trader @ https://github.com/" in runtime_lock
    assert all(f"--hash=sha256:{digest}" in runtime_lock for digest in EXPECTED_HASHES)
