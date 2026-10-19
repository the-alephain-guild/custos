#!/usr/bin/env python3
"""Refuse internal vocabulary in the text of published contract files.

This repository is public, and its contract files (schemas, goldens, vectors,
indexes) are copied verbatim into other repositories and read by integrators.
``check-public-surface.py`` refuses private files by path; this check reads the
text of the files that are published on purpose and refuses words that name the
private design behind them: internal table and view names, planning references
and the private planning repository.

Two scopes, both derived, never listed by hand:

- the standing scope (default): every path named by ``authority-manifest.json``
  and by ``docs/authority/contract-revisions-v1.json``, the changelog, the
  RunnerFact pages of the documentation in both languages, the fact builders and
  the contract generators;
- ``--since <commit>``: every file changed since that commit and still present,
  together with the standing scope. A change under review uses it to cover its
  sources and tests as well, and adds the terms in ``ACCEPTANCE_TERMS``.

An empty scope is an error: a check that reads nothing proves nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path("scripts/check-contract-disclosure.py")
AUTHORITY_MANIFEST = Path("authority-manifest.json")
REVISION_INDEX = Path("docs/authority/contract-revisions-v1.json")
STANDING_EXTRAS = (
    Path("CHANGELOG.md"),
    Path("docs-site/docs/03-concepts/runner-fact.md"),
    Path("docs-site/docs/06-integration/consuming-runner-fact.md"),
    Path(
        "docs-site/i18n/zh-Hans/docusaurus-plugin-content-docs/current/03-concepts/runner-fact.md"
    ),
    Path(
        "docs-site/i18n/zh-Hans/docusaurus-plugin-content-docs/current/"
        "06-integration/consuming-runner-fact.md"
    ),
    Path("src/custos/core/runner_fact.py"),
    Path("scripts/generate_runner_fact_contract.py"),
    Path("scripts/generate_contract_conformance.py"),
    Path("scripts/generate_contract_revisions.py"),
    Path("scripts/generate_strategy_signal_contract.py"),
)
TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal projection name", re.compile(r"\bexecution_lineage\b")),
    ("internal table name", re.compile(r"\brunner_order_events\b")),
    ("internal read-model name", re.compile(r"\brollups?\b", re.IGNORECASE)),
    ("private planning repository", re.compile(r"\btabularium\b", re.IGNORECASE)),
    ("planning reference", re.compile(r"\bplan\s+\d{1,3}[a-z]?\b", re.IGNORECASE)),
    ("delivery slice", re.compile(r"(?<![\w.-])S[1-6](?![\w.-])")),
    ("failure-mode reference", re.compile(r"\bFM-\d+\b")),
    ("decision reference", re.compile(r"\bDS\d-\d+[a-z]?\b")),
)
# Terms a change under review must not add, though files it does not touch may
# still hold them until they are cleaned up.
ACCEPTANCE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal service name", re.compile(r"\bcrucible-rust\b")),
)
# Lines left as they are, by (path, term label), each group with its reason. A
# line is exempt only while its text, stripped, is exactly one of these; a new
# line with the same term in the same file is refused, and an exempt line that
# is gone is reported, so the list only shrinks as the files are cleaned up.
_SIGNED_ELSEWHERE = (
    "a receipt another repository signed, kept byte for byte as the record of an "
    "acceptance; it is not rewritten"
)
_PROVENANCE = (
    "provenance text written before this check; registered for the clean-up of the "
    "remaining internal references"
)
_RECEIPTS = "docs/authority/receipts/vendor/"
_CAPABILITY_RECEIPT = _RECEIPTS + "crucible-runner-capability-runtime-v1-consumer-receipt.json"
_LIFECYCLE_RECEIPT = _RECEIPTS + "crucible-runner-fact-position-lifecycle-v1-consumer-receipt.json"
_FACT_RECEIPT = _RECEIPTS + "crucible-runner-fact-v1-consumer-receipt.json"
_PLANNING_REPOSITORY = '"repository": "the-alephain-guild/tabularium",'
_CAPABILITY_EVIDENCE = (
    '"evidence": "tabularium c025ea41, custos plan 03 decision record and Custos segment '
    'implementation record"'
)
_LIFECYCLE_EVIDENCE = (
    '"evidence": "DEV-03-9 approved by the decision maker on 2026-10-04 (tabularium '
    '05c43abc, custos plan 03 deviation log)"'
)
EXEMPTIONS: dict[tuple[str, str], tuple[str, frozenset[str]]] = {
    (_CAPABILITY_RECEIPT, "private planning repository"): (
        _SIGNED_ELSEWHERE,
        frozenset({_PLANNING_REPOSITORY, _CAPABILITY_EVIDENCE}),
    ),
    (_CAPABILITY_RECEIPT, "planning reference"): (
        _SIGNED_ELSEWHERE,
        frozenset(
            {
                '"dispatch": "main session dispatch for custos plan 03 T3 (crucible-rust '
                'segment), decisions D1-D8 and DEV-03-1 ruled 2026-10-03",',
                _CAPABILITY_EVIDENCE,
            }
        ),
    ),
    (_LIFECYCLE_RECEIPT, "private planning repository"): (
        _SIGNED_ELSEWHERE,
        frozenset({_PLANNING_REPOSITORY, _LIFECYCLE_EVIDENCE}),
    ),
    (_LIFECYCLE_RECEIPT, "planning reference"): (
        _SIGNED_ELSEWHERE,
        frozenset(
            {
                '"dispatch": "main session dispatch for custos plan 03 step 7 integration and '
                'contract close-out",',
                _LIFECYCLE_EVIDENCE,
            }
        ),
    ),
    (_FACT_RECEIPT, "private planning repository"): (
        _SIGNED_ELSEWHERE,
        frozenset(
            {'"source": "tabularium .forge/fixes/2026-09/runner-fact-cash-flows-09-114-fixes.md",'}
        ),
    ),
    ("tests/test_runner_fact_contract_v1.py", "internal service name"): (
        "asserts the historical producer receipt, which names the repository that "
        "accepted it; the receipt is not rewritten",
        frozenset({'"repository": "tesseract-trading/crucible-rust",'}),
    ),
    ("Makefile", "internal service name"): (
        "the path of the sibling copy the vendoring script is compared with; renaming "
        "it breaks the comparison",
        frozenset({"--sibling-copy ../crucible-rust/scripts/contract_vendor.py"}),
    ),
    ("docs/authority/strategy-toolkit-contract.md", "planning reference"): (
        _PROVENANCE,
        frozenset(
            {
                "Plan 18 T4 maps those inputs one-to-one into `custos_toolkit`,",
                "Plan 18 T3 moves the exact reviewed execution-contract source bytes to",
                "289 Nautilus-adapter errors are acknowledged debt, not a strict PASS. Plan 18",
                "Crucible's owner-produced Plan 88 contract receipt is pinned by exact commit and",
            }
        ),
    ),
    ("docs/authority/strategy-toolkit-provenance.md", "planning reference"): (
        _PROVENANCE,
        frozenset(
            {
                "(philosophers-stone plan 64): the toolkit is the only implementation, and "
                "there is no",
                "Task 4 establishes package authority only. Runtime verifier activation and "
                "consumer cutover remain later Plan 18 tasks.",
            }
        ),
    ),
}


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _paths_in(document: object) -> Iterable[str]:
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "path" and isinstance(value, str):
                yield value
            else:
                yield from _paths_in(value)
    elif isinstance(document, list):
        for item in document:
            yield from _paths_in(item)


def standing_scope(root: Path) -> set[Path]:
    scope: set[Path] = set()
    for index in (AUTHORITY_MANIFEST, REVISION_INDEX):
        document = json.loads((root / index).read_text(encoding="utf-8"))
        scope |= {Path(path) for path in _paths_in(document)}
        scope.add(index)
    scope |= set(STANDING_EXTRAS)
    return {path for path in scope if (root / path).is_file()}


def changed_scope(since: str) -> set[Path]:
    names = _git("diff", "--name-only", "--diff-filter=d", f"{since}..HEAD").splitlines()
    return {Path(name) for name in names if name}


def scan(
    root: Path,
    scope: set[Path],
    terms: tuple[tuple[str, re.Pattern[str]], ...],
    exemptions: dict[tuple[str, str], tuple[str, frozenset[str]]] = EXEMPTIONS,
) -> list[str]:
    if not scope:
        raise SystemExit("contract disclosure: the scope is empty; nothing was read")
    findings = []
    used: set[tuple[str, str, str]] = set()
    for path in sorted(scope - {SELF}):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in terms:
                match = pattern.search(line)
                if not match:
                    continue
                _, exempt = exemptions.get((str(path), label), ("", frozenset()))
                if line.strip() in exempt:
                    used.add((str(path), label, line.strip()))
                    continue
                findings.append(f"{path}:{number}: {label}: {match.group(0)!r}")
    labels = {label for label, _ in terms}
    for (path, label), (_, lines) in exemptions.items():
        if Path(path) not in scope or label not in labels:
            continue
        for line in sorted(lines):
            if (path, label, line) not in used:
                findings.append(f"{path}: {label}: exempt line no longer present: {line!r}")
    return findings


def self_test() -> None:
    """The check must fail on a planted term and on an empty scope."""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        schema = root / "schema.json"
        schema.write_text(
            json.dumps({"description": "routed to execution_lineage"}), encoding="utf-8"
        )
        clean = root / "clean.json"
        clean.write_text(json.dumps({"description": "an order event"}), encoding="utf-8")
        assert scan(root, {Path("schema.json")}, TERMS), "a planted term went unseen"
        assert not scan(root, {Path("clean.json")}, TERMS), "a clean file was refused"
        for planted in ("plan 15", "S1 freeze", "FM-33", "DS1-3a", "crucible-rust"):
            clean.write_text(planted, encoding="utf-8")
            assert scan(root, {Path("clean.json")}, TERMS + ACCEPTANCE_TERMS), planted
        for harmless in ("plan15.json", "us-east-1", "VS1", "S10", "S1.5"):
            clean.write_text(harmless, encoding="utf-8")
            assert not scan(root, {Path("clean.json")}, TERMS), harmless
        notes = root / "notes.md"
        exempt = {("notes.md", "planning reference"): ("kept", frozenset({"see plan 18"}))}
        notes.write_text("see plan 18\n", encoding="utf-8")
        assert not scan(root, {Path("notes.md")}, TERMS, exempt), "an exempt line was refused"
        notes.write_text("see plan 18\nsee plan 19\n", encoding="utf-8")
        findings = scan(root, {Path("notes.md")}, TERMS, exempt)
        assert [finding.split(": ")[0] for finding in findings] == ["notes.md:2"], findings
        notes.write_text("cleaned up\n", encoding="utf-8")
        findings = scan(root, {Path("notes.md")}, TERMS, exempt)
        assert len(findings) == 1 and "no longer present" in findings[0], findings
        try:
            scan(root, set(), TERMS)
        except SystemExit:
            pass
        else:
            raise AssertionError("an empty scope passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="also scan every file changed since this commit")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        print("contract disclosure self-test passed")
        return 0
    scope = standing_scope(ROOT)
    findings = scan(ROOT, scope, TERMS)
    if args.since:
        changed = changed_scope(args.since)
        findings += [
            finding
            for finding in scan(ROOT, changed, TERMS + ACCEPTANCE_TERMS)
            if finding not in findings
        ]
        scope |= changed
    if findings:
        print("contract disclosure: internal vocabulary in published files:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(f"contract disclosure: {len(scope)} file(s) read, no internal vocabulary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
