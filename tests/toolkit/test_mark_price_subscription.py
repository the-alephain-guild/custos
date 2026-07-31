"""The guards price positions off the mark price, so something has to subscribe it.

Real evidence, 2026-07-31: the fallback breaker fail-closed at startup with
``mark_price_unavailable`` and tried to flatten a live position. Investigation showed
this was not a race that waiting would fix. The portfolio snapshot prices each open
position by asking, in order:

    cache.mark_price(instrument_id)                 -- nothing subscribed it, always None
    cache.price(instrument_id, PriceType.MID)       -- needs quotes, and this deployment
                                                       subscribes bars and trades

Measured inside the runner image: with only a trade tick in the cache, ``LAST`` resolves
and ``MID`` / ``BID`` / ``ASK`` are all None; adding a quote tick makes ``MID`` resolve.
So with an open position the snapshot could never be reliable, the breaker could never
do anything but fail closed, and the record blamed a missing price rather than a missing
subscription.

Mark price is also the right input rather than a workaround: a perpetual's unrealised
PnL and liquidation are marked against it, not against the last trade.

The subscription is deliberately unconditional. Tick subscriptions are gated on the
strategy's own exit mode and tick-monitoring config, and the guards' need for a price
has nothing to do with either -- hanging it off that config is how the breaker ended up
depending on data nobody had asked for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator


def _strategy_with(instrument_ids: list[str]) -> SimpleNamespace:
    subscribed: list = []
    contexts = {
        f"pair-{i}": SimpleNamespace(instrument_id=iid, pair=f"pair-{i}")
        for i, iid in enumerate(instrument_ids)
    }
    return SimpleNamespace(
        _contexts=contexts,
        subscribed_mark_prices=subscribed,
        subscribe_mark_prices=lambda instrument_id: subscribed.append(instrument_id),
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )


def test_every_pair_gets_a_mark_price_subscription() -> None:
    strat = _strategy_with(["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"])
    coord = PairContextCoordinator(strat)

    coord.subscribe_mark_prices()

    assert strat.subscribed_mark_prices == [
        "BTCUSDT-PERP.BINANCE",
        "ETHUSDT-PERP.BINANCE",
    ]


def test_the_subscription_does_not_depend_on_tick_monitoring() -> None:
    """A strategy with tick monitoring off still needs its positions priced.

    This is the specific shape of the incident: the breaker's input was reachable only
    through configuration that exists for a different purpose.
    """
    strat = _strategy_with(["BTCUSDT-PERP.BINANCE"])
    strat._mode = SimpleNamespace(subscribes_tick_stream=False)
    strat._get_tick_monitoring_config = lambda: None
    coord = PairContextCoordinator(strat)

    coord.subscribe_ticks()  # no-op for this mode
    coord.subscribe_mark_prices()

    assert strat.subscribed_mark_prices == ["BTCUSDT-PERP.BINANCE"]


def test_on_start_subscribes_mark_prices() -> None:
    """The step has to be wired, not merely available -- an unwired subscription is
    exactly the state that produced the incident."""
    import inspect

    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.on_start)
    assert "subscribe_mark_prices()" in source
