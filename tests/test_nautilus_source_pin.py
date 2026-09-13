"""Contract for deriving the NautilusTrader source pin from uv.lock.

The Docker build compiles NautilusTrader from the fork at an exact commit.
That commit must come from `uv.lock` rather than being written into the
Dockerfile or the Makefile by hand: a hand-copied sha and the lock drift
apart silently, and the image then answers questions about a revision the
tree no longer pins (historical-lessons C7).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from nautilus_source_pin import NautilusSourcePinError, read_pin  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "nautilus_source_pin.py"


def test_pin_comes_from_the_committed_lock() -> None:
    pin = read_pin(REPO_ROOT / "uv.lock")

    assert pin.url == "https://github.com/the-alephain-guild/nautilus_trader"
    assert len(pin.sha) == 40
    assert set(pin.sha) <= set("0123456789abcdef")
    assert pin.version.startswith("2.0.0rc5")
    assert pin.subdirectory == "python"


def test_a_registry_source_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """After the switch to a published wheel there is nothing to compile.

    The script must say so instead of returning a sha it invented, because
    its callers build a Docker stage out of the answer.
    """
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "nautilus-trader"\nversion = "2.0.0rc5"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )

    with pytest.raises(NautilusSourcePinError, match="not a git source"):
        read_pin(lock)


def test_a_lock_without_nautilus_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname = "structlog"\nversion = "26.1.0"\n', encoding="utf-8")

    with pytest.raises(NautilusSourcePinError, match="no nautilus-trader"):
        read_pin(lock)


def test_disagreeing_rev_and_fragment_are_refused(tmp_path: Path) -> None:
    """uv writes the resolved commit twice. If the two ever disagree we do
    not understand the format well enough to pick one, so we stop."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "nautilus-trader"\nversion = "2.0.0rc5"\n'
        'source = { git = "https://example.invalid/fork?subdirectory=python'
        "&rev=" + "a" * 40 + "#" + "b" * 40 + '" }\n',
        encoding="utf-8",
    )

    with pytest.raises(NautilusSourcePinError, match="disagree"):
        read_pin(lock)


@pytest.mark.parametrize("field", ["sha", "url", "version", "subdirectory"])
def test_the_cli_prints_one_field_for_make_to_consume(field: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--field", field],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert proc.returncode == 0, proc.stderr
    value = proc.stdout.strip()
    assert value
    assert "\n" not in value
