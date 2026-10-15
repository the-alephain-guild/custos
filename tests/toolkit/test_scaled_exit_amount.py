"""An exit signal that carries an amount sells that amount, not the whole position.

``Signal.amount`` is a quote-currency notional, and the entry path already reads it that
way. The bar-exit path ignored it and sold the whole position, so a strategy that
scales out -- the scale_in_probe fixture's slots 14 to 16 -- was flattened by its first
exit and its later exits found nothing to sell (plan 05 T1: six fills per cycle where
the schedule has eight).

The amount is converted at the bar's close, floored to the step and capped at the
position; on a cash account it is further capped at what the account may sell. An amount
that covers the position closes it. One that converts to less than the venue accepts is
not sent, and that is said.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator  # noqa: E402
from custos_toolkit_nautilus.adapter.execution import ExecutionManager  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    Currency,
    CurrencyPair,
    InstrumentId,
    Money,
    OrderSide,
    Price,
    Quantity,
    Symbol,
)

_USDT = Currency.from_str("USDT")
# A spot pair shaped like the fixture's: step and minimum quantity 0.00001, notional 5.
_PAIR = CurrencyPair(
    InstrumentId.from_str("BTCUSDT.BINANCE"),
    Symbol("BTCUSDT"),
    Currency.from_str("BTC"),
    _USDT,
    2,
    5,
    Price.from_str("0.01"),
    Quantity.from_str("0.00001"),
    0,
    0,
    min_quantity=Quantity.from_str("0.00001"),
    min_notional=Money(Decimal("5"), _USDT),
)
# The fixture's first scale-out bar in plan 05 T1: close 95569, a 0.00630 position.
_CLOSE = Price.from_str("95569.00")


def _harness(account_type: AccountType, *, free: str = "1", position: str = "0.00630") -> Harness:
    h = Harness(mode=SLTPMode.TICK)
    h.instrument = _PAIR
    h.ctx.instrument_id = _PAIR.id
    h.position.quantity = Quantity.from_str(position)
    h.position.instrument_id = _PAIR.id
    held = lambda currency: Money(Decimal(free), currency)  # noqa: E731
    h.cache.account_for_venue = lambda _venue: NS(
        account_type=account_type, balance_free=held, balance_total=held
    )
    h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
    return h


def _exit(h: Harness, amount: str | None) -> None:
    signal = Signal.exit_long(
        Decimal("95569"), pair=h.ctx.pair, amount=None if amount is None else Decimal(amount)
    )
    SignalExecutionCoordinator(h).execute_exit_for_pair(h.ctx, signal, NS(close=_CLOSE))


def _warned(h: Harness) -> str:
    return " ".join(str(call) for call in h.log.warning.call_args_list)


@pytest.mark.parametrize("account_type", [AccountType.CASH, AccountType.MARGIN])
def test_a_partial_exit_takes_the_signal_amount(account_type: AccountType) -> None:
    """200 at 95569 is 0.0020927..., floored to 0.00209; the rest of the position stays."""
    h = _harness(account_type)

    _exit(h, "200")

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00209")]
    assert h.sent[0].side == OrderSide.SELL
    assert h.sent[0].is_reduce_only is (account_type is AccountType.MARGIN)


def test_an_exit_without_an_amount_closes_the_position() -> None:
    h = _harness(AccountType.CASH)

    _exit(h, None)

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00630")]


def test_an_amount_that_covers_the_position_closes_it() -> None:
    """1000 at 95569 is 0.01046, more than the 0.00630 held: a full close, not an oversell."""
    h = _harness(AccountType.CASH)

    _exit(h, "1000")

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00630")]


@pytest.mark.parametrize("account_type", [AccountType.CASH, AccountType.MARGIN])
@pytest.mark.parametrize(
    ("amount", "reason"),
    [("0.5", "below_minimum_quantity"), ("4", "below_minimum_notional")],
    ids=["under one step", "under the minimum notional"],
)
def test_an_amount_too_small_for_the_venue_is_not_sent(
    account_type: AccountType, amount: str, reason: str
) -> None:
    """0.5 converts to less than one step; 4 to 0.00004, worth 3.82 against a minimum of 5."""
    h = _harness(account_type)

    _exit(h, amount)

    assert h.sent == [] and h.closed == []
    assert reason in _warned(h)


def test_a_cash_partial_exit_is_capped_at_what_the_account_may_sell() -> None:
    h = _harness(AccountType.CASH, free="0.00100")
    h.ctx.spot_reserved_remainder = Decimal("0")

    _exit(h, "200")

    assert [order.quantity for order in h.sent] == [Quantity.from_str("0.00100")]
