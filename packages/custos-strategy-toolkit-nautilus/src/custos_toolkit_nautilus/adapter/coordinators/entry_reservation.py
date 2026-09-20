"""Release of capital an entry reserved but never spent.

An entry reserves capital and records a position entry before the order reaches
the venue. Both are settled on ``PositionClosed`` -- but an order that terminates
without filling never opens a position, so that event never arrives and the
reservation would stay held for the life of the process.

Cancellation and rejection are separate callbacks on separate coordinators, and
both end an entry the same way, so the settlement lives here once.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.common import LogColor

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


def release_unfilled_entry(strategy: NautilusTradingStrategy, ctx: PairContext) -> None:
    """Settle a terminated entry: return what it never spent, drop what never opened.

    Must be called while the tracker still holds the entry, i.e. before
    ``clear_entry_order``, since the fill and reservation figures live there.
    """
    unfilled_capital = ctx.order_tracker.entry_unfilled_capital
    had_fills = ctx.order_tracker.entry_has_fills

    if unfilled_capital > 0:
        returned = min(unfilled_capital, ctx.allocated_capital)
        ctx.allocated_capital -= returned
        if strategy._capital_allocator:
            strategy._capital_allocator.release(ctx.pair, returned)
        strategy.log.info(
            f"[{ctx.pair}] Returned {returned:.4f} reserved for the part of the entry "
            "that never filled",
            color=LogColor.YELLOW,
        )

    # An attempt that bought nothing is not an entry. Leave a partially filled one
    # alone: that exposure is real, and the venue will close it like any other.
    if had_fills:
        return
    positions = strategy.cache.positions_open(instrument_id=ctx.instrument_id)
    if positions:
        return
    if ctx.position_tracker.entry_count > 0:
        ctx.position_tracker.reset()
        ctx.allocated_capital = Decimal("0")
