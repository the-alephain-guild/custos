"""The hand-written trailing stop manager against the engine's own trailing order.

Measures how the two actually behave in a real backtest, rather than reasoning about
it from the source:

- The **extrema origin** differs in the source — one tracks the peak from the open,
  the other only after activation — but a profit-based activation threshold
  structurally hides that. What is actually observable is trigger **tick rounding**:
  bounded, and explicable. Differing source does not mean differing behaviour.
- The engine **treats MARK as LAST**: a backtest routes MARK_PRICE through
  calculate_with_last and reads the traded price, so the trigger source is not
  faithful to what a real venue does.
- The native path issues **no per-tick MARKET close**: at cache level the order types
  are exactly ``{MARKET: 1, TRAILING_STOP_MARKET: 1}``, with no extra reduce_only
  MARKET.

These assertions pin the trailing behaviour of one specific engine version — its tick
rounding and its ratchet — so an engine upgrade needs them re-checked. Nothing is sent
to a real venue here; comparing against one is a separate gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.tick_monitor import TrailingStopManager
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import (
    AccountType,
    AggressorSide,
    OmsType,
    OrderSide,
    TimeInForce,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.trading.strategy import Strategy

_INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()
_VENUE = Venue("BINANCE")


# --------------------------------------------------------------------------- #
# The hand-written manager, driven purely by logic                              #
# --------------------------------------------------------------------------- #
def run_handwritten(
    prices: list[float],
    entry: float,
    activation_pct: Decimal,
    trailing_pct: Decimal,
    is_long: bool,
) -> tuple[int, float] | None:
    """Drives it tick by tick, returning the first exit as (index, price), or None.

    Reproduces the strategy's own update_peak-then-check order.
    """
    mgr = TrailingStopManager(activation_pct, trailing_pct)
    mgr.init_position(Decimal(str(entry)), is_long)
    for i, p in enumerate(prices):
        price = Decimal(str(p))
        mgr.update_peak(price, is_long)
        action = mgr.check(price, Decimal(str(entry)), is_long)
        if action is not None:
            return (i, float(action.price))
    return None


# --------------------------------------------------------------------------- #
# A minimal backtest harness running the engine's own trailing stop order       #
# --------------------------------------------------------------------------- #
class _TrailingProbe(Strategy):
    """Opens a position, submits one native trailing stop, and records its fill."""

    def __init__(
        self,
        side: OrderSide,
        qty: Quantity,
        activation_price,
        trailing_offset: Decimal,
        trigger_type: TriggerType,
    ) -> None:
        super().__init__()
        self._side = side
        self._qty = qty
        self._activation_price = activation_price
        self._trailing_offset = trailing_offset
        self._trigger_type = trigger_type
        self._entry_submitted = False
        self._trailing_id = None
        # What this harness observes
        self.trailing_fill_ts: int | None = None
        self.trailing_fill_px: float | None = None
        self.trigger_history: list[tuple[int, float]] = []  # how trigger_price evolves
        self.order_type_counts: dict[str, int] = {}  # real order types, read from the cache
        self.ts_to_index: dict[int, int] = {}  # ts to prices index, filled after the run

    def on_start(self) -> None:
        self.subscribe_quote_ticks(_INSTRUMENT.id)
        self.subscribe_trade_ticks(_INSTRUMENT.id)
        # Open the position with a market order
        entry = self.order_factory.market(
            instrument_id=_INSTRUMENT.id,
            order_side=self._side,
            quantity=self._qty,
        )
        self._entry_submitted = True
        self.submit_order(entry)

    def on_position_opened(self, event) -> None:
        # Once the position is open, attach the native trailing stop as reduce_only
        close_side = OrderSide.SELL if self._side == OrderSide.BUY else OrderSide.BUY
        trailing = self.order_factory.trailing_stop_market(
            instrument_id=_INSTRUMENT.id,
            order_side=close_side,
            quantity=self._qty,
            trailing_offset=self._trailing_offset,
            activation_price=self._activation_price,
            trigger_type=self._trigger_type,
            trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
        )
        self._trailing_id = trailing.client_order_id
        self.submit_order(trailing)

    def on_order_updated(self, event) -> None:
        # Record how the trailing order's trigger_price ratchets
        if (
            self._trailing_id is not None
            and event.client_order_id == self._trailing_id
            and getattr(event, "trigger_price", None) is not None
        ):
            self.trigger_history.append((event.ts_event, float(event.trigger_price)))

    def on_order_filled(self, event) -> None:
        if self._trailing_id is not None and event.client_order_id == self._trailing_id:
            self.trailing_fill_ts = event.ts_event
            self.trailing_fill_px = float(event.last_px)


def _make_engine() -> BacktestEngine:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(_INSTRUMENT)
    return engine


def _ticks_at(price: float, ts: int) -> list:
    """A quote and a trade at one instant: the quote makes a market order fillable,
    the trade drives last."""
    return [
        TestDataStubs.quote_tick(
            _INSTRUMENT, bid_price=price, ask_price=price, ts_event=ts, ts_init=ts
        ),
        TestDataStubs.trade_tick(
            _INSTRUMENT,
            price=price,
            size=1.0,
            aggressor_side=AggressorSide.BUYER,
            ts_event=ts,
            ts_init=ts,
        ),
    ]


def run_native_backtest(
    prices: list[float],
    entry: float,
    activation_pct: Decimal,
    trailing_pct: Decimal,
    is_long: bool,
    trigger_type: TriggerType = TriggerType.LAST_PRICE,
) -> _TrailingProbe:
    """Feeds a price series through the native trailing order and returns the probe.

    The entry anchor sits at ts=1e9 and is not part of the prices index; prices[i] is
    at ts=(i+2)*1e9.
    """
    engine = _make_engine()
    side = OrderSide.BUY if is_long else OrderSide.SELL
    if is_long:
        activation_price = _INSTRUMENT.make_price(entry * (1 + float(activation_pct)))
    else:
        activation_price = _INSTRUMENT.make_price(entry * (1 - float(activation_pct)))
    trailing_offset = trailing_pct * Decimal("10000")  # BASIS_POINTS

    probe = _TrailingProbe(
        side=side,
        qty=_INSTRUMENT.make_qty(1.0),
        activation_price=activation_price,
        trailing_offset=trailing_offset,
        trigger_type=trigger_type,
    )
    engine.add_strategy(probe)

    probe.ts_to_index = {(i + 2) * 1_000_000_000: i for i in range(len(prices))}
    ticks: list = _ticks_at(entry, 1_000_000_000)  # the entry anchor
    for i, p in enumerate(prices):
        ticks += _ticks_at(p, (i + 2) * 1_000_000_000)
    engine.add_data(ticks)
    engine.run()
    # Real order types read from the cache, rather than a count this test maintains
    for order in engine.cache.orders():
        name = order.order_type.name
        probe.order_type_counts[name] = probe.order_type_counts.get(name, 0) + 1
    engine.dispose()
    return probe


def native_exit(probe: _TrailingProbe) -> tuple[int, float] | None:
    """Pulls the exit as (prices index, fill price), through an explicit ts-to-index
    map so an unexpected timestamp raises rather than passing silently."""
    if probe.trailing_fill_ts is None:
        return None
    assert probe.trailing_fill_ts in probe.ts_to_index, (
        f"the trailing fill at ts={probe.trailing_fill_ts} is not in the prices tick map "
        f"(fired early, or on the entry anchor?); known ts={sorted(probe.ts_to_index)}"
    )
    return (probe.ts_to_index[probe.trailing_fill_ts], probe.trailing_fill_px)


# --------------------------------------------------------------------------- #
# Four synthetic scenarios; every difference asserted below is explicable        #
# --------------------------------------------------------------------------- #
# Shared: entry=100, activation at 2% profit (102 long / 98 short), callback rate 1%
_ENTRY = 100.0
_ACT = Decimal("0.02")
_TRAIL = Decimal("0.01")


def test_g1_tick_rounding_long_diverges():
    """Standard monotonic long — they differ, and the difference is trigger rounding.

    The native trigger is the peak times (1 minus the rate), rounded to the tick:
    105 * 0.99 = 103.95 rounds to 104.0, so a price of 104 fires it. The hand-written
    one uses an exact Decimal drawdown (0.95% against 1%) and fires a step later at
    103. Rounding, not the extrema origin — activation hides that, as the next test
    shows.
    """
    prices = [100, 102, 105, 104, 103, 103.4]
    a = run_handwritten(prices, _ENTRY, _ACT, _TRAIL, is_long=True)
    probe = run_native_backtest(prices, _ENTRY, _ACT, _TRAIL, is_long=True)
    b = native_exit(probe)
    assert a == (4, 103.0)
    assert b == (3, 104.0)
    assert b[1] > a[1], "the rounded trigger sits higher, so it fires earlier and better"
    # Real order types from the cache: only the opening MARKET plus the native
    # trailing, with no extra reduce_only MARKET close
    assert probe.order_type_counts == {"MARKET": 1, "TRAILING_STOP_MARKET": 1}
    # The whole trigger evolution, including the artifact where a quote tick reads a
    # stale last (idx1 99.0 is the entry's 100 * 0.99), then 101.0 once activated, then
    # the peak's 105 * 0.99 rounded to 104.0
    th = [(probe.ts_to_index.get(ts, -1), tr) for ts, tr in probe.trigger_history]
    assert th == [(1, 99.0), (1, 101.0), (2, 104.0)]


def test_g2_extrema_origin_masked_by_activation_threshold():
    """A spike before activation — they agree, because activation hides the difference.

    In the source one tracks the peak from the open and the other only after
    activation. But with activation above entry, every pre-activation price is below
    the activation price, which is at or below the post-activation peak. So a
    pre-activation high — 101.5 here, under an activation price of 102 — can never
    become the peak: the two peaks coincide at the moment of activation.
    """
    prices = [100, 101.5, 100.2, 104, 102.5, 103.0]  # pre-activation high of 101.5 is under 102
    a = run_handwritten(prices, _ENTRY, _ACT, _TRAIL, is_long=True)
    b = native_exit(run_native_backtest(prices, _ENTRY, _ACT, _TRAIL, is_long=True))
    assert a == b == (4, 102.5)


def test_g3_short_symmetry():
    """Short direction — they agree, and the rounding happens to align in this case."""
    prices = [100, 98, 95, 96, 97, 96.6]
    a = run_handwritten(prices, _ENTRY, _ACT, _TRAIL, is_long=False)
    probe = run_native_backtest(prices, _ENTRY, _ACT, _TRAIL, is_long=False)
    b = native_exit(probe)
    assert a == b == (3, 96.0)
    assert probe.order_type_counts == {"MARKET": 1, "TRAILING_STOP_MARKET": 1}


def test_g4_mark_equals_last_in_backtest():
    """A backtest exits identically on MARK_PRICE and LAST_PRICE: it treats MARK as LAST.

    Both trigger types route through calculate_with_last and read the traded price.
    That is source-level routing, not a degradation when no mark feed is present, and
    it is the fidelity gap: production defaults to a mark trigger, and a real venue
    triggers on an independent mark price. That comparison belongs to the live gate.
    """
    prices = [100, 102, 105, 104, 103, 103.4]
    last = native_exit(
        run_native_backtest(prices, _ENTRY, _ACT, _TRAIL, True, TriggerType.LAST_PRICE)
    )
    mark = native_exit(
        run_native_backtest(prices, _ENTRY, _ACT, _TRAIL, True, TriggerType.MARK_PRICE)
    )
    assert last == mark == (3, 104.0), (
        "a backtest exits the same on MARK and LAST — the routing reads the traded price"
    )
