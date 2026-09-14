#!/usr/bin/env python3
"""Offline reproducible-build seam for Custos toolkit RC candidates."""

from __future__ import annotations

import argparse
import email
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Final

PACKAGE_PATHS: Final = {
    "custos-strategy-toolkit": Path("packages/custos-strategy-toolkit"),
    "custos-strategy-toolkit-nautilus": Path("packages/custos-strategy-toolkit-nautilus"),
}
CANDIDATE_VERSION_RE: Final = re.compile(r"^0\.1\.0rc[1-9][0-9]*$")
FORBIDDEN_TOP_LEVELS: Final = frozenset({"shared", "pandas_ta"})


@dataclass(frozen=True, slots=True)
class WheelBuildEvidence:
    distribution_name: str
    version: str
    path: Path
    sha256: str
    coordinate: str
    requires_python: str
    requires_dist: tuple[str, ...]
    top_level_modules: tuple[str, ...]
    sbom_input_path: Path
    sbom_input_sha256: str


@dataclass(frozen=True, slots=True)
class BuildPassEvidence:
    name: str
    wheels: dict[str, WheelBuildEvidence]


@dataclass(frozen=True, slots=True)
class ToolkitRcBuildCandidate:
    source_commit: str
    source_date_epoch: int
    candidate_version: str
    first: BuildPassEvidence
    second: BuildPassEvidence
    manifest_input_path: Path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _resolve_clean_source_commit(repository_root: Path, source_commit: str) -> str:
    resolved = _run(
        ["git", "rev-parse", "--verify", f"{source_commit}^{{commit}}"],
        cwd=repository_root,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError("source_commit must resolve to an exact Git commit")
    package_arguments = [path.as_posix() for path in PACKAGE_PATHS.values()]
    tracked = subprocess.run(
        ["git", "diff", "--quiet", resolved, "--", *package_arguments],
        cwd=repository_root,
        check=False,
    )
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *package_arguments],
        cwd=repository_root,
    ).stdout.strip()
    if tracked.returncode != 0 or untracked:
        raise ValueError("toolkit package sources must exactly match the clean source commit")
    return resolved


def _materialize_commit(repository_root: Path, source_commit: str, destination: Path) -> None:
    destination.mkdir(parents=True)
    archive = _run(
        [
            "git",
            "archive",
            "--format=tar",
            source_commit,
            *[path.as_posix() for path in PACKAGE_PATHS.values()],
        ],
        cwd=repository_root,
        text=False,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        destination_root = destination.resolve()
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination_root):
                raise ValueError("git archive contains a path outside the staging root")
        tar.extractall(destination, filter="data")


def _replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise ValueError(f"release metadata transform expected exactly one {old!r} in {path}")
    path.write_text(source.replace(old, new), encoding="utf-8")


def _stage_candidate_version(stage_root: Path, candidate_version: str) -> None:
    for package_path in PACKAGE_PATHS.values():
        _replace_once(
            stage_root / package_path / "pyproject.toml",
            'version = "0.1.0"',
            f'version = "{candidate_version}"',
        )
    _replace_once(
        stage_root / PACKAGE_PATHS["custos-strategy-toolkit-nautilus"] / "pyproject.toml",
        '"custos-strategy-toolkit==0.1.0"',
        f'"custos-strategy-toolkit=={candidate_version}"',
    )


def _wheel_metadata(archive: zipfile.ZipFile) -> Message:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(names) != 1:
        raise ValueError("toolkit wheel must contain exactly one METADATA file")
    return email.message_from_bytes(archive.read(names[0]))


def _validate_dependencies(distribution: str, dependencies: tuple[str, ...], version: str) -> None:
    for dependency in dependencies:
        lowered = dependency.lower()
        if (
            " @ " in lowered
            or "file:" in lowered
            or "path:" in lowered
            or lowered.startswith(("-e ", "--editable "))
            or "../" in lowered
        ):
            raise ValueError("toolkit RC wheel contains an editable or path dependency")
    if distribution == "custos-strategy-toolkit-nautilus":
        required = {
            f"custos-strategy-toolkit=={version}",
            "nautilus-trader==2.0.0rc5+sodex.1",
        }
        if not required.issubset(dependencies):
            raise ValueError("Nautilus wheel dependency policy differs")


def _write_sbom_input(
    *,
    wheel: Path,
    distribution: str,
    version: str,
    source_commit: str,
    source_date_epoch: int,
    output_path: Path,
) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    wheel_bytes = wheel.read_bytes()
    wheel_digest = _sha256_bytes(wheel_bytes)
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        metadata = _wheel_metadata(archive)
        metadata_name = metadata.get("Name")
        metadata_version = metadata.get("Version")
        requires_python = metadata.get("Requires-Python") or ""
        dependencies = tuple(metadata.get_all("Requires-Dist") or ())
        if metadata_name != distribution or metadata_version != version:
            raise ValueError("toolkit RC wheel name/version metadata differs")
        expected_python = ">=3.11" if distribution == "custos-strategy-toolkit" else "<3.13,>=3.12"
        if requires_python != expected_python:
            raise ValueError("toolkit RC wheel Python policy differs")
        _validate_dependencies(distribution, dependencies, version)
        top_levels = tuple(
            sorted(
                {
                    name.split("/", 1)[0]
                    for name in archive.namelist()
                    if "/" in name and ".dist-info" not in name.split("/", 1)[0]
                }
            )
        )
        if FORBIDDEN_TOP_LEVELS.intersection(top_levels):
            raise ValueError("toolkit RC wheel exposes a forbidden legacy top-level module")
        files = [
            {
                "path": name,
                "sha256": _sha256_bytes(archive.read(name)),
                "size_bytes": archive.getinfo(name).file_size,
            }
            for name in sorted(archive.namelist())
            if not name.endswith("/")
        ]
    document = {
        "schema_version": "alephain.custos.toolkit-rc-sbom-input.v1",
        "distribution_name": distribution,
        "version": version,
        "wheel_sha256": wheel_digest,
        "source_commit": source_commit,
        "source_date_epoch": source_date_epoch,
        "files": files,
    }
    content = _json_bytes(document)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return requires_python, dependencies, _sha256_bytes(content), top_levels


def _build_pass(
    *,
    name: str,
    repository_root: Path,
    source_commit: str,
    source_date_epoch: int,
    candidate_version: str,
    output_root: Path,
) -> BuildPassEvidence:
    pass_root = output_root / name
    stage_root = pass_root / "source"
    _materialize_commit(repository_root, source_commit, stage_root)
    _stage_candidate_version(stage_root, candidate_version)
    environment = {
        **os.environ,
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "PYTHONHASHSEED": "0",
        "UV_OFFLINE": "1",
        "UV_NO_PYTHON_DOWNLOADS": "1",
    }
    wheels: dict[str, WheelBuildEvidence] = {}
    for distribution, package_path in PACKAGE_PATHS.items():
        dist = pass_root / "dist" / distribution
        dist.mkdir(parents=True)
        _run(
            [
                "uv",
                "build",
                "--offline",
                "--no-config",
                "--no-create-gitignore",
                "--wheel",
                "--out-dir",
                str(dist),
                str(stage_root / package_path),
            ],
            cwd=stage_root,
            env=environment,
        )
        built = sorted(dist.glob("*.whl"))
        if len(built) != 1:
            raise ValueError(f"expected one {distribution} wheel, found {len(built)}")
        wheel = built[0]
        digest = _sha256_bytes(wheel.read_bytes())
        coordinate = (
            f"toolkit-rc://custos/{distribution}/{candidate_version}/{wheel.name}@sha256:{digest}"
        )
        sbom_path = pass_root / "evidence" / f"{distribution}-sbom-input.json"
        requires_python, dependencies, sbom_digest, top_levels = _write_sbom_input(
            wheel=wheel,
            distribution=distribution,
            version=candidate_version,
            source_commit=source_commit,
            source_date_epoch=source_date_epoch,
            output_path=sbom_path,
        )
        wheels[distribution] = WheelBuildEvidence(
            distribution_name=distribution,
            version=candidate_version,
            path=wheel,
            sha256=digest,
            coordinate=coordinate,
            requires_python=requires_python,
            requires_dist=dependencies,
            top_level_modules=top_levels,
            sbom_input_path=sbom_path,
            sbom_input_sha256=sbom_digest,
        )
    return BuildPassEvidence(name=name, wheels=wheels)


def _wheel_difference(first: Path, second: Path) -> str:
    with zipfile.ZipFile(first) as left, zipfile.ZipFile(second) as right:
        left_members = {name: _sha256_bytes(left.read(name)) for name in sorted(left.namelist())}
        right_members = {name: _sha256_bytes(right.read(name)) for name in sorted(right.namelist())}
    changed = sorted(
        name
        for name in set(left_members) | set(right_members)
        if left_members.get(name) != right_members.get(name)
    )
    if changed:
        return f"member content differs: {', '.join(changed)}"
    return "ZIP container metadata differs while member bytes are identical"


def _wheel_document(wheel: WheelBuildEvidence) -> dict[str, object]:
    return {
        "distribution_name": wheel.distribution_name,
        "version": wheel.version,
        "filename": wheel.path.name,
        "coordinate": wheel.coordinate,
        "sha256": wheel.sha256,
        "size_bytes": wheel.path.stat().st_size,
        "requires_python": wheel.requires_python,
        "requires_dist": list(wheel.requires_dist),
        "top_level_modules": list(wheel.top_level_modules),
        "sbom_input": {
            "path": wheel.sbom_input_path.name,
            "sha256": wheel.sbom_input_sha256,
        },
    }


def build_reproducible_toolkit_rc_candidate(
    *,
    repository_root: Path,
    source_commit: str,
    source_date_epoch: int,
    candidate_version: str,
    output_root: Path,
) -> ToolkitRcBuildCandidate:
    """Build and compare two offline toolkit RC wheel sets without publishing them."""

    repository_root = repository_root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise FileExistsError("toolkit RC output root is immutable and must not exist")
    if not CANDIDATE_VERSION_RE.fullmatch(candidate_version):
        raise ValueError("candidate_version must be an immutable 0.1.0rcN version")
    if source_date_epoch < 315_532_800:
        raise ValueError("SOURCE_DATE_EPOCH must be at or after 1980-01-01")
    resolved_commit = _resolve_clean_source_commit(repository_root, source_commit)
    output_root.mkdir(parents=True)
    first = _build_pass(
        name="build-1",
        repository_root=repository_root,
        source_commit=resolved_commit,
        source_date_epoch=source_date_epoch,
        candidate_version=candidate_version,
        output_root=output_root,
    )
    second = _build_pass(
        name="build-2",
        repository_root=repository_root,
        source_commit=resolved_commit,
        source_date_epoch=source_date_epoch,
        candidate_version=candidate_version,
        output_root=output_root,
    )
    for distribution, first_wheel in first.wheels.items():
        second_wheel = second.wheels[distribution]
        if first_wheel.path.read_bytes() != second_wheel.path.read_bytes():
            detail = _wheel_difference(first_wheel.path, second_wheel.path)
            raise RuntimeError(f"{distribution} wheel is not reproducible: {detail}")

    manifest_path = output_root / "toolkit-rc-build-manifest-input.json"
    manifest_path.write_bytes(
        _json_bytes(
            {
                "schema_version": "alephain.custos.toolkit-rc-build-candidate.v1",
                "status": "BUILD_CANDIDATE_ONLY",
                "source_commit": resolved_commit,
                "source_date_epoch": source_date_epoch,
                "source_date": datetime.fromtimestamp(source_date_epoch, tz=UTC).isoformat(),
                "candidate_version": candidate_version,
                "metadata_transform": {
                    "project_version": f"0.1.0 -> {candidate_version}",
                    "nautilus_base_dependency": (
                        f"custos-strategy-toolkit==0.1.0 -> "
                        f"custos-strategy-toolkit=={candidate_version}"
                    ),
                },
                "builds": {
                    first.name: {
                        name: _wheel_document(wheel) for name, wheel in first.wheels.items()
                    },
                    second.name: {
                        name: _wheel_document(wheel) for name, wheel in second.wheels.items()
                    },
                },
                "reproducible": True,
                "registry_accessed": False,
                "ready_receipt_created": False,
                "strategy_release_bom_created": False,
                "future_evidence_required": [
                    "Sigstore attestation bundles",
                    "immutable toolkit RC receipt",
                ],
            }
        )
    )
    return ToolkitRcBuildCandidate(
        source_commit=resolved_commit,
        source_date_epoch=source_date_epoch,
        candidate_version=candidate_version,
        first=first,
        second=second,
        manifest_input_path=manifest_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Build two offline Custos toolkit RC wheel sets and require exact bytes.")
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
        help="Custos Git repository root (default: current directory).",
    )
    parser.add_argument(
        "--source-commit",
        required=True,
        help="Exact clean Git source commit to archive.",
    )
    parser.add_argument(
        "--source-date-epoch",
        required=True,
        type=int,
        help="Fixed SOURCE_DATE_EPOCH used by both isolated builds.",
    )
    parser.add_argument(
        "--candidate-version",
        required=True,
        help="Immutable candidate version in the form 0.1.0rcN.",
    )
    parser.add_argument(
        "--output-root",
        required=True,
        type=Path,
        help="New ephemeral directory for wheel and manifest inputs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the candidate-only build seam without creating release authority."""

    arguments = _parser().parse_args(argv)
    candidate = build_reproducible_toolkit_rc_candidate(
        repository_root=arguments.repository_root,
        source_commit=arguments.source_commit,
        source_date_epoch=arguments.source_date_epoch,
        candidate_version=arguments.candidate_version,
        output_root=arguments.output_root,
    )
    print(
        json.dumps(
            {
                "candidate_version": candidate.candidate_version,
                "manifest_input_path": str(candidate.manifest_input_path),
                "source_commit": candidate.source_commit,
                "status": "BUILD_CANDIDATE_ONLY",
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "BuildPassEvidence",
    "ToolkitRcBuildCandidate",
    "WheelBuildEvidence",
    "build_reproducible_toolkit_rc_candidate",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
