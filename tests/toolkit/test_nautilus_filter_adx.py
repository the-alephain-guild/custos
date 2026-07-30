# tests/test_nautilus_filter_adx.py
"""Tests for NautilusAdxFilter (DM + ATR + WilderMA composition).

The ADX formula once had a bug, and this retires it: absolute-value assertions against
a known data pair from the reference implementation, so the wiring yields standard ADX
"""

from dataclasses import dataclass

import pytest

pytest.importorskip("nautilus_trader")
pandas_ta = pytest.importorskip("pandas_ta")
import pandas as pd  # noqa: E402
from custos_toolkit_nautilus.adapter.config.filters import AdxFilterConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.filters import NautilusAdxFilter  # noqa: E402


@dataclass
class MockBar:
    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 100.0
    volume: float = 1000.0
    timestamp: int = 0


def _synthetic_ohlc(n: int = 80) -> pd.DataFrame:
    """Deterministic synthetic OHLC — a sine trend plus fixed noise, nothing random."""
    import math

    rows = []
    price = 100.0
    for i in range(n):
        drift = 0.5 * math.sin(i / 8.0) + 0.15
        noise = ((i * 37) % 11 - 5) * 0.12
        o = price
        price = price + drift + noise
        h = max(o, price) + abs(noise) * 0.6 + 0.5
        low = min(o, price) - abs(noise) * 0.6 - 0.5
        rows.append((o, h, low, price))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


class TestNautilusAdxFilter:
    def test_blocks_during_warmup(self):
        f = NautilusAdxFilter(AdxFilterConfig(enabled=True, period=14, threshold=25))
        assert f.check(MockBar()).passed is False
        assert "warming up" in f.check(MockBar()).reason

    def test_allows_when_disabled(self):
        f = NautilusAdxFilter(AdxFilterConfig(enabled=False, period=14))
        assert f.check(MockBar()).passed is True

    def test_strong_uptrend_high_adx_allows(self):
        """A strong one-way trend gives a high ADX, which is allowed through."""
        f = NautilusAdxFilter(AdxFilterConfig(enabled=True, period=14, threshold=25))
        price = 100.0
        last = MockBar()
        for _ in range(60):
            o = price
            price += 1.0
            last = MockBar(open=o, high=price + 0.3, low=o - 0.2, close=price)
            f.update(last)
        assert f.is_ready() is True
        assert f.get_adx() > 25.0
        assert f.check(last).passed is True

    def test_choppy_low_adx_blocks(self):
        """A ranging market gives a low ADX, which is blocked."""
        f = NautilusAdxFilter(AdxFilterConfig(enabled=True, period=14, threshold=40))
        base = 100.0
        last = MockBar()
        for i in range(80):
            o = base + (0.5 if i % 2 == 0 else -0.5)
            c = base + (-0.5 if i % 2 == 0 else 0.5)
            last = MockBar(open=o, high=max(o, c) + 0.2, low=min(o, c) - 0.2, close=c)
            f.update(last)
        assert f.is_ready() is True
        assert f.get_adx() < 40.0
        assert f.check(last).passed is False
        assert "Weak trend" in f.check(last).reason

    def test_adx_absolute_value_matches_pandas_ta(self):
        """The ADX value must match the reference implementation within a tolerance of 1.0."""
        df = _synthetic_ohlc(80)
        P = 14
        f = NautilusAdxFilter(AdxFilterConfig(enabled=True, period=P, threshold=25))
        for _, r in df.iterrows():
            f.update(MockBar(open=r.open, high=r.high, low=r.low, close=r.close))

        ref = pandas_ta.adx(df.high, df.low, df.close, length=P)[f"ADX_{P}"].iloc[-1]
        assert f.get_adx() is not None
        assert abs(f.get_adx() - float(ref)) < 1.0, (
            f"nautilus ADX {f.get_adx():.3f} vs pandas_ta {float(ref):.3f}"
        )

    def test_di_normalization_exposed(self):
        """+DI and -DI come from dm.pos and dm.neg normalised by ATR, not from raw dm."""
        df = _synthetic_ohlc(60)
        f = NautilusAdxFilter(AdxFilterConfig(enabled=True, period=14))
        for _, r in df.iterrows():
            f.update(MockBar(open=r.open, high=r.high, low=r.low, close=r.close))
        assert f.get_plus_di() is not None and 0.0 <= f.get_plus_di() <= 100.0
        assert f.get_minus_di() is not None and 0.0 <= f.get_minus_di() <= 100.0

    def test_no_shared_filters_import(self):
        import inspect
        import re

        import custos_toolkit_nautilus.adapter.filters.adx as mod

        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
        assert not any("shared.filters" in ln or "..filters" in ln for ln in import_lines)
