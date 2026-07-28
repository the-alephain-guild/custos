"""Plan 12 T6 contract: LTS commitment doc surface (FM4 + FM10).

The doc's audit-non-silence value is only real if the required sections
plus the EOL-date rows actually exist. This test catches the sneaky
regression where an operator "cleans up" the EOL table but leaves the
`## EOL Window` header behind — the doc would look complete but the
commitment table would be empty (Plan 12 L1 / FM10 audit-non-silence
guard).
"""

from __future__ import annotations

import re
from pathlib import Path

DOC = (
    Path(__file__).resolve().parent.parent
    / "docs-site"
    / "docs"
    / "10-release-governance"
    / "semver-lts.md"
)


def test_lts_doc_exists():
    assert DOC.exists(), f"missing {DOC}"


def test_lts_doc_has_required_sections():
    text = DOC.read_text()
    # DP7 baseline sections + wording that lock the concrete numbers.
    assert "## EOL Window" in text, "missing `## EOL Window` header"
    assert "## Security Patch SLA" in text, "missing `## Security Patch SLA` header"
    assert "## Release Cadence" in text, "missing `## Release Cadence` header"
    assert "12 months" in text, "12-month EOL commitment wording missing"
    assert "30 days" in text, "30-day security-patch SLA wording missing"


def test_lts_doc_has_eol_date_row():
    """L1 / FM10: at least one concrete EOL-date row of the form
    `| 0.<minor>.x | <YYYY-MM-DD>`.

    Without a real date row the `## EOL Window` header is a hollow claim
    — an auditor can't check whether we're actually inside the promised
    window. This assertion catches that hollowing.
    """
    text = DOC.read_text()
    rows = re.findall(r"\|\s*0\.\d+\.x\s*\|\s*\d{4}-\d{2}-\d{2}", text)
    assert len(rows) >= 1, (
        "no EOL rows of the form `| 0.X.x | YYYY-MM-DD` in "
        "the LTS chapter; the section header is present but the "
        "actual commitment is empty — audit-non-silence red-line (FM10)."
    )
