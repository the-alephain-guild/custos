# tests/test_nautilus_filter_volatility.py
"""Tests for NautilusVolatilityFilter (nautilus-backed ATR).

Absolute-value assertions on known input/output pairs, not merely ranges or directions.
"""

from dataclasses import dataclass

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config.filters import VolatilityFilterConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.filters import NautilusVolatilityFilter  # noqa: E402
from nautilus_trader.indicators.volatility import AverageTrueRange  # noqa: E402


@dataclass
class MockBar:
    """Mock bar for testing (BarProtocol-shaped)."""

    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 102.0
    volume: float = 1000.0
    timestamp: int = 0


def _warmed(
    config: VolatilityFilterConfig, high: float, low: float, close: float
) -> NautilusVolatilityFilter:
    f = NautilusVolatilityFilter(config)
    for _ in range(config.atr_lookback):
        f.update(MockBar(high=high, low=low, close=close))
    return f


class TestNautilusVolatilityFilter:
    def test_blocks_during_warmup(self):
        f = NautilusVolatilityFilter(VolatilityFilterConfig(enabled=True, atr_lookback=14))
        result = f.check(MockBar())
        assert result.passed is False
        assert "warming up" in result.reason

    def test_ready_after_warmup(self):
        f = NautilusVolatilityFilter(VolatilityFilterConfig(enabled=True, atr_lookback=5))
        for i in range(5):
            f.update(MockBar(high=100.0 + i, low=95.0 + i, close=98.0 + i))
        assert f.is_ready() is True

    def test_atr_absolute_value_matches_nautilus(self):
        """The ATR must equal what the engine's AverageTrueRange gives on the same data."""
        config = VolatilityFilterConfig(
            enabled=True, atr_lookback=3, min_atr_pct=0.0, max_atr_pct=100.0
        )
        f = NautilusVolatilityFilter(config)
        ref = AverageTrueRange(3)
        bars = [(105.0, 95.0, 100.0), (110.0, 105.0, 108.0), (108.0, 102.0, 104.0)]
        for h, low, c in bars:
            f.update(MockBar(high=h, low=low, close=c))
            ref.update_raw(h, low, c)
        assert f.get_atr() is not None
        assert abs(f.get_atr() - ref.value) < 1e-9

    def test_btc_scale_normal_volatility_allows(self):
        """At a price of 100000 an ATR of 600 is 0.6%, inside [0.3%, 5%], so allowed."""
        config = VolatilityFilterConfig(
            enabled=True, atr_lookback=3, min_atr_pct=0.003, max_atr_pct=0.05
        )
        f = _warmed(config, high=100300.0, low=99700.0, close=100000.0)
        result = f.check(MockBar(high=100300.0, low=99700.0, close=100000.0))
        assert result.passed is True, f"0.6% ATR within [0.3%, 5%] must pass: {result.reason}"

    def test_btc_scale_low_volatility_blocks(self):
        config = VolatilityFilterConfig(
            enabled=True, atr_lookback=3, min_atr_pct=0.003, max_atr_pct=0.05
        )
        f = _warmed(config, high=100050.0, low=99950.0, close=100000.0)
        result = f.check(MockBar(high=100050.0, low=99950.0, close=100000.0))
        assert result.passed is False
        assert "too low" in result.reason

    def test_btc_scale_high_volatility_blocks(self):
        config = VolatilityFilterConfig(
            enabled=True, atr_lookback=3, min_atr_pct=0.003, max_atr_pct=0.05
        )
        f = _warmed(config, high=103000.0, low=97000.0, close=100000.0)
        result = f.check(MockBar(high=103000.0, low=97000.0, close=100000.0))
        assert result.passed is False
        assert "too high" in result.reason

    def test_allows_when_disabled(self):
        f = NautilusVolatilityFilter(VolatilityFilterConfig(enabled=False, atr_lookback=3))
        assert f.check(MockBar()).passed is True

    def test_handles_zero_price(self):
        config = VolatilityFilterConfig(
            enabled=True, atr_lookback=2, min_atr_pct=0.0, max_atr_pct=100.0
        )
        f = _warmed(config, high=105.0, low=95.0, close=100.0)
        result = f.check(MockBar(close=0.0))
        assert result.passed is False
        assert "Invalid price" in result.reason

    def test_default_config_decimal_semantics(self):
        """Typed default: enabled=False (Struct default), ratio fields are decimals."""
        f = NautilusVolatilityFilter(VolatilityFilterConfig())
        assert f.enabled is False
        assert f.atr_lookback == 14
        assert f.min_atr_pct == 0.003
        assert f.max_atr_pct == 0.05

    def test_no_shared_filters_import(self):
        """The engine-backed filter must not import the platform-neutral one — imports only."""
        import inspect
        import re

        import custos_toolkit_nautilus.adapter.filters.volatility as mod

        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
        assert not any("shared.filters" in ln or "..filters" in ln for ln in import_lines)
