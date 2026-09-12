# shared/nautilus/filters/momentum.py
"""Nautilus-backed momentum filter (RSI / MACD / ROC).

Indicator computation uses nautilus native indicators with the decision logic
inlined. Unit/composition notes:
  - RSI: nautilus returns 0..1 -> x100 before comparing against the threshold (0..100).
  - ROC: ``RateOfChange(period, use_log=False)`` returns a fraction -> x100 for percent.
  - MACD: nautilus ``.value`` is only the MACD line; add an
    ``ExponentialMovingAverage(signal)`` fed macd.value to get the signal line, with
    histogram = line - signal.

RSI/ROC/MACD parameters are read from the nested typed sub-configs (RsiConfig /
RocConfig / MacdConfig), which the framework always populates with defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custos_toolkit.protocols.bar import BarProtocol
from custos_toolkit.protocols.filter import FilterResult
from custos_toolkit.signals.types import SignalDirection
from nautilus_trader.indicators import (
    ExponentialMovingAverage,
    MovingAverageConvergenceDivergence,
    RateOfChange,
    RelativeStrengthIndex,
)

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.config.filters import MomentumFilterConfig

_VALID_INDICATORS = ("rsi", "macd", "roc")


class NautilusMomentumFilter:
    """Filter trades by momentum indicator (RSI/MACD/ROC) via nautilus indicators.

    Takes a typed ``MomentumFilterConfig`` with nested ``rsi`` / ``roc`` / ``macd``
    sub-configs. RSI and ROC carry separate long/short thresholds, so this filter
    is direction-aware: ``check`` reads the band matching the candidate entry
    direction (``direction_aware`` opts it into FilterManager's direction dispatch).
    """

    # Momentum reads long vs short thresholds; FilterManager passes the entry
    # direction only to filters that declare this. Other filters keep check(bar).
    direction_aware = True

    def __init__(self, config: MomentumFilterConfig):
        self.config = config
        self.enabled = config.enabled
        self.indicator = config.indicator
        if self.indicator not in _VALID_INDICATORS:
            raise ValueError(
                f"Invalid indicator '{self.indicator}'. Must be one of: {_VALID_INDICATORS}"
            )

        self.rsi_period = config.rsi.period
        self.rsi_long_min = config.rsi.long_min
        self.rsi_long_max = config.rsi.long_max
        self.rsi_short_min = config.rsi.short_min
        self.rsi_short_max = config.rsi.short_max

        self.roc_period = config.roc.period
        self.roc_long_threshold = config.roc.long_threshold
        self.roc_short_threshold = config.roc.short_threshold

        self.macd_fast = config.macd.fast
        self.macd_slow = config.macd.slow
        self.macd_signal = config.macd.signal
        self.macd_histogram_positive = config.macd.histogram_positive

        # Build the selected nautilus indicator
        self._rsi: RelativeStrengthIndex | None = None
        self._roc: RateOfChange | None = None
        self._macd: MovingAverageConvergenceDivergence | None = None
        self._signal_ema: ExponentialMovingAverage | None = None

        if self.indicator == "rsi":
            self._rsi = RelativeStrengthIndex(self.rsi_period)
        elif self.indicator == "roc":
            self._roc = RateOfChange(self.roc_period, False)  # use_log=False
        elif self.indicator == "macd":
            self._macd = MovingAverageConvergenceDivergence(self.macd_fast, self.macd_slow)
            self._signal_ema = ExponentialMovingAverage(self.macd_signal)

        self._ready = False

    @property
    def name(self) -> str:
        return "momentum"

    def update(self, bar: BarProtocol) -> None:
        close = float(bar.close)

        if self.indicator == "rsi":
            rsi = self._rsi
            assert rsi is not None
            rsi.update_raw(close)
            self._ready = rsi.initialized
        elif self.indicator == "roc":
            roc = self._roc
            assert roc is not None
            roc.update_raw(close)
            self._ready = roc.initialized
        elif self.indicator == "macd":
            macd = self._macd
            signal_ema = self._signal_ema
            assert macd is not None
            assert signal_ema is not None
            macd.update_raw(close)
            if macd.initialized:
                signal_ema.update_raw(macd.value)
                self._ready = signal_ema.initialized

    def check(self, bar: BarProtocol, direction: SignalDirection | None = None) -> FilterResult:
        if not self.enabled:
            return FilterResult.allow()
        if not self._ready:
            return FilterResult.block("Momentum filter not ready (warming up)")

        if self.indicator == "rsi":
            return self._check_rsi(direction)
        if self.indicator == "roc":
            return self._check_roc(direction)
        return self._check_macd()

    def _check_rsi(self, direction: SignalDirection | None) -> FilterResult:
        indicator = self._rsi
        assert indicator is not None
        rsi = indicator.value * 100.0  # nautilus RSI is 0..1
        # A short entry uses the short band; long entries (and direction-agnostic
        # legacy calls) use the long band.
        if direction == SignalDirection.ENTER_SHORT:
            lo, hi = self.rsi_short_min, self.rsi_short_max
        else:
            lo, hi = self.rsi_long_min, self.rsi_long_max
        if rsi < lo:
            return FilterResult.block(f"RSI {rsi:.1f} below minimum {lo}")
        if rsi > hi:
            return FilterResult.block(f"RSI {rsi:.1f} above maximum {hi}")
        return FilterResult.allow()

    def _check_roc(self, direction: SignalDirection | None) -> FilterResult:
        indicator = self._roc
        assert indicator is not None
        roc = indicator.value * 100.0  # nautilus ROC is a fraction
        # Long entries need positive momentum (>= long_threshold); short entries need
        # negative momentum (<= short_threshold).
        if direction == SignalDirection.ENTER_SHORT:
            if roc > self.roc_short_threshold:
                return FilterResult.block(
                    f"ROC {roc:.2f}% above short threshold {self.roc_short_threshold}%"
                )
            return FilterResult.allow()
        if roc < self.roc_long_threshold:
            return FilterResult.block(f"ROC {roc:.2f}% below threshold {self.roc_long_threshold}%")
        return FilterResult.allow()

    def _check_macd(self) -> FilterResult:
        macd = self._macd
        signal_ema = self._signal_ema
        assert macd is not None
        assert signal_ema is not None
        if self.macd_histogram_positive:
            histogram = macd.value - signal_ema.value
            if histogram <= 0:
                return FilterResult.block(f"MACD histogram {histogram:.4f} is not positive")
        return FilterResult.allow()

    def is_ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        for ind in (self._rsi, self._roc, self._macd, self._signal_ema):
            if ind is not None:
                ind.reset()
        self._ready = False

    def get_rsi(self) -> float | None:
        if self.indicator != "rsi" or not self._ready:
            return None
        assert self._rsi is not None
        return self._rsi.value * 100.0

    def get_roc(self) -> float | None:
        if self.indicator != "roc" or not self._ready:
            return None
        assert self._roc is not None
        return self._roc.value * 100.0

    def get_macd(self) -> dict[str, float] | None:
        if self.indicator != "macd" or not self._ready:
            return None
        assert self._macd is not None
        assert self._signal_ema is not None
        line = self._macd.value
        signal = self._signal_ema.value
        return {"macd_line": line, "signal_line": signal, "histogram": line - signal}
