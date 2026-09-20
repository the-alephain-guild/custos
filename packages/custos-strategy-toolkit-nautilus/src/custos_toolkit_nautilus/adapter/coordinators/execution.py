"""Tick-driven execution component.

Holds the tick-monitor execution path: routing trade/quote ticks to the correct
pair context, and executing the exit actions the tick monitor produces (partial
reduce-only exits and full position closes with reduce-only flood protection).
Injects a strategy reference and reaches ``cache`` / ``log`` / ``clock`` /
``order_factory`` / ``submit_order`` / ``close_position`` plus ``_mode`` /
``_get_context_from_instrument`` / ``_sltp_coordinator`` through it.

The nautilus tick callbacks (``on_core_trade_tick`` / ``on_core_quote_tick``)
stay on the Strategy class -- the core dispatches them by name -- their thin
shells delegate the body to this component's ``handle_*`` methods.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, cast

from nautilus_trader.common import LogColor
from nautilus_trader.model import OrderSide, QuoteTick, TimeInForce, TradeTick

from custos_toolkit_nautilus.adapter.orders import _CLOSE_INFLIGHT_TIMEOUT_NS

if TYPE_CHECKING:

    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.tick_monitor import ExitAction
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    from ..runtime_types import Position


class ExecutionCoordinator:
    """Tick-monitor execution path.

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def handle_trade_tick(self, tick: TradeTick) -> None:
        """Handle trade tick - route to correct pair context."""
        s = self._strategy
        ctx = s._get_context_from_instrument(tick.instrument_id)
        if ctx is None:
            return

        # native_trailing/exchange: SL/TP is venue-managed, no tick-path exit
        if not s._mode.uses_tick_monitor:
            return

        if not ctx.tick_monitor or not ctx.tick_monitor.is_active:
            return

        self._drain_exit_actions(ctx, Decimal(str(tick.price)))

    def handle_quote_tick(self, tick: QuoteTick) -> None:
        """Handle quote tick - route to correct pair context."""
        s = self._strategy
        ctx = s._get_context_from_instrument(tick.instrument_id)
        if ctx is None:
            return

        # native_trailing/exchange: SL/TP is venue-managed, no tick-path exit
        if not s._mode.uses_tick_monitor:
            return

        if not ctx.tick_monitor or not ctx.tick_monitor.is_active:
            return

        mid_price = (Decimal(str(tick.bid_price)) + Decimal(str(tick.ask_price))) / 2
        self._drain_exit_actions(ctx, mid_price)

    def _drain_exit_actions(self, ctx: PairContext, price: Decimal) -> None:
        """Execute every exit the monitor has at this price, not just the first.

        One tick can clear several scaled levels at once. A price that spikes
        through two targets and falls straight back is exactly what a scaled exit
        is for, and a level left behind there is only revisited if price stays
        above its target -- which, after that kind of move, it does not.

        Only partial exits loop. A full close ends the position, and the fixed and
        trailing checks carry no per-level state, so asking them again at the same
        price would never stop.

        Termination does not rest on the level state advancing. A level whose lot
        could not be sent is handed back to ARMED on purpose -- that is how a
        refused dispatch stays retryable -- and it would be offered again on the
        very next turn of this loop. Each level therefore gets one turn per tick,
        counted here; the retry belongs to the next tick, not to this one.
        """
        s = self._strategy
        monitor = ctx.tick_monitor
        if monitor is None:
            return
        offered: set[int] = set()
        while True:
            positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
            if not positions:
                return
            action = monitor.check(price)
            if action is None:
                return
            if action.level is not None:
                if action.level in offered:
                    # Asking cost something: check() marks the level pending. This
                    # turn is not going to execute it, so hand it back before
                    # leaving, or it sits pending and no later tick offers it again.
                    monitor.release_level(action.level)
                    return
                offered.add(action.level)
            self._execute_exit_action_for_pair(ctx, action, positions[0])
            if not action.partial_pct:
                return

    def _execute_exit_action_for_pair(
        self, ctx: PairContext, action: ExitAction, position: Position
    ) -> None:
        """Execute exit action from tick monitor for a specific pair."""
        self._strategy.log.info(
            f"[{ctx.pair}] TICK EXIT: {action.exit_type} | {action.reason}",
            color=LogColor.MAGENTA,
        )

        if action.partial_pct:
            self._execute_partial_exit_for_pair(
                ctx, position, action.partial_pct, action.reason, action.level
            )
        else:
            self._execute_trailing_stop_exit_for_pair(ctx, action.price, action.reason)

    def _execute_partial_exit_for_pair(
        self,
        ctx: PairContext,
        position: Position,
        exit_pct: Decimal,
        reason: str,
        level: int | None = None,
    ) -> None:
        """Execute partial position exit for a specific pair.

        A scaled level arrives here already marked pending. Every path that ends
        without a live order at the venue must hand the level back, or its quantity
        is never taken and no further tick will retry it.
        """
        s = self._strategy

        def abandon_level() -> None:
            if level is not None and ctx.tick_monitor is not None:
                ctx.tick_monitor.release_level(level)

        remaining = Decimal(str(position.quantity))
        if level is not None and ctx.tick_monitor is not None:
            raw_exit_qty = ctx.tick_monitor.planned_exit_quantity(level, remaining)
        else:
            raw_exit_qty = remaining * exit_pct
        # Never ask to reduce more than is held; the venue would refuse the lot.
        raw_exit_qty = min(raw_exit_qty, remaining)

        instrument = s.cache.instrument(ctx.instrument_id)
        if instrument is None:
            s.log.error(f"Instrument not found: {ctx.instrument_id}")
            abandon_level()
            return
        exit_qty = instrument.make_qty(raw_exit_qty)

        if exit_qty <= 0:
            abandon_level()
            return

        order = s.order_factory.market(
            instrument_id=ctx.instrument_id,
            order_side=OrderSide.SELL if position.is_long else OrderSide.BUY,
            quantity=exit_qty,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
        )

        dispatched = cast(bool | None, s.submit_order(order))
        if dispatched is False:
            s.log.warning(
                f"[{ctx.pair}] PARTIAL EXIT was refused locally before dispatch; "
                "the level remains available",
            )
            abandon_level()
            return

        # Own the order. A partial take-profit that no tracker claims is read as a
        # failed full close when the venue rejects it, and that path cancels every
        # order for the instrument -- including a stop that is doing its job.
        ctx.order_tracker.add_tp_order(order.client_order_id)
        if level is not None and ctx.tick_monitor is not None:
            ctx.tick_monitor.bind_level_order(level, order.client_order_id)
            ctx.tick_monitor.record_dispatch(level, Decimal(str(exit_qty)))

        s.log.info(
            f"[{ctx.pair}] PARTIAL EXIT: {reason} | qty={exit_qty} ({exit_pct * 100:.0f}%)",
            color=LogColor.MAGENTA,
        )

    def _execute_trailing_stop_exit_for_pair(
        self, ctx: PairContext, current_price: Decimal, reason: str
    ) -> None:
        """Execute full position exit (tick SL/TP/trailing) for a specific pair.

        Roots out reduce-only close flooding (-2022). Three layers of protection:
        1. In-flight / cooldown gate -- while a close is in flight or within the
           post-rejection backoff, do not re-send every tick.
        2. Cancel serialization -- if the venue still has resting reduce_only
           orders (hybrid safety SL / exchange SL/TP), cancel them first and
           **skip the close this tick** (close on the next tick once the cancel
           is confirmed), so a new close order and the resting orders don't add
           up past the position size and get rejected.
        3. With no resting reduce_only, submit a **single** full-close order and
           arm the in-flight gate.
        """
        s = self._strategy
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        if not positions:
            return

        # Protection 1: in-flight / cooldown gate
        now_ns = s.clock.timestamp_ns()
        if not ctx.order_tracker.can_submit_close(now_ns):
            return

        position = positions[0]

        # Protection 2: decide whether resting reduce_only orders remain using the
        # venue's actual open-order state (not the local tracker). If so, cancel
        # first, skip the close this tick, and close on the next tick once the
        # cancel takes effect.
        resting_reduce_only = [
            o for o in s.cache.orders_open(instrument_id=ctx.instrument_id) if o.is_reduce_only
        ]
        if resting_reduce_only:
            s._sltp_coordinator.cancel_sl_tp_orders(ctx)
            s.log.info(
                f"[{ctx.pair}] TICK EXIT pending: cancelling "
                f"{len(resting_reduce_only)} resting reduce-only order(s) before close",
                color=LogColor.MAGENTA,
            )
            return

        # Protection 3: no contention -> use native close_position() to submit a
        # single full-close order and arm the in-flight gate. close_position
        # auto-reverses side, closes the full position.quantity, and is idempotent
        # for an already-closed position (is_closed_c). Pass IOC explicitly
        # (default GTC) + reduce_only=True to keep the current semantics.

        s.close_position(
            position,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
        )
        ctx.order_tracker.mark_closing(now_ns, _CLOSE_INFLIGHT_TIMEOUT_NS)
        s.log.info(
            f"[{ctx.pair}] TICK EXIT: {reason} at {current_price}",
            color=LogColor.MAGENTA,
        )
