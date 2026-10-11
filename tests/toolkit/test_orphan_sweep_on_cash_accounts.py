"""The orphan sweep recognises a cash account's plain exits and stops.

The per-bar sweep cancels this strategy's protective and closing orders once nothing
claims them any more -- the reversal and rejection paths cancel fire-and-forget and rely
on it to catch what a failed cancel leaves behind. It used to consider reduce-only
orders only. On a cash account those orders go out plain, so once orphaned nothing
would ever cancel them, and a resting orphan sell on a cash account sells base currency
the strategy no longer owns.

So an order enters the sweep's judgement when it can only reduce: reduce-only, or on a
cash account this strategy's own sell. Everything after that step is unchanged -- a
tracked order is kept, a young order is kept, a protective-side order is kept while a
position is open, and one order is cancelled at most once per cool-down.
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
from custos_toolkit_nautilus.adapter.orders import (  # noqa: E402
    STALE_ORDER_MIN_AGE_NS,
    STALE_SWEEP_RETRY_COOLDOWN_NS,
)
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    OrderSide,
    OrderType,
    Price,
    StrategyId,
)

_SIBLING = StrategyId("Sibling-001")


def _harness(account_type: AccountType | None, *, flat: bool = True) -> Harness:
    h = Harness()
    # Past the sweep's per-order cool-down measured from the epoch, as a live clock is.
    h.now_ns = 10 * STALE_SWEEP_RETRY_COOLDOWN_NS
    h.cache.account_for_venue = lambda _venue: (
        None if account_type is None else NS(account_type=account_type)
    )
    if flat:
        h.flat()
    return h


def _working(h: Harness, kind: OrderType, side: OrderSide, *, reduce_only: bool = False) -> NS:
    extra: dict[str, object] = {}
    if kind is OrderType.LIMIT:
        extra["price"] = Price.from_str("150.00")
    if kind is OrderType.STOP_MARKET:
        extra["trigger_price"] = Price.from_str("90.00")
    return h.order(
        kind,
        quantity=h.instrument.make_qty(Decimal("1")),
        order_side=side,
        reduce_only=reduce_only,
        **extra,
    )


def _sweep(h: Harness) -> list[str]:
    before = len(h.cancelled)
    OrderReconciler(h).sweep_stale_orders_for_pair(h.ctx)
    return h.cancelled[before:]


@pytest.mark.parametrize("kind", [OrderType.LIMIT, OrderType.STOP_MARKET, OrderType.MARKET])
def test_a_cash_orphan_plain_sell_is_swept(kind) -> None:
    h = _harness(AccountType.CASH)
    orphan = _working(h, kind, OrderSide.SELL)

    assert _sweep(h) == [orphan.client_order_id]

    h.now_ns += STALE_SWEEP_RETRY_COOLDOWN_NS - 1
    assert _sweep(h) == [], "one cancel per cool-down"


def test_cash_plain_orders_the_sweep_must_leave_alone() -> None:
    held = _harness(AccountType.CASH, flat=False)
    protective = _working(held, OrderType.STOP_MARKET, OrderSide.SELL)
    assert _sweep(held) == [], "a protective-side sell stays while the position is open"
    assert protective.is_open

    flat = _harness(AccountType.CASH)
    _working(flat, OrderType.LIMIT, OrderSide.BUY)
    foreign = _working(flat, OrderType.LIMIT, OrderSide.SELL)
    foreign.strategy_id = _SIBLING
    young = _working(flat, OrderType.LIMIT, OrderSide.SELL)
    young.ts_init = flat.now_ns - STALE_ORDER_MIN_AGE_NS + 1

    assert _sweep(flat) == [], (
        "an entry is not the sweep's, another strategy's sell is not ours, "
        "and a young order may still be in flight"
    )


@pytest.mark.parametrize("account_type", [AccountType.MARGIN, None])
def test_margin_and_unknown_accounts_sweep_as_before(account_type) -> None:
    h = _harness(account_type)
    _working(h, OrderType.LIMIT, OrderSide.SELL)
    reduce_only = _working(h, OrderType.STOP_MARKET, OrderSide.SELL, reduce_only=True)

    assert _sweep(h) == [reduce_only.client_order_id]
