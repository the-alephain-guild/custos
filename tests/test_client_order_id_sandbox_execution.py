"""Local matching still works with the shortened client order id.

Sandbox mode is not the pure simulator — `--engine nautilus` with
`trading_mode: sandbox` builds a real NautilusTrader node whose execution client
matches locally (`_build_exec_plan` returns `SandboxLiveExecClientFactory`). It
therefore uses the same strategy config as testnet and live, so the id shape changed
underneath it too.

A local venue has no length rule to break, which is a reason to expect this to pass and
not a reason to skip checking: an order is submitted through a real engine and its fill
is read back, rather than the conclusion being reasoned to.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit.config import load_config  # noqa: E402
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
)
from nautilus_trader.model.identifiers import Venue  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402
from nautilus_trader.test_kit.stubs.data import TestDataStubs  # noqa: E402
from nautilus_trader.trading.strategy import Strategy  # noqa: E402

from custos.engines.nautilus.venue_binance import (  # noqa: E402
    BINANCE_CLIENT_ORDER_ID_MAX_LEN,
)

_INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()
_VENUE = Venue("BINANCE")


class _SubmitsOneOrder(Strategy):
    """Submits a single market order and records the fill the venue reports back."""

    def __init__(self, config: NautilusTradingStrategyConfig) -> None:
        super().__init__(config=config)
        self.filled_client_order_id: str | None = None

    def on_start(self) -> None:
        self.submit_order(
            self.order_factory.market(
                instrument_id=_INSTRUMENT.id,
                order_side=OrderSide.BUY,
                quantity=_INSTRUMENT.make_qty(1.0),
            )
        )

    def on_order_filled(self, event) -> None:
        self.filled_client_order_id = event.client_order_id.value


def _strategy_config(directory) -> NautilusTradingStrategyConfig:
    (directory / "config.yaml").write_text("strategy:\n  name: probe\n", encoding="utf-8")
    return NautilusTradingStrategyConfig(
        **build_nautilus_base_config(load_config(directory / "config.yaml"))
    )


def test_an_order_still_fills_locally_and_carries_the_short_id(tmp_path) -> None:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(_INSTRUMENT)

    strategy = _SubmitsOneOrder(_strategy_config(tmp_path))
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
        ]
    )
    engine.run()
    filled = strategy.filled_client_order_id
    engine.dispose()

    assert filled is not None, "the order never filled, so local matching is broken"
    assert len(filled) < BINANCE_CLIENT_ORDER_ID_MAX_LEN, (
        f"the filled order's id {filled!r} is {len(filled)} characters — local matching "
        "accepted an id a real venue would refuse"
    )
    assert "-" not in filled, f"{filled!r} still carries hyphens, so it would be 36 not 32"
