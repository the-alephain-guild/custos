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
  and by ``docs/authority/contract-revisions-v1.json``, the RunnerFact pages of
  the documentation in both languages, the fact builders and the contract
  generators;
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
# Exact (path, term label) pairs left as they are, each with its reason.
_SIGNED_ELSEWHERE = (
    "a receipt another repository signed, kept byte for byte as the record of an "
    "acceptance; it is not rewritten"
)
_PROVENANCE = (
    "provenance text written before this check; registered for the clean-up of the "
    "remaining internal references"
)
EXEMPTIONS: dict[tuple[str, str], str] = {
    **{
        (f"docs/authority/receipts/vendor/{name}", label): _SIGNED_ELSEWHERE
        for name in (
            "crucible-runner-capability-runtime-v1-consumer-receipt.json",
            "crucible-runner-fact-position-lifecycle-v1-consumer-receipt.json",
            "crucible-runner-fact-v1-consumer-receipt.json",
        )
        for label in ("private planning repository", "planning reference")
    },
    ("tests/test_runner_fact_contract_v1.py", "internal service name"): (
        "asserts the historical producer receipt, which names the repository that "
        "accepted it; the receipt is not rewritten"
    ),
    ("docs/authority/strategy-toolkit-contract.md", "planning reference"): _PROVENANCE,
    ("docs/authority/strategy-toolkit-provenance.md", "planning reference"): _PROVENANCE,
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


def scan(root: Path, scope: set[Path], terms: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    if not scope:
        raise SystemExit("contract disclosure: the scope is empty; nothing was read")
    findings = []
    for path in sorted(scope - {SELF}):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in terms:
                if (str(path), label) in EXEMPTIONS:
                    continue
                match = pattern.search(line)
                if match:
                    findings.append(f"{path}:{number}: {label}: {match.group(0)!r}")
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
