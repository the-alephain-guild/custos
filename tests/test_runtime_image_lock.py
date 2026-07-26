from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
RUNTIME_LOCK = ROOT / "docker" / "runtime-requirements.lock"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

PYTHON_BASE = (
    "python:3.12.13-slim-trixie@"
    "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)


def _requirement_blocks() -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in RUNTIME_LOCK.read_text().splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            if current:
                blocks.append(current)
            current = [line]
        elif current and line.strip() and not line.lstrip().startswith("#"):
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def test_runtime_image_uses_one_digest_pinned_python_base() -> None:
    text = DOCKERFILE.read_text()

    assert f"ARG PYTHON_BASE_IMAGE={PYTHON_BASE}" in text
    assert text.count("FROM ${PYTHON_BASE_IMAGE}") == 2
    assert "FROM python:" not in text


def test_runtime_lock_is_hash_complete_and_excludes_workspace_packages() -> None:
    blocks = _requirement_blocks()
    requirements = [block[0] for block in blocks]

    assert requirements
    assert all(any("--hash=sha256:" in line for line in block) for block in blocks)
    assert all(" @ file:" not in line for line in requirements)
    assert all(not line.startswith(("-e ", "--editable ")) for line in requirements)
    assert all(not line.startswith("custos-") for line in requirements)


def test_runtime_image_installs_only_locked_dependencies_and_exact_local_wheels() -> None:
    text = DOCKERFILE.read_text()

    assert "--require-hashes" in text
    assert "--requirement /tmp/runtime-requirements.lock" in text
    assert text.count("--no-deps") == 2
    assert "custos_strategy_toolkit-*.whl" in text
    assert "custos_strategy_toolkit_nautilus-*.whl" in text
    assert "custos_runner-*.whl" in text
    assert 'pip install --root-user-action=ignore "${wheel}[nautilus]"' not in text
    assert "pip check" in text


def test_release_builds_all_runtime_wheels_but_publishes_only_runner_to_pypi() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    build_steps = workflow["jobs"]["build-wheel"]["steps"]
    publish_steps = workflow["jobs"]["publish-pypi"]["steps"]
    build_script = "\n".join(str(step.get("run", "")) for step in build_steps)
    publish_script = "\n".join(str(step.get("run", "")) for step in publish_steps)

    assert "make dist" in build_script
    assert "custos_strategy_toolkit-*.whl" in publish_script
    assert "custos_strategy_toolkit_nautilus-*.whl" in publish_script
