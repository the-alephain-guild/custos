#!/usr/bin/env python3
"""Verify the versioned Plan 18 Task 4b typing-closure candidate."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_IMPLEMENTATION_COMMIT = "b5ff7ee9cea0e78f4462a478bafa42f8f6e18805"
EXTRACTION_PATH = ROOT / "docs/authority/strategy-toolkit-extraction-v1.json"
EXTRACTION_RECEIPT_PATH = (
    ROOT / "docs/authority/receipts/strategy-toolkit-extraction-receipt-v1.json"
)
BASELINE_PATH = ROOT / "docs/authority/strategy-toolkit-typing-baseline-v1.json"
CLOSURE_PATH = ROOT / "docs/authority/strategy-toolkit-typing-closure-v1.json"
CLOSURE_RECEIPT_PATH = (
    ROOT / "docs/authority/receipts/strategy-toolkit-typing-closure-receipt-v1.json"
)
BASE_SOURCE_ROOT = ROOT / "packages/custos-strategy-toolkit/src"
NAUTILUS_SOURCE_ROOT = ROOT / "packages/custos-strategy-toolkit-nautilus/src"
FULLWIDTH_PUNCTUATION = re.compile(r"[\uFF01-\uFF60]")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_object(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _git_blob(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


def _target_path(record: dict[str, object]) -> Path:
    target = Path(cast(str, record["target_path"]))
    if target.parts[0] == "custos_toolkit":
        return BASE_SOURCE_ROOT / target
    if target.parts[0] == "custos_toolkit_nautilus":
        return NAUTILUS_SOURCE_ROOT / target
    raise ValueError(f"unsupported toolkit target: {target}")


def _candidate_content(path: Path, typed_commit: str | None) -> bytes:
    if typed_commit is None:
        return path.read_bytes()
    return _git_blob(typed_commit, str(path.relative_to(ROOT)))


def _tree_digest(records: list[tuple[str, str]]) -> str:
    canonical = "".join(f"{path}\0{digest}\n" for path, digest in sorted(records))
    return _sha256(canonical.encode("utf-8"))


def _contains_explicit_any(path: Path, content: bytes) -> bool:
    tree = ast.parse(content.decode("utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "Any":
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            if any(alias.name == "Any" for alias in node.names):
                return True
    return False


def _run_mypy(config: str, source: str) -> str | None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            config,
            source,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    return (result.stdout + result.stderr).strip()


def check() -> list[str]:
    errors: list[str] = []
    extraction = _json_object(EXTRACTION_PATH)
    extraction_receipt = _json_object(EXTRACTION_RECEIPT_PATH)
    baseline = _json_object(BASELINE_PATH)
    closure = _json_object(CLOSURE_PATH)
    receipt = _json_object(CLOSURE_RECEIPT_PATH)

    extraction_implementation = cast(dict[str, object], extraction_receipt["implementation"])
    if extraction_implementation.get("implementation_commit") != EXTRACTION_IMPLEMENTATION_COMMIT:
        errors.append("historical T4 receipt implementation commit drifted")
    if closure.get("extracted_implementation_commit") != EXTRACTION_IMPLEMENTATION_COMMIT:
        errors.append("T4b manifest does not bind the canonical T4 implementation commit")
    if closure.get("extraction_manifest_sha256") != _sha256(EXTRACTION_PATH.read_bytes()):
        errors.append("T4b extraction-manifest digest mismatch")
    if closure.get("extraction_receipt_sha256") != _sha256(EXTRACTION_RECEIPT_PATH.read_bytes()):
        errors.append("T4b historical-receipt digest mismatch")
    if closure.get("typing_baseline_sha256") != _sha256(BASELINE_PATH.read_bytes()):
        errors.append("T4b typing-baseline digest mismatch")

    profiles = cast(dict[str, object], baseline["profiles"])
    old_counts = {
        name: cast(dict[str, object], profile)["expected_error_count"]
        for name, profile in profiles.items()
    }
    if old_counts != {"platform_neutral": 75, "nautilus_adapter": 289}:
        errors.append(f"historical typing debt baseline drifted: {old_counts}")

    typed_commit_raw = closure.get("typed_implementation_commit")
    typed_commit = cast(str | None, typed_commit_raw)
    if typed_commit is None:
        if receipt.get("receipt_status") != "VERIFIED_PENDING_COMMIT":
            errors.append("uncommitted T4b candidate must remain VERIFIED_PENDING_COMMIT")
        if receipt.get("handoff_ready") is not False:
            errors.append("uncommitted T4b candidate cannot be handoff-ready")
    else:
        if closure.get("verification_mode") != "exact_commit_snapshot":
            errors.append("committed T4b closure must use exact_commit_snapshot verification")
        if receipt.get("typed_implementation_commit") != typed_commit:
            errors.append("T4b receipt does not bind the manifest implementation commit")
        if receipt.get("receipt_status") != "READY_TYPING_CLOSURE":
            errors.append("committed T4b closure must be READY_TYPING_CLOSURE")
        if receipt.get("handoff_ready") is not True:
            errors.append("committed T4b closure must be handoff-ready for its scoped artifact")
        if receipt.get("handoff_scope") != "Custos strategy toolkit typing closure only":
            errors.append("T4b handoff scope is missing or broader than typing closure")
        if receipt.get("production_ready") is not False:
            errors.append("typing closure cannot claim Plan 18 production readiness")

    extraction_files = cast(list[dict[str, object]], extraction["files"])
    closure_files = cast(list[dict[str, object]], closure["files"])
    extraction_by_target = {cast(str, record["target_path"]): record for record in extraction_files}
    closure_by_target = {cast(str, record["target_path"]): record for record in closure_files}
    if len(extraction_by_target) != 241 or len(closure_by_target) != 241:
        errors.append("T4b manifest must map all 241 extracted files exactly once")
        return errors
    if extraction_by_target.keys() != closure_by_target.keys():
        errors.append("T4b target inventory differs from historical extraction inventory")
        return errors

    digest_records: list[tuple[str, str]] = []
    changed_package_paths: set[str] = set()
    for target_name, extraction_record in extraction_by_target.items():
        closure_record = closure_by_target[target_name]
        target = _target_path(extraction_record)
        repo_path = str(target.relative_to(ROOT))
        extraction_content = _git_blob(EXTRACTION_IMPLEMENTATION_COMMIT, repo_path)
        extraction_digest = _sha256(extraction_content)
        historical_digest = cast(str, extraction_record["target_sha256"])
        if extraction_digest != historical_digest:
            errors.append(f"extraction commit digest mismatch: {target_name}")
        if closure_record.get("extracted_target_sha256") != extraction_digest:
            errors.append(f"typing closure source digest mismatch: {target_name}")
        typed_content = _candidate_content(target, typed_commit)
        typed_digest = _sha256(typed_content)
        if closure_record.get("typed_target_sha256") != typed_digest:
            errors.append(f"T4b target digest mismatch: {target_name}")
        changed = typed_digest != extraction_digest
        if closure_record.get("changed") is not changed:
            errors.append(f"T4b changed flag mismatch: {target_name}")
        if cast(str, extraction_record["category"]) == "private_vendor" and changed:
            errors.append(f"private vendor source changed during T4b: {target_name}")
        if changed:
            changed_package_paths.add(repo_path)
        digest_records.append((repo_path, typed_digest))

    support_files = cast(list[dict[str, object]], closure["support_files"])
    support_paths: set[str] = set()
    for support in support_files:
        repo_path = cast(str, support["path"])
        support_paths.add(repo_path)
        path = ROOT / repo_path
        content = _candidate_content(path, typed_commit)
        digest = _sha256(content)
        if support.get("typed_sha256") != digest:
            errors.append(f"T4b support-file digest mismatch: {repo_path}")
        digest_records.append((repo_path, digest))

    if typed_commit is None:
        diff_result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                EXTRACTION_IMPLEMENTATION_COMMIT,
                "--",
                "packages/custos-strategy-toolkit",
                "packages/custos-strategy-toolkit-nautilus",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        untracked_result = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                "packages/custos-strategy-toolkit",
                "packages/custos-strategy-toolkit-nautilus",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        actual_changed = {
            line
            for line in (*diff_result.stdout.splitlines(), *untracked_result.stdout.splitlines())
            if line
        }
        expected_changed = changed_package_paths | support_paths
        if actual_changed != expected_changed:
            errors.append(
                "T4b package diff is not fully represented by manifest: "
                f"missing={sorted(actual_changed - expected_changed)}, "
                f"stale={sorted(expected_changed - actual_changed)}"
            )

    candidate_digest = _tree_digest(digest_records)
    if closure.get("typed_candidate_tree_sha256") != candidate_digest:
        errors.append("T4b typed candidate tree digest mismatch")
    if receipt.get("typed_candidate_tree_sha256") != candidate_digest:
        errors.append("T4b receipt candidate tree digest mismatch")
    if receipt.get("manifest_sha256") != _sha256(CLOSURE_PATH.read_bytes()):
        errors.append("T4b receipt manifest digest mismatch")

    if typed_commit is None:
        source_roots = (BASE_SOURCE_ROOT, NAUTILUS_SOURCE_ROOT / "custos_toolkit_nautilus/adapter")
        for source_root in source_roots:
            for path in source_root.rglob("*.py"):
                content = path.read_bytes()
                if _contains_explicit_any(path, content):
                    errors.append(
                        f"explicit Any remains in owned toolkit source: {path.relative_to(ROOT)}"
                    )
                if FULLWIDTH_PUNCTUATION.search(content.decode("utf-8")):
                    errors.append(f"fullwidth punctuation remains: {path.relative_to(ROOT)}")

        for config_path in (
            ROOT / "packages/custos-strategy-toolkit/pyproject.toml",
            ROOT / "packages/custos-strategy-toolkit-nautilus/pyproject.toml",
        ):
            config_text = config_path.read_text(encoding="utf-8")
            forbidden = (
                'follow_imports = "skip"',
                "ignore_missing_imports = true",
                "ignore_errors = true",
            )
            if any(item in config_text for item in forbidden):
                errors.append(f"permissive mypy setting remains: {config_path.relative_to(ROOT)}")

        for config, source in (
            (
                "packages/custos-strategy-toolkit/pyproject.toml",
                "packages/custos-strategy-toolkit/src/custos_toolkit",
            ),
            (
                "packages/custos-strategy-toolkit-nautilus/pyproject.toml",
                "packages/custos-strategy-toolkit-nautilus/src/custos_toolkit_nautilus",
            ),
        ):
            failure = _run_mypy(config, source)
            if failure:
                errors.append(f"strict mypy failed for {source}:\n{failure}")

    return errors


def main() -> int:
    try:
        errors = check()
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"toolkit typing closure check failed: {error}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    status = _json_object(CLOSURE_RECEIPT_PATH)["receipt_status"]
    print(f"strategy toolkit typing closure: strict zero, {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
