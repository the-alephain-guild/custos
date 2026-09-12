# shared/nautilus/filters/regime.py
"""Nautilus-backed regime filter (EfficiencyRatio native + atr_percentile / adx_slope).

  - method="efficiency_ratio": nautilus ``EfficiencyRatio``; trending if ratio >=
    ``trending_threshold`` (0..1 scale).
  - method="atr_percentile": business price-range computation; trending if
    (range / avg) >= ``range_pct_threshold`` (decimal price-range scale).
  - method="adx_slope": ADX (nautilus DM+ATR+WilderMA combo, all WILDER) over time;
    trending if ADX is rising (slope > 0) across ``lookback``.

Note: FilterManager does not pass ``allow_regime``; it defaults to "trending"
(consistent with the legacy RegimeFilter).
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from custos_toolkit.protocols.bar import BarProtocol
from custos_toolkit.protocols.filter import FilterResult
from nautilus_trader.indicators import (
    AverageTrueRange,
    DirectionalMovement,
    EfficiencyRatio,
    MovingAverageType,
    WilderMovingAverage,
)

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.filters import RegimeFilterConfig

_VALID_METHODS = ("efficiency_ratio", "atr_percentile", "adx_slope")
_VALID_REGIMES = ("trending", "ranging", "both")


class NautilusRegimeFilter:
    """Filter trades by detected market regime.

    Takes a typed ``RegimeFilterConfig``. Each method reads its own threshold
    (trending_threshold / range_pct_threshold) so scales don't collide.
    """

    def __init__(self, config: RegimeFilterConfig):
        self.config = config
        self.enabled = config.enabled
        self.method = config.method
        self.lookback = config.lookback
        self.trending_threshold = config.trending_threshold
        self.range_pct_threshold = config.range_pct_threshold
        self.adx_period = config.adx_period
        self.allow_regime = config.allow_regime

        if self.method not in _VALID_METHODS:
            raise ValueError(f"Invalid method '{self.method}'. Must be one of: {_VALID_METHODS}")
        if self.allow_regime not in _VALID_REGIMES:
            raise ValueError(
                f"Invalid allow_regime '{self.allow_regime}'. Must be one of: {_VALID_REGIMES}"
            )

        # Per-method state: efficiency_ratio -> nautilus indicator; atr_percentile ->
        # price deque; adx_slope -> ADX combo + a deque of ADX values for the slope.
        self._er: EfficiencyRatio | None = None
        self._prices: deque[float] | None = None
        self._adx_dm: DirectionalMovement | None = None
        self._adx_atr: AverageTrueRange | None = None
        self._adx_ma: WilderMovingAverage | None = None
        self._adx_values: deque[float] | None = None
        if self.method == "efficiency_ratio":
            self._er = EfficiencyRatio(self.lookback)
        elif self.method == "atr_percentile":
            self._prices = deque(maxlen=self.lookback)
        else:  # adx_slope
            wilder = MovingAverageType.Wilder
            self._adx_dm = DirectionalMovement(self.adx_period, wilder)
            self._adx_atr = AverageTrueRange(self.adx_period, ma_type=wilder)
            self._adx_ma = WilderMovingAverage(self.adx_period)
            self._adx_values = deque(maxlen=self.lookback + 1)

        self._current_regime = "unknown"
        self._ready = False

    @property
    def name(self) -> str:
        return "regime"

    def update(self, bar: BarProtocol) -> None:
        if self.method == "efficiency_ratio":
            er = self._er
            assert er is not None
            er.update_raw(float(bar.close))
            if er.initialized:
                self._ready = True
                self._current_regime = (
                    "trending" if er.value >= self.trending_threshold else "ranging"
                )
            else:
                self._ready = False
                self._current_regime = "unknown"
        elif self.method == "atr_percentile":
            prices = self._prices
            assert prices is not None
            prices.append(float(bar.close))
            if len(prices) >= self.lookback:
                self._ready = True
                self._current_regime = self._classify_atr_percentile()
            else:
                self._ready = False
                self._current_regime = "unknown"
        else:  # adx_slope
            self._update_adx_slope(bar)

    def _update_adx_slope(self, bar: BarProtocol) -> None:
        adx_dm = self._adx_dm
        adx_atr = self._adx_atr
        adx_ma = self._adx_ma
        adx_values = self._adx_values
        assert adx_dm is not None
        assert adx_atr is not None
        assert adx_ma is not None
        assert adx_values is not None
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        adx_dm.update_raw(high, low)
        adx_atr.update_raw(high, low, close)
        if not (adx_dm.initialized and adx_atr.initialized) or adx_atr.value <= 0:
            self._ready = False
            self._current_regime = "unknown"
            return

        plus_di = 100.0 * adx_dm.pos / adx_atr.value
        minus_di = 100.0 * adx_dm.neg / adx_atr.value
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
        adx_ma.update_raw(dx)
        if adx_ma.initialized:
            adx_values.append(adx_ma.value)

        if len(adx_values) >= self.lookback + 1:
            self._ready = True
            self._current_regime = self._classify_adx_slope()
        else:
            self._ready = False
            self._current_regime = "unknown"

    def _classify_adx_slope(self) -> str:
        # Rising ADX over the lookback window = strengthening trend.
        adx_values = self._adx_values
        assert adx_values is not None
        if len(adx_values) < self.lookback + 1:
            return "unknown"
        slope = adx_values[-1] - adx_values[0]
        return "trending" if slope > 0 else "ranging"

    def _classify_atr_percentile(self) -> str:
        raw_prices = self._prices
        assert raw_prices is not None
        prices = list(raw_prices)
        if len(prices) < 2:
            return "unknown"
        price_range = max(prices) - min(prices)
        avg_price = sum(prices) / len(prices)
        if avg_price > 0:
            range_pct = price_range / avg_price
            return "trending" if range_pct >= self.range_pct_threshold else "ranging"
        return "unknown"

    def check(self, bar: BarProtocol) -> FilterResult:
        if not self.enabled:
            return FilterResult.allow()

        if not self._ready:
            return FilterResult.block(f"Regime not ready (warming up, need {self.lookback} bars)")

        if self.allow_regime == "both":
            return FilterResult.allow()

        if self._current_regime == self.allow_regime:
            return FilterResult.allow()

        return FilterResult.block(
            f"Regime '{self._current_regime}' not allowed (requires '{self.allow_regime}')"
        )

    def is_ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        if self._er is not None:
            self._er.reset()
        if self._prices is not None:
            self._prices.clear()
        # adx_slope state (no-op for the other methods, which leave these None).
        if self._adx_dm is not None:
            self._adx_dm.reset()
        if self._adx_atr is not None:
            self._adx_atr.reset()
        if self._adx_ma is not None:
            self._adx_ma.reset()
        if self._adx_values is not None:
            self._adx_values.clear()
        self._current_regime = "unknown"
        self._ready = False

    def get_current_regime(self) -> str:
        return self._current_regime

    def get_efficiency_ratio(self) -> float | None:
        if self._er is None or not self._er.initialized:
            return None
        return self._er.value
