"""A spot exit sells what the account holds above what was there before the run.

NautilusTrader reports a position rounded to the step. On SoDEX spot a buy's fee comes
out of the base, so after buying 0.00100 vBTC the account holds 0.00099935 of it plus
whatever it held before; the position still reads 0.00100. An exit sized from the
position either fails for want of balance or takes the shortfall from base currency
that is not this strategy's -- on account 60366, the 0.00000935 remainder the decision
maker asked to keep.

Every path that sells a spot holding is capped the same way: the free base balance,
less the sub-step remainder recorded when the strategy started, floored to the step.
A capped quantity the venue would refuse for its size is not sent, and that is said.
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    PairContextCoordinator,
    SignalExecutionCoordinator,
)
from custos_toolkit_nautilus.adapter.execution import ExecutionManager  # noqa: E402
from custos_toolkit_nautilus.adapter.orders import OrderTracker  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402
from nautilus_trader.core import UUID4  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountBalance,
    AccountId,
    AccountState,
    AccountType,
    CashAccount,
    Currency,
    CurrencyPair,
    InstrumentId,
    Money,
    OrderSide,
    Price,
    Quantity,
    Symbol,
)

_VBTC = Currency.from_str("vBTC")
_VUSDC = Currency.from_str("vUSDC")
# SoDEX testnet vBTC_vUSDC: step and minimum quantity 0.00001, minimum notional 5.
_PAIR = CurrencyPair(
    InstrumentId.from_str("vBTC_vUSDC.SODEX_SPOT"),
    Symbol("vBTC_vUSDC"),
    _VBTC,
    _VUSDC,
    0,
    5,
    Price.from_str("1"),
    Quantity.from_str("0.00001"),
    0,
    0,
    min_quantity=Quantity.from_str("0.00001"),
    min_notional=Money(Decimal("5"), _VUSDC),
)
_PRICE = Decimal("82962")
_REMAINDER = Decimal("0.00000935")


def _account(account_type: AccountType, free: str, locked: str = "0") -> object:
    available = Money(Decimal(free), _VBTC)
    held_back = Money(Decimal(locked), _VBTC)
    total = Money(Decimal(free) + Decimal(locked), _VBTC)
    state = AccountState(
        AccountId("SODEX_SPOT-60366"),
        account_type,
        [AccountBalance(total, held_back, available)],
        [],
        True,
        UUID4(),
        0,
        0,
    )
    if account_type is AccountType.CASH:
        return CashAccount(state, False)
    return NS(account_type=account_type)


def _harness(
    *,
    position: str,
    free: str,
    reserved: Decimal | None,
    account_type: AccountType = AccountType.CASH,
    monitor: TickMonitorManager | None = None,
    locked: str = "0",
) -> Harness:
    h = Harness(mode=SLTPMode.TICK, monitor=monitor)
    h.instrument = _PAIR
    h.ctx.instrument_id = _PAIR.id
    h.position.quantity = Quantity.from_str(position)
    h.position.avg_px_open = _PRICE
    h.position.instrument_id = _PAIR.id
    account = _account(account_type, free, locked)
    h.cache.account_for_venue = lambda _venue: account
    h.ctx.spot_reserved_remainder = reserved
    h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
    return h


def _bar_exit(h: Harness) -> None:
    SignalExecutionCoordinator(h).execute_exit_for_pair(
        h.ctx, Signal.exit_long(_PRICE, pair=h.ctx.pair), NS(close=Price.from_str("82962"))
    )


def _tick_full_close(h: Harness) -> None:
    h.ctx.tick_monitor = TickMonitorManager(
        mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.02")
    )
    h.ctx.tick_monitor.init_position(_PRICE, True)
    h.tick("85000")


def _containment(h: Harness) -> None:
    core = _CoreStub(
        cache=h.cache,
        log=h.log,
        order_factory=h.order_factory,
        submit_order=h.submit_order,
        close_position=h.close_position,
        order_tracker_for=lambda _instrument_id: h.ctx.order_tracker,
        spot_reserve_for=lambda _instrument_id: h.ctx.spot_reserved_remainder,
    )
    core.close_all_positions_with_fallback(_PAIR.id)


class _CoreStub(NS):
    """The core's own methods bound onto a plain object (see test_spot_close_form)."""

    def __getattr__(self, name: str) -> object:
        attribute = getattr(NautilusStrategyCore, name)
        if callable(attribute):
            return types.MethodType(attribute, self)
        return attribute


_FULL_EXITS = {
    "bar exit": _bar_exit,
    "tick full close": _tick_full_close,
    "containment": _containment,
}


@pytest.mark.parametrize("path", list(_FULL_EXITS))
def test_a_spot_exit_leaves_the_starting_remainder(path: str) -> None:
    """0.00100 bought with the fee in base, on top of 0.00000935: 0.00099 is sold."""
    h = _harness(position="0.00100", free="0.00100870", reserved=_REMAINDER)

    _FULL_EXITS[path](h)

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00099")]
    assert h.sent[0].side == OrderSide.SELL
    assert h.closed == [], "a cash account's close is a submitted order"


@pytest.mark.parametrize("path", list(_FULL_EXITS))
def test_a_whole_step_start_sells_the_whole_position(path: str) -> None:
    """Nothing below one step at start, nothing reserved: the full position goes."""
    h = _harness(position="0.00100", free="0.00100", reserved=Decimal("0"))

    _FULL_EXITS[path](h)

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00100")]


@pytest.mark.parametrize("path", list(_FULL_EXITS))
def test_an_exit_with_nothing_to_sell_is_not_sent_and_says_why(path: str) -> None:
    """After a capped exit the account holds 0.0000187: the reserve, and one more remainder."""
    h = _harness(position="0.00001", free="0.00001870", reserved=_REMAINDER)

    _FULL_EXITS[path](h)

    assert h.sent == [] and h.closed == []
    said = " ".join(str(call) for call in h.log.warning.call_args_list)
    assert "spot_exit_not_sent" in said and "below_minimum_quantity" in said


def test_an_exit_under_the_minimum_notional_is_not_sent() -> None:
    """0.00004 vBTC is a whole number of steps, and 3.32 vUSDC: the venue refuses it."""
    h = _harness(position="0.00004", free="0.00004935", reserved=_REMAINDER)

    _bar_exit(h)

    assert h.sent == []
    said = " ".join(str(call) for call in h.log.warning.call_args_list)
    assert "below_minimum_notional" in said


def test_base_held_by_a_resting_sell_is_not_sold_again() -> None:
    """A resting sell holds 0.0005 of the 0.00100870: only what is free can go now."""
    h = _harness(position="0.00100", free="0.00050870", locked="0.0005", reserved=_REMAINDER)

    _bar_exit(h)

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00049")]


def test_a_margin_account_exit_is_not_capped() -> None:
    """Without a cash account there is no base balance to respect; the position decides."""
    h = _harness(position="0.00100", free="0", reserved=None, account_type=AccountType.MARGIN)

    _bar_exit(h)

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00100")]
    assert h.sent[0].is_reduce_only is True


def test_a_partial_exit_never_sells_more_than_is_free() -> None:
    """A scaled level asks for half the position; only 0.00030 is free above the reserve."""
    monitor = TickMonitorManager(
        mode="tick",
        tp_method="scaled",
        tp_levels=[{"target_pct": Decimal(".02"), "exit_pct": Decimal(".5")}],
    )
    monitor.init_position(_PRICE, True)
    h = _harness(position="0.00100", free="0.00030935", reserved=_REMAINDER, monitor=monitor)

    h.tick("85000")

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00030")]


def test_on_start_records_the_sub_step_remainder() -> None:
    h = _harness(position="0.00001", free="0.00000935", reserved=None)
    h.config.trading.pairs = [h.ctx.pair]

    PairContextCoordinator(h).record_spot_reserves()

    assert h.ctx.spot_reserved_remainder == _REMAINDER
    said = " ".join(str(call) for call in h.log.info.call_args_list)
    assert f"spot_reserved_remainder pair={h.ctx.pair} quantity={_REMAINDER}" in said


def test_on_start_reserves_nothing_without_a_cash_account() -> None:
    h = _harness(
        position="0.00001", free="0", reserved=Decimal("1"), account_type=AccountType.MARGIN
    )

    PairContextCoordinator(h).record_spot_reserves()

    assert h.ctx.spot_reserved_remainder is None


def test_the_reserve_hook_reads_the_pair_context() -> None:
    """The containment path reaches the reserve through the strategy, as it reaches the tracker."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    ctx = NS(spot_reserved_remainder=_REMAINDER, order_tracker=OrderTracker())
    strat = NS(_contexts={_PAIR.id: ctx}, log=MagicMock())
    strat.spot_reserve_for = types.MethodType(NautilusTradingStrategy.spot_reserve_for, strat)

    assert strat.spot_reserve_for(_PAIR.id) == _REMAINDER
    assert strat.spot_reserve_for(InstrumentId.from_str("vETH_vUSDC.SODEX_SPOT")) is None
    assert NautilusStrategyCore.spot_reserve_for(strat, _PAIR.id) is None, (
        "the core keeps no pair state, so it reserves nothing"
    )


def test_an_exit_signal_priced_with_the_bar_close_is_sized() -> None:
    """Strategies build exit signals from ``bar.close``, a NautilusTrader ``Price``.

    ``Signal`` normalises only int and float, so the price arrives as ``Price``. The plan 05
    T7 backtest of the scale_in_probe fixture found every exit without an amount failing
    on it with a TypeError inside on_bar, the position left open.
    """
    h = _harness(position="0.00100", free="0.00100870", reserved=_REMAINDER)

    SignalExecutionCoordinator(h).execute_exit_for_pair(
        h.ctx,
        Signal.exit_long(Price.from_str("82962"), pair=h.ctx.pair),
        NS(close=Price.from_str("82962")),
    )

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00099")]
