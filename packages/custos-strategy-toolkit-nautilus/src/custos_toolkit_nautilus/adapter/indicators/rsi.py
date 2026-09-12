"""RSI (Relative Strength Index) indicator wrapping pandas-ta."""

import pandas as pd
from nautilus_trader.indicators.base import Indicator
from nautilus_trader.model import Bar

from ._pandas_ta import ta


class RSI(Indicator):
    """
    RSI (Relative Strength Index) indicator using pandas-ta.

    RSI measures momentum on a scale from 0 to 100.
    Values below 30 typically indicate oversold, above 70 overbought.

    Parameters
    ----------
    period : int
        The period for RSI calculation (default: 14)
    """

    def __init__(self, period: int = 14) -> None:
        super().__init__(params=[period])

        if period < 1:
            raise ValueError("Period must be at least 1")

        self.period = period

        # Data collection
        self._closes: list[float] = []

        # RSI state
        self._rsi: float = 50.0
        self._prev_rsi: float = 50.0

        # Warmup requirement
        self._warmup_period = period + 1

    @property
    def value(self) -> float:
        """Return the current RSI value (0-100)."""
        return self._rsi

    def crossed_above(self, threshold: float) -> bool:
        """
        Check if RSI crossed above a threshold.

        Parameters
        ----------
        threshold : float
            The threshold to check (e.g., 30 for oversold recovery)

        Returns
        -------
        bool
            True if RSI crossed from below/at threshold to above
        """
        return self._prev_rsi <= threshold and self._rsi > threshold

    def crossed_below(self, threshold: float) -> bool:
        """
        Check if RSI crossed below a threshold.

        Parameters
        ----------
        threshold : float
            The threshold to check (e.g., 70 for overbought)

        Returns
        -------
        bool
            True if RSI crossed from above/at threshold to below
        """
        return self._prev_rsi >= threshold and self._rsi < threshold

    @property
    def has_inputs(self) -> bool:
        """Return whether the indicator has received inputs."""
        return len(self._closes) > 0

    @property
    def initialized(self) -> bool:
        """Return whether the indicator is warmed up and ready."""
        return len(self._closes) >= self._warmup_period

    def handle_bar(self, bar: Bar) -> None:
        """Process a bar and update the indicator."""
        self.update_raw(close=bar.close.as_double())

    def update_raw(self, close: float) -> None:
        """Update the indicator with raw close price."""
        # Store previous value before update
        self._prev_rsi = self._rsi

        self._closes.append(close)

        # Limit data size
        max_size = self.period + 50
        if len(self._closes) > max_size:
            self._closes = self._closes[-max_size:]

        if not self.initialized:
            return

        # Calculate RSI using pandas-ta
        df = pd.DataFrame({"close": self._closes})

        result = ta.rsi(df["close"], length=self.period)

        if result is not None and len(result) > 0:
            val = result.iloc[-1]
            if pd.notna(val):
                self._rsi = float(val)

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._closes.clear()
        self._rsi = 50.0
        self._prev_rsi = 50.0
