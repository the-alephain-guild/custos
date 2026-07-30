# tests/test_nautilus_filter_volume.py
"""Tests for NautilusVolumeFilter (volume EMA/SMA)."""

from dataclasses import dataclass

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config.filters import VolumeFilterConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.filters import NautilusVolumeFilter  # noqa: E402
from nautilus_trader.indicators.averages import (  # noqa: E402
    ExponentialMovingAverage,
    SimpleMovingAverage,
)


@dataclass
class MockBar:
    open: float = 100.0
    high: float = 105.0
    low: float = 95.0
    close: float = 100.0
    volume: float = 1000.0
    timestamp: int = 0


class TestNautilusVolumeFilter:
    def test_blocks_during_warmup(self):
        f = NautilusVolumeFilter(VolumeFilterConfig(enabled=True, ma_period=20))
        assert f.check(MockBar(volume=1000.0)).passed is False
        assert "warming up" in f.check(MockBar(volume=1000.0)).reason

    def test_allows_when_disabled(self):
        f = NautilusVolumeFilter(VolumeFilterConfig(enabled=False, ma_period=5))
        assert f.check(MockBar()).passed is True

    def test_ema_matches_nautilus(self):
        config = VolumeFilterConfig(enabled=True, ma_period=5, threshold=1.0, ma_type="ema")
        f = NautilusVolumeFilter(config)
        ref = ExponentialMovingAverage(5)
        vols = [1000, 1200, 800, 1100, 900, 1300, 1000]
        for v in vols:
            f.update(MockBar(volume=float(v)))
            ref.update_raw(float(v))
        assert f.current_ma is not None
        assert abs(f.current_ma - ref.value) < 1e-9

    def test_sma_matches_nautilus(self):
        config = VolumeFilterConfig(enabled=True, ma_period=4, threshold=1.0, ma_type="sma")
        f = NautilusVolumeFilter(config)
        ref = SimpleMovingAverage(4)
        vols = [1000, 1200, 800, 1100, 900, 1300]
        for v in vols:
            f.update(MockBar(volume=float(v)))
            ref.update_raw(float(v))
        assert f.current_ma is not None
        assert abs(f.current_ma - ref.value) < 1e-9

    def test_passing_returns_no_amplification(self):
        """Volume filter is reduction-only: passing returns size_factor 1.0.

        FilterManager merges only factors < 1.0, so any amplification (>1.0) the volume
        filter advertised was dead -- the filter must not promise sizing it can't deliver.
        """
        config = VolumeFilterConfig(enabled=True, ma_period=3, threshold=1.0, ma_type="sma")
        f = NautilusVolumeFilter(config)
        for _ in range(3):
            f.update(MockBar(volume=1000.0))
        # MA=1000; current=2000 -> high volume passes, but no position amplification.
        result = f.check(MockBar(volume=2000.0))
        assert result.passed is True
        assert result.size_factor == 1.0

    def test_blocks_below_threshold(self):
        config = VolumeFilterConfig(enabled=True, ma_period=3, threshold=1.5, ma_type="sma")
        f = NautilusVolumeFilter(config)
        for _ in range(3):
            f.update(MockBar(volume=1000.0))
        # MA=1000; required=1500; current=900 → block
        result = f.check(MockBar(volume=900.0))
        assert result.passed is False
        assert "below threshold" in result.reason

    def test_zero_volume_ma_blocks(self):
        """A volume moving average at or below zero must block, not pass on a zero baseline."""
        config = VolumeFilterConfig(enabled=True, ma_period=3, threshold=1.0, ma_type="sma")
        f = NautilusVolumeFilter(config)
        for _ in range(3):
            f.update(MockBar(volume=0.0))  # warmed entirely on zero volume, so the MA is 0
        result = f.check(MockBar(volume=0.0))
        assert result.passed is False
        assert "zero" in result.reason.lower() or "MA" in result.reason

    def test_default_ma_type_is_ema(self):
        """ma_type defaults to ema, which is the struct default the manager relies on."""
        f = NautilusVolumeFilter(VolumeFilterConfig(enabled=True, ma_period=20))
        assert f._ma_type == "ema"

    def test_invalid_ma_type_raises(self):
        with pytest.raises(ValueError):
            NautilusVolumeFilter(VolumeFilterConfig(enabled=True, ma_period=5, ma_type="bogus"))

    def test_no_shared_filters_import(self):
        import inspect
        import re

        import custos_toolkit_nautilus.adapter.filters.volume as mod

        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
        assert not any("shared.filters" in ln or "..filters" in ln for ln in import_lines)
