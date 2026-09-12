"""
SuperTrend indicator using pandas-ta.

Provides NautilusTrader-compatible SuperTrend indicator with snapshot support
for indicator warmup alignment.
"""

import logging
from collections import deque
from typing import cast

import pandas as pd
from nautilus_trader.indicators.base import Indicator
from nautilus_trader.model import Bar

from ._pandas_ta import ta

# Module-level logger for indicator errors
_logger = logging.getLogger(__name__)


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


class SuperTrend(Indicator):
    """
    SuperTrend indicator using pandas-ta.

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
        super().__init__(params=[length, multiplier])

        if length < 1:
            raise ValueError("Length must be at least 1")
        if multiplier <= 0:
            raise ValueError("Multiplier must be positive")

        # Parameters (exposed for logging/display)
        self.length = length
        self.multiplier = multiplier

        # Data collection using deque for efficient FIFO with automatic size limit
        max_size = length + 50
        self._highs: deque[float] = deque(maxlen=max_size)
        self._lows: deque[float] = deque(maxlen=max_size)
        self._closes: deque[float] = deque(maxlen=max_size)

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
        return len(self._closes) > 0

    @property
    def initialized(self) -> bool:
        """Return whether the indicator is warmed up and ready.

        A loaded snapshot makes the indicator immediately usable (it carries a valid
        trend/value); otherwise readiness requires ``length + 1`` real bars.
        """
        return self._from_snapshot or len(self._closes) >= self._warmup_period

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
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)

        # deque with maxlen handles size limiting automatically

        # Compute only once enough *real* bars are buffered. Before that, a loaded
        # snapshot keeps its trend/value (so the strategy trades on the snapshot),
        # and a cold start stays neutral. Gating on real-bar count (not on
        # ``initialized``) prevents feeding a short/snapshot-seeded window into
        # ta.supertrend, which would produce a contaminated value.
        if len(self._closes) < self._warmup_period:
            return

        # Calculate SuperTrend using pandas-ta with Series (more efficient than DataFrame)
        high_series = pd.Series(self._highs)
        low_series = pd.Series(self._lows)
        close_series = pd.Series(self._closes)

        result = ta.supertrend(
            high_series,
            low_series,
            close_series,
            length=self.length,
            multiplier=self.multiplier,
        )

        # Validate pandas-ta result before use
        if result is None:
            _logger.error("pandas-ta supertrend returned None")
            return

        if len(result) == 0:
            _logger.error("pandas-ta supertrend returned empty DataFrame")
            return

        if result is not None and len(result) > 0:
            # pandas-ta column naming convention
            st_col, dir_col, long_col, short_col = _supertrend_column_names(
                self.length, self.multiplier
            )

            # Fallback: try to find column by prefix if exact match fails
            if st_col not in result.columns:
                for col in result.columns:
                    if (
                        col.startswith("SUPERT_")
                        and not col.startswith("SUPERTd")
                        and not col.startswith("SUPERTl")
                        and not col.startswith("SUPERTs")
                    ):
                        st_col = col
                        # Derive other column names from the found pattern
                        suffix = col.replace("SUPERT_", "")
                        dir_col = f"SUPERTd_{suffix}"
                        long_col = f"SUPERTl_{suffix}"
                        short_col = f"SUPERTs_{suffix}"
                        break

            # Validate values are not NaN before using
            st_value = result[st_col].iloc[-1]
            dir_value = result[dir_col].iloc[-1]

            if pd.isna(st_value):
                _logger.warning("pandas-ta supertrend value is NaN")
                return
            if pd.isna(dir_value):
                _logger.warning("pandas-ta supertrend direction is NaN")
                return

            self._supertrend = float(st_value)
            self._trend = int(dir_value)

            # Extract band values
            if long_col in result.columns:
                val = result[long_col].iloc[-1]
                if pd.notna(val):
                    self._lower_band = float(val)
            if short_col in result.columns:
                val = result[short_col].iloc[-1]
                if pd.notna(val):
                    self._upper_band = float(val)

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
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
        # the strategy can trade on the snapshot trend/value. The price deques stay
        # empty and accumulate only real bars; the snapshot value is held until a
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

    SNAPSHOT_VERSION = 1

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
            "data": {
                "highs": list(self._highs),
                "lows": list(self._lows),
                "closes": list(self._closes),
            },
            "state": {
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

        # Restore data window
        data = cast(dict[str, object], snapshot.get("data", {}))
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()

        for h in cast(list[float], data.get("highs", [])):
            self._highs.append(h)
        for low in cast(list[float], data.get("lows", [])):
            self._lows.append(low)
        for c in cast(list[float], data.get("closes", [])):
            self._closes.append(c)

        # Restore state
        state = cast(dict[str, object], snapshot.get("state", {}))
        self._trend = cast(int, state.get("trend", 0))
        self._supertrend = cast(float, state.get("supertrend", 0.0))
        self._upper_band = cast(float, state.get("upper_band", 0.0))
        self._lower_band = cast(float, state.get("lower_band", 0.0))

        # Mark as restored from snapshot
        self._from_snapshot = True

        _logger.info(
            f"SuperTrend restored from snapshot: "
            f"trend={self._trend}, value={self._supertrend:.2f}, "
            f"data_points={len(self._closes)}"
        )
