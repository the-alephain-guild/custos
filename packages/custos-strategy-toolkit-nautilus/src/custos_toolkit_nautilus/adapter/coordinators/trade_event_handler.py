"""Trade-event tracking component.

Holds the order/position event bodies: entry-order tracker cleanup, post-fill
protection dispatch, and position-close cleanup. Injects a strategy reference and
reaches ``cache`` / ``log`` / ``config``
/ ``_mode`` plus ``_order_signal_map``
/ ``_risk_controller`` / ``_capital_allocator`` / ``_get_risk_equity`` /
``_sltp_coordinator`` / ``_get_context_from_instrument`` /
``on_trade_closed`` through it.

The nautilus event callbacks (``on_order_filled`` etc.) stay on the Strategy
class -- the engine dispatches them by name and subclasses chain ``super()`` --
their thin shells delegate the body to this component's ``handle_*`` methods.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, cast

from custos_toolkit.risk import RiskController
from custos_toolkit_nautilus.adapter.coordinators.entry_reservation import release_unfilled_entry
from nautilus_trader.common import LogColor
from nautilus_trader.model import (
    OrderCanceled,
    OrderFilled,
    PositionClosed,
)

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class TradeEventHandler:
    """Order / position event tracking.

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def handle_order_filled(self, event: OrderFilled) -> None:
        """Track filled orders - route to correct pair context."""
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

        # A scaled take-profit level is spent by its fill, not by the price that
        # triggered it. This is settled before the entry-ownership gate below, which
        # returns early for any fill that is not the tracked entry.
        if ctx.tick_monitor is not None:
            ctx.tick_monitor.confirm_level_order(
                event.client_order_id, Decimal(str(event.last_qty))
            )

        # Binance's user stream is account-wide. When two nodes share an account,
        # a sibling order fill can arrive while this strategy still has its own entry
        # signal pending. Only the exact tracked entry may consume that signal and arm
        # its protection; otherwise the sibling fill steals the pending signal and this
        # instance later ignores its own fill.
        tracked_entry_id = ctx.order_tracker.entry_order_id
        pending_signal = ctx.position_tracker.pending_signal
        is_tracked_entry_fill = (
            tracked_entry_id is not None and tracked_entry_id == event.client_order_id
        )
        if pending_signal is not None and not is_tracked_entry_fill:
            return

        order = s.cache.order(event.client_order_id)
        order_is_terminal = bool(order is not None and order.is_closed)

        s.log.info(
            f"[{ctx.pair}] Order FILLED: {event.order_side} {event.last_qty} @ {event.last_px}",
            color=LogColor.CYAN,
        )

        # The order is done, so its signal link can go. This used to ride along inside
        # a telemetry block; the telemetry is gone and the cleanup is not optional --
        # the map is per-order and nothing else drops entries on a fill.
        if order_is_terminal:
            s._order_signal_map.pop(str(event.client_order_id), None)

        protection_quantity = Decimal("0")
        initialize_position = False
        if is_tracked_entry_fill:
            protection_quantity, initialize_position = ctx.order_tracker.record_entry_fill(
                Decimal(str(event.last_qty))
            )

        pos_config = s.config.position
        if pos_config.capital_mode == "compound":
            equity = s._get_risk_equity()
            cast(RiskController, s._risk_controller).update_peak_equity(equity)

        if pending_signal is not None and protection_quantity > 0:
            signal = pending_signal
            entry_atr = ctx.position_tracker.pending_entry_atr

            positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
            position = positions[0] if positions else None

            # Only tag reversal entries: SL/TP submitted here for a normal entry
            # belong to the current position and must be cancelled on close by
            # on_position_closed. Per-mode post-fill protection is dispatched by
            # SLTPMode.on_entry_filled.
            if initialize_position:
                ctx.sl_tp_submitted_for_reversal = ctx.pending_entry_is_reversal
                ctx.pending_entry_is_reversal = False
            s._mode.on_entry_filled(
                s,
                ctx,
                signal,
                position,
                event.last_px,
                entry_atr,
                protection_quantity=protection_quantity,
                initialize_position=initialize_position,
            )

        # An OrderFilled event is one trade lot. Keep the entry and pending signal
        # correlated across partial fills; release them only when the cached source
        # order confirms a terminal state. A later OrderCanceled event owns the
        # partial-fill-then-cancel terminal path.
        if is_tracked_entry_fill and order_is_terminal:
            ctx.order_tracker.clear_entry_order()
            ctx.position_tracker.clear_pending_signal()
            ctx.pending_entry_is_reversal = False

    def handle_position_closed(self, event: PositionClosed) -> None:
        """Track closed positions - route to correct pair context."""
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

        realized_pnl = (
            event.realized_pnl.as_decimal()
            if event.realized_pnl is not None
            else Decimal("0")
        )
        pnl_color = LogColor.GREEN if realized_pnl > 0 else LogColor.RED
        s.log.info(f"[{ctx.pair}] Position CLOSED: realized_pnl={realized_pnl}", color=pnl_color)

        cast(RiskController, s._risk_controller).record_trade(realized_pnl)

        if s._capital_allocator:
            s._capital_allocator.release(ctx.pair, ctx.allocated_capital)
            ctx.allocated_capital = Decimal("0")

        # Cancel all SL/TP orders on position close to prevent orphaned orders.
        # Skip cancellation if SL/TP were just submitted for an incoming reversal position.
        pending_entry_signal = ctx.position_tracker.pending_signal
        pending_entry_atr = ctx.position_tracker.pending_entry_atr
        # Bind the signal itself rather than a bare flag: the invariant "a reversal
        # entry continues only when a pending signal exists" is then stated once and
        # carries through to the re-arm below.
        # What outlives this close? Two things can. The entry order may still be at
        # the venue with more to fill -- reversal is only one way that happens, a
        # half-filled entry whose half got stopped out is another, and both need the
        # ownership kept or the later fill arrives unowned and unprotected. And on a
        # netting reversal the replacement position is already in the cache, its
        # protection seeded by the fill that opened it.
        entry_order_id = ctx.order_tracker.entry_order_id
        entry_order = s.cache.order(entry_order_id) if entry_order_id is not None else None
        entry_may_still_fill = entry_order is not None and entry_order.is_open
        reversal_entry_signal = (
            pending_entry_signal
            if entry_order_id is not None
            and (
                ctx.sl_tp_submitted_for_reversal
                or getattr(ctx, "pending_entry_is_reversal", False)
                or entry_may_still_fill
            )
            else None
        )
        reversal_entry_continues = reversal_entry_signal is not None
        surviving_positions = s.cache.positions_open(instrument_id=ctx.instrument_id)

        if ctx.sl_tp_submitted_for_reversal:
            s.log.info(
                f"[{ctx.pair}] Skipping SL/TP cancellation — reversal SL/TP already submitted",
                color=LogColor.YELLOW,
            )
            ctx.sl_tp_submitted_for_reversal = False
            # This branch skips cancel_sl_tp_orders (which normally clears the tracker),
            # so clear just the close gate here -- a stale in-flight deadline must not
            # block the new reversed position from closing. The reversal SL/TP stay intact.
            ctx.order_tracker.clear_closing()
        elif reversal_entry_continues:
            # A partial fill can close the old side exactly before later lots open
            # target-direction exposure. Cancel the old position's protection, but
            # preserve the source entry correlation and pending signal for those lots.
            cancelled = s._sltp_coordinator.cancel_sl_tp_orders(ctx, preserve_entry=True)
            ctx.pending_entry_is_reversal = False
            if cancelled > 0:
                s.log.info(
                    f"[{ctx.pair}] Cancelled {cancelled} old-position SL/TP orders "
                    "while reversal entry remains in flight",
                    color=LogColor.YELLOW,
                )
        else:
            ctx.pending_entry_is_reversal = False
            cancelled = s._sltp_coordinator.cancel_sl_tp_orders(ctx)
            if cancelled > 0:
                s.log.info(
                    f"[{ctx.pair}] Cancelled {cancelled} SL/TP orders on position close",
                    color=LogColor.YELLOW,
                )

        # Only reset the position view when nothing is left holding it, and nothing
        # is about to. A netting reversal has already opened the replacement. An
        # entry still working at the venue has not opened its position yet, but the
        # entry price recorded here is what its protection will be sized against --
        # resetting it to zero makes the later fill unpriceable.
        if not surviving_positions and not entry_may_still_fill:
            ctx.position_tracker.reset()
        if reversal_entry_signal is not None:
            ctx.position_tracker.set_pending_signal(reversal_entry_signal, pending_entry_atr)
        # Position confirmed flat -> reset the consecutive close-reject halt count. Both the
        # normal and reversal close paths converge here, so this is the single point that
        # owns the reset (clear()/clear_closing() must not, they also run on the reject path).
        ctx.order_tracker.reset_close_rejects()
        ctx.break_even_applied = False
        # Note: order_tracker.clear() is called inside cancel_sl_tp_orders
        # The monitor describes the position being held, not the one that just
        # ended. On a reversal the fill that opened the replacement has already
        # seeded it, and there is no generic PositionOpened callback that would
        # build it again if it were cleared here.
        if ctx.tick_monitor and not surviving_positions:
            ctx.tick_monitor.reset()

        # Business hook: trade-result feedback (realized PnL) for martingale / adaptive
        # strategies to retune, without overriding the heavy on_position_closed callback.
        # Run after close cleanup -- if the hook raises, the on_position_closed top-level
        # guard catches it, never disturbing SL/TP cancellation or tracker reset.
        s.on_trade_closed(ctx, realized_pnl)

    def handle_order_canceled(self, event: OrderCanceled) -> None:
        """Handle order canceled event - clean up entry order tracker."""
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

        # A canceled order will never fill, so its signal link goes here.
        s._order_signal_map.pop(str(event.client_order_id), None)

        # Likewise it took no quantity, so any scaled level it carried is still owed.
        if ctx.tick_monitor is not None:
            ctx.tick_monitor.release_level_order(event.client_order_id)

        # Clean up entry order tracker if this was our tracked entry order
        if (
            ctx.order_tracker.entry_order_id is not None
            and ctx.order_tracker.entry_order_id == event.client_order_id
        ):
            release_unfilled_entry(s, ctx)
            ctx.order_tracker.clear_entry_order()
            # Entry cancel confirmation is terminal: a leftover pending_signal /
            # pending_entry_is_reversal would be consumed by any later fill on the
            # same instrument and submit stale SL/TP. Note: the cancel_rejected path
            # does NOT clear (cancel failure means the order may still fill).
            ctx.position_tracker.clear_pending_signal()
            ctx.pending_entry_is_reversal = False
            s.log.info(
                f"[{ctx.pair}] Entry order canceled: {event.client_order_id}",
                color=LogColor.YELLOW,
            )
