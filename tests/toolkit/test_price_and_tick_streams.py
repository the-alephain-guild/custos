"""Two consumers of market data, two decisions -- one subscription each, and a clean stop.

The guards need a price for every open position, so the strategy subscribes one
unconditionally. Tick monitoring needs the stream its configuration names. These used to
be tangled together in three ways, each seen on the SoDEX spot probe run:

* Spot was priced from trades alone, on the premise that SoDEX spot publishes no order
  book. It does (best bid 82961, best ask 82962 on the public endpoint, 2026-10-10),
  while its last trade was three days old -- so the one stream subscribed for pricing
  was the one that was silent. Spot is now priced from trades and quotes.
* A strategy monitoring trades subscribed them twice, once per consumer, and the second
  subscription drew "attempted duplicate trade subscription" from the engine.
* Shutdown unsubscribed only what tick monitoring had asked for, never the pricing
  streams.

Untangling them has one deliberate consequence, stated here because it removes a
behaviour some strategies relied on without choosing it: tick monitoring now reads only
the stream its ``tick_type`` names. Before, any subscribed trade or quote reached the
monitor -- so the trades subscribed for pricing were driving take-profits in strategies
configured for quotes, or with monitoring switched off.
"""

from __future__ import annotations

import sys
import types
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit_nautilus.adapter.config import TickMonitoringConfig  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    ExecutionCoordinator,
    PairContextCoordinator,
)
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy  # noqa: E402


class _Strategy(NS):
    """Records every subscription; anything else is the real strategy's method.

    Binding on lookup lets ``NautilusTradingStrategy.on_stop`` run against this object,
    which the engine-backed base type would not allow.
    """

    def __getattr__(self, name: str) -> object:
        attribute = getattr(NautilusTradingStrategy, name)
        if callable(attribute):
            return types.MethodType(attribute, self)
        return attribute


def _strategy(
    connector: str,
    *,
    tick: TickMonitoringConfig | None,
    instrument_ids: tuple[str, ...] = ("vBTC_vUSDC.SODEX_SPOT",),
    tmp_path: Path | None = None,
) -> _Strategy:
    calls: list[tuple[str, str]] = []

    def recorder(action: str):
        return lambda target, *args, **kwargs: calls.append((action, str(target)))

    strat = _Strategy(
        calls=calls,
        config=NS(
            trading=NS(connector=connector),
            risk=NS(tick_monitoring=tick or TickMonitoringConfig(enabled=False)),
        ),
        _contexts={
            iid: NS(instrument_id=iid, pair=iid.split(".")[0], bar_type=f"{iid}-BAR")
            for iid in instrument_ids
        },
        _mode=SLTPMode.TICK,
        _shutdown_position_policy=None,
        _ready_file=str((tmp_path or Path("/nonexistent")) / "ready"),
        log=MagicMock(),
        cancel_all_orders=MagicMock(),
        on_strategy_stop=MagicMock(),
    )
    for action in (
        "subscribe_trades",
        "subscribe_quotes",
        "subscribe_mark_prices",
        "unsubscribe_trades",
        "unsubscribe_quotes",
        "unsubscribe_mark_prices",
        "unsubscribe_bars",
    ):
        setattr(strat, action, recorder(action))
    strat._get_tick_monitoring_config = lambda: strat.config.risk.tick_monitoring
    strat._pair_context_coordinator = PairContextCoordinator(strat)
    return strat


def _start(strat: _Strategy) -> None:
    """The two subscription steps of on_start, in its order: tick streams, then pricing."""
    coordinator = strat._pair_context_coordinator
    coordinator.subscribe_ticks()
    coordinator.subscribe_mark_prices()


def _subscribed(strat: _Strategy) -> Counter:
    return Counter(
        (action.removeprefix("subscribe_"), target)
        for action, target in strat.calls
        if action.startswith("subscribe_")
    )


def _unsubscribed(strat: _Strategy) -> Counter:
    return Counter(
        (action.removeprefix("unsubscribe_"), target)
        for action, target in strat.calls
        if action.startswith("unsubscribe_") and action != "unsubscribe_bars"
    )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("connector", ["sodex", "binance", "okx"])
def test_a_spot_pair_is_priced_from_trades_and_quotes(connector) -> None:
    strat = _strategy(connector, tick=None)

    PairContextCoordinator(strat).subscribe_mark_prices()

    assert set(_subscribed(strat)) == {
        ("trades", "vBTC_vUSDC.SODEX_SPOT"),
        ("quotes", "vBTC_vUSDC.SODEX_SPOT"),
    }, "a quiet spot market still has a book; its mid is the price to value at"


@pytest.mark.parametrize("connector", ["binance_perpetual", "okx_perpetual", "sodex_perpetual"])
def test_a_perpetual_pair_is_priced_from_its_mark_alone(connector) -> None:
    strat = _strategy(connector, tick=None, instrument_ids=("BTC-USD.SODEX_PERPS",))

    PairContextCoordinator(strat).subscribe_mark_prices()

    assert set(_subscribed(strat)) == {("mark_prices", "BTC-USD.SODEX_PERPS")}


# ---------------------------------------------------------------------------
# One subscription per stream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tick_type",
    ["trade", "quote", "both"],
)
def test_each_stream_is_subscribed_once(tick_type) -> None:
    strat = _strategy("sodex", tick=TickMonitoringConfig(enabled=True, tick_type=tick_type))

    _start(strat)

    repeated = {stream: count for stream, count in _subscribed(strat).items() if count > 1}
    assert repeated == {}, f"each stream once per instrument; repeated: {repeated}"


# ---------------------------------------------------------------------------
# Stopping undoes starting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("connector", "tick"),
    [
        ("sodex", None),
        ("sodex", TickMonitoringConfig(enabled=True, tick_type="trade")),
        ("binance_perpetual", TickMonitoringConfig(enabled=True, tick_type="quote")),
        ("binance_perpetual", None),
    ],
)
def test_stop_unsubscribes_everything_start_subscribed(connector, tick, tmp_path) -> None:
    strat = _strategy(connector, tick=tick, tmp_path=tmp_path)
    _start(strat)

    strat.on_stop()

    assert set(_unsubscribed(strat)) == set(_subscribed(strat)), (
        "every stream the start subscribed must be released by the stop"
    )


# ---------------------------------------------------------------------------
# Tick monitoring reads the stream it was configured for
# ---------------------------------------------------------------------------


def _monitored(tick: TickMonitoringConfig) -> Harness:
    monitor = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.02"))
    monitor.init_position(Decimal("100"), True)
    harness = Harness(mode=SLTPMode.TICK, monitor=monitor)
    harness._get_tick_monitoring_config = lambda: tick
    harness.config.risk.tick_monitoring = tick
    return harness


def _feed(harness: Harness, stream: str) -> None:
    """One tick at the take-profit price, on the named stream."""
    coordinator = ExecutionCoordinator(harness)
    price = Decimal("103")
    if stream == "trade":
        coordinator.handle_trade_tick(NS(instrument_id=harness.instrument.id, price=price))
    else:
        coordinator.handle_quote_tick(
            NS(instrument_id=harness.instrument.id, bid_price=price, ask_price=price)
        )


def _drives_the_monitor(tick: TickMonitoringConfig, stream: str) -> bool:
    harness = _monitored(tick)
    _feed(harness, stream)
    return bool(harness.closed or harness.sent)


@pytest.mark.parametrize(
    ("tick_type", "stream"),
    [("trade", "trade"), ("quote", "quote"), ("both", "trade"), ("both", "quote")],
)
def test_the_configured_stream_drives_the_monitor(tick_type, stream) -> None:
    assert _drives_the_monitor(TickMonitoringConfig(enabled=True, tick_type=tick_type), stream)


@pytest.mark.parametrize(
    ("tick", "stream"),
    [
        (TickMonitoringConfig(enabled=True, tick_type="trade"), "quote"),
        (TickMonitoringConfig(enabled=True, tick_type="quote"), "trade"),
        (TickMonitoringConfig(enabled=False, tick_type="trade"), "trade"),
        (TickMonitoringConfig(enabled=False, tick_type="both"), "quote"),
    ],
)
def test_only_the_configured_tick_stream_drives_the_monitor(tick, stream) -> None:
    """Removes a behaviour on purpose: pricing streams no longer trigger take-profits."""
    assert not _drives_the_monitor(tick, stream), (
        f"a {stream} tick reached a monitor configured for {tick.tick_type} "
        f"(enabled={tick.enabled})"
    )
