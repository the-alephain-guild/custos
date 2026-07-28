"""Plan 12 T8 contract: reproducible build (FM5).

Two ``@pytest.mark.slow`` gates. Both invoke ``uv build`` twice, hash
the resulting wheels, and compare:

- ``test_wheel_bytes_identical_across_rebuild``: with ``SOURCE_DATE_EPOCH``
  pinned, both builds must produce byte-for-byte identical wheels.
- ``test_wheel_bytes_differ_without_epoch``: without an epoch pin, the
  wheels *should* differ (proves the epoch is the reproducibility knob;
  hatchling >= 1.20 is largely deterministic already, so if this test
  passes an epoch-less rebuild it means hatchling is deterministic
  enough that the epoch is defence-in-depth rather than the sole knob —
  the docstring calls out that scenario).

Marked slow because a double `uv build` is minutes-scale; the CI
nightly job runs this, `make verify` skips it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(env: dict, out_dir: Path) -> Path:
    subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        check=True,
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    wheels = sorted(out_dir.glob("*.whl"))
    assert wheels, f"uv build produced no wheels in {out_dir}"
    return wheels[0]


@pytest.mark.slow
def test_wheel_bytes_identical_across_rebuild():
    """SOURCE_DATE_EPOCH pinned => two rebuilds MUST hash-equal."""
    epoch = "1704067200"  # 2024-01-01 00:00 UTC — arbitrary but stable.
    env = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
    with tempfile.TemporaryDirectory() as d1_str, tempfile.TemporaryDirectory() as d2_str:
        h1 = _sha256(_build(env, Path(d1_str)))
        # Sleep so an mtime-leaking hatchling variant would show up as a
        # differing hash rather than a coincidence.
        time.sleep(2)
        h2 = _sha256(_build(env, Path(d2_str)))
        assert h1 == h2, (
            f"reproducibility broken: two builds with SOURCE_DATE_EPOCH={epoch}"
            f" produced different wheels: {h1} vs {h2}. Investigate hatchling"
            f" or the custom hatch_build.py hook."
        )


@pytest.mark.slow
@pytest.mark.xfail(
    reason=(
        "hatchling >= 1.20 is natively deterministic — an epoch-less rebuild "
        "already produces identical wheel bytes on the currently pinned "
        "hatchling. The epoch pin is defence-in-depth (defends against a "
        "future hatchling regression that reintroduces host-clock leakage), "
        "not the sole reproducibility knob. See the signed release chain chapter "
        "§'The three knobs' for the rationale. If a hatchling upgrade "
        "regresses on determinism this test will unexpectedly-PASS and the "
        "xfail marker itself will fail, alerting us to a real regression."
    ),
    strict=True,
)
def test_wheel_bytes_differ_without_epoch():
    """No epoch pin => rebuilds normally differ (proves the epoch is the knob).

    hatchling >= 1.20 is largely deterministic on its own; if this test
    passes an epoch-less rebuild without failure, the epoch is defence-
    in-depth rather than the sole reproducibility knob. The Plan 12 M4
    guidance calls for `xfail(strict=True)` in that case, which is where
    we are today — see the marker above.
    """
    env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
    with tempfile.TemporaryDirectory() as d1_str, tempfile.TemporaryDirectory() as d2_str:
        h1 = _sha256(_build(env, Path(d1_str)))
        time.sleep(2)  # let host clock advance so wheel mtimes could diverge
        h2 = _sha256(_build(env, Path(d2_str)))
        assert h1 != h2, (
            "No SOURCE_DATE_EPOCH pin, yet two rebuilds produced the same"
            " wheel bytes. hatchling looks natively deterministic — the epoch"
            " is now defence-in-depth rather than the sole knob. Update"
            " the signed release chain chapter and consider `@pytest.mark.xfail`"
            " on this test rather than removing the epoch pin."
        )
