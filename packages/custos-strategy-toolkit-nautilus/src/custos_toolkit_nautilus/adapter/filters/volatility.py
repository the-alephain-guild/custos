# shared/nautilus/filters/volatility.py
"""Nautilus-backed volatility filter (ATR).

Indicator computation uses nautilus native ``AverageTrueRange`` (layer 0), with the
decision logic (ATR/price band) inlined (layer 2). Does not depend on
``custos_toolkit.filters`` -- a nautilus-path-specific implementation.

Unit: ratios use decimal semantics end to end (0.003 = 0.3%), matching base_config.yaml.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.indicators import AverageTrueRange
from custos_toolkit.protocols.bar import BarProtocol
from custos_toolkit.protocols.filter import FilterResult

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.filters import VolatilityFilterConfig


class NautilusVolatilityFilter:
    """Filter trades by ATR percentage, computed via nautilus AverageTrueRange.

    Takes a typed ``VolatilityFilterConfig``. ATR bounds are decimal fractions
    (0.003 = 0.3%), matching base_config.yaml.
    """

    def __init__(self, config: VolatilityFilterConfig):
        self.config = config
        self.enabled = config.enabled
        self.atr_lookback = config.atr_lookback
        self.min_atr_pct = config.min_atr_pct
        self.max_atr_pct = config.max_atr_pct

        # nautilus ATR default ma_type is SIMPLE (== legacy SMA-of-TR semantics).
        self._atr = AverageTrueRange(self.atr_lookback)
        self._ready = False

    @property
    def name(self) -> str:
        return "volatility"

    def update(self, bar: BarProtocol) -> None:
        self._atr.update_raw(float(bar.high), float(bar.low), float(bar.close))
        if self._atr.initialized:
            self._ready = True

    def check(self, bar: BarProtocol) -> FilterResult:
        if not self.enabled:
            return FilterResult.allow()

        if not self._ready or not self._atr.initialized:
            return FilterResult.block("ATR not yet calculated (warming up)")

        price = float(bar.close)
        if price <= 0:
            return FilterResult.block("Invalid price (zero or negative)")

        # Decimal fraction (0.006 = 0.6%) — same unit as config.
        atr_pct = self._atr.value / price

        if atr_pct < self.min_atr_pct:
            return FilterResult.block(
                f"Volatility too low: ATR {atr_pct:.4f} < min {self.min_atr_pct}"
            )

        if atr_pct > self.max_atr_pct:
            return FilterResult.block(
                f"Volatility too high: ATR {atr_pct:.4f} > max {self.max_atr_pct}"
            )

        return FilterResult.allow()

    def is_ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        self._atr.reset()
        self._ready = False

    def get_atr(self) -> float | None:
        return self._atr.value if self._atr.initialized else None

    def get_atr_pct(self, price: float) -> float | None:
        if not self._atr.initialized or price <= 0:
            return None
        return self._atr.value / price
