# shared/nautilus/filters/adx.py
"""Nautilus-backed ADX filter (DirectionalMovement + ATR + WilderMA).

nautilus has no complete ADX class. Standard ADX is composed of three parts (all
Wilder-smoothed):
  - ``DirectionalMovement(period, WILDER)`` -> smoothed +DM/-DM (``.pos``/``.neg``)
  - ``AverageTrueRange(period, WILDER)``    -> ATR used to normalize the DI
  - ``WilderMovingAverage(period)``         -> smooths DX into ADX

  +DI = 100 * pos / ATR ; -DI = 100 * neg / ATR
  DX  = 100 * |+DI - -DI| / (+DI + -DI)
  ADX = WilderMA(DX)

WARNING: ``DirectionalMovement`` and ``AverageTrueRange`` both default to ma_type
EXPONENTIAL, but standard ADX requires WILDER, which must be passed explicitly
(otherwise it diverges from custos_toolkit_nautilus._vendor.pandas_ta by 10+ points).

Using a nautilus native-indicator composition instead of hand-written Wilder
smoothing eliminates the initialization-formula bug at the source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.indicators import (
    AverageTrueRange,
    DirectionalMovement,
    MovingAverageType,
    WilderMovingAverage,
)
from custos_toolkit.protocols.bar import BarProtocol
from custos_toolkit.protocols.filter import FilterResult

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.filters import AdxFilterConfig


class NautilusAdxFilter:
    """Filter trades by ADX trend strength via nautilus indicator composition.

    Takes a typed ``AdxFilterConfig`` (enabled / period / threshold).
    """

    def __init__(self, config: AdxFilterConfig):
        self.config = config
        self.enabled = config.enabled
        self.period = config.period
        self.threshold = config.threshold

        wilder = MovingAverageType.Wilder
        self._dm = DirectionalMovement(self.period, wilder)
        self._atr = AverageTrueRange(self.period, ma_type=wilder)
        self._adx_ma = WilderMovingAverage(self.period)

        self._plus_di: float | None = None
        self._minus_di: float | None = None
        self._ready = False

    @property
    def name(self) -> str:
        return "adx"

    def update(self, bar: BarProtocol) -> None:
        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)

        self._dm.update_raw(high, low)
        self._atr.update_raw(high, low, close)

        if not (self._dm.initialized and self._atr.initialized) or self._atr.value <= 0:
            return

        self._plus_di = 100.0 * self._dm.pos / self._atr.value
        self._minus_di = 100.0 * self._dm.neg / self._atr.value

        di_sum = self._plus_di + self._minus_di
        dx = 100.0 * abs(self._plus_di - self._minus_di) / di_sum if di_sum > 0 else 0.0

        self._adx_ma.update_raw(dx)
        if self._adx_ma.initialized:
            self._ready = True

    def check(self, bar: BarProtocol) -> FilterResult:
        if not self.enabled:
            return FilterResult.allow()

        if not self._ready:
            return FilterResult.block("ADX not yet calculated (warming up)")

        adx = self._adx_ma.value
        if adx < self.threshold:
            return FilterResult.block(f"Weak trend: ADX {adx:.1f} < threshold {self.threshold}")

        return FilterResult.allow()

    def is_ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        self._dm.reset()
        self._atr.reset()
        self._adx_ma.reset()
        self._plus_di = None
        self._minus_di = None
        self._ready = False

    def get_adx(self) -> float | None:
        return self._adx_ma.value if self._ready else None

    def get_plus_di(self) -> float | None:
        return self._plus_di

    def get_minus_di(self) -> float | None:
        return self._minus_di
