"""MACD (Moving Average Convergence Divergence) indicator wrapping pandas-ta."""

import pandas as pd
from nautilus_trader.indicators.base import Indicator
from nautilus_trader.model import Bar

from ._pandas_ta import ta


class MACD(Indicator):
    """
    MACD (Moving Average Convergence Divergence) indicator using pandas-ta.

    MACD = EMA(fast) - EMA(slow)
    Signal = EMA(MACD, signal_period)
    Histogram = MACD - Signal

    Parameters
    ----------
    fast_period : int
        The period for fast EMA (default: 12)
    slow_period : int
        The period for slow EMA (default: 26)
    signal_period : int
        The period for signal line EMA (default: 9)
    """

    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> None:
        super().__init__(params=[fast_period, slow_period, signal_period])

        if fast_period < 1:
            raise ValueError("Fast period must be at least 1")
        if slow_period < fast_period:
            raise ValueError("Slow period must be greater than fast period")
        if signal_period < 1:
            raise ValueError("Signal period must be at least 1")

        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period

        # Data collection for pandas-ta
        self._closes: list[float] = []

        # MACD state
        self._macd: float = 0.0
        self._signal: float = 0.0
        self._histogram: float = 0.0
        self._prev_histogram: float = 0.0

        # Warmup requirement
        self._warmup_period = slow_period + signal_period

    @property
    def macd_value(self) -> float:
        """Return the current MACD line value."""
        return self._macd

    @property
    def signal_value(self) -> float:
        """Return the current signal line value."""
        return self._signal

    @property
    def histogram(self) -> float:
        """Return the current histogram value."""
        return self._histogram

    @property
    def crossed_above_signal(self) -> bool:
        """
        Check if MACD crossed above signal line (golden cross).

        Returns True when histogram changes from <= 0 to > 0.
        """
        return self._prev_histogram <= 0 and self._histogram > 0

    @property
    def crossed_below_signal(self) -> bool:
        """
        Check if MACD crossed below signal line (death cross).

        Returns True when histogram changes from >= 0 to < 0.
        """
        return self._prev_histogram >= 0 and self._histogram < 0

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
        # Store previous histogram before update
        self._prev_histogram = self._histogram

        self._closes.append(close)

        # Limit data size to avoid memory growth
        max_size = self.slow_period + self.signal_period + 50
        if len(self._closes) > max_size:
            self._closes = self._closes[-max_size:]

        if not self.initialized:
            return

        # Calculate MACD using pandas-ta
        df = pd.DataFrame({"close": self._closes})

        result = ta.macd(
            df["close"],
            fast=self.fast_period,
            slow=self.slow_period,
            signal=self.signal_period,
        )

        if result is not None and len(result) > 0:
            macd_col = f"MACD_{self.fast_period}_{self.slow_period}_{self.signal_period}"
            signal_col = f"MACDs_{self.fast_period}_{self.slow_period}_{self.signal_period}"
            hist_col = f"MACDh_{self.fast_period}_{self.slow_period}_{self.signal_period}"

            if macd_col in result.columns:
                val = result[macd_col].iloc[-1]
                if pd.notna(val):
                    self._macd = float(val)

            if signal_col in result.columns:
                val = result[signal_col].iloc[-1]
                if pd.notna(val):
                    self._signal = float(val)

            if hist_col in result.columns:
                val = result[hist_col].iloc[-1]
                if pd.notna(val):
                    self._histogram = float(val)

    def reset(self) -> None:
        """Reset the indicator to its initial state."""
        self._closes.clear()
        self._macd = 0.0
        self._signal = 0.0
        self._histogram = 0.0
        self._prev_histogram = 0.0
