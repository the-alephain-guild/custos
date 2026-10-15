"""The released Nautilus fork is consumed as exact platform wheels."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.0.0rc5+sodex.3"
EXPECTED_HASHES = {
    "7966215b87d84fae180084c138c5b6e4ea6b28291c4b57ed1ca5a508884ec483",
    "148179acb5c17d629c2c9c0ed52b010914c913fd73ab7d9517cc6cb2f726f3eb",
    "52affdf1980bafd3cc5aa9131890a46bf4abef3373c4dff612b16eb556d9ed02",
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
