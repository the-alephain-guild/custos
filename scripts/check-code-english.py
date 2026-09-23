#!/usr/bin/env python3
"""
Enforce the "code artifacts must be English" rule from CONTRIBUTING.md.

Scans **newly-added lines** (git staged `+` lines, not existing lines) in
`.py` / `.rs` / `.ts` / `.tsx` files for CJK characters. Existing Chinese
comments / logs are intentionally NOT flagged — the policy is "touch-and-fix"
(rewrite when you edit them), not "big-bang rewrite the world".

Scope:
- IN scope: source code (`.py`, `.rs`, `.ts`, `.tsx`)
- OUT of scope: Markdown, `.planning/*`, Grimoire, config YAML, user-facing UI
  copy (i18n bundles / message JSON), CJK inside `data:` / URL literals

Exemption: a new line may contain `noqa: language` (or `noqa: lang`) at end of
line to opt out — use sparingly (e.g. a genuine user-facing error message that
must be Chinese for a Chinese-only product surface).

Exit:
  0 = clean
  1 = violations found (commit blocked)
  2 = unexpected internal error
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
NOQA_PATTERN = re.compile(r"noqa:\s*(language|lang|zh)\b", re.IGNORECASE)

TARGET_SUFFIXES = (".py", ".rs", ".ts", ".tsx")
REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_MANIFEST_PATH = REPO_ROOT / "docs/authority/strategy-toolkit-extraction-v1.json"

# Paths inside repo that are documentation / planning / i18n bundles — do not
# scan even if they happen to be `.ts`/`.py` (rare but possible for i18n).
EXEMPT_PATH_PREFIXES = (
    ".planning/",
    "docs/",
    "grimoire/",
    "brand-guide/",
)
EXEMPT_PATH_SUBSTRINGS = (
    "/i18n/",
    "/locales/",
    "/messages/",
    "/.planning/",
    "/docs/",
    "/grimoire/",
    # This script's own CJK_PATTERN Unicode range literal is a tool
    # implementation detail (defines what counts as CJK), not user-facing
    # text subject to the English-only discipline it enforces.
    "check-code-english.py",
)


def _staged_target_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=True,
    )
    files: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.endswith(TARGET_SUFFIXES):
            continue
        if any(line.startswith(p) for p in EXEMPT_PATH_PREFIXES):
            continue
        if any(sub in "/" + line for sub in EXEMPT_PATH_SUBSTRINGS):
            continue
        files.append(line)
    return files


def _staged_added_lines(path: str) -> list[tuple[int, str]]:
    """Return list of (new_line_number, content) for lines added by this commit."""
    result = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--", path],
        capture_output=True,
        text=True,
        check=True,
    )
    added: list[tuple[int, str]] = []
    hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    current_new_line = 0
    for line in result.stdout.splitlines():
        m = hunk_header_re.match(line)
        if m:
            current_new_line = int(m.group(1))
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append((current_new_line, line[1:]))
            current_new_line += 1
        elif line.startswith("-"):
            # deletion — new-line pointer does not advance
            pass
        elif line.startswith(" "):
            current_new_line += 1
    return added


def _violates(content: str) -> bool:
    if NOQA_PATTERN.search(content):
        return False
    return bool(CJK_PATTERN.search(content))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_extraction_manifest() -> dict[str, Any]:
    return json.loads(EXTRACTION_MANIFEST_PATH.read_text(encoding="utf-8"))


def _repo_target_path(target_path: str) -> str | None:
    target = Path(target_path)
    if not target.parts or target.is_absolute() or ".." in target.parts:
        return None
    if target.parts[0] == "custos_toolkit":
        return (Path("packages/custos-strategy-toolkit/src") / target).as_posix()
    if target.parts[0] == "custos_toolkit_nautilus":
        return (Path("packages/custos-strategy-toolkit-nautilus/src") / target).as_posix()
    return None


def _git_blob(object_name: str) -> bytes:
    return subprocess.run(
        ["git", "show", object_name],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _exact_relocation_exemption(
    path: str,
    cjk_lines: list[tuple[int, str]],
    manifest: dict[str, Any],
) -> tuple[bool, str | None]:
    """Prove CJK lines are unchanged bytes from one exact inventory relocation."""
    record = next(
        (
            item
            for item in manifest.get("files", [])
            if _repo_target_path(item.get("target_path", "")) == path
        ),
        None,
    )
    if record is None:
        return False, None

    source_commit = manifest.get("source_commit")
    legacy_path = record.get("legacy_path")
    legacy_sha256 = record.get("legacy_sha256")
    target_sha256 = record.get("target_sha256")
    if not (
        isinstance(source_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", source_commit)
        and isinstance(legacy_path, str)
        and isinstance(legacy_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", legacy_sha256)
        and isinstance(target_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", target_sha256)
    ):
        return False, None

    try:
        source_blob = _git_blob(f"{source_commit}:{legacy_path}")
        staged_blob = _git_blob(f":{path}")
        current_blob = (REPO_ROOT / path).read_bytes()
    except (OSError, subprocess.CalledProcessError):
        return False, None

    if _sha256(source_blob) != legacy_sha256:
        return False, None
    if _sha256(staged_blob) != target_sha256 or _sha256(current_blob) != target_sha256:
        return False, None

    try:
        source_lines = source_blob.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False, None
    if any(
        line_no < 1 or line_no > len(source_lines) or source_lines[line_no - 1] != content
        for line_no, content in cjk_lines
    ):
        return False, None

    note = (
        f"[check-code-english] EXEMPT exact relocated file: {path}; "
        f"{len(cjk_lines)} pre-existing CJK line(s) match "
        f"{legacy_path}@{source_commit[:12]}; source sha256={legacy_sha256[:12]}..., "
        f"staged/current target sha256={target_sha256[:12]}..."
    )
    return True, note


def main() -> int:
    try:
        files = _staged_target_files()
    except subprocess.CalledProcessError as e:
        print(f"[check-code-english] git error: {e}", file=sys.stderr)
        return 2

    if not files:
        return 0

    violations: list[tuple[str, int, str]] = []
    exemption_notes: list[str] = []
    extraction_manifest: dict[str, Any] | None = None
    for path in files:
        try:
            cjk_lines = [
                (line_no, content)
                for line_no, content in _staged_added_lines(path)
                if _violates(content)
            ]
        except subprocess.CalledProcessError as e:
            print(f"[check-code-english] git diff failed for {path}: {e}", file=sys.stderr)
            return 2
        if not cjk_lines:
            continue

        if extraction_manifest is None:
            try:
                extraction_manifest = _load_extraction_manifest()
            except (OSError, json.JSONDecodeError):
                extraction_manifest = {}
        exempt, note = _exact_relocation_exemption(path, cjk_lines, extraction_manifest)
        if exempt:
            if note is not None:
                exemption_notes.append(note)
            continue
        violations.extend((path, line_no, content.rstrip()) for line_no, content in cjk_lines)

    for note in exemption_notes:
        print(note)

    if not violations:
        return 0

    print(
        "\n[check-code-english] BLOCKED: newly-added lines contain Chinese "
        "characters in source code (.py / .rs / .ts / .tsx).\n"
        "Rule: see CONTRIBUTING.md, 'Before you open the PR'.\n"
        "Rewrite these lines in English (comments, log/event names, "
        "error messages). User-facing UI copy belongs in i18n bundles, not "
        "source code strings.\n"
        "Escape hatch (use rarely, with justification): append `noqa: language` "
        "to the end of the offending line.\n",
        file=sys.stderr,
    )
    print(f"Total violations: {len(violations)}\n", file=sys.stderr)
    for path, line_no, content in violations[:50]:
        preview = content if len(content) <= 200 else content[:200] + "…"
        print(f"  {path}:{line_no}: {preview}", file=sys.stderr)
    if len(violations) > 50:
        print(f"  … and {len(violations) - 50} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
