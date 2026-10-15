"""A venue that cannot hold a stop order refuses the modes that depend on one, at startup.

SoDEX maps only market and limit orders, on spot and on perpetuals alike; a stop-market
order is denied locally ("StopMarket orders is not supported by SoDEX"). Under the
default hybrid mode the strategy re-submitted its safety stop every sixty seconds, each
one denied, never paused, and kept entering -- a position with no stop at all, taking on
more.

Falling back to tick mode silently would be worse: tick monitoring handles take-profit
and the trailing exit, not a stop loss. So the refusal is made at construction, where
the author sees it, and it names the trade-off they must accept: tick mode has no stop.

Only venues known to lack the order type are refused. A venue that is not listed is
treated as able to hold a stop, as before.
"""

from __future__ import annotations

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config import (  # noqa: E402
    BacktestingConfig,
    FiltersConfig,
    PlatformsConfig,
    PositionConfig,
    RiskConfig,
    TradingConfig,
)
from custos_toolkit_nautilus.adapter.config.risk import TradeRiskConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_config import (  # noqa: E402
    NautilusTradingStrategyConfig,
)

_EXCHANGE_STOP_MODES = ("hybrid", "exchange", "native_trailing")


def _config(connector: str, sl_tp_mode: str) -> NautilusTradingStrategyConfig:
    return NautilusTradingStrategyConfig(
        trading=TradingConfig(connector=connector),
        position=PositionConfig(),
        risk=RiskConfig(trade=TradeRiskConfig(sl_tp_mode=sl_tp_mode)),
        filters=FiltersConfig(),
        platforms=PlatformsConfig(),
        backtesting=BacktestingConfig(),
    )


@pytest.mark.parametrize("connector", ["sodex", "sodex_perpetual"])
@pytest.mark.parametrize("sl_tp_mode", _EXCHANGE_STOP_MODES)
def test_a_venue_without_exchange_stops_refuses_modes_that_need_them(connector, sl_tp_mode) -> None:
    with pytest.raises(ValueError) as refused:
        _config(connector, sl_tp_mode)

    message = str(refused.value).lower()
    assert "tick" in message, "the refusal must name the mode that can run here"
    assert "no stop loss" in message, "and say plainly what that mode gives up"
    assert connector in message


@pytest.mark.parametrize("connector", ["sodex", "sodex_perpetual"])
def test_tick_mode_starts_on_a_venue_without_exchange_stops(connector) -> None:
    assert _config(connector, "tick").risk.trade.sl_tp_mode == "tick"


@pytest.mark.parametrize("connector", ["binance", "binance_perpetual", "okx_perpetual"])
@pytest.mark.parametrize("sl_tp_mode", _EXCHANGE_STOP_MODES)
def test_a_venue_not_known_to_lack_stops_is_not_refused(connector, sl_tp_mode) -> None:
    assert _config(connector, sl_tp_mode).risk.trade.sl_tp_mode == sl_tp_mode
