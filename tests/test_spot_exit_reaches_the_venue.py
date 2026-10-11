"""A cash-account close reaches a real matching engine, and a denial reaches the strategy.

The unit tests of the close form run against doubles: an account that says CASH because
the test told it to, an order factory that records arguments. Two claims cannot be
settled that way. That a plain market sell, built from a position on a real cash
account, actually closes it at the venue without leaving the account short; and that an
order the risk engine denies before it ever leaves the process is delivered to the
toolkit's strategy at all -- the denial path is the one the SoDEX spot probe never
handled, and a handler that is not called fixes nothing.

Both run through a real ``BacktestEngine``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit.config import load_config  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    OrderReconciler,
    SignalExecutionCoordinator,
)
from custos_toolkit_nautilus.adapter.execution import ExecutionManager  # noqa: E402
from custos_toolkit_nautilus.adapter.orders import OrderTracker  # noqa: E402
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_config import (  # noqa: E402
    NautilusTradingStrategyConfig,
    build_nautilus_base_config,
)
from custos_toolkit_nautilus.adapter.trading_strategy import (  # noqa: E402
    NautilusTradingStrategy,
)
from nautilus_trader.backtest import BacktestEngine  # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    Currency,
    Money,
    OmsType,
    OrderSide,
    OrderStatus,
    PositionSide,
    Quantity,
    QuoteTick,
    TimeInForce,
    Venue,
)
from nautilus_trader.testkit.providers import TestInstrumentProvider  # noqa: E402

_SPOT = TestInstrumentProvider.btcusdt_binance()
_VENUE = Venue("BINANCE")
_USDT = Currency.from_str("USDT")
_BTC = Currency.from_str("BTC")


def _strategy_config(directory, *, tick_type: str | None = None) -> NautilusTradingStrategyConfig:
    (directory / "config.yaml").write_text(
        "strategy:\n"
        "  name: probe\n"
        "trading:\n"
        "  connector:\n"
        "    value: binance\n"
        "  pairs:\n"
        "    value: [BTC-USDT]\n"
        "risk:\n"
        "  trade:\n"
        "    sl_tp_mode:\n"
        "      value: tick\n"
        + (
            "  tick_monitoring:\n"
            "    enabled:\n"
            "      value: true\n"
            "    tick_type:\n"
            f"      value: {tick_type}\n"
            if tick_type
            else ""
        ),
        encoding="utf-8",
    )
    return NautilusTradingStrategyConfig(
        **build_nautilus_base_config(load_config(directory / "config.yaml"))
    )


def _engine(base: Money | None = None) -> BacktestEngine:
    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True), run_analysis=False)
    )
    engine.add_venue(
        _VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, _USDT), base or Money(0, _BTC)],
    )
    engine.add_instrument(_SPOT)
    return engine


def _quotes(count: int, price: float = 80_000.0) -> list[QuoteTick]:
    px = _SPOT.make_price(price)
    size = _SPOT.make_qty(10)
    return [
        QuoteTick(_SPOT.id, px, px, size, size, n * 1_000_000_000, n * 1_000_000_000)
        for n in range(1, count + 1)
    ]


# ---------------------------------------------------------------------------
# A plain close on a cash account closes the position at the venue
# ---------------------------------------------------------------------------


class _BuysThenExits(NautilusStrategyCore):
    """Buy on the first quote, then exit through the toolkit's own bar-exit path.

    The exit is the real ``SignalExecutionCoordinator.execute_exit_for_pair`` with the
    real ``ExecutionManager``, reading this strategy's real cache -- so the account it
    decides from is the engine's, not one a test wrote down.
    """

    def __new__(cls, config: NautilusTradingStrategyConfig, **kwargs: object):
        return super().__new__(cls, config)

    def __init__(self, config: NautilusTradingStrategyConfig) -> None:
        super().__init__(config)
        self._order_signal_map: dict[str, str] = {}
        self._quotes = 0
        self.exit_order_ids: list = []
        self.position_sides: list[PositionSide] = []

    def on_start(self) -> None:
        self.subscribe_quotes(_SPOT.id)

    def on_core_quote_tick(self, tick) -> None:
        self._quotes += 1
        for position in self.cache.positions(instrument_id=_SPOT.id):
            self.position_sides.append(position.side)
        if self._quotes == 1:
            self.submit_order(
                self.order_factory.market(
                    instrument_id=_SPOT.id,
                    order_side=OrderSide.BUY,
                    quantity=_SPOT.make_qty(0.0063),
                    time_in_force=TimeInForce.IOC,
                )
            )
        elif self._quotes == 3:
            ctx = NS(
                instrument_id=_SPOT.id,
                pair="BTC-USDT",
                order_tracker=OrderTracker(),
                execution_manager=ExecutionManager(self.order_factory, self.cache, self.log),
            )
            before = {order.client_order_id for order in self.cache.orders()}
            SignalExecutionCoordinator(self).execute_exit_for_pair(
                ctx, Signal.exit_long(Decimal("80000"), pair="BTC-USDT"), bar=None
            )
            self.exit_order_ids = [
                order.client_order_id
                for order in self.cache.orders()
                if order.client_order_id not in before
            ]

    # Required by the base class; nothing here needs them.
    def get_indicator_history(self) -> dict[str, object]:
        return {}

    def on_core_bar(self, bar) -> None: ...
    def _on_bar_risk_hygiene(self, bar) -> None: ...


@pytest.mark.xfail(strict=True, reason="the exit on a cash account still carries reduce_only")
def test_a_cash_exit_closes_the_position_at_the_venue(tmp_path) -> None:
    engine = _engine()
    strategy = _BuysThenExits(_strategy_config(tmp_path))
    engine.add_strategy(strategy)
    engine.add_data(_quotes(5))
    try:
        engine.run()

        assert len(strategy.exit_order_ids) == 1, "the exit path must have sent one order"
        exit_order = engine.cache.order(strategy.exit_order_ids[0])
        assert exit_order.side == OrderSide.SELL
        assert exit_order.quantity == Quantity.from_str("0.006300")
        assert exit_order.status == OrderStatus.FILLED, (
            f"the plain close must fill at the venue, got {exit_order.status!r}"
        )
        assert not engine.cache.positions_open(instrument_id=_SPOT.id), "the position is closed"
        assert PositionSide.SHORT not in strategy.position_sides, "a cash close never goes short"
        account = engine.cache.account_for_venue(_VENUE)
        assert account.balance_total(_BTC).as_decimal() == Decimal("0"), (
            "the account holds no base currency after the close"
        )
        assert exit_order.is_reduce_only is False, (
            "on a cash account the close must be plain; spot adapters deny reduce-only"
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# A denied order reaches the toolkit strategy
# ---------------------------------------------------------------------------


class _DeniedOnFirstQuote(NautilusTradingStrategy):
    """A toolkit strategy that sends one order the risk engine must deny.

    The order asks for ten times the instrument's maximum quantity, so the risk engine
    denies it before it reaches the simulated venue -- the same place a spot adapter's
    local refusal arrives from. A second, valid order follows, to show the engine kept
    running after the denial was handled.
    """

    def __init__(self, config: NautilusTradingStrategyConfig) -> None:
        super().__init__(config)
        self._probe_quotes = 0
        self.probe_order_ids: list = []

    def on_strategy_start(self) -> None: ...

    def on_strategy_stop(self) -> None: ...

    def calculate_signal(self, ctx, bar) -> Signal:
        return Signal.neutral(float(bar.close), pair=ctx.pair)

    def get_indicator_history(self) -> dict[str, object]:
        return {}

    def on_core_quote_tick(self, tick) -> None:
        self._probe_quotes += 1
        if self._probe_quotes == 1:
            quantity = Quantity(float(_SPOT.max_quantity) * 10, _SPOT.size_precision)
        elif self._probe_quotes == 2:
            quantity = _SPOT.make_qty(0.001)
        else:
            return
        order = self.order_factory.market(
            instrument_id=_SPOT.id,
            order_side=OrderSide.BUY,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
        )
        self.probe_order_ids.append(order.client_order_id)
        self.submit_order(order)


class _Handled:
    """Stands in for the reconciler's denial handler and raises on its first call."""

    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)
        if len(self.events) == 1:
            raise RuntimeError("a handler that fails must not stop the engine")


@pytest.mark.xfail(strict=True, reason="the toolkit strategy does not handle OrderDenied yet")
def test_the_engine_delivers_denials_to_the_strategy(tmp_path) -> None:
    engine = _engine()
    strategy = _DeniedOnFirstQuote(_strategy_config(tmp_path, tick_type="quote"))
    engine.add_strategy(strategy)
    handled = _Handled()
    strategy._reconciler.handle_order_denied = handled  # noqa: SLF001 - the seam under test
    engine.add_data(_quotes(4))
    try:
        engine.run()

        orders = {order.client_order_id: order for order in engine.cache.orders()}
        assert len(strategy.probe_order_ids) == 2, "both probe orders must have been sent"
        denied_id, valid_id = strategy.probe_order_ids
        assert orders[denied_id].status == OrderStatus.DENIED
        assert orders[valid_id].status == OrderStatus.FILLED, (
            "the engine must keep running after the denial handler raised"
        )
        assert [event.client_order_id for event in handled.events] == [denied_id], (
            "the denial must reach the toolkit's handler"
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# An orphan plain take-profit on a cash account is cancelled at the venue
# ---------------------------------------------------------------------------


# One quote a second from t=1s: the sweep's per-order cool-down (120s) is measured from
# the epoch, so the first sweep that may cancel anything runs after t=120s.
_SWEEP_AT_QUOTE = 125


class _LeavesAnOrphan(NautilusStrategyCore):
    """Buy, rest a plain take-profit far above, close with a market sell, then sweep.

    The take-profit is left behind untracked, the shape a fire-and-forget cancel that
    failed leaves. The account also holds base currency that is not this strategy's,
    which is what the orphan would sell if it ever filled. The sweep is the real
    ``OrderReconciler.sweep_stale_orders_for_pair`` reading the engine's cache and
    account.
    """

    def __new__(cls, config: NautilusTradingStrategyConfig, **kwargs: object):
        return super().__new__(cls, config)

    def __init__(self, config: NautilusTradingStrategyConfig) -> None:
        super().__init__(config)
        self._quotes = 0
        self.take_profit_id = None

    def on_start(self) -> None:
        self.subscribe_quotes(_SPOT.id)

    def _market(self, side: OrderSide) -> None:
        self.submit_order(
            self.order_factory.market(
                instrument_id=_SPOT.id,
                order_side=side,
                quantity=_SPOT.make_qty(0.0063),
                time_in_force=TimeInForce.IOC,
            )
        )

    def on_core_quote_tick(self, tick) -> None:
        self._quotes += 1
        if self._quotes == 1:
            self._market(OrderSide.BUY)
        elif self._quotes == 2:
            take_profit = self.order_factory.limit(
                instrument_id=_SPOT.id,
                order_side=OrderSide.SELL,
                quantity=_SPOT.make_qty(0.0063),
                price=_SPOT.make_price(160_000.0),
                time_in_force=TimeInForce.GTC,
            )
            self.take_profit_id = take_profit.client_order_id
            self.submit_order(take_profit)
        elif self._quotes == 3:
            self._market(OrderSide.SELL)
        elif self._quotes == _SWEEP_AT_QUOTE:
            ctx = NS(
                instrument_id=_SPOT.id,
                pair="BTC-USDT",
                order_tracker=OrderTracker(),
                stale_cancel_attempts={},
            )
            OrderReconciler(self).sweep_stale_orders_for_pair(ctx)

    def get_indicator_history(self) -> dict[str, object]:
        return {}

    def on_core_bar(self, bar) -> None: ...
    def _on_bar_risk_hygiene(self, bar) -> None: ...


def test_an_orphan_plain_take_profit_is_cancelled_at_the_venue(tmp_path) -> None:
    engine = _engine(base=Money(0.01, _BTC))
    strategy = _LeavesAnOrphan(_strategy_config(tmp_path))
    engine.add_strategy(strategy)
    engine.add_data(_quotes(_SWEEP_AT_QUOTE + 2))
    try:
        engine.run()

        assert not engine.cache.positions_open(instrument_id=_SPOT.id), "precondition: flat"
        take_profit = engine.cache.order(strategy.take_profit_id)
        assert take_profit.is_reduce_only is False
        assert take_profit.status == OrderStatus.CANCELED, (
            f"the orphan must be cancelled, got {take_profit.status!r}"
        )
        account = engine.cache.account_for_venue(_VENUE)
        assert account.balance_total(_BTC).as_decimal() == Decimal("0.01"), (
            "the base currency that was never the strategy's is still there"
        )
    finally:
        engine.dispose()
