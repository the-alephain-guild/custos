"""The authority layer must name the offline lane and bound it below live.

The offline lane accepts unsigned desired state, which the Trust rule otherwise
forbids outright. That exception is only safe while it stays mechanically bounded:
sandbox and testnet only, opt-in, and routed through one guard. These tests assert
the authority layer says so, that the gate enforcing it actually bites, and that
admitting the lane did not quietly relax the bans protecting the signed path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts/check-authority-docs.py"
MANDATORY_RULES = ROOT / ".claude/rules/mandatory-rules.md"
MANIFEST = ROOT / "authority-manifest.json"

SIGNED_PATH_BANS = (
    r"DeploymentMessage\.create",
    r"from\s+custos\.core\.deployment_reconciler\s+import",
    "publish_deployment_status",
    "standalone_nats",
)


@pytest.fixture
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_authority_docs", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _lane_manifest(**overrides: object) -> dict:
    lane = {
        "module_root": "src/custos/offline",
        "guard_module": "mode_guard.py",
        "guard_symbol": "refuse_live",
        "permitted_modes": ["sandbox", "testnet"],
        "excluded_mode": "live",
        "guarded_modules": ["spec.py", "reconciler.py"],
        "mode_agnostic_modules": ["__init__.py", "mode_guard.py", "transport.py"],
        "guarded_entry_points": ["src/custos/cli/subcommands/deployment.py"],
    }
    lane.update(overrides)
    return {"offline_lane": lane}


def _lane_tree(root: Path, *, modules: dict[str, str]) -> None:
    lane = root / "src/custos/offline"
    lane.mkdir(parents=True)
    for name, body in modules.items():
        (lane / name).write_text(body, encoding="utf-8")


def test_trust_rule_admits_the_offline_lane_and_still_refuses_live() -> None:
    text = MANDATORY_RULES.read_text(encoding="utf-8")

    assert "Live mode fails closed without signed promotion evidence." in text
    assert "Offline lane" in text
    assert "sandbox" in text and "testnet" in text
    assert "src/custos/offline/mode_guard.py" in text
    assert "--reconcile-strategy-id" in text


def test_manifest_declares_the_lane_as_non_live(manifest: dict) -> None:
    lane = manifest["offline_lane"]

    assert lane["excluded_mode"] == "live"
    assert sorted(lane["permitted_modes"]) == ["sandbox", "testnet"]
    assert lane["module_root"] == "src/custos/offline"
    assert lane["guard_module"] == "mode_guard.py"


def test_pre_existing_topology_bans_survive_the_amendment(manifest: dict) -> None:
    """Admitting a new lane must not weaken what kept the old one from returning."""

    banned = manifest["doc_drift"]["forbidden_regex"]
    for pattern in SIGNED_PATH_BANS:
        assert pattern in banned, f"authority amendment dropped the ban on {pattern!r}"


def test_gate_accepts_the_repository_as_it_stands(checker: ModuleType, manifest: dict) -> None:
    errors: list[str] = []
    checker.verify_offline_lane(manifest, errors, root=ROOT)
    assert errors == []


def test_gate_rejects_a_lane_module_that_bypasses_the_guard(
    checker: ModuleType, tmp_path: Path
) -> None:
    _lane_tree(
        tmp_path,
        modules={
            "mode_guard.py": "def refuse_live(mode):\n    ...\n",
            "spec.py": "class OfflineDeploymentSpec:\n    ...\n",
        },
    )
    errors: list[str] = []

    checker.verify_offline_lane(_lane_manifest(), errors, root=tmp_path)

    assert any("bypasses the mode guard" in error for error in errors), errors


def test_gate_rejects_an_unclassified_lane_module(checker: ModuleType, tmp_path: Path) -> None:
    """A new lane file must be classified as guarded or mode-agnostic, never assumed."""

    _lane_tree(
        tmp_path,
        modules={
            "mode_guard.py": "def refuse_live(mode):\n    ...\n",
            "smuggler.py": "def publish_anything():\n    ...\n",
        },
    )
    errors: list[str] = []

    checker.verify_offline_lane(_lane_manifest(), errors, root=tmp_path)

    assert any("unclassified" in error for error in errors), errors


def test_gate_rejects_a_lane_that_lost_its_guard(checker: ModuleType, tmp_path: Path) -> None:
    _lane_tree(tmp_path, modules={"spec.py": "refuse_live\n"})
    errors: list[str] = []

    checker.verify_offline_lane(_lane_manifest(), errors, root=tmp_path)

    assert any("without its mode guard" in error for error in errors), errors


def test_gate_rejects_a_lane_declared_for_live(checker: ModuleType, tmp_path: Path) -> None:
    errors: list[str] = []

    checker.verify_offline_lane(_lane_manifest(excluded_mode="none"), errors, root=tmp_path)

    assert any("live" in error for error in errors), errors
