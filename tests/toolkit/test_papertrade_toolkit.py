"""Papertrade uses native derivative symbols and reference quotes, without trade ticks."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("nautilus_trader")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from custos_toolkit_nautilus.adapter.config import TickMonitoringConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.utils import (  # noqa: E402
    derive_instrument_id,
    get_venue_from_connector,
    instrument_id_str,
    is_futures_connector,
)
from test_exchange_stop_capability import _config  # noqa: E402
from test_price_and_tick_streams import (  # noqa: E402
    _start,
    _strategy,
    _subscribed,
    _unsubscribed,
)


@pytest.mark.parametrize("pair", ["BTC-USD-PERP", "ETH-USD-PERP"])
def test_papertrade_preserves_the_native_symbol_and_venue(pair):
    from custos_toolkit_nautilus.adapter.config.trading import TradingConfig

    assert get_venue_from_connector("papertrade") == "PAPERTRADE"
    assert instrument_id_str(pair, "papertrade") == pair + ".PAPERTRADE"
    assert str(derive_instrument_id(TradingConfig(connector="papertrade", pairs=[pair]))) == (
        pair + ".PAPERTRADE"
    )
    assert is_futures_connector("papertrade") is True


@pytest.mark.parametrize("pair", ["BTC-USDT", "BTC-USD", "BTCUSDT", "btc-USD-PERP", "BTC/USD"])
def test_papertrade_never_translates_a_foreign_pair_into_an_invented_market(pair):
    with pytest.raises(ValueError, match="native"):
        instrument_id_str(pair, "papertrade")


def test_papertrade_external_order_claims_match_the_same_native_symbols(tmp_path):
    from custos_toolkit.config import load_config
    from custos_toolkit_nautilus.adapter.trading_config import build_nautilus_base_config

    path = tmp_path / "papertrade.yaml"
    path.write_text(
        "strategy:\n  name: probe\n"
        "trading:\n  connector: papertrade\n  pairs: [BTC-USD-PERP, ETH-USD-PERP]\n"
        "risk:\n  trade:\n    sl_tp_mode: tick\n",
        encoding="utf-8",
    )
    claims = build_nautilus_base_config(load_config(path))["external_order_instrument_ids"]
    assert [str(claim) for claim in claims] == [
        "BTC-USD-PERP.PAPERTRADE",
        "ETH-USD-PERP.PAPERTRADE",
    ]


@pytest.mark.parametrize("enabled", [False, True])
def test_papertrade_prices_every_position_from_one_quote_subscription_and_releases_it(enabled):
    iid = "BTC-USD-PERP.PAPERTRADE"
    strategy = _strategy(
        "papertrade",
        tick=TickMonitoringConfig(enabled=enabled, tick_type="quote"),
        instrument_ids=(iid,),
    )
    _start(strategy)
    assert _subscribed(strategy) == {("quotes", iid): 1}
    strategy._pair_context_coordinator.unsubscribe_streams()
    assert _unsubscribed(strategy) == {("quotes", iid): 1}


@pytest.mark.parametrize("mode", ["hybrid", "exchange", "native_trailing"])
def test_papertrade_refuses_every_mode_that_requires_a_venue_stop(mode):
    with pytest.raises(ValueError, match="cannot hold a stop"):
        _config("papertrade", mode)


@pytest.mark.parametrize("tick_type", ["trade", "both"])
def test_papertrade_config_refuses_unsupported_trade_tick_monitoring(tick_type):
    import msgspec

    config = _config("papertrade", "tick")
    risk = msgspec.structs.replace(
        config.risk,
        tick_monitoring=TickMonitoringConfig(enabled=True, tick_type=tick_type),
    )
    with pytest.raises(ValueError, match="Papertrade.*quote"):
        type(config)(
            trading=config.trading,
            position=config.position,
            risk=risk,
            filters=config.filters,
            platforms=config.platforms,
            backtesting=config.backtesting,
        )


def test_papertrade_config_accepts_explicit_quote_ticks():
    import msgspec

    config = _config("papertrade", "tick")
    risk = msgspec.structs.replace(
        config.risk, tick_monitoring=TickMonitoringConfig(enabled=True, tick_type="quote")
    )
    configured = type(config)(
        trading=config.trading,
        position=config.position,
        risk=risk,
        filters=config.filters,
        platforms=config.platforms,
        backtesting=config.backtesting,
    )
    assert configured.risk.tick_monitoring.tick_type == "quote"
