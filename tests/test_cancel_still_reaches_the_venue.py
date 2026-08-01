"""The recorded cancel still cancels: a real engine, a real order, really gone.

The overrides that record cancel requests sit directly in front of NautilusTrader's own
``cancel_order`` / ``cancel_all_orders``. Their unit tests replace that handoff, because
the Cython base needs a running engine — which means the one step that must not break is
precisely the step those tests stub out. On a money path that is not an acceptable place
to reason instead of measure: a broken handoff would leave a stop-loss alive at the
venue while every test stayed green and the log cheerfully recorded the request.

So this drives both overrides through a real ``BacktestEngine`` and reads the order's
status back from the venue afterwards.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit.config import load_config  # noqa: E402
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_config import (  # noqa: E402
    NautilusTradingStrategyConfig,
    build_nautilus_base_config,
)
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USDT  # noqa: E402
from nautilus_trader.model.enums import (  # noqa: E402
    AccountType,
    AggressorSide,
    OmsType,
    OrderSide,
    OrderStatus,
)
from nautilus_trader.model.identifiers import Venue  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402
from nautilus_trader.test_kit.stubs.data import TestDataStubs  # noqa: E402

_INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()
_VENUE = Venue("BINANCE")


class _RestsThenCancels(NautilusStrategyCore):
    """Rest a limit order well away from the market, then cancel it on the next tick.

    A resting order is the shape that matters here: the orphans this machinery exists for
    are resting reduce-only stops, and only an order the venue still holds can be
    observably cancelled.
    """

    def __init__(self, config: NautilusTradingStrategyConfig, *, bulk: bool) -> None:
        super().__init__(config=config)
        self._bulk = bulk
        self._order = None
        self.canceled_ids: list[str] = []

    def on_start(self) -> None:
        # Order events arrive unasked; data does not. Without this the cancel tick never
        # lands and the test would fail looking exactly like a broken delegation.
        self.subscribe_trade_ticks(_INSTRUMENT.id)
        self._order = self.order_factory.limit(
            instrument_id=_INSTRUMENT.id,
            order_side=OrderSide.BUY,
            quantity=_INSTRUMENT.make_qty(1.0),
            price=_INSTRUMENT.make_price(1.0),  # far below the market, so it rests
        )
        self.submit_order(self._order)

    def on_core_trade_tick(self, tick) -> None:
        if self._order is None:
            return
        order, self._order = self._order, None
        if self._bulk:
            self.cancel_all_orders(_INSTRUMENT.id)
        else:
            self.cancel_order(order)

    def on_order_canceled(self, event) -> None:
        self.canceled_ids.append(event.client_order_id.value)

    # Required by the base class; nothing here needs them.
    def get_indicator_history(self) -> dict[str, object]:
        return {}

    def on_core_bar(self, bar) -> None: ...
    def on_core_quote_tick(self, tick) -> None: ...
    def _on_bar_risk_hygiene(self, bar) -> None: ...


def _strategy_config(directory) -> NautilusTradingStrategyConfig:
    (directory / "config.yaml").write_text("strategy:\n  name: probe\n", encoding="utf-8")
    return NautilusTradingStrategyConfig(
        **build_nautilus_base_config(load_config(directory / "config.yaml"))
    )


def _run(tmp_path, *, bulk: bool) -> tuple[list[str], list]:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(_INSTRUMENT)

    strategy = _RestsThenCancels(_strategy_config(tmp_path), bulk=bulk)
    engine.add_strategy(strategy)

    ts = 1_000_000_000
    engine.add_data(
        [
            TestDataStubs.quote_tick(
                _INSTRUMENT, bid_price=100.0, ask_price=100.0, ts_event=ts, ts_init=ts
            ),
            TestDataStubs.trade_tick(
                _INSTRUMENT,
                price=100.0,
                size=1.0,
                aggressor_side=AggressorSide.BUYER,
                ts_event=ts,
                ts_init=ts,
            ),
            TestDataStubs.trade_tick(
                _INSTRUMENT,
                price=100.0,
                size=1.0,
                aggressor_side=AggressorSide.BUYER,
                ts_event=ts + 1,
                ts_init=ts + 1,
            ),
        ]
    )
    engine.run()
    orders = list(engine.cache.orders())
    canceled = list(strategy.canceled_ids)
    engine.dispose()
    return canceled, orders


def test_a_recorded_single_cancel_really_cancels(tmp_path) -> None:
    canceled, orders = _run(tmp_path, bulk=False)

    assert len(orders) == 1, "the probe order never reached the venue"
    assert orders[0].status == OrderStatus.CANCELED, (
        f"the order is {orders[0].status!r} — the override recorded the request but the "
        "cancel did not reach the venue"
    )
    assert canceled == [orders[0].client_order_id.value]


def test_a_recorded_bulk_cancel_really_cancels(tmp_path) -> None:
    """The bulk path enumerates the cache before delegating, so it has more to get wrong."""
    canceled, orders = _run(tmp_path, bulk=True)

    assert len(orders) == 1
    assert orders[0].status == OrderStatus.CANCELED, (
        f"the order is {orders[0].status!r} — enumerating for the record must not have "
        "consumed or replaced the delegation"
    )
    assert canceled == [orders[0].client_order_id.value]
