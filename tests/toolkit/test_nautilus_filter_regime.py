# tests/test_nautilus_filter_regime.py
"""Tests for NautilusRegimeFilter (native EfficiencyRatio + atr_percentile business)."""

from dataclasses import dataclass

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config.filters import RegimeFilterConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.filters import NautilusRegimeFilter  # noqa: E402
from nautilus_trader.indicators.momentum import EfficiencyRatio  # noqa: E402


@dataclass
class MockBar:
    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 100.0
    volume: float = 1000.0
    timestamp: int = 0


class TestNautilusRegimeEfficiencyRatio:
    def test_er_matches_nautilus(self):
        f = NautilusRegimeFilter(
            RegimeFilterConfig(enabled=True, method="efficiency_ratio", lookback=10)
        )
        ref = EfficiencyRatio(10)
        closes = [100 + i * 0.7 - (i % 3) for i in range(30)]
        for c in closes:
            f.update(MockBar(close=float(c)))
            ref.update_raw(float(c))
        assert f.get_efficiency_ratio() is not None
        assert abs(f.get_efficiency_ratio() - ref.value) < 1e-9

    def test_trending_allows(self):
        """A one-way rise gives a high efficiency ratio, reads as trending, and is allowed."""
        f = NautilusRegimeFilter(
            RegimeFilterConfig(
                enabled=True,
                method="efficiency_ratio",
                lookback=10,
                trending_threshold=0.5,
            )
        )
        last = MockBar()
        for i in range(20):
            last = MockBar(close=100.0 + i)
            f.update(last)
        assert f.get_current_regime() == "trending"
        assert f.check(last).passed is True

    def test_ranging_blocks(self):
        """Ranging gives a low efficiency ratio, reads as ranging, and is blocked."""
        f = NautilusRegimeFilter(
            RegimeFilterConfig(
                enabled=True,
                method="efficiency_ratio",
                lookback=10,
                trending_threshold=0.5,
            )
        )
        last = MockBar()
        for i in range(30):
            last = MockBar(close=100.0 + (1 if i % 2 == 0 else -1))
            f.update(last)
        assert f.get_current_regime() == "ranging"
        assert f.check(last).passed is False


class TestNautilusRegimeAtrPercentile:
    def test_atr_percentile_business_logic(self):
        """The atr_percentile method keeps its own price-range-over-average maths."""
        f = NautilusRegimeFilter(
            RegimeFilterConfig(
                enabled=True,
                method="atr_percentile",
                lookback=10,
                trending_threshold=0.1,
            )
        )
        last = MockBar()
        for i in range(15):
            last = MockBar(close=100.0 + i * 2)  # wide range, so range/avg is high — trending
            f.update(last)
        assert f.get_current_regime() in ("trending", "ranging")
        assert f.is_ready() is True


class TestNautilusRegimeMisc:
    def test_blocks_during_warmup(self):
        f = NautilusRegimeFilter(
            RegimeFilterConfig(enabled=True, method="efficiency_ratio", lookback=20)
        )
        assert f.check(MockBar()).passed is False

    def test_allow_regime_both(self):
        f = NautilusRegimeFilter(
            RegimeFilterConfig(
                enabled=True,
                method="efficiency_ratio",
                lookback=5,
                allow_regime="both",
            )
        )
        last = MockBar()
        for i in range(10):
            last = MockBar(close=100.0 + (1 if i % 2 == 0 else -1))
            f.update(last)
        assert f.check(last).passed is True

    def test_default_allow_regime_is_trending(self):
        """allow_regime defaults to trending, which is the struct default the manager relies on."""
        f = NautilusRegimeFilter(RegimeFilterConfig(enabled=True, method="efficiency_ratio"))
        assert f.allow_regime == "trending"

    def test_allows_when_disabled(self):
        f = NautilusRegimeFilter(RegimeFilterConfig(enabled=False))
        assert f.check(MockBar()).passed is True

    def test_invalid_method_raises(self):
        with pytest.raises(ValueError):
            NautilusRegimeFilter(RegimeFilterConfig(enabled=True, method="bogus"))

    def test_does_not_import_the_platform_neutral_filters(self):
        import inspect
        import re

        import custos_toolkit_nautilus.adapter.filters.regime as mod

        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
        assert not any("custos_toolkit.filters" in ln or "..filters" in ln for ln in import_lines)
