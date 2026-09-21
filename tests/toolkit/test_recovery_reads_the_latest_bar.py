"""Restarting must restore protection from where the market is now.

The recovery path read ``cache.bars(...)[-1]``. NautilusTrader stores bars with
``push_front`` and documents index 0 as the most recent, so ``[-1]`` is the *oldest*
bar the cache still holds -- up to ``bar_capacity`` ago. A restart therefore rebuilt
the trailing stop's state from a stale price: it could miss the activation that the
real price had already crossed, or adopt a high-water mark that belongs to no
position that is currently open.

A double holding one bar cannot tell the two ends apart, which is why every case
here feeds at least two.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import OrderReconciler  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402


def _trailing_monitor() -> TickMonitorManager:
    """Activates 2% above entry, then exits on a 1% slip from the peak."""
    return TickMonitorManager(
        mode="tick",
        tp_method="trailing",
        trailing_activation_pct=Decimal("0.02"),
        trailing_pct=Decimal("0.01"),
    )


def _recovered(bars: list[str], *, entry: str = "100") -> Harness:
    """A restart: an open position, a warm bar cache, and a fresh monitor."""
    harness = Harness(mode=SLTPMode.TICK, monitor=_trailing_monitor())
    harness.position.avg_px_open = Decimal(entry)
    harness.position.quantity = Decimal("1")
    # Newest first, as the real cache stores them.
    harness.bars = [NS(close=Decimal(price)) for price in bars]
    OrderReconciler(harness).recover_from_existing_positions()
    return harness


def test_the_peak_comes_from_the_newest_bar_not_the_oldest() -> None:
    monitor = _recovered(["104", "101"]).ctx.tick_monitor

    assert monitor.peak_price == Decimal("104"), (
        "the cache holds newest first; reading the other end restores a stale price"
    )


def test_an_activation_the_market_already_crossed_is_restored() -> None:
    """Activation at 2%: the real price is 104, the oldest cached bar is 101."""
    monitor = _recovered(["104", "101"]).ctx.tick_monitor

    assert monitor._trailing_manager is not None  # noqa: SLF001 - state under test
    assert monitor._trailing_manager.activated is True  # noqa: SLF001 - state under test


def test_a_slip_after_that_activation_actually_exits() -> None:
    """The consequence the reviewer measured: 101.5 must trigger, and did not."""
    monitor = _recovered(["104", "101"]).ctx.tick_monitor

    action = monitor.check(Decimal("101.5"))

    assert action is not None, "a 2.4% slip from a 104 peak must take the position out"
    assert action.exit_type == "trailing_stop"


def test_a_price_that_never_reached_activation_does_not_exit() -> None:
    """The control: reading the newest bar must not invent an activation either."""
    monitor = _recovered(["101", "100.5"]).ctx.tick_monitor

    assert monitor._trailing_manager.activated is False  # noqa: SLF001 - state under test
    assert monitor.check(Decimal("100.2")) is None


def test_an_old_high_does_not_become_this_position_s_peak() -> None:
    """A spike from before this position opened must not set the high-water mark."""
    monitor = _recovered(["103", "140", "101"]).ctx.tick_monitor

    assert monitor.peak_price == Decimal("103")
    assert monitor.peak_price != Decimal("140"), (
        "140 belongs to an older bar; adopting it would exit on a drawdown that "
        "this position never had"
    )


def test_recovery_without_any_bars_falls_back_to_the_entry() -> None:
    """No market to observe: the peak stays at the entry, which is the honest baseline.

    It must not be left unset, and it must not be carried over from some other
    position -- either would make the first tick after recovery compute a drawdown
    against a number nobody chose.
    """
    harness = _recovered([])
    monitor = harness.ctx.tick_monitor

    assert monitor.is_active
    assert monitor._entry_price == Decimal("100")  # noqa: SLF001 - state under test
    assert monitor.peak_price == Decimal("100")
    assert monitor._trailing_manager.activated is False  # noqa: SLF001 - state under test
    assert monitor.check(Decimal("99")) is None


def test_the_position_itself_is_still_restored() -> None:
    """The bar is one input among several; the rest of recovery must still happen."""
    harness = _recovered(["104", "101"])

    assert harness.ctx.tick_monitor._entry_price == Decimal("100")  # noqa: SLF001
    assert harness.ctx.tick_monitor._is_long is True  # noqa: SLF001


@pytest.mark.parametrize("newest", ["104", "108", "120"])
def test_whatever_the_newest_bar_is_that_is_the_peak(newest: str) -> None:
    """Parametrised so the assertion cannot pass by matching one hard-coded number."""
    monitor = _recovered([newest, "101", "99"]).ctx.tick_monitor

    assert monitor.peak_price == Decimal(newest)


def test_against_a_real_engine_cache_the_newest_bar_is_the_one_restored() -> None:
    """The ordering claim, checked against NautilusTrader itself rather than a double.

    Everything above runs through the harness, where the ordering is whatever the
    double says it is. This one feeds two bars to a real BacktestEngine, asserts the
    cache really does hand them back newest-first, and then recovers through that
    cache -- so the fix is pinned to the library's behaviour, not to our stand-in.
    """
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggerConfig
    from nautilus_trader.model import (
        AccountType,
        Bar,
        BarType,
        Currency,
        Money,
        OmsType,
        Venue,
    )
    from nautilus_trader.testkit.providers import TestInstrumentProvider

    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True), run_analysis=False)
    )
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    try:
        engine.add_venue(
            Venue("BINANCE"),
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money.from_str("100000 USDT")],
            base_currency=Currency.from_str("USDT"),
        )
        engine.add_instrument(instrument)
        engine.add_data(
            [
                Bar(
                    bar_type,
                    instrument.make_price(price),
                    instrument.make_price(price + 1),
                    instrument.make_price(price - 1),
                    instrument.make_price(price),
                    instrument.make_qty(10),
                    minute * 60 * 10**9,
                    minute * 60 * 10**9,
                )
                for minute, price in enumerate((101, 104), 1)
            ]
        )
        engine.run()

        cached = engine.cache.bars(bar_type)
        assert [Decimal(str(bar.close)) for bar in cached] == [
            Decimal("104"),
            Decimal("101"),
        ], "the library stores newest first; the rest of this file assumes it"

        monitor = _trailing_monitor()
        harness = Harness(mode=SLTPMode.TICK, monitor=monitor)
        harness.ctx.bar_type = bar_type
        harness.ctx.instrument_id = instrument.id
        harness.position.instrument_id = instrument.id
        harness.position.avg_px_open = Decimal("100")
        harness._contexts = {instrument.id: harness.ctx}  # noqa: SLF001 - fixture wiring
        # Both accessors come from the real cache, so nothing here can be answered
        # by the harness's own list.
        harness.cache.bars = engine.cache.bars
        harness.cache.bar = engine.cache.bar

        OrderReconciler(harness).recover_from_existing_positions()

        assert monitor.peak_price == Decimal("104")
        assert monitor.check(Decimal("101.5")) is not None, (
            "a slip from the real latest price must take the position out"
        )
    finally:
        engine.dispose()
