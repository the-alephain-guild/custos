"""ADX (Average Directional Index) indicator wrapping pandas-ta."""

import pandas as pd
from nautilus_trader.model import Bar

from ._pandas_ta import ta


class ADX:
    """
    Measures trend strength on a scale from 0 to 100.
    Values above 25 typically indicate a strong trend.

    Parameters
    ----------
    length : int
        The period for ADX calculation (default: 14)
    """

    def __init__(self, length: int = 14) -> None:
        if length < 1:
            raise ValueError("Length must be at least 1")

        self.length = length

        # Data collection
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []

        # ADX state
        self._adx: float = 0.0
        self._prev_adx: float = 0.0
        self._plus_di: float = 0.0
        self._minus_di: float = 0.0

        # Warmup requirement (ADX needs more data than length)
        self._warmup_period = length * 2

    @property
    def value(self) -> float:
        """Return the current ADX value (0-100)."""
        return self._adx

    @property
    def plus_di(self) -> float:
        """Return the current +DI value."""
        return self._plus_di

    @property
    def minus_di(self) -> float:
        """Return the current -DI value."""
        return self._minus_di

    @property
    def decreasing(self) -> bool:
        """Return whether ADX is decreasing."""
        return self._adx < self._prev_adx

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
        # Store previous value before update
        self._prev_adx = self._adx

        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)

        # Limit data size
        max_size = self.length * 3
        if len(self._closes) > max_size:
            self._highs = self._highs[-max_size:]
            self._lows = self._lows[-max_size:]
            self._closes = self._closes[-max_size:]

        if not self.initialized:
            return

        # Calculate ADX using pandas-ta
        df = pd.DataFrame(
            {
                "high": self._highs,
                "low": self._lows,
                "close": self._closes,
            }
        )

        result = ta.adx(
            df["high"],
            df["low"],
            df["close"],
            length=self.length,
        )

        if result is not None and len(result) > 0:
            adx_col = f"ADX_{self.length}"
            dmp_col = f"DMP_{self.length}"
            dmn_col = f"DMN_{self.length}"

            if adx_col in result.columns:
                val = result[adx_col].iloc[-1]
                if pd.notna(val):
                    self._adx = float(val)
            if dmp_col in result.columns:
                val = result[dmp_col].iloc[-1]
                if pd.notna(val):
                    self._plus_di = float(val)
            if dmn_col in result.columns:
                val = result[dmn_col].iloc[-1]
                if pd.notna(val):
                    self._minus_di = float(val)

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._adx = 0.0
        self._prev_adx = 0.0
        self._plus_di = 0.0
        self._minus_di = 0.0
