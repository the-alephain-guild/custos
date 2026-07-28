"""The published LTS commitment must match what was actually released.

The support window is a promise an auditor should be able to check. That cuts
both ways, and this file guards both directions:

- a `## EOL Window` header with no rows under it is a hollow claim;
- a row naming a release date for a version that was never tagged is worse,
  because it looks like evidence. The page carried two such rows -- dated from
  changelog entries for versions that have no tag and no published artifact.

So the rule is not "there must be a row". It is that the rows must correspond
to releases that exist, and that a repository with no releases must say so.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs-site" / "docs" / "10-release-governance" / "semver-lts.md"

_EOL_ROW = re.compile(r"\|\s*(0\.\d+)\.x\s*\|\s*(\d{4}-\d{2}-\d{2})")
_RELEASE_TAG = re.compile(r"^v(0\.\d+)\.\d+$")


def _released_minor_lines() -> set[str]:
    proc = subprocess.run(
        ["git", "tag"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        return set()
    return {match.group(1) for tag in proc.stdout.split() if (match := _RELEASE_TAG.match(tag))}


def test_lts_doc_exists():
    assert DOC.exists(), f"missing {DOC}"


def test_lts_doc_has_required_sections():
    text = DOC.read_text()

    assert "## EOL Window" in text, "missing `## EOL Window` header"
    assert "## Security Patch SLA" in text, "missing `## Security Patch SLA` header"
    assert "## Release Cadence" in text, "missing `## Release Cadence` header"
    assert "12 months" in text, "12-month EOL commitment wording missing"
    assert "30 days" in text, "30-day security-patch SLA wording missing"


def test_eol_rows_name_only_released_lines() -> None:
    """Every published window must correspond to a tag that exists."""
    text = DOC.read_text()
    released = _released_minor_lines()

    claimed = {line for line, _ in _EOL_ROW.findall(text)}
    unbacked = sorted(claimed - released)

    assert not unbacked, (
        f"the LTS table publishes a support window for {unbacked}, which has no "
        "release tag. A changelog entry is not a release, and a window with no "
        "start date is a commitment nobody can hold us to."
    )


def test_every_released_line_publishes_a_window() -> None:
    """And the converse: a released line must not be missing from the table."""
    text = DOC.read_text()
    released = _released_minor_lines()

    claimed = {line for line, _ in _EOL_ROW.findall(text)}
    undocumented = sorted(released - claimed)

    assert not undocumented, (
        f"released line(s) {undocumented} have no row in the LTS table; the "
        "support window they are already inside is unpublished"
    )


def test_empty_table_says_why() -> None:
    """A table with no rows must explain itself, not just be blank.

    This is the original guard: `## EOL Window` above an empty table reads as
    an oversight, and an auditor cannot tell an unmade promise from a lost one.
    """
    if _released_minor_lines():
        return

    text = DOC.read_text()
    assert "No line has started its window yet" in text, (
        "no releases exist, so the EOL table is empty -- the section must say "
        "so explicitly rather than leaving a bare header over a blank table"
    )
