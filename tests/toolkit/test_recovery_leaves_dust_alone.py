"""A restart that finds only dust leaves it alone.

Account 60366 holds 0.00000935 vBTC, below the pair's 0.00001 step. NautilusTrader
rounds it to a 0.00001 LONG position, and the recovery path used to treat that as a
position to protect: it placed a stop for it, armed tick monitoring on it, and a later
tick could send an exit for it -- orders the venue refuses, for a holding no order can
take off the book. The runner counts that holding as flat; the toolkit now does the
same and places nothing for it.

The position and the account are NautilusTrader's own types, built from a fill whose
fee is taken in the base currency the way SoDEX spot takes it: the rounding that turns
0.00000935 into 0.00001 is the type's behaviour, and a double would only assume it.
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
from custos_toolkit_nautilus.adapter.coordinators import OrderReconciler  # noqa: E402
from custos_toolkit_nautilus.adapter.orders import NativeTrailingStopSubmitter  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402
from nautilus_trader.core import UUID4  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountBalance,
    AccountId,
    AccountState,
    AccountType,
    CashAccount,
    ClientOrderId,
    Currency,
    CurrencyPair,
    InstrumentId,
    LiquiditySide,
    Money,
    OrderFilled,
    OrderSide,
    OrderType,
    Position,
    PositionId,
    Price,
    Quantity,
    StrategyId,
    Symbol,
    TradeId,
    TraderId,
    VenueOrderId,
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
_BASE_FEE = Money(Decimal("0.00000065"), _VBTC)


def _cash_account(base: str) -> CashAccount:
    held = Money(Decimal(base), _VBTC)
    state = AccountState(
        AccountId("SODEX_SPOT-60366"),
        AccountType.CASH,
        [AccountBalance(held, Money(0, _VBTC), held)],
        [],
        True,
        UUID4(),
        0,
        0,
    )
    return CashAccount(state, False)


def _position(bought: str) -> Position:
    """A long built from one buy whose fee came out of the base."""
    fill = OrderFilled(
        TraderId("TRADER-001"),
        StrategyId("Harness-000"),
        _PAIR.id,
        ClientOrderId("O-BUY"),
        VenueOrderId("V-BUY"),
        AccountId("SODEX_SPOT-60366"),
        TradeId("T-BUY"),
        OrderSide.BUY,
        OrderType.MARKET,
        Quantity.from_str(bought),
        Price.from_str("82961"),
        _VUSDC,
        LiquiditySide.TAKER,
        UUID4(),
        1,
        1,
        False,
        PositionId("P-1"),
        _BASE_FEE,
    )
    return Position(_PAIR, fill)


def _restarted(mode: SLTPMode, *, bought: str, held: str) -> Harness:
    """A restart on SoDEX spot: a cash account, an open position, a warm bar cache."""
    monitor = (
        TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.02"))
        if mode.uses_tick_monitor
        else None
    )
    h = Harness(mode=mode, monitor=monitor)
    h.instrument = _PAIR
    h.ctx.instrument_id = _PAIR.id
    h.position = _position(bought)
    h.positions = [h.position]
    h.ctx.position_tracker.reset()
    account = _cash_account(held)
    h.cache.account_for_venue = lambda _venue: account
    h.bars = [NS(close=Decimal("82962"))]
    h.order_factory.trailing_stop_market = lambda **kw: h.order(
        OrderType.TRAILING_STOP_MARKET, **kw
    )
    h.ctx.native_trailing_submitter = NativeTrailingStopSubmitter(h.order_factory, h.cache, h.log)
    h.config.risk = NS(
        trade=NS(
            take_profit=NS(method="none", scaled=None),
            max_loss_pct=Decimal("0.05"),
            stop_loss=NS(
                trailing=NS(
                    trailing_pct=Decimal("0.01"),
                    activation_pct=Decimal("0.01"),
                    trigger_price_type="last",
                )
            ),
        ),
        tick_monitoring=h.config.risk.tick_monitoring,
    )
    return h


def test_the_position_is_the_rounded_dust() -> None:
    """The premise, on the real type: 0.00000935 reads as a 0.00001 position."""
    position = _position("0.00001")

    assert position.quantity == Quantity.from_str("0.00001")
    assert position.is_long and not position.is_closed


@pytest.mark.parametrize("mode", [SLTPMode.HYBRID, SLTPMode.TICK, SLTPMode.NATIVE_TRAILING])
def test_recovery_leaves_dust_alone(mode: SLTPMode) -> None:
    h = _restarted(mode, bought="0.00001", held="0.00000935")

    OrderReconciler(h).recover_from_existing_positions()

    assert h.sent == [], "no stop, trailing stop or exit is placed for a holding no order can sell"
    assert h.ctx.position_tracker.entry_count == 0, "dust is not adopted as a position"
    if h.ctx.tick_monitor is not None:
        assert h.ctx.tick_monitor.check(Decimal("100000")) is None, "no position is monitored"
        h.tick("100000")
        assert h.sent == [] and h.closed == [], "a later tick sends no exit for it"


@pytest.mark.parametrize("mode", [SLTPMode.HYBRID, SLTPMode.NATIVE_TRAILING])
def test_the_per_bar_repair_leaves_dust_alone(mode: SLTPMode) -> None:
    """The repair runs on every bar; skipping dust only at restart would place it an hour later."""
    h = _restarted(mode, bought="0.00001", held="0.00000935")
    reconciler = OrderReconciler(h)

    reconciler.ensure_exchange_sl_protection(h.ctx)
    reconciler.ensure_native_trailing_protection(h.ctx)

    assert h.sent == []


@pytest.mark.parametrize("mode", [SLTPMode.HYBRID, SLTPMode.TICK])
def test_a_whole_step_position_is_recovered_as_before(mode: SLTPMode) -> None:
    """The control: 0.00100 bought, 0.00100870 held, worth 83 vUSDC. A real holding."""
    h = _restarted(mode, bought="0.00100", held="0.00100870")

    OrderReconciler(h).recover_from_existing_positions()

    assert h.ctx.position_tracker.entry_count == 1
    if mode is SLTPMode.HYBRID:
        assert [order.order_type for order in h.sent] == [OrderType.STOP_MARKET]
    else:
        assert h.ctx.tick_monitor.is_active
