"""Test counts written into a close-out must be counts, not estimates.

Plan 21's close-out claimed 91 new tests. The real number was 117, its own
breakdown summed to 108, and one whole suite was missing from the list — three
different ways of being wrong in one sentence, none of which required anyone to
lie, only to write a number without counting it.

So the numbers get counted here instead. A close-out states one row per test
file; this collects each file and compares. A bare total with no rows behind it
is refused, because that is the shape the wrong number came in.

A close-out records a moment, so the newest plan to count a file is the one held
to today's number. Rewriting an older close-out's rows every time a later plan
grows the same file would replace a record of what that plan delivered with a
rolling figure — and the plan that grows the file has to recount it either way,
which is the discipline the counting was for.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / ".forge/plans"

_ROW = re.compile(r"^\|\s*`(tests/[\w/]+\.py)`\s*\|\s*(\d+)\s*\|", re.MULTILINE)
# Close-outs are written in Chinese, so a total shows up in one of two phrases:
# "<n> newly added tests" or "the table above totals <n>". They are spelled as
# escapes rather than literals so this file carries no CJK bytes of its own, which
# is what the language rule is protecting — matching Chinese prose is not writing it.
_ADDED = "\u65b0\u589e"
_TESTS = "\u4e2a\u6d4b\u8bd5"
_TABLE_TOTALS = "\u4e0a\u8868\u5408\u8ba1"
_ITEMS = "\u6761"
_BARE_TOTAL = re.compile(
    rf"(?:{_ADDED}\s*(?P<a>\d+)\s*{_TESTS}|{_TABLE_TOTALS}\s*(?P<b>\d+)\s*{_ITEMS})"
)


def _plans_with_test_tables() -> list[Path]:
    return sorted(
        path for path in PLANS.rglob("*.md") if _ROW.search(path.read_text(encoding="utf-8"))
    )


_SKIPPED_FILE = re.compile(r"^SKIPPED \[\d+\] (tests/[\w/]+\.py):", re.MULTILINE)


def _collect(files: list[str]) -> tuple[Counter[str], set[str]]:
    """Ask pytest how many tests each file has, and which ones it could not collect.

    A file whose module-level ``importorskip`` fires collects nothing, and that is not
    the same as a wrong count. The toolkit's engine-adapter tests are exactly that: the
    adapter distribution ships in the ``nautilus`` extra, so under the base profile they
    skip and count zero, while under ``make verify-nt`` they run. Counting them as zero
    would make every close-out that records them red in one profile and green in the
    other, which says nothing about whether the number is right.

    So skipped files are reported separately rather than folded into the counts, and
    their rows are named out loud instead of quietly excused.
    """

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header", "-rs", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"collection failed for {files}:\n{result.stdout}\n{result.stderr}")
    counts: Counter[str] = Counter()
    for line in result.stdout.splitlines():
        node, separator, _ = line.partition("::")
        if separator and node.endswith(".py"):
            counts[node] += 1
    return counts, set(_SKIPPED_FILE.findall(result.stdout))


def _collected_counts(files: list[str]) -> Counter[str]:
    counts, _ = _collect(files)
    return counts


def _newest_claim() -> dict[str, tuple[str, int]]:
    """Each counted file, mapped to the last close-out that counted it."""

    claim: dict[str, tuple[str, int]] = {}
    for plan in _plans_with_test_tables():
        for path, count in _ROW.findall(plan.read_text(encoding="utf-8")):
            claim[path] = (plan.name, int(count))
    return claim


@pytest.mark.parametrize("plan", _plans_with_test_tables(), ids=lambda p: p.name)
def test_a_close_out_counts_no_test_file_that_has_since_been_deleted(plan: Path) -> None:
    """A count of a file nobody kept is the loudest thing a close-out can say."""

    claimed = dict(_ROW.findall(plan.read_text(encoding="utf-8")))

    missing = [path for path in claimed if not (ROOT / path).is_file()]

    assert not missing, f"{plan.name} counts test files that do not exist: {missing}"


def test_the_newest_count_of_every_test_file_matches_what_pytest_collects() -> None:
    claim = _newest_claim()

    collected, skipped = _collect(sorted(claim))
    wrong = {
        path: {"plan": plan, "claimed": count, "collected": collected.get(path, 0)}
        for path, (plan, count) in claim.items()
        if path not in skipped and collected.get(path, 0) != count
    }

    assert not wrong, (
        f"the newest close-out to count these files disagrees with pytest: {wrong}. "
        "A plan that grows a counted file recounts it in its own close-out rather "
        "than editing the older one."
    )

    unverified = sorted(path for path in claim if path in skipped)
    if unverified:
        # Named, not silenced: under this profile these counts were not checked at all,
        # and a reader of a green run should be able to see which ones.
        print(
            f"{len(unverified)} counted files did not collect under this profile, so their "
            f"counts are unchecked here and are covered by the profile that runs them: "
            f"{unverified}"
        )


@pytest.mark.parametrize("plan", _plans_with_test_tables(), ids=lambda p: p.name)
def test_a_close_out_states_no_total_its_rows_do_not_support(plan: Path) -> None:
    """A total nobody can check is exactly how the wrong one survived review."""

    text = plan.read_text(encoding="utf-8")
    rows = sum(int(count) for _, count in _ROW.findall(text))

    for match in _BARE_TOTAL.finditer(text):
        total = match.group("a") or match.group("b")
        assert int(total) == rows, (
            f"{plan.name} claims {total} tests while its rows account for {rows}; "
            "state a total its own table supports, or drop the total"
        )


def test_the_probe_tells_a_skipped_file_apart_from_an_uncollectable_one() -> None:
    """Proves the excusal is earned by a real skip, not handed to any file counting zero.

    Written into ``tests/`` rather than a tmp_path because pytest reports paths relative
    to the root, and a count is only excused for a path this repository would recognise.
    """

    marker = ROOT / "tests" / "_probe_skips_at_collection.py"
    marker.write_text(
        "import pytest\n\n"
        'pytest.importorskip("a_module_this_repository_will_never_have")\n\n\n'
        "def test_never_runs() -> None:\n"
        "    raise AssertionError\n",
        encoding="utf-8",
    )
    try:
        counts, skipped = _collect(
            ["tests/_probe_skips_at_collection.py", "tests/test_plan_closeout_counts.py"]
        )
    finally:
        marker.unlink()

    assert "tests/_probe_skips_at_collection.py" in skipped
    assert counts["tests/_probe_skips_at_collection.py"] == 0
    # The file that did collect is not excused, so a wrong count for it still fails.
    assert "tests/test_plan_closeout_counts.py" not in skipped
    assert counts["tests/test_plan_closeout_counts.py"] > 0


def test_the_probe_notices_a_count_that_drifted(tmp_path: Path) -> None:
    """Proves this file can fail rather than passing on whatever it is given."""

    target = tmp_path / "fake_plan.md"
    target.write_text("| `tests/test_plan_closeout_counts.py` | 999 |\n", encoding="utf-8")
    claimed = {path: int(count) for path, count in _ROW.findall(target.read_text())}

    collected = _collected_counts(sorted(claimed))

    assert (
        collected["tests/test_plan_closeout_counts.py"]
        != claimed["tests/test_plan_closeout_counts.py"]
    )
