"""On a cash account an order's role cannot be read from its reduce-only flag.

The toolkit used to answer "is this one of my stops, take-profits or closes?" by
looking at ``is_reduce_only``. On a cash account none of its orders carry the flag --
spot adapters refuse it -- so every one of those answers would flip to "no":

* a tick full close would not wait for the protective lots it competes with;
* a preserve shutdown would cancel the very protection it is meant to preserve;
* a venue rejection of a plain close would not be recognised as a failed close;
* a restart would not recognise the strategy's own stops and would place another.

Two kinds of evidence replace the flag, each where it fits. What the strategy has out
right now is known by id, because the order tracker records every stop, take-profit and
close it sends. What the venue itself records survives a restart: the flag on a margin
account, and on a cash account a sell -- a cash account cannot sell what it does not
hold -- provided the sell is this strategy's own.

On a margin account, and when the account cannot be read, every answer stays as it was.
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    ExecutionCoordinator,
    OrderReconciler,
)
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    OrderSide,
    OrderType,
    Price,
    StrategyId,
)

_SIBLING = StrategyId("Sibling-001")


def _on(h: Harness, account_type: AccountType | None) -> Harness:
    h.cache.account_for_venue = lambda _venue: (
        None if account_type is None else NS(account_type=account_type)
    )
    return h


def _resting(h: Harness, kind: OrderType, side: OrderSide, *, reduce_only: bool, **kw) -> NS:
    """An order resting at the venue, old enough to be past any in-flight window."""
    if kind is OrderType.LIMIT:
        kw.setdefault("price", Price.from_str("110.00"))
    if kind in (OrderType.STOP_MARKET, OrderType.TRAILING_STOP_MARKET):
        kw.setdefault("trigger_price", Price.from_str("90.00"))
    return h.order(
        kind,
        quantity=h.instrument.make_qty(Decimal("1")),
        order_side=side,
        reduce_only=reduce_only,
        **kw,
    )


# ---------------------------------------------------------------------------
# The tick full close waits for the protection it competes with
# ---------------------------------------------------------------------------


def _fixed_take_profit_harness(account_type: AccountType) -> Harness:
    monitor = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.02"))
    monitor.init_position(Decimal("100"), True)
    return _on(Harness(mode=SLTPMode.TICK, monitor=monitor), account_type)


def test_a_cash_full_close_first_cancels_its_plain_protection() -> None:
    """A tracked take-profit lot without the flag still holds the base the close sells."""
    h = _fixed_take_profit_harness(AccountType.CASH)
    lot = _resting(h, OrderType.LIMIT, OrderSide.SELL, reduce_only=False)
    h.ctx.order_tracker.add_tp_order(lot.client_order_id)

    h.tick("103")

    assert h.cancelled == [lot.client_order_id], "the competing lot is cancelled first"
    assert h.closed == [] and h.sent == [], "and the close waits for the next tick"


def test_a_margin_full_close_still_waits_for_reduce_only_orders() -> None:
    h = _fixed_take_profit_harness(AccountType.MARGIN)
    lot = _resting(h, OrderType.LIMIT, OrderSide.SELL, reduce_only=True)
    h.ctx.order_tracker.add_tp_order(lot.client_order_id)

    h.tick("103")

    assert h.cancelled == [lot.client_order_id]
    assert h.closed == []


def test_an_untracked_plain_order_does_not_hold_up_the_close() -> None:
    """Only what the tracker owns is cancelled first, so only that may delay the close.

    Waiting on an order the cancel step never reaches would hold the close back for good.
    """
    h = _fixed_take_profit_harness(AccountType.MARGIN)
    _resting(h, OrderType.LIMIT, OrderSide.SELL, reduce_only=False)

    ExecutionCoordinator(h).handle_trade_tick(
        NS(instrument_id=h.instrument.id, price=Decimal("103"))
    )

    assert h.cancelled == []
    assert len(h.closed) + len(h.sent) == 1, "the close goes out"


# ---------------------------------------------------------------------------
# A preserve shutdown keeps protection and cancels entry intent
# ---------------------------------------------------------------------------


def _stopping(h: Harness) -> Harness:
    h._shutdown_position_policy = "preserve"
    h._ready_file = "/nonexistent/strategy_ready"
    h._ready = False
    h.unsubscribe_bars = lambda *_a, **_k: None
    h._get_tick_monitoring_config = lambda: NS(enabled=False)
    h.on_strategy_stop = lambda: None
    h.on_stop = types.MethodType(NautilusTradingStrategy.on_stop, h)
    return h


def test_preserve_keeps_a_cash_accounts_own_sells() -> None:
    h = _stopping(_on(Harness(mode=SLTPMode.HYBRID), AccountType.CASH))
    stop = _resting(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=False)
    take_profit = _resting(h, OrderType.LIMIT, OrderSide.SELL, reduce_only=False)
    entry = _resting(h, OrderType.LIMIT, OrderSide.BUY, reduce_only=False)

    h.on_stop()

    assert h.cancelled == [entry.client_order_id], (
        "only the entry is future intent; the sells can only take the position down"
    )
    assert stop.client_order_id not in h.cancelled
    assert take_profit.client_order_id not in h.cancelled


def test_preserve_on_a_cash_account_does_not_adopt_another_strategys_sell() -> None:
    h = _stopping(_on(Harness(mode=SLTPMode.HYBRID), AccountType.CASH))
    foreign = _resting(h, OrderType.LIMIT, OrderSide.SELL, reduce_only=False)
    foreign.strategy_id = _SIBLING

    h.on_stop()

    assert h.cancelled == [foreign.client_order_id], "the rule as before: not ours, not kept"


@pytest.mark.parametrize("account_type", [AccountType.MARGIN, None])
def test_preserve_elsewhere_reads_the_flag_as_before(account_type) -> None:
    h = _stopping(_on(Harness(mode=SLTPMode.HYBRID), account_type))
    plain_sell = _resting(h, OrderType.LIMIT, OrderSide.SELL, reduce_only=False)
    stop = _resting(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=True)

    h.on_stop()

    assert h.cancelled == [plain_sell.client_order_id]
    assert stop.client_order_id not in h.cancelled


# ---------------------------------------------------------------------------
# A restart recognises the strategy's own stops
# ---------------------------------------------------------------------------


def _recovering(account_type: AccountType | None, mode: SLTPMode) -> Harness:
    h = _on(Harness(mode=mode), account_type)
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
        )
    )
    return h


def test_recovery_reclaims_a_cash_accounts_own_plain_stop() -> None:
    h = _recovering(AccountType.CASH, SLTPMode.HYBRID)
    stop = _resting(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=False)

    OrderReconciler(h).ensure_exchange_sl_exists(h.ctx, h.position)

    assert h.ctx.order_tracker.exchange_sl_order_ids == [stop.client_order_id]
    assert h.sent == [], "a second stop on a cash account would sell the same base twice"


def test_recovery_on_a_cash_account_does_not_claim_another_strategys_stop() -> None:
    h = _recovering(AccountType.CASH, SLTPMode.HYBRID)
    foreign = _resting(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=False)
    foreign.strategy_id = _SIBLING

    OrderReconciler(h).ensure_exchange_sl_exists(h.ctx, h.position)

    assert foreign.client_order_id not in h.ctx.order_tracker.exchange_sl_order_ids
    assert len(h.sent) == 1, "this strategy's own position still gets its own stop"


@pytest.mark.parametrize("account_type", [AccountType.MARGIN, None])
def test_recovery_elsewhere_claims_only_reduce_only_stops(account_type) -> None:
    h = _recovering(account_type, SLTPMode.HYBRID)
    plain = _resting(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=False)

    OrderReconciler(h).ensure_exchange_sl_exists(h.ctx, h.position)

    assert plain.client_order_id not in h.ctx.order_tracker.exchange_sl_order_ids


def test_recovery_reclaims_a_cash_accounts_own_plain_trailing_stop() -> None:
    h = _recovering(AccountType.CASH, SLTPMode.NATIVE_TRAILING)
    trailing = _resting(h, OrderType.TRAILING_STOP_MARKET, OrderSide.SELL, reduce_only=False)

    found = OrderReconciler(h).find_existing_trailing_orders(h.ctx, h.position)

    assert [order.client_order_id for order in found] == [trailing.client_order_id]


def test_recovery_on_a_margin_account_ignores_a_plain_trailing_stop() -> None:
    h = _recovering(AccountType.MARGIN, SLTPMode.NATIVE_TRAILING)
    _resting(h, OrderType.TRAILING_STOP_MARKET, OrderSide.SELL, reduce_only=False)

    assert OrderReconciler(h).find_existing_trailing_orders(h.ctx, h.position) == []


# ---------------------------------------------------------------------------
# A margin account's rejected escape-hatch close is now read as a close
# ---------------------------------------------------------------------------


def test_a_rejected_margin_hatch_close_is_counted_and_takes_the_protection_with_it() -> None:
    """The one change on a margin account, pinned with its full consequence.

    The hatch's plain close carries no reduce-only flag, so a venue rejection of it used
    to fall through as unknown: no count, no back-off. The tracker now knows it as a
    close, so it is handled like any other rejected close on the logic tier -- counted,
    backed off, and every open order on the instrument cancelled. That includes the
    position's own stop: the tracker forgets it and the per-bar repair rebuilds it.
    """
    from custos_toolkit.signals.types import Signal
    from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator
    from custos_toolkit_nautilus.adapter.coordinators.order_reconciler import (
        _CLOSE_REJECT_COOLDOWN_NS,
    )
    from custos_toolkit_nautilus.adapter.execution import ExecutionManager

    h = _recovering(AccountType.MARGIN, SLTPMode.HYBRID)
    h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
    stop = _resting(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=True)
    h.ctx.order_tracker.add_exchange_sl_order(stop.client_order_id, Decimal("1"))
    h.ctx.order_tracker.record_reduce_only_refusal()

    SignalExecutionCoordinator(h).execute_exit_for_pair(
        h.ctx, Signal.exit_long(Decimal("100"), pair=h.ctx.pair), bar=NS(close=Decimal("100"))
    )
    hatch = h.sent[-1]
    assert hatch.is_reduce_only is False, "precondition: the one plain attempt went out"

    OrderReconciler(h).handle_order_rejected(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=hatch.client_order_id,
            reason="code=-2019, msg=Margin is insufficient.",
        )
    )

    tracker = h.ctx.order_tracker
    assert tracker.close_reject_count == 1, "counted towards the halt"
    assert tracker.can_submit_close(h.now_ns) is False
    assert tracker.can_submit_close(h.now_ns + _CLOSE_REJECT_COOLDOWN_NS) is True
    assert stop.client_order_id in h.cancelled, "the logic tier cancels the stop as well"
    assert tracker.exchange_sl_order_ids == [], "and the tracker no longer holds it"

    stop.is_open, stop.is_closed = False, True  # the venue confirms the cancel
    OrderReconciler(h).ensure_exchange_sl_protection(h.ctx)

    rebuilt = [o for o in h.sent if o.order_type is OrderType.STOP_MARKET]
    assert len(rebuilt) == 1, "the per-bar repair puts a stop back"
