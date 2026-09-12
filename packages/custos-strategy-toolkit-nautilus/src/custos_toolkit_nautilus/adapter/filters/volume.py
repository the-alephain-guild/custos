# shared/nautilus/filters/volume.py
"""Nautilus-backed volume filter (volume EMA/SMA).

Computes the volume moving average with nautilus ``ExponentialMovingAverage`` /
``SimpleMovingAverage`` (fed ``bar.volume``); a bar passes when current volume meets
``ma * threshold``. Reduction-only: it never amplifies position size.

Note: FilterManager does not pass ``ma_type``; it defaults to ``ema`` (matching the
legacy VolumeFilter).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custos_toolkit.protocols.bar import BarProtocol
from custos_toolkit.protocols.filter import FilterResult
from nautilus_trader.indicators import ExponentialMovingAverage, SimpleMovingAverage

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.filters import VolumeFilterConfig


class NautilusVolumeFilter:
    """Filter trades by volume relative to its moving average.

    Takes a typed ``VolumeFilterConfig`` (enabled / ma_period / threshold / ma_type).
    """

    def __init__(self, config: VolumeFilterConfig):
        self.config = config
        self.enabled = config.enabled
        self._ma_period = config.ma_period
        self._threshold = config.threshold
        self._ma_type = config.ma_type.lower()

        if self._ma_type not in ("ema", "sma"):
            raise ValueError(f"Invalid ma_type: {self._ma_type}. Must be 'ema' or 'sma'")

        self._ma: ExponentialMovingAverage | SimpleMovingAverage
        if self._ma_type == "ema":
            self._ma = ExponentialMovingAverage(self._ma_period)
        else:
            self._ma = SimpleMovingAverage(self._ma_period)

        self._ready = False

    @property
    def name(self) -> str:
        return "volume"

    def update(self, bar: BarProtocol) -> None:
        self._ma.update_raw(float(bar.volume))
        if self._ma.initialized:
            self._ready = True

    def check(self, bar: BarProtocol) -> FilterResult:
        if not self.enabled:
            return FilterResult.allow()

        if not self._ready or not self._ma.initialized:
            return FilterResult.block("Volume filter not ready (warming up)")

        current_volume = float(bar.volume)
        volume_ma = self._ma.value

        # No meaningful baseline (e.g. all-zero warmup / illiquid bars) → block
        # rather than pass on a zero threshold.
        if volume_ma <= 0:
            return FilterResult.block("Volume MA is zero (no meaningful baseline)")

        required_volume = volume_ma * self._threshold

        if current_volume >= required_volume:
            # Reduction-only contract: high volume passes at full size. FilterManager
            # merges only factors < 1.0, so this filter never amplifies position size.
            return FilterResult.allow()

        ratio = current_volume / volume_ma if volume_ma > 0 else 0
        return FilterResult.block(
            f"Volume {current_volume:.0f} below threshold "
            f"({ratio:.2f}x avg, need {self._threshold:.2f}x)"
        )

    def is_ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        self._ma.reset()
        self._ready = False

    @property
    def current_ma(self) -> float | None:
        return self._ma.value if self._ma.initialized else None
