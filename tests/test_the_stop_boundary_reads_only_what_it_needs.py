"""The stop-boundary read values an open position held by a real engine.

The terminal valuation carries equity and, per open position, quantity, mark and
currency. NautilusTrader reports a position's opening average only as a binary float
(``Position.avg_px_open -> f64``), and the stop-boundary read refuses binary floats. A
read that also fetched the opening average therefore failed on every stop that still
held a position -- on a margin account and on a cash account alike -- while the
unit tests passed, because their stand-in position reported the average as a decimal.

These tests hold the positions in a real ``BacktestEngine`` so the types are the
engine's own, then read the account exactly as the stop boundary does.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit.config import load_config  # noqa: E402
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_config import (  # noqa: E402
    NautilusTradingStrategyConfig,
    build_nautilus_base_config,
)
from nautilus_trader.backtest import BacktestEngine  # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    AggressorSide,
    Currency,
    Money,
    OmsType,
    OrderSide,
    PriceType,
)
from nautilus_trader.testkit.providers import TestInstrumentProvider  # noqa: E402

from custos.engines.nautilus.portfolio_snapshot import (  # noqa: E402
    NautilusPortfolioSnapshotProvider,
)
from tests.fixtures import nt_data_stubs as TestDataStubs  # noqa: E402

USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")

# Three fills whose weighted average, 90000.480952380952..., has no finite decimal
# form: the engine can only report it rounded, and it reports it as a float.
_FILL_PRICES = (90000.1, 90000.3, 90000.7)
_FILL_SIZES = (0.003, 0.007, 0.011)
_MARK = 89950.5


class _TradesOnEachTick(NautilusStrategyCore):
    """Submit one market order per trade tick until the sizes run out."""

    # 2.0's Strategy is a pyclass that initialises in __new__, so every argument
    # handed to the subclass reaches __new__ too. Absorb the extra ones there.
    def __new__(cls, config: NautilusTradingStrategyConfig, **kwargs: object):
        return super().__new__(cls, config)

    def __init__(self, config, *, instrument, side, sizes) -> None:
        super().__init__(config)
        self._instrument = instrument
        self._side = side
        self._sizes = list(sizes)

    def on_start(self) -> None:
        self.subscribe_trades(self._instrument.id)

    def on_core_trade_tick(self, tick) -> None:
        if not self._sizes:
            return
        self.submit_order(
            self.order_factory.market(
                instrument_id=self._instrument.id,
                order_side=self._side,
                quantity=self._instrument.make_qty(self._sizes.pop(0)),
            )
        )

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


def _ticks(instrument, price: float, ts: int) -> list:
    return [
        TestDataStubs.quote_tick(
            instrument, bid_price=price, ask_price=price, ts_event=ts, ts_init=ts
        ),
        TestDataStubs.trade_tick(
            instrument,
            price=price,
            size=1.0,
            aggressor_side=AggressorSide.BUY,
            ts_event=ts + 1,
            ts_init=ts + 1,
        ),
    ]


@pytest.fixture
def holding_engine(tmp_path):
    """Run a real engine until it holds a position opened in three fills."""

    engines: list[BacktestEngine] = []

    def run(instrument, account_type, balances, side):
        engine = BacktestEngine(
            config=BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True))
        )
        engines.append(engine)
        engine.add_venue(
            venue=instrument.id.venue,
            oms_type=OmsType.NETTING,
            account_type=account_type,
            base_currency=USDT if account_type == AccountType.MARGIN else None,
            starting_balances=balances,
        )
        engine.add_instrument(instrument)
        engine.add_strategy(
            _TradesOnEachTick(
                _strategy_config(tmp_path), instrument=instrument, side=side, sizes=_FILL_SIZES
            )
        )
        data, ts = [], 1_000_000_000
        for price in (*_FILL_PRICES, _MARK):
            data.extend(_ticks(instrument, price, ts))
            ts += 1_000_000
        engine.add_data(data)
        engine.run()
        return SimpleNamespace(cache=engine.cache, portfolio=engine.portfolio)

    yield run
    for engine in engines:
        engine.dispose()


def _provider() -> NautilusPortfolioSnapshotProvider:
    # The host builds it with these price types.
    return NautilusPortfolioSnapshotProvider(
        price_type_mid=PriceType.MID, price_type_last=PriceType.LAST
    )


def test_the_engine_reports_the_opening_average_as_a_binary_float(holding_engine) -> None:
    """The premise of this file, measured rather than assumed."""
    runtime = holding_engine(
        TestInstrumentProvider.btcusdt_perp_binance(),
        AccountType.MARGIN,
        [Money(1_000_000, USDT)],
        OrderSide.SELL,
    )

    (position,) = runtime.cache.positions_open()

    assert type(position.avg_px_open) is float


def test_a_stop_boundary_read_values_an_open_short_on_a_real_margin_engine(
    holding_engine,
) -> None:
    runtime = holding_engine(
        TestInstrumentProvider.btcusdt_perp_binance(),
        AccountType.MARGIN,
        [Money(1_000_000, USDT)],
        OrderSide.SELL,
    )

    boundary = _provider().snapshot(runtime, currency="USDT", refuse_float=True)

    assert boundary.reliable is True, boundary.unreliable_reason
    # Wallet balance plus the engine's unrealized PnL, both handed over as Money.
    (venue_equity,) = runtime.portfolio.equity(
        TestInstrumentProvider.btcusdt_perp_binance().id.venue
    ).values()
    assert boundary.equity == venue_equity.as_decimal()
    assert boundary.runner_fact_rows() == [
        {
            "instrument": "BTCUSDT-PERP.BINANCE",
            "quantity": "-0.021",
            "mark_price": "89950.50",
            "currency": "USDT",
        }
    ]


def test_a_stop_boundary_read_values_an_open_long_on_a_real_cash_engine(holding_engine) -> None:
    runtime = holding_engine(
        TestInstrumentProvider.btcusdt_binance(),
        AccountType.CASH,
        [Money(1_000_000, USDT), Money(0, BTC)],
        OrderSide.BUY,
    )

    boundary = _provider().snapshot(runtime, currency="USDT", refuse_float=True)

    assert boundary.reliable is True, boundary.unreliable_reason
    # Cash balances valued at the mark in decimal arithmetic end to end.
    (usdt,) = (
        money
        for currency, money in runtime.portfolio.equity(
            TestInstrumentProvider.btcusdt_binance().id.venue
        ).items()
        if str(currency) == "USDT"
    )
    assert boundary.equity == usdt.as_decimal() + Decimal("0.021") * Decimal("89950.500")
    assert boundary.runner_fact_rows() == [
        {
            "instrument": "BTCUSDT.BINANCE",
            "quantity": "0.021000",
            "mark_price": "89950.500",
            "currency": "USDT",
        }
    ]


def test_the_periodic_read_still_carries_the_cost_basis(holding_engine) -> None:
    """Leaving the average out is the boundary's choice, not the provider's.

    The valuation checkpoint and the status surface still read it; only the stop
    boundary, which refuses binary floats, does without.
    """
    runtime = holding_engine(
        TestInstrumentProvider.btcusdt_perp_binance(),
        AccountType.MARGIN,
        [Money(1_000_000, USDT)],
        OrderSide.SELL,
    )

    periodic = _provider().snapshot(runtime, currency="USDT")
    boundary = _provider().snapshot(runtime, currency="USDT", refuse_float=True)

    assert periodic.reliable is True, periodic.unreliable_reason
    (row,) = periodic.valuation_rows()
    assert Decimal(row["avg_entry_price"]) == pytest.approx(Decimal("90000.4809523809"))
    assert periodic.equity == boundary.equity
    assert periodic.runner_fact_rows() == boundary.runner_fact_rows()
