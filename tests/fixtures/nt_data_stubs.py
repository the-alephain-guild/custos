"""The two tick constructors these tests used to get from nautilus's test_kit.

2.0 ships no `test_kit`; only `testkit.providers` survived. Three tests needed
exactly two of its factories, so they are rebuilt here rather than vendored
wholesale.

The originals took an instrument plus plain numbers and did the quantisation
internally. 2.0's `QuoteTick` / `TradeTick` constructors take an instrument id
and already-typed `Price` / `Quantity`, so that conversion moves here and the
call sites stay as they were.
"""

from __future__ import annotations

from typing import Any

from nautilus_trader.model import AggressorSide, QuoteTick, TradeId, TradeTick

__all__ = ["quote_tick", "trade_tick"]


def quote_tick(
    instrument: Any,
    *,
    bid_price: float,
    ask_price: float,
    ts_event: int,
    ts_init: int,
    bid_size: float = 1.0,
    ask_size: float = 1.0,
) -> QuoteTick:
    """Build a QuoteTick, quantising prices and sizes to the instrument."""
    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=instrument.make_price(bid_price),
        ask_price=instrument.make_price(ask_price),
        bid_size=instrument.make_qty(bid_size),
        ask_size=instrument.make_qty(ask_size),
        ts_event=ts_event,
        ts_init=ts_init,
    )


def trade_tick(
    instrument: Any,
    *,
    price: float,
    size: float,
    aggressor_side: AggressorSide,
    ts_event: int,
    ts_init: int,
    trade_id: str | None = None,
) -> TradeTick:
    """Build a TradeTick; the trade id defaults to the event timestamp."""
    return TradeTick(
        instrument_id=instrument.id,
        price=instrument.make_price(price),
        size=instrument.make_qty(size),
        aggressor_side=aggressor_side,
        trade_id=TradeId(trade_id if trade_id is not None else str(ts_event)),
        ts_event=ts_event,
        ts_init=ts_init,
    )
