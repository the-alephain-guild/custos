"""Self-contained, SuperTrend-shaped minimal NautilusTrader strategy fixture.

A real ``nautilus_trader.trading.strategy.Strategy`` subclass with **no runtime
dependency on** ``philosophers-stone/shared/`` — the real supertrend strategy
subclasses the ps ``NautilusTradingStrategy`` base and imports the ps ``shared``
package, which custos (an Apache-2.0 open repo) must not reach into. This
fixture reproduces the *shape* of a SuperTrend strategy (ATR bands + trend-flip
market entries) so the host lifecycle — ``add_strategy`` → ``build`` →
``run_async`` → ``stop_async`` — runs against genuine NautilusTrader machinery.

Deliberately minimal: the signal is not a faithful SuperTrend and never trades
against a live venue in the offline test suite. Indicator/band math uses float
(NautilusTrader indicator convention); the order *quantity* is Decimal-derived.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar, BarType, InstrumentId, OrderSide
from nautilus_trader.trading import Strategy, StrategyConfig

_DEFAULT_INSTRUMENT = "BTCUSDT-PERP.BINANCE"


class MinimalSupertrendConfig(StrategyConfig):
    """Config for the SuperTrend-shaped fixture strategy.

    ``StrategyConfig`` is a rust pyclass in 2.0, so this is a plain subclass with a
    keyword-only constructor rather than a msgspec Struct. Fields are assigned
    before ``super().__init__()``, matching the shape the toolkit's own config base
    settled on.
    """

    def __init__(
        self,
        *,
        instrument_id: InstrumentId,
        bar_type: BarType,
        atr_period: int = 10,
        atr_multiplier: float = 3.0,
        trade_size: Decimal = Decimal("0.001"),
        **kwargs: Any,
    ) -> None:
        assign = object.__setattr__
        assign(self, "instrument_id", instrument_id)
        assign(self, "bar_type", bar_type)
        assign(self, "atr_period", atr_period)
        assign(self, "atr_multiplier", atr_multiplier)
        assign(self, "trade_size", trade_size)
        super().__init__(**kwargs)


class MinimalSupertrendStrategy(Strategy):
    """ATR-band trend-flip strategy — SuperTrend-shaped, minimal on purpose.

    Proves the host wiring (build / add_strategy / run / stop), not trading
    quality. Enters flat on a trend flip; exits everything on stop.
    """

    def __init__(self, config: MinimalSupertrendConfig) -> None:
        super().__init__(config)
        self._atr = AverageTrueRange(config.atr_period)
        self._prev_trend = 0
        self._instrument = None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self.config.instrument_id)
        self.register_indicator_for_bars(self.config.bar_type, self._atr)
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        if not self._atr.initialized:
            return
        close = float(bar.close)
        hl2 = (float(bar.high) + float(bar.low)) / 2.0
        band = self.config.atr_multiplier * self._atr.value
        if close > hl2 + band:
            trend = 1
        elif close < hl2 - band:
            trend = -1
        else:
            trend = self._prev_trend

        if self.portfolio.is_flat(self.config.instrument_id):
            if trend == 1 and self._prev_trend != 1:
                self._submit_market(OrderSide.BUY)
            elif trend == -1 and self._prev_trend != -1:
                self._submit_market(OrderSide.SELL)
        self._prev_trend = trend

    def _submit_market(self, side: OrderSide) -> None:
        if self._instrument is None:
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self._instrument.make_qty(self.config.trade_size),
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)


def create_strategy(config: dict) -> MinimalSupertrendStrategy:
    """Entry-point factory: build the strategy from a spec.strategy_config dict.

    The host prefers this over a bare constructor, mirroring the ps entry-point
    convention. Absent keys fall back to sensible defaults so the fixture is
    usable with an empty config.
    """
    instrument_id = InstrumentId.from_str(config.get("instrument_id", _DEFAULT_INSTRUMENT))
    bar_type_str = config.get("bar_type") or f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
    return MinimalSupertrendStrategy(
        MinimalSupertrendConfig(
            instrument_id=instrument_id,
            bar_type=BarType.from_str(bar_type_str),
            atr_period=int(config.get("atr_period", 10)),
            atr_multiplier=float(config.get("atr_multiplier", 3.0)),
            trade_size=Decimal(str(config.get("trade_size", "0.001"))),
        )
    )
