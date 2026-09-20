"""SuperTrend must agree with the same algorithm run over the whole history.

SuperTrend is path dependent: each bar's bands are clamped against the previous
bar's already-clamped bands, the direction carries the previous direction, and
the ATR underneath is a Wilder recursion. Recomputing a rolling window therefore
re-derives history from a moving starting point, and the answer drifts.

The reference here is the vendored pandas-ta over the full series -- the same
implementation the indicator used to call, just never truncated.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("pandas")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from custos_toolkit_nautilus.adapter.indicators._pandas_ta import ta  # noqa: E402
from custos_toolkit_nautilus.adapter.indicators.supertrend import SuperTrend  # noqa: E402


def _reference(highs, lows, closes, length, multiplier):
    """Direction per bar, from the untruncated series."""
    frame = ta.supertrend(
        pd.Series(highs),
        pd.Series(lows),
        pd.Series(closes),
        length=length,
        multiplier=multiplier,
    )
    suffix = f"{length}_{float(multiplier)}"
    return list(frame[f"SUPERTd_{suffix}"]), list(frame[f"SUPERT_{suffix}"])


def _streamed(highs, lows, closes, length, multiplier):
    """Direction per bar, fed one at a time as the strategy would."""
    indicator = SuperTrend(length=length, multiplier=multiplier)
    directions, values = [], []
    for high, low, close in zip(highs, lows, closes, strict=True):
        indicator.update_raw(high, low, close)
        directions.append(indicator.trend)
        values.append(indicator.value)
    return directions, values


def _step_down_then_flat():
    """The review's sequence: flat, a decline, then a long flat stretch."""
    closes = [200.0] * 15 + list(np.arange(195.0, 99.0, -5.0)) + [100.0] * 100
    return [c + 1 for c in closes], [c - 1 for c in closes], closes


def _random_walk(n, seed):
    rng = np.random.default_rng(seed)
    walk = 100 + np.cumsum(rng.normal(0, 1.5, n))
    return (
        list(walk + rng.uniform(0.1, 2.0, n)),
        list(walk - rng.uniform(0.1, 2.0, n)),
        list(walk),
    )


def _compare(highs, lows, closes, length, multiplier):
    """Directions must match once both sides have a value; values must too."""
    ref_dir, ref_val = _reference(highs, lows, closes, length, multiplier)
    got_dir, got_val = _streamed(highs, lows, closes, length, multiplier)
    mismatches = [
        (i, got_dir[i], ref_dir[i])
        for i in range(len(closes))
        if got_dir[i] != 0 and got_dir[i] != ref_dir[i]
    ]
    priced = [
        (got_val[i], ref_val[i])
        for i in range(len(closes))
        if got_dir[i] != 0 and ref_val[i] == ref_val[i]
    ]
    return mismatches, priced


class TestSuperTrendMatchesTheFullHistory:
    def test_the_review_sequence_never_flips_on_a_flat_price(self):
        """The headline case: 100 flat bars must not turn a short into a long."""
        highs, lows, closes = _step_down_then_flat()

        mismatches, _ = _compare(highs, lows, closes, 10, 3.0)

        assert mismatches == [], f"first divergence at bar {mismatches[0][0]}" if mismatches else ""

    @pytest.mark.parametrize("length,multiplier", [(7, 3.0), (10, 3.0), (14, 2.0)])
    def test_the_review_sequence_agrees_for_common_parameters(self, length, multiplier):
        highs, lows, closes = _step_down_then_flat()

        mismatches, priced = _compare(highs, lows, closes, length, multiplier)

        assert mismatches == []
        assert np.allclose([a for a, _ in priced], [b for _, b in priced], rtol=1e-9, atol=1e-9)

    @pytest.mark.parametrize(
        "n,seed,length,multiplier", [(150, 7, 7, 1.5), (300, 11, 10, 3.0), (500, 13, 14, 2.0)]
    )
    def test_a_random_walk_agrees_bar_for_bar(self, n, seed, length, multiplier):
        highs, lows, closes = _random_walk(n, seed)

        mismatches, priced = _compare(highs, lows, closes, length, multiplier)

        assert mismatches == []
        assert np.allclose([a for a, _ in priced], [b for _, b in priced], rtol=1e-9, atol=1e-9)

    def test_a_degenerate_bar_where_high_equals_low(self):
        """non_zero_range's epsilon path must not knock the streams apart."""
        flat = [100.0] * 200

        mismatches, _ = _compare(flat, flat, flat, 10, 3.0)

        assert mismatches == []

    def test_a_turning_point_older_than_the_window_is_still_honoured(self):
        """A trend set long ago must survive once its bars age out.

        This is the property a rolling recompute cannot have: 400 flat bars is
        far more than any window, so a window-based implementation re-derives the
        trend from flat data alone.
        """
        closes = [100.0] * 20 + list(np.arange(105.0, 201.0, 5.0)) + [200.0] * 400
        highs, lows = [c + 1 for c in closes], [c - 1 for c in closes]

        mismatches, _ = _compare(highs, lows, closes, 10, 3.0)

        assert mismatches == []


class TestTheSnapshotCarriesTheRecursion:
    """The snapshot has to restore the recursion, not a window of prices.

    Replaying a price window would restart the recursion at that window's first
    bar -- which is the defect the recursion replaced.
    """

    def test_a_restored_indicator_continues_the_same_series(self):
        highs, lows, closes = _random_walk(200, 3)
        split = 120

        whole = SuperTrend(length=10, multiplier=3.0)
        for high, low, close in zip(highs, lows, closes, strict=True):
            whole.update_raw(high, low, close)

        first = SuperTrend(length=10, multiplier=3.0)
        for high, low, close in zip(highs[:split], lows[:split], closes[:split], strict=True):
            first.update_raw(high, low, close)
        resumed = SuperTrend(length=10, multiplier=3.0)
        resumed.from_snapshot(first.to_snapshot())
        for high, low, close in zip(highs[split:], lows[split:], closes[split:], strict=True):
            resumed.update_raw(high, low, close)

        assert resumed.trend == whole.trend
        assert resumed.value == pytest.approx(whole.value, rel=1e-12)

    def test_a_version_1_snapshot_is_refused(self):
        """A price-window snapshot cannot restore this indicator; say so."""
        indicator = SuperTrend(length=10, multiplier=3.0)

        with pytest.raises(ValueError, match="version mismatch"):
            indicator.from_snapshot(
                {
                    "version": 1,
                    "params": {"length": 10, "multiplier": 3.0},
                    "data": {"highs": [1.0], "lows": [1.0], "closes": [1.0]},
                    "state": {},
                }
            )
