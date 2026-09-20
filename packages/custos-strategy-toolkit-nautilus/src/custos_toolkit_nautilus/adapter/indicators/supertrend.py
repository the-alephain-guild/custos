"""
SuperTrend indicator, carried forward bar by bar.

Provides NautilusTrader-compatible SuperTrend indicator with snapshot support
for indicator warmup alignment.

SuperTrend is path dependent. Each bar's bands are clamped against the previous
bar's *already clamped* bands, the direction carries the previous direction when
price sits between them, and the ATR underneath is a Wilder recursion. Recomputing
a rolling window re-derives all of that from a starting point that moves with the
window, so a trend established before the window can be reinvented out of flat
data. This carries the state forward instead, which is what the algorithm means.

The arithmetic matches the vendored pandas-ta run over the whole series, bar for
bar; ``tests/toolkit/test_supertrend_continuity.py`` holds it to that.
"""

import logging
import sys
from typing import cast

from nautilus_trader.model import Bar

# Module-level logger for indicator errors
_logger = logging.getLogger(__name__)

# ``non_zero_range`` nudges a zero high-low range by this much before it reaches
# the true range, which happens on flat bars in crypto data.
_ZERO_RANGE_NUDGE = sys.float_info.epsilon


def _supertrend_column_names(length: float, multiplier: float) -> tuple[str, str, str, str]:
    """Build the (supertrend, direction, long-band, short-band) pandas_ta column names.

    pandas_ta uses the multiplier's full float repr (3.0 -> 'SUPERT_10_3.0'). Stripping
    the trailing zero misses every integer multiplier and would force the prefix
    fallback each bar, so keep the plain ``str(float)`` form.
    """
    suffix = f"{length}_{multiplier}"
    return (
        f"SUPERT_{suffix}",
        f"SUPERTd_{suffix}",
        f"SUPERTl_{suffix}",
        f"SUPERTs_{suffix}",
    )


class SuperTrend:
    """
    ATR-based trend following indicator that provides dynamic support/resistance lines.
    When price is above the line, the trend is bullish; when below, the trend is bearish.

    Parameters
    ----------
    length : int
        The period for ATR calculation (default: 10)
    multiplier : float
        The ATR multiplier for band calculation (default: 3.0)
    """

    def __init__(
        self,
        length: int = 10,
        multiplier: float = 3.0,
    ) -> None:
        if length < 1:
            raise ValueError("Length must be at least 1")
        if multiplier <= 0:
            raise ValueError("Multiplier must be positive")

        # Parameters (exposed for logging/display)
        self.length = length
        self.multiplier = multiplier

        # Carried state. Nothing here is a price history: the recursion needs the
        # previous bar and its outcome, not the bars before it.
        self._bar_count: int = 0
        self._prev_close: float | None = None
        # Wilder ATR as pandas' ewm(alpha=1/length, adjust=True) computes it: the
        # numerator and denominator each carry forward, and their ratio is the ATR.
        self._atr_alpha: float = 1.0 / length
        self._atr_numerator: float = 0.0
        self._atr_denominator: float = 0.0
        self._atr_samples: int = 0
        # The previous bar's clamped bands and direction -- clamped, because that is
        # what the next bar compares against.
        self._prev_upper: float | None = None
        self._prev_lower: float | None = None
        self._prev_direction: int = 1

        # SuperTrend state
        self._trend: int = 0  # 1: bullish, -1: bearish, 0: neutral
        self._supertrend: float = 0.0
        self._upper_band: float = 0.0
        self._lower_band: float = 0.0

        # Snapshot support
        self._snapshot_atr: float = 0.0
        self._from_snapshot: bool = False

        # Warmup requirement
        self._warmup_period = length + 1

    @property
    def trend(self) -> int:
        """
        Return the current trend direction.

        Returns
        -------
        int
            1 for bullish trend, -1 for bearish trend, 0 if not initialized
        """
        return self._trend

    @property
    def value(self) -> float:
        """
        Return the current SuperTrend line value.

        Returns
        -------
        float
            The SuperTrend line price level
        """
        return self._supertrend

    @property
    def upper_band(self) -> float:
        """Return the current upper band value (resistance in downtrend)."""
        return self._upper_band

    @property
    def lower_band(self) -> float:
        """Return the current lower band value (support in uptrend)."""
        return self._lower_band

    @property
    def has_inputs(self) -> bool:
        """Return whether the indicator has received inputs."""
        return self._bar_count > 0

    @property
    def initialized(self) -> bool:
        """Return whether the indicator is warmed up and ready.

        A loaded snapshot makes the indicator immediately usable (it carries a valid
        trend/value); otherwise readiness requires ``length + 1`` real bars.
        """
        return self._from_snapshot or self._bar_count >= self._warmup_period

    def handle_bar(self, bar: Bar) -> None:
        """
        Process a bar and update the indicator.

        Parameters
        ----------
        bar : Bar
            The bar to process
        """
        self.update_raw(
            high=bar.high.as_double(),
            low=bar.low.as_double(),
            close=bar.close.as_double(),
        )

    def update_raw(
        self,
        high: float,
        low: float,
        close: float,
    ) -> None:
        """
        Update the indicator with raw price values.

        Parameters
        ----------
        high : float
            The high price
        low : float
            The low price
        close : float
            The close price
        """
        self._bar_count += 1

        # True range. The first bar has no previous close, so it has no range --
        # pandas-ta writes NaN there and the ATR ignores it.
        if self._prev_close is not None:
            high_low = high - low
            if high_low == 0.0:
                high_low += _ZERO_RANGE_NUDGE
            true_range = max(
                abs(high_low),
                abs(high - self._prev_close),
                abs(self._prev_close - low),
            )
            decay = 1.0 - self._atr_alpha
            self._atr_numerator = true_range + decay * self._atr_numerator
            self._atr_denominator = 1.0 + decay * self._atr_denominator
            self._atr_samples += 1

        previous_close = self._prev_close
        self._prev_close = close

        # Until the ATR has its full sample the bands have no value, and neither
        # does a direction derived from them. A loaded snapshot keeps its own
        # trend/value through this stretch; a cold start stays neutral.
        if self._atr_samples < self.length:
            return

        atr = self._atr_numerator / self._atr_denominator
        midpoint = 0.5 * (high + low)
        upper = midpoint + self.multiplier * atr
        lower = midpoint - self.multiplier * atr

        if self._prev_upper is None or self._prev_lower is None or previous_close is None:
            # First priced bar: seed the recursion, direction defaults to long the
            # way pandas-ta seeds its direction array.
            direction = self._prev_direction
        elif close > self._prev_upper:
            direction = 1
        elif close < self._prev_lower:
            direction = -1
        else:
            # Price sits between the bands: the trend stands, and the band behind
            # it may not loosen.
            direction = self._prev_direction
            if direction > 0 and lower < self._prev_lower:
                lower = self._prev_lower
            if direction < 0 and upper > self._prev_upper:
                upper = self._prev_upper

        self._prev_upper = upper
        self._prev_lower = lower
        self._prev_direction = direction

        self._trend = direction
        self._upper_band = upper
        self._lower_band = lower
        self._supertrend = lower if direction > 0 else upper

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._bar_count = 0
        self._prev_close = None
        self._atr_numerator = 0.0
        self._atr_denominator = 0.0
        self._atr_samples = 0
        self._prev_upper = None
        self._prev_lower = None
        self._prev_direction = 1
        self._trend = 0
        self._supertrend = 0.0
        self._upper_band = 0.0
        self._lower_band = 0.0
        # Drop snapshot seeding so the indicator is uninitialized until re-warmed.
        self._from_snapshot = False

    def load_snapshot(self, values: dict[str, float]) -> None:
        """
        Load indicator state from a snapshot.

        This allows initializing the indicator from TradingView or other
        external data sources without needing historical bars.

        Parameters
        ----------
        values : dict[str, float]
            Snapshot values with keys:
            - value: SuperTrend line value
            - trend: Trend direction (1 or -1)
            - upper_band: Upper band value
            - lower_band: Lower band value
            - atr: ATR value (optional, for reference)
        """
        self._supertrend = values.get("value", 0.0)
        self._trend = int(values.get("trend", 0))
        self._upper_band = values.get("upper_band", 0.0)
        self._lower_band = values.get("lower_band", 0.0)

        # Store ATR for reference (not used in calculations but useful for debugging)
        self._snapshot_atr = values.get("atr", 0.0)

        # Mark as snapshot-initialized: ``initialized`` returns True immediately so
        # the strategy can trade on the snapshot trend/value. The carried state stays
        # unset and accumulate only real bars; the snapshot value is held until a
        # full real warmup window is collected (see update_raw). No dummy bars.
        self._from_snapshot = True

    def export_snapshot(self) -> dict[str, float]:
        """
        Export current indicator state as a snapshot.

        Returns
        -------
        dict[str, float]
            Current indicator values
        """
        return {
            "value": self._supertrend,
            "trend": float(self._trend),
            "upper_band": self._upper_band,
            "lower_band": self._lower_band,
        }

    # Full Snapshot Persistence (for Redis-based recovery)

    SNAPSHOT_VERSION = 2

    @property
    def snapshot_version(self) -> int:
        """Return snapshot format version for compatibility checking."""
        return self.SNAPSHOT_VERSION

    def to_snapshot(self) -> dict[str, object]:
        """
        Export complete indicator state for persistence.

        This method exports all internal state needed to fully restore
        the indicator without reprocessing historical data.

        Returns
        -------
        dict
            Complete snapshot containing:
            - version: Snapshot format version
            - params: Indicator parameters
            - data: Historical data window
            - state: Current indicator values
        """
        return {
            "version": self.SNAPSHOT_VERSION,
            "params": {
                "length": self.length,
                "multiplier": self.multiplier,
            },
            # Version 2 carries the recursion rather than a price window. A window
            # cannot restore this indicator: replaying it would restart the
            # recursion from the window's first bar, which is the defect this
            # snapshot format replaced.
            "state": {
                "bar_count": self._bar_count,
                "prev_close": self._prev_close,
                "atr_numerator": self._atr_numerator,
                "atr_denominator": self._atr_denominator,
                "atr_samples": self._atr_samples,
                "prev_upper": self._prev_upper,
                "prev_lower": self._prev_lower,
                "prev_direction": self._prev_direction,
                "trend": self._trend,
                "supertrend": self._supertrend,
                "upper_band": self._upper_band,
                "lower_band": self._lower_band,
            },
        }

    def from_snapshot(self, snapshot: dict[str, object]) -> None:
        """
        Restore indicator state from a complete snapshot.

        This method restores all internal state from a previously saved
        snapshot, allowing the indicator to continue from where it left off.

        Parameters
        ----------
        snapshot : dict
            Complete snapshot from to_snapshot()

        Raises
        ------
        ValueError
            If snapshot version or parameters don't match
        """
        # Version check
        version = snapshot.get("version", 0)
        if version != self.SNAPSHOT_VERSION:
            raise ValueError(
                f"Snapshot version mismatch: got {version}, expected {self.SNAPSHOT_VERSION}"
            )

        # Parameter validation
        params = cast(dict[str, object], snapshot.get("params", {}))
        if params.get("length") != self.length:
            raise ValueError(
                f"Snapshot length mismatch: got {params.get('length')}, expected {self.length}"
            )
        if params.get("multiplier") != self.multiplier:
            raise ValueError(
                f"Snapshot multiplier mismatch: got {params.get('multiplier')}, "
                f"expected {self.multiplier}"
            )

        # Restore the recursion itself, so the next bar continues the series
        # rather than starting a new one.
        state = cast(dict[str, object], snapshot.get("state", {}))
        self._bar_count = cast(int, state.get("bar_count", 0))
        prev_close = state.get("prev_close")
        self._prev_close = None if prev_close is None else float(cast(float, prev_close))
        self._atr_numerator = cast(float, state.get("atr_numerator", 0.0))
        self._atr_denominator = cast(float, state.get("atr_denominator", 0.0))
        self._atr_samples = cast(int, state.get("atr_samples", 0))
        prev_upper = state.get("prev_upper")
        prev_lower = state.get("prev_lower")
        self._prev_upper = None if prev_upper is None else float(cast(float, prev_upper))
        self._prev_lower = None if prev_lower is None else float(cast(float, prev_lower))
        self._prev_direction = cast(int, state.get("prev_direction", 1))
        self._trend = cast(int, state.get("trend", 0))
        self._supertrend = cast(float, state.get("supertrend", 0.0))
        self._upper_band = cast(float, state.get("upper_band", 0.0))
        self._lower_band = cast(float, state.get("lower_band", 0.0))

        # Mark as restored from snapshot
        self._from_snapshot = True

        _logger.info(
            f"SuperTrend restored from snapshot: "
            f"trend={self._trend}, value={self._supertrend:.2f}, "
            f"atr_samples={self._atr_samples}"
        )
