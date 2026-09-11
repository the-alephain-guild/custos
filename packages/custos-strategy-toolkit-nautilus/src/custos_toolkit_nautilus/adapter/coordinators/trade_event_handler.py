"""Trade-event tracking component.

Holds the order/position event bodies: telemetry publishing, entry-order tracker
cleanup, post-fill protection dispatch, and position-close cleanup. Injects a
strategy reference and reaches ``cache`` / ``log`` / ``config``
/ ``_mode`` plus ``_event_publisher`` / ``_order_signal_map``
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
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderFilled,
    PositionClosed,
    PositionOpened,
)

from custos_toolkit_nautilus.adapter.event_publisher import extract_signal_id_from_tags

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class TradeEventHandler:
    """Order / position event tracking.

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def handle_order_accepted(self, event: OrderAccepted) -> None:
        """Publish an order event (status=accepted) as soon as the venue accepts
        the order, so WORKING SL/TP orders reach the persistence path (Path B
        EventPersister has no terminal-state gate). Otherwise resting stop orders
        would never be stored and signal SL/TP derivation would have no source.

        signal_id prefers the order tags (persistent), falling back to the in-memory
        map (use get, not pop -- the terminal-state hook still needs it). The base
        on_order_accepted is a nautilus no-op, so no super() call is needed.
        """
        s = self._strategy
        if not s._event_publisher.enabled:
            return
        _order = s.cache.order(event.client_order_id)
        if _order is None:
            return
        _oid = str(event.client_order_id)
        _sig_id = extract_signal_id_from_tags(_order.tags) or s._order_signal_map.get(_oid)
        s._event_publisher.publish_order_event(
            event=event,
            order=_order,
            signal_id=_sig_id,
            side=_order.side.name,
            order_type=_order.order_type.name,
            quantity=str(_order.quantity),
            status="accepted",
            fill_price=None,
            include_price=True,
        )

    def handle_order_filled(self, event: OrderFilled) -> None:
        """Track filled orders - route to correct pair context."""
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

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

        # Telemetry block (order filled) is locally isolated -- it runs before SL/TP
        # submission, so an exception escaping here would be swallowed by the
        # on_order_filled top-level guard and skip the money path too (new position
        # left unprotected). Telemetry failure only logs; it never blocks what follows.
        if s._event_publisher.enabled:
            try:
                _oid = str(event.client_order_id)
                _sig_id = (
                    s._order_signal_map.pop(_oid, None)
                    if order_is_terminal
                    else s._order_signal_map.get(_oid)
                ) or extract_signal_id_from_tags(order.tags if order else None)
                s._event_publisher.publish_order_event(
                    event=event,
                    order=order,
                    signal_id=_sig_id,
                    side=event.order_side.name,
                    order_type=order.order_type.name if order else "UNKNOWN",
                    quantity=str(event.last_qty),
                    fill_price=str(event.last_px),
                    status="filled",
                    include_price=True,
                )
            except Exception as exc:
                s.log.warning(
                    f"[{ctx.pair}] Order-fill telemetry failed (money path continues): {exc}"
                )

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

    def handle_position_opened(self, event: PositionOpened) -> None:
        """Handle position opened event -- publish position event."""
        s = self._strategy
        if s._event_publisher.enabled:
            _order = (
                s.cache.order(event.opening_order_id)
                if hasattr(event, "opening_order_id")
                else None
            )
            _sig_id = extract_signal_id_from_tags(_order.tags if _order else None)
            s._event_publisher.publish_position_event(
                event=event,
                signal_id=_sig_id,
                status="opened",
            )

    def handle_position_closed(self, event: PositionClosed) -> None:
        """Track closed positions - route to correct pair context."""
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

        realized_pnl = (
            event.realized_pnl.as_decimal() if hasattr(event, "realized_pnl") else Decimal("0")
        )
        pnl_color = LogColor.GREEN if realized_pnl > 0 else LogColor.RED
        s.log.info(f"[{ctx.pair}] Position CLOSED: realized_pnl={realized_pnl}", color=pnl_color)

        # Telemetry block (position closed) is locally isolated -- it runs before SL/TP
        # cancellation and tracker reset, so an exception escaping here would be swallowed
        # by the top-level guard and skip the close cleanup too (orphaned SL left behind).
        # Telemetry failure only logs.
        if s._event_publisher.enabled:
            try:
                _sig_id = None
                if hasattr(event, "opening_order_id"):
                    _order = s.cache.order(event.opening_order_id)
                    _sig_id = extract_signal_id_from_tags(_order.tags if _order else None)
                s._event_publisher.publish_position_event(
                    event=event,
                    signal_id=_sig_id,
                    status="closed",
                    realized_pnl=str(realized_pnl),
                )
            except Exception as exc:
                s.log.warning(
                    f"[{ctx.pair}] Position-close telemetry failed (cleanup continues): {exc}"
                )

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
        reversal_entry_signal = (
            pending_entry_signal
            if ctx.order_tracker.entry_order_id is not None
            and (
                ctx.sl_tp_submitted_for_reversal or getattr(ctx, "pending_entry_is_reversal", False)
            )
            else None
        )
        reversal_entry_continues = reversal_entry_signal is not None

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

        ctx.position_tracker.reset()
        if reversal_entry_signal is not None:
            ctx.position_tracker.set_pending_signal(reversal_entry_signal, pending_entry_atr)
        # Position confirmed flat -> reset the consecutive close-reject halt count. Both the
        # normal and reversal close paths converge here, so this is the single point that
        # owns the reset (clear()/clear_closing() must not, they also run on the reject path).
        ctx.order_tracker.reset_close_rejects()
        ctx.break_even_applied = False
        # Note: order_tracker.clear() is called inside cancel_sl_tp_orders
        if ctx.tick_monitor:
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

        # Event emission: order canceled
        if s._event_publisher.enabled:
            _order = s.cache.order(event.client_order_id)
            _oid = str(event.client_order_id)
            _sig_id = s._order_signal_map.pop(_oid, None) or extract_signal_id_from_tags(
                _order.tags if _order else None
            )
            s._event_publisher.publish_order_event(
                event=event,
                order=_order,
                signal_id=_sig_id,
                side=event.order_side.name if hasattr(event, "order_side") else "UNKNOWN",
                order_type=_order.order_type.name if _order else "UNKNOWN",
                quantity=str(_order.quantity) if _order else "0",
                status="canceled",
                fill_price=None,
            )

        # Clean up entry order tracker if this was our tracked entry order
        if (
            ctx.order_tracker.entry_order_id is not None
            and ctx.order_tracker.entry_order_id == event.client_order_id
        ):
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
