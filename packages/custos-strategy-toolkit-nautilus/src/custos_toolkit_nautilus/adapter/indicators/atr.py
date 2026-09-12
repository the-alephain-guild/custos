"""ATR (Average True Range) indicator wrapping pandas-ta."""

import pandas as pd
from nautilus_trader.indicators.base import Indicator
from nautilus_trader.model import Bar

from ._pandas_ta import ta


class ATR(Indicator):
    """
    ATR (Average True Range) indicator using pandas-ta.

    Measures market volatility by calculating the average range of price bars.

    Parameters
    ----------
    length : int
        The period for ATR calculation (default: 14)
    """

    def __init__(self, length: int = 14) -> None:
        super().__init__(params=[length])

        if length < 1:
            raise ValueError("Length must be at least 1")

        self.length = length

        # Data collection
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        # ATR state
        self._atr: float = 0.0

        # Warmup requirement
        self._warmup_period = length + 1

    @property
    def value(self) -> float:
        """Return the current ATR value."""
        return self._atr

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
        """Update the indicator with raw price values."""
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)

        # Limit data size
        max_size = self.length + 50
        if len(self._closes) > max_size:
            self._highs = self._highs[-max_size:]
            self._lows = self._lows[-max_size:]
            self._closes = self._closes[-max_size:]

        if not self.initialized:
            return

        # Calculate ATR using pandas-ta
        df = pd.DataFrame(
            {
                "high": self._highs,
                "low": self._lows,
                "close": self._closes,
            }
        )

        result = ta.atr(
            df["high"],
            df["low"],
            df["close"],
            length=self.length,
        )

        if result is not None and len(result) > 0:
            val = result.iloc[-1]
            if pd.notna(val):
                self._atr = float(val)

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._atr = 0.0
