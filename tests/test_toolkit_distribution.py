from __future__ import annotations

import email
import json
import os
import runpy
import subprocess
import sys
import tomllib
import zipfile
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_PROJECT = ROOT / "packages/custos-strategy-toolkit/pyproject.toml"
NAUTILUS_PROJECT = ROOT / "packages/custos-strategy-toolkit-nautilus/pyproject.toml"
BASE_SOURCE = (
    ROOT / "packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_execution.py"
)


def _toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _build_wheel(package: str, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["uv", "build", "--package", package, "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    wheels = sorted(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _metadata(wheel: Path) -> Message:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1
        return email.message_from_bytes(archive.read(metadata_names[0]))


def test_uv_workspace_declares_both_toolkit_distributions() -> None:
    root = _toml(ROOT / "pyproject.toml")
    members = set(root["tool"]["uv"]["workspace"]["members"])
    assert members == {
        "packages/custos-strategy-toolkit",
        "packages/custos-strategy-toolkit-nautilus",
    }


def test_distribution_metadata_has_disjoint_python_baselines_and_exact_runtime() -> None:
    base = _toml(BASE_PROJECT)
    nautilus = _toml(NAUTILUS_PROJECT)

    assert base["project"]["name"] == "custos-strategy-toolkit"
    assert base["project"]["version"] == "0.1.0"
    assert base["project"]["requires-python"] == ">=3.11"
    assert base["tool"]["mypy"]["strict"] is True

    assert nautilus["project"]["name"] == "custos-strategy-toolkit-nautilus"
    assert nautilus["project"]["version"] == "0.1.0"
    assert nautilus["project"]["requires-python"] == ">=3.12,<3.13"
    assert nautilus["tool"]["mypy"]["strict"] is True
    dependencies = nautilus["project"]["dependencies"]
    assert "custos-strategy-toolkit==0.1.0" in dependencies
    assert "nautilus-trader==1.230.0" in dependencies
    assert all("python_version" not in dependency for dependency in dependencies)


def test_contract_implementation_has_one_canonical_source() -> None:
    canonical = BASE_SOURCE.read_text(encoding="utf-8")

    assert "class StrategyExecutionContextV1" in canonical


def test_historical_v1_evidence_identifies_its_source_without_pinning_current_bytes() -> None:
    receipt_path = (
        ROOT / "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    producer = receipt["producer"]
    assert producer["source_path"] == BASE_SOURCE.relative_to(ROOT).as_posix()
    assert len(producer["source_sha256"]) == 64
    assert set(producer["source_sha256"]) <= set("0123456789abcdef")
    assert receipt["status"] == "CANONICAL_V1_PENDING_CONSUMER_RECEIPTS"
    assert receipt["production_ready"] is False
    assert "predecessor" not in receipt
    assert "historical_task_2_source" not in receipt

    generator = runpy.run_path(
        str(ROOT / "scripts/generate_strategy_contract_assets.py"),
        run_name="strategy_contract_asset_generator_test",
    )
    assert generator["HISTORICAL_CONTRACT_EVIDENCE_PATHS"] == {
        "docs/authority/strategy-contract-assets-v1.json",
        "docs/authority/receipts/custos-strategy-contract-v1-producer-receipt.json",
        "docs/authority/crucible-runner-command-consumer-assets-v1.json",
        "docs/authority/receipts/custos-crucible-runner-command-v1-consumer-receipt.json",
    }

    checker = runpy.run_path(
        str(ROOT / "scripts/check-authority-docs.py"),
        run_name="authority_docs_checker_test",
    )
    errors: list[str] = []
    checker["validate_historical_asset_record"](
        {"path": "recorded/revision/source.py", "sha256": "a" * 64, "size_bytes": 7},
        errors,
        label="runner command consumer asset",
    )
    assert errors == []


def test_lightweight_base_import_does_not_load_nautilus_or_mutate_sys_path() -> None:
    probe = (
        "import sys; before=tuple(sys.path); "
        "import custos_toolkit.contracts.strategy_execution; "
        "assert tuple(sys.path)==before; assert 'nautilus_trader' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", probe], cwd=ROOT, check=True)


def test_built_wheels_are_namespace_isolated_and_have_exact_metadata(tmp_path: Path) -> None:
    base_wheel = _build_wheel("custos-strategy-toolkit", tmp_path / "base")
    nautilus_wheel = _build_wheel("custos-strategy-toolkit-nautilus", tmp_path / "nautilus")

    with zipfile.ZipFile(base_wheel) as archive:
        base_names = archive.namelist()
    with zipfile.ZipFile(nautilus_wheel) as archive:
        nautilus_names = archive.namelist()

    assert any(name == "custos_toolkit/py.typed" for name in base_names)
    assert any(name == "custos_toolkit_nautilus/py.typed" for name in nautilus_names)
    for names in (base_names, nautilus_names):
        top_levels = {name.split("/", 1)[0] for name in names}
        assert "shared" not in top_levels
        assert "pandas_ta" not in top_levels

    base_metadata = _metadata(base_wheel)
    nautilus_metadata = _metadata(nautilus_wheel)
    assert base_metadata["Requires-Python"] == ">=3.11"
    assert nautilus_metadata["Requires-Python"] == "<3.13,>=3.12"
    requires_dist = nautilus_metadata.get_all("Requires-Dist") or []
    assert "custos-strategy-toolkit==0.1.0" in requires_dist
    assert "nautilus-trader==1.230.0" in requires_dist
    assert all("python_version" not in dependency for dependency in requires_dist)


def test_base_wheel_imports_on_python311_without_nautilus_or_path_mutation(
    tmp_path: Path,
) -> None:
    wheel = _build_wheel("custos-strategy-toolkit", tmp_path / "wheel")
    find = subprocess.run(
        ["uv", "--no-python-downloads", "python", "find", "3.11"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert find.returncode == 0, find.stderr
    python311 = find.stdout.strip()
    target = tmp_path / "python311-base"
    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            python311,
            "--target",
            str(target),
            str(wheel),
        ],
        cwd=ROOT,
        env={**os.environ, "UV_NO_PYTHON_DOWNLOADS": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    probe = (
        "import sys; before=tuple(sys.path); "
        "import custos_toolkit.contracts.strategy_execution; "
        "assert tuple(sys.path)==before; assert 'nautilus_trader' not in sys.modules"
    )
    subprocess.run(
        [python311, "-c", probe],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(target)},
        check=True,
    )


def test_nautilus_wheel_install_fails_on_python311(tmp_path: Path) -> None:
    wheel = _build_wheel("custos-strategy-toolkit-nautilus", tmp_path / "wheel")
    find = subprocess.run(
        ["uv", "--no-python-downloads", "python", "find", "3.11"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert find.returncode == 0, find.stderr
    python311 = find.stdout.strip()
    target = tmp_path / "python311-target"
    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            python311,
            "--target",
            str(target),
            "--no-deps",
            str(wheel),
        ],
        cwd=ROOT,
        env={**os.environ, "UV_NO_PYTHON_DOWNLOADS": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode != 0
    assert "requires-python" in install.stderr.lower() or "python" in install.stderr.lower()
