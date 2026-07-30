# tests/test_nautilus_filter_regime_t9.py
"""Regime filter correctness.

#13 adx_slope: a real regime method (was advertised in config/schema but absent
    from the implementation, so selecting it raised). Trend = rising ADX.
#14 atr_percentile: must use its own ``range_pct_threshold`` (price-range scale
    ~0.02-0.05), not the efficiency-ratio ``trending_threshold`` (0..1) -- sharing
    0.5 made the price-range ratio (typically <0.1) always read as "ranging".

Slope classification is asserted on absolute injected ADX values, because the
ADX values themselves are pandas_ta-regressed in test_nautilus_filter_adx).
"""

from collections import deque
from dataclasses import dataclass

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config.filters import RegimeFilterConfig
from custos_toolkit_nautilus.adapter.filters import NautilusRegimeFilter


@dataclass
class MockBar:
    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 100.0
    volume: float = 1000.0
    timestamp: int = 0


# --- #13 adx_slope ---


def test_adx_slope_is_a_valid_method():
    """Selecting adx_slope must construct without raising (was rejected)."""
    f = NautilusRegimeFilter(RegimeFilterConfig(enabled=True, method="adx_slope", lookback=5))
    assert f.method == "adx_slope"


def test_adx_slope_rising_is_trending():
    f = NautilusRegimeFilter(RegimeFilterConfig(enabled=True, method="adx_slope", lookback=1))
    # rising ADX over the window -> positive slope -> trending
    f._adx_values = deque([20.0, 30.0], maxlen=2)
    assert f._classify_adx_slope() == "trending"


def test_adx_slope_falling_is_ranging():
    f = NautilusRegimeFilter(RegimeFilterConfig(enabled=True, method="adx_slope", lookback=1))
    # falling ADX -> non-positive slope -> ranging
    f._adx_values = deque([30.0, 20.0], maxlen=2)
    assert f._classify_adx_slope() == "ranging"


def test_adx_slope_warming_up_is_unknown():
    f = NautilusRegimeFilter(RegimeFilterConfig(enabled=True, method="adx_slope", lookback=5))
    f._adx_values = deque([25.0], maxlen=6)  # fewer than lookback+1
    assert f._classify_adx_slope() == "unknown"


def test_adx_slope_reset_clears_state():
    """reset() must clear the adx_slope indicators + value deque, not just efficiency
    ratio / price state (otherwise stale ADX values survive a reset)."""
    f = NautilusRegimeFilter(
        RegimeFilterConfig(enabled=True, method="adx_slope", lookback=5, adx_period=14)
    )
    for i in range(60):
        price = 100.0 + i * 2.0
        f.update(MockBar(open=price, high=price + 1.0, low=price - 1.0, close=price))
    assert f.is_ready() is True
    assert len(f._adx_values) > 0

    f.reset()
    assert f.is_ready() is False
    assert f.get_current_regime() == "unknown"
    assert len(f._adx_values) == 0
    assert f._adx_ma.initialized is False


def test_adx_slope_runs_end_to_end_and_becomes_ready():
    """A strongly trending series feeds the real ADX combo and yields a valid regime."""
    f = NautilusRegimeFilter(
        RegimeFilterConfig(enabled=True, method="adx_slope", lookback=5, adx_period=14)
    )
    last = MockBar()
    for i in range(60):
        price = 100.0 + i * 2.0
        last = MockBar(open=price, high=price + 1.0, low=price - 1.0, close=price)
        f.update(last)
    assert f.is_ready() is True
    assert f.get_current_regime() in ("trending", "ranging")


# --- #14 atr_percentile independent threshold ---


def test_atr_percentile_uses_range_pct_threshold_not_trending_threshold():
    """prices 100..118 over lookback=10: range_pct = 18/109 ~= 0.165. With
    range_pct_threshold=0.05 it is trending even though trending_threshold=0.5
    (the old shared scale) would have called it ranging."""
    f = NautilusRegimeFilter(
        RegimeFilterConfig(
            enabled=True,
            method="atr_percentile",
            lookback=10,
            trending_threshold=0.5,  # efficiency-ratio scale -- must NOT be used here
            range_pct_threshold=0.05,
        )
    )
    last = MockBar()
    for i in range(10):
        last = MockBar(close=100.0 + i * 2.0)  # 100,102,...,118
        f.update(last)
    assert f.get_current_regime() == "trending"


def test_atr_percentile_below_range_pct_threshold_is_ranging():
    f = NautilusRegimeFilter(
        RegimeFilterConfig(
            enabled=True,
            method="atr_percentile",
            lookback=10,
            range_pct_threshold=0.2,  # 0.165 < 0.2 -> ranging
        )
    )
    last = MockBar()
    for i in range(10):
        last = MockBar(close=100.0 + i * 2.0)
        f.update(last)
    assert f.get_current_regime() == "ranging"


# --- schema <-> impl guard (#13): every config's regime method enum must equal the
# implemented method set, so the UI can't offer a method the filter rejects ---
