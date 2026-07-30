# tests/test_nautilus_filter_momentum.py
"""Tests for NautilusMomentumFilter (RSI / MACD / ROC).

Covers the unit conversions (RSI 0..1 scaled by 100, ROC as a fraction scaled by 100),
histogram = line minus signal, and absolute-value assertions. Also that the filter reads
the nested typed config — the old one read a flat key and ignored it.
"""

from dataclasses import dataclass

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config.filters import (  # noqa: E402
    MacdConfig,
    MomentumFilterConfig,
    RocConfig,
    RsiConfig,
)
from custos_toolkit_nautilus.adapter.filters import NautilusMomentumFilter  # noqa: E402
from nautilus_trader.indicators.averages import ExponentialMovingAverage  # noqa: E402
from nautilus_trader.indicators.momentum import (  # noqa: E402
    RateOfChange,
    RelativeStrengthIndex,
)
from nautilus_trader.indicators.trend import (  # noqa: E402
    MovingAverageConvergenceDivergence,
)


@dataclass
class MockBar:
    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 100.0
    volume: float = 1000.0
    timestamp: int = 0


class TestNautilusMomentumRSI:
    def test_rsi_value_is_scaled_to_0_100(self):
        """The engine's RSI runs 0..1 and must be scaled by 100 before comparing or reporting."""
        f = NautilusMomentumFilter(
            MomentumFilterConfig(enabled=True, indicator="rsi", rsi=RsiConfig(period=14))
        )
        ref = RelativeStrengthIndex(14)
        closes = [100 + (i % 5) - 2 for i in range(40)]
        for c in closes:
            f.update(MockBar(close=float(c)))
            ref.update_raw(float(c))
        assert f.get_rsi() is not None
        assert abs(f.get_rsi() - ref.value * 100.0) < 1e-9
        assert 0.0 <= f.get_rsi() <= 100.0

    def test_rsi_config_band_is_honored(self):
        """The filter must read the nested rsi config; the old one read a flat key and missed it."""
        closes = [100.0 + (1 if i % 2 == 0 else -1) * 0.5 for i in range(40)]

        f_wide = NautilusMomentumFilter(
            MomentumFilterConfig(
                enabled=True,
                indicator="rsi",
                rsi=RsiConfig(period=14, long_min=0, long_max=100),
            )
        )
        for c in closes:
            f_wide.update(MockBar(close=c))
        assert f_wide.check(MockBar(close=closes[-1])).passed is True

        # Narrowed to an impossible band, so it must block — which proves the config was read
        f_narrow = NautilusMomentumFilter(
            MomentumFilterConfig(
                enabled=True,
                indicator="rsi",
                rsi=RsiConfig(period=14, long_min=99, long_max=100),
            )
        )
        for c in closes:
            f_narrow.update(MockBar(close=c))
        assert f_narrow.check(MockBar(close=closes[-1])).passed is False

    def test_blocks_during_warmup(self):
        f = NautilusMomentumFilter(
            MomentumFilterConfig(enabled=True, indicator="rsi", rsi=RsiConfig(period=14))
        )
        assert f.check(MockBar()).passed is False


class TestNautilusMomentumROC:
    def test_roc_scaled_to_percent_and_threshold(self):
        """ROC is a fraction scaled to a percentage; positive momentum passes, negative blocks."""
        f = NautilusMomentumFilter(
            MomentumFilterConfig(
                enabled=True, indicator="roc", roc=RocConfig(period=10, long_threshold=0.0)
            )
        )
        last = MockBar()
        for i in range(20):
            last = MockBar(close=100.0 + i)  # monotonically rising, so ROC > 0
            f.update(last)
        assert f.get_roc() is not None
        assert f.get_roc() > 0.0
        assert f.check(last).passed is True

    def test_roc_negative_blocks(self):
        f = NautilusMomentumFilter(
            MomentumFilterConfig(
                enabled=True, indicator="roc", roc=RocConfig(period=10, long_threshold=0.0)
            )
        )
        last = MockBar()
        for i in range(20):
            last = MockBar(close=100.0 - i)  # falling, so ROC < 0
            f.update(last)
        assert f.check(last).passed is False

    def test_roc_absolute_value_matches_nautilus(self):
        """ROC is RateOfChange(period, use_log=False).value scaled by 100."""
        f = NautilusMomentumFilter(
            MomentumFilterConfig(
                enabled=True, indicator="roc", roc=RocConfig(period=10, long_threshold=0.0)
            )
        )
        ref = RateOfChange(10, False)
        closes = [100.0 + i * 0.5 - (i % 4) for i in range(25)]
        for c in closes:
            f.update(MockBar(close=c))
            ref.update_raw(c)
        assert f.get_roc() is not None
        assert abs(f.get_roc() - ref.value * 100.0) < 1e-9


class TestNautilusMomentumMACD:
    def _macd_config(self) -> MomentumFilterConfig:
        return MomentumFilterConfig(
            enabled=True,
            indicator="macd",
            macd=MacdConfig(fast=12, slow=26, signal=9, histogram_positive=True),
        )

    def test_macd_histogram_positive_allows(self):
        """histogram = macd.value minus signal_ema.value; rising puts it above zero, so allow."""
        f = NautilusMomentumFilter(self._macd_config())
        last = MockBar()
        for i in range(60):
            last = MockBar(close=100.0 + i * 0.8)  # sustained rise, so the line crosses signal
            f.update(last)
        macd = f.get_macd()
        assert macd is not None
        assert "histogram" in macd
        assert macd["histogram"] > 0
        assert f.check(last).passed is True

    def test_macd_histogram_negative_blocks(self):
        f = NautilusMomentumFilter(self._macd_config())
        # Rising then turning down, so the histogram goes negative
        last = MockBar()
        for i in range(40):
            last = MockBar(close=100.0 + i * 0.8)
            f.update(last)
        for i in range(30):
            last = MockBar(close=132.0 - i * 1.5)
            f.update(last)
        macd = f.get_macd()
        assert macd is not None and macd["histogram"] <= 0
        assert f.check(last).passed is False

    def test_macd_histogram_matches_independent_reference(self):
        """histogram = MACD line minus a signal EMA fed with the line — computed independently."""
        f = NautilusMomentumFilter(self._macd_config())
        ref_macd = MovingAverageConvergenceDivergence(12, 26)
        ref_signal = ExponentialMovingAverage(9)
        closes = [100.0 + i * 0.6 - (i % 5) for i in range(80)]
        for c in closes:
            f.update(MockBar(close=c))
            ref_macd.update_raw(c)
            if ref_macd.initialized:
                ref_signal.update_raw(ref_macd.value)
        got = f.get_macd()
        assert got is not None
        assert abs(got["macd_line"] - ref_macd.value) < 1e-9
        assert abs(got["signal_line"] - ref_signal.value) < 1e-9
        assert abs(got["histogram"] - (ref_macd.value - ref_signal.value)) < 1e-9


class TestNautilusMomentumMisc:
    def test_allows_when_disabled(self):
        f = NautilusMomentumFilter(MomentumFilterConfig(enabled=False, indicator="rsi"))
        assert f.check(MockBar()).passed is True

    def test_invalid_indicator_raises(self):
        with pytest.raises(ValueError):
            NautilusMomentumFilter(MomentumFilterConfig(enabled=True, indicator="bogus"))

    def test_no_shared_filters_import(self):
        import inspect
        import re

        import custos_toolkit_nautilus.adapter.filters.momentum as mod

        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
        assert not any("shared.filters" in ln or "..filters" in ln for ln in import_lines)
