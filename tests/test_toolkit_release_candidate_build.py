from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.toolkit_rc_build import (
    ToolkitRcBuildCandidate,
    build_reproducible_toolkit_rc_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATE_EPOCH = 1_704_067_200


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def candidate(tmp_path_factory: pytest.TempPathFactory) -> ToolkitRcBuildCandidate:
    return build_reproducible_toolkit_rc_candidate(
        repository_root=ROOT,
        source_commit=_head(),
        source_date_epoch=SOURCE_DATE_EPOCH,
        candidate_version="0.1.0rc1",
        output_root=tmp_path_factory.mktemp("toolkit-release-build") / "toolkit-rc-candidate",
    )


def test_real_toolkit_rc_wheels_are_byte_reproducible(
    candidate: ToolkitRcBuildCandidate,
) -> None:
    result = candidate
    assert result.source_commit == _head()
    assert result.source_date_epoch == SOURCE_DATE_EPOCH
    assert result.candidate_version == "0.1.0rc1"
    assert set(result.first.wheels) == {
        "custos-strategy-toolkit",
        "custos-strategy-toolkit-nautilus",
    }
    for distribution, first in result.first.wheels.items():
        second = result.second.wheels[distribution]
        assert first.sha256 == second.sha256
        assert first.path.read_bytes() == second.path.read_bytes()

    manifest = json.loads(result.manifest_input_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "BUILD_CANDIDATE_ONLY"
    assert manifest["reproducible"] is True
    assert manifest["ready_receipt_created"] is False
    assert manifest["registry_accessed"] is False


def test_real_wheels_enforce_rc_metadata_and_ephemeral_sbom_inputs(
    candidate: ToolkitRcBuildCandidate,
) -> None:
    base = candidate.first.wheels["custos-strategy-toolkit"]
    nautilus = candidate.first.wheels["custos-strategy-toolkit-nautilus"]

    assert base.requires_python == ">=3.11"
    assert nautilus.requires_python == "<3.13,>=3.12"
    assert {
        "custos-strategy-toolkit==0.1.0rc1",
        "nautilus-trader==2.0.0rc5+sodex.1",
    }.issubset(nautilus.requires_dist)

    for wheel in (base, nautilus):
        assert wheel.version == "0.1.0rc1"
        assert wheel.coordinate == (
            f"toolkit-rc://custos/{wheel.distribution_name}/0.1.0rc1/"
            f"{wheel.path.name}@sha256:{wheel.sha256}"
        )
        assert {"shared", "pandas_ta"}.isdisjoint(wheel.top_level_modules)
        assert all(
            " @ " not in dependency
            and "file:" not in dependency.lower()
            and "../" not in dependency
            for dependency in wheel.requires_dist
        )
        sbom_input = json.loads(wheel.sbom_input_path.read_text(encoding="utf-8"))
        assert sbom_input["schema_version"] == ("alephain.custos.toolkit-rc-sbom-input.v1")
        assert sbom_input["wheel_sha256"] == wheel.sha256
        assert sbom_input["source_commit"] == candidate.source_commit
        assert sbom_input["source_date_epoch"] == SOURCE_DATE_EPOCH
        assert sbom_input["files"]

    manifest = json.loads(candidate.manifest_input_path.read_text(encoding="utf-8"))
    assert manifest["strategy_release_bom_created"] is False
    assert manifest["future_evidence_required"] == [
        "Sigstore attestation bundles",
        "immutable toolkit RC receipt",
    ]


def test_candidate_coordinate_cannot_be_mutable_or_overwritten(
    candidate: ToolkitRcBuildCandidate, tmp_path: Path
) -> None:
    with pytest.raises(FileExistsError, match="immutable"):
        build_reproducible_toolkit_rc_candidate(
            repository_root=ROOT,
            source_commit=candidate.source_commit,
            source_date_epoch=SOURCE_DATE_EPOCH,
            candidate_version="0.1.0rc1",
            output_root=candidate.manifest_input_path.parent,
        )
    with pytest.raises(ValueError, match="0.1.0rcN"):
        build_reproducible_toolkit_rc_candidate(
            repository_root=ROOT,
            source_commit=candidate.source_commit,
            source_date_epoch=SOURCE_DATE_EPOCH,
            candidate_version="0.1.0",
            output_root=tmp_path / "mutable-version",
        )


def test_dedicated_workflow_has_no_publish_or_ready_authority() -> None:
    workflow = ROOT / ".github/workflows/toolkit-rc-reproducibility.yml"
    assert workflow.is_file()
    source = workflow.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in source
    assert "python scripts/toolkit_rc_build.py" in source
    assert "$RUNNER_TEMP/toolkit-rc-${{ inputs.candidate_version }}" in source
    assert "SOURCE_DATE_EPOCH: '1704067200'" in source
    assert "upload-artifact" not in source
    assert "release.yml" not in source
    assert "publish" not in source.lower()
    assert "READY_TOOLKIT_RC" not in source

    help_result = subprocess.run(
        [sys.executable, "scripts/toolkit_rc_build.py", "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--candidate-version" in help_result.stdout
