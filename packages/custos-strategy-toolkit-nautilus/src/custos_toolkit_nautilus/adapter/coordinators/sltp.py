"""SL/TP order coordination component.

Holds the stop-loss / take-profit submission, cancellation, and break-even
relocation bodies. Injects a strategy reference and reaches ``cache`` / ``log`` /
``config`` / ``_mode`` / ``submit_order`` / ``cancel_order`` / ``_order_calculator``
/ ``_order_signal_map`` through it.

Callers reach these via ``strategy._sltp_coordinator``:
SLTPMode.on_entry_filled (post-fill protection), OrderReconciler (restart
re-protection), TradeEventHandler (cancel on close), and the bar pipeline
(``SignalExecutionCoordinator.manage_positions_for_pair`` break-even).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, cast

from custos_toolkit.signals.types import Signal
from nautilus_trader.common import LogColor
from nautilus_trader.model import OrderSide, Quantity

from custos_toolkit_nautilus.adapter.signal_correlation import make_signal_tag
from custos_toolkit_nautilus.adapter.orders import StopLossSubmitter, TakeProfitSubmitter
from custos_toolkit_nautilus.adapter.runtime_types import Order, Position
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class SLTPCoordinator:
    """SL/TP submission, cancellation, and break-even relocation.

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def _link_order_to_signal(self, order: Order, ctx: PairContext) -> None:
        """Link an SL/TP order to the open position's entry signal.

        Registered in ``_order_signal_map``, which complements the tag the order also
        carries: the tag survives a restart and the map survives a fill (a market
        order's tags are gone from the cache once it fills).
        """
        s = self._strategy
        if order is not None and ctx.active_signal_id:
            s._order_signal_map[str(order.client_order_id)] = ctx.active_signal_id

    def _signal_tags(self, ctx: PairContext) -> list[str] | None:
        """Order tag for the open position's entry signal.

        Tags SL/TP orders with ``signal_id:<id>`` so a protective order says which
        entry it is protecting -- nothing else in the execution stream does. Returns
        None when there is no entry signal.
        """
        if ctx.active_signal_id:
            return [make_signal_tag(ctx.active_signal_id)]
        return None

    def move_stop_to_break_even(
        self, ctx: PairContext, position: Position, entry_price: Decimal
    ) -> None:
        """Move stop loss to break-even (entry price) for a specific pair."""
        s = self._strategy
        instrument = s.cache.instrument(ctx.instrument_id)
        if instrument is None:
            return

        # Cancel existing SL order(s) depending on mode
        if s._mode is SLTPMode.EXCHANGE:
            for sl_order_id in ctx.order_tracker.sl_order_ids:
                order = s.cache.order(sl_order_id)
                if order and order.is_open:
                    s.cancel_order(order)
                ctx.order_tracker.remove_order(sl_order_id)
        elif s._mode is SLTPMode.HYBRID:
            for exchange_sl_id in ctx.order_tracker.exchange_sl_order_ids:
                order = s.cache.order(exchange_sl_id)
                if order and order.is_open:
                    s.cancel_order(order)
                ctx.order_tracker.remove_order(exchange_sl_id)

        # Build the break-even SL at the entry price via the StopLossSubmitter
        # shared primitive: unified side-aware tick alignment + reduce-only. The
        # entry comes from the fill avg price (already on the tick grid), so
        # alignment does not change the trigger; the primitive's side-aware
        # rounding only differs from nearest if entry is ever sub-tick.
        side = OrderSide.SELL if position.is_long else OrderSide.BUY
        new_sl = cast(StopLossSubmitter, ctx.sl_submitter).create_order_from_price(
            instrument_id=ctx.instrument_id,
            side=side,
            quantity=position.quantity,
            stop_price=entry_price,
            tags=self._signal_tags(ctx),  # SL order carries signal_id tag
        )
        if new_sl is None:
            return

        if s._mode is SLTPMode.EXCHANGE:
            ctx.order_tracker.set_sl_order(new_sl.client_order_id, position.quantity)
        elif s._mode is SLTPMode.HYBRID:
            ctx.order_tracker.set_exchange_sl_order(new_sl.client_order_id, position.quantity)

        s.submit_order(new_sl)
        self._link_order_to_signal(new_sl, ctx)
        ctx.break_even_applied = True
        s.log.info(
            f"[{ctx.pair}] Break-even: SL moved to entry price {new_sl.trigger_price}",
            color=LogColor.GREEN,
        )

    def cancel_sl_tp_orders(self, ctx: PairContext, *, preserve_entry: bool = False) -> int:
        """Cancel all tracked SL/TP orders for a specific pair.

        ``preserve_entry`` is used only when a partially filled reversal has closed
        the old position but its source entry order can still open the new side.
        """
        s = self._strategy
        cancelled = 0

        # Cancel tick-based SL order
        for sl_order_id in ctx.order_tracker.sl_order_ids:
            order = s.cache.order(sl_order_id)
            if order and order.is_open:
                s.cancel_order(order)
                s.log.info(f"[{ctx.pair}] Cancelled SL order: {sl_order_id}")
                cancelled += 1

        # Cancel exchange safety SL order (hybrid mode)
        for exchange_sl_id in ctx.order_tracker.exchange_sl_order_ids:
            order = s.cache.order(exchange_sl_id)
            if order and order.is_open:
                s.cancel_order(order)
                s.log.info(f"[{ctx.pair}] Cancelled exchange SL order: {exchange_sl_id}")
                cancelled += 1

        # Cancel all TP orders
        for tp_id in ctx.order_tracker.tp_order_ids:
            order = s.cache.order(tp_id)
            if order and order.is_open:
                s.cancel_order(order)
                s.log.info(f"[{ctx.pair}] Cancelled TP order: {tp_id}")
                cancelled += 1

        if preserve_entry:
            ctx.order_tracker.clear_protection_orders()
        else:
            ctx.order_tracker.clear()
        return cancelled

    def cancel_exchange_safety_sl(self, ctx: PairContext) -> bool:
        """Cancel exchange safety net stop loss for a specific pair.

        Note: this has no current production caller (only a guard test references it)
        -- a candidate for a separate dead-code review; preserved as-is here.
        """
        s = self._strategy
        exchange_sl_order_ids = ctx.order_tracker.exchange_sl_order_ids
        if not exchange_sl_order_ids:
            return False

        cancelled = False
        for exchange_sl_order_id in exchange_sl_order_ids:
            order = s.cache.order(exchange_sl_order_id)
            if order and order.is_open:
                s.cancel_order(order)
                s.log.info(f"[{ctx.pair}] Cancelled safety SL: {exchange_sl_order_id}")
                cancelled = True
            ctx.order_tracker.remove_order(exchange_sl_order_id)
        return cancelled

    def submit_stop_loss(
        self,
        ctx: PairContext,
        signal: Signal,
        *,
        quantity: Quantity | Decimal | None = None,
    ) -> None:
        """Submit stop loss order for a specific pair."""
        s = self._strategy
        entry_price = ctx.position_tracker.first_entry_price
        if entry_price <= 0:
            return

        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position = positions[0] if positions else None
        if position is None:
            return

        order = cast(StopLossSubmitter, ctx.sl_submitter).create_order(
            instrument_id=ctx.instrument_id,
            signal=signal,
            entry_price=entry_price,
            atr=ctx.position_tracker.pending_entry_atr,
            position=position,
            tags=self._signal_tags(ctx),  # SL order carries signal_id tag
            quantity=quantity,
        )
        if order:
            protected_quantity = position.quantity if quantity is None else quantity
            ctx.order_tracker.add_sl_order(order.client_order_id, protected_quantity)
            s.submit_order(order)
            self._link_order_to_signal(order, ctx)
            s.log.info(
                f"[{ctx.pair}] STOP_LOSS: submitted (id={order.client_order_id})",
                color=LogColor.RED,
            )

    def submit_take_profit(
        self,
        ctx: PairContext,
        signal: Signal,
        *,
        quantity: Quantity | Decimal | None = None,
    ) -> None:
        """Submit take profit order(s) for a specific pair."""
        s = self._strategy
        entry_price = ctx.position_tracker.first_entry_price
        if entry_price <= 0:
            return

        tp_config = s.config.risk.trade.take_profit
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position = positions[0] if positions else None
        if position is None:
            return

        entry_atr = ctx.position_tracker.pending_entry_atr

        if tp_config.method == "scaled" and tp_config.scaled:
            orders = cast(TakeProfitSubmitter, ctx.tp_submitter).create_scaled_orders(
                instrument_id=ctx.instrument_id,
                signal=signal,
                entry_price=entry_price,
                position=position,
                scaled_config=tp_config.scaled,
                tags=self._signal_tags(ctx),  # TP order carries signal_id tag
                quantity=quantity,
            )
            for order in orders:
                ctx.order_tracker.add_tp_order(order.client_order_id)
                s.submit_order(order)
                self._link_order_to_signal(order, ctx)
                s.log.info(
                    f"[{ctx.pair}] TAKE_PROFIT: scaled (id={order.client_order_id})",
                    color=LogColor.GREEN,
                )
        else:
            sl_price = s._order_calculator.calculate_stop_loss(
                entry_price, signal.direction, entry_atr
            )

            single_order = cast(TakeProfitSubmitter, ctx.tp_submitter).create_single_order(
                instrument_id=ctx.instrument_id,
                signal=signal,
                entry_price=entry_price,
                atr=entry_atr,
                stop_loss=sl_price,
                position=position,
                tags=self._signal_tags(ctx),  # TP order carries signal_id tag
                quantity=quantity,
            )
            if single_order:
                ctx.order_tracker.add_tp_order(single_order.client_order_id)
                s.submit_order(single_order)
                self._link_order_to_signal(single_order, ctx)
                s.log.info(
                    f"[{ctx.pair}] TAKE_PROFIT: submitted (id={single_order.client_order_id})",
                    color=LogColor.GREEN,
                )

    def submit_safety_stop_loss(
        self,
        ctx: PairContext,
        signal: Signal,
        *,
        quantity: Quantity | Decimal | None = None,
    ) -> None:
        """Submit exchange safety net stop loss for a specific pair (hybrid mode only)."""
        s = self._strategy
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position = positions[0] if positions else None
        if position is None:
            return

        entry_price_raw = position.avg_px_open
        if entry_price_raw is None:
            return
        entry_price = Decimal(str(entry_price_raw))

        max_loss_pct = Decimal(str(s.config.risk.trade.max_loss_pct))

        if position.is_long:
            sl_price = entry_price * (1 - max_loss_pct)
        else:
            sl_price = entry_price * (1 + max_loss_pct)

        # Build via the StopLossSubmitter shared primitive (side-aware tick alignment +
        # reduce-only), eliminating hand-rolled order_factory.stop_market inconsistency.
        order = cast(StopLossSubmitter, ctx.sl_submitter).create_order_from_price(
            instrument_id=ctx.instrument_id,
            side=OrderSide.SELL if position.is_long else OrderSide.BUY,
            quantity=position.quantity if quantity is None else quantity,
            stop_price=sl_price,
            tags=self._signal_tags(ctx),  # safety SL order carries signal_id tag
        )
        if order is None:
            return

        protected_quantity = position.quantity if quantity is None else quantity
        ctx.order_tracker.add_exchange_sl_order(order.client_order_id, protected_quantity)
        s.submit_order(order)
        self._link_order_to_signal(order, ctx)
        s.log.info(
            f"[{ctx.pair}] SAFETY SL: {order.trigger_price} ({max_loss_pct * 100:.1f}%) "
            f"(id={order.client_order_id})",
            color=LogColor.RED,
        )

    def submit_native_trailing(
        self,
        ctx: PairContext,
        signal: Signal,
        *,
        quantity: Quantity | Decimal | None = None,
    ) -> Order | None:
        """Submit an exchange-managed trailing stop for native_trailing mode.

        The TrailingStopMarketOrder is itself the venue-managed protective stop:
        no separate tick SL and no emulated order. It is tracked as the exchange
        SL so the sweep / recovery paths treat it as protective.

        Returns the submitted order, or None when no position exists or the
        submitter rejects the config (fail-fast — out-of-range trailing_pct).
        """
        s = self._strategy
        # Protection-path failures must be logged loudly (never silent); only the
        # normal "no position" case stays silent.
        if ctx.native_trailing_submitter is None:
            s.log.error(
                f"[{ctx.pair}] NATIVE_TRAILING: submitter not initialized — cannot protect "
                f"position (check sl_tp_mode wiring)",
                color=LogColor.RED,
            )
            return None

        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position = positions[0] if positions else None
        if position is None:
            return None  # no position — nothing to protect (normal, silent)

        entry_price_raw = position.avg_px_open
        if entry_price_raw is None:
            s.log.error(
                f"[{ctx.pair}] NATIVE_TRAILING: position has no avg_px_open — cannot derive "
                f"activation price; position left UNPROTECTED",
                color=LogColor.RED,
            )
            return None
        entry_price = Decimal(str(entry_price_raw))

        trailing_cfg = s.config.risk.trade.stop_loss.trailing

        order = ctx.native_trailing_submitter.create_order(
            instrument_id=ctx.instrument_id,
            signal=signal,
            entry_price=entry_price,
            position=position,
            trailing_cfg=trailing_cfg,
            tags=self._signal_tags(ctx),  # signal_id tag
            quantity=quantity,
        )
        if order:
            # Track as the exchange-managed protective stop (sweep/recovery aware)
            protected_quantity = position.quantity if quantity is None else quantity
            ctx.order_tracker.add_exchange_sl_order(order.client_order_id, protected_quantity)
            s.submit_order(order)
            self._link_order_to_signal(order, ctx)
            s.log.info(
                f"[{ctx.pair}] NATIVE_TRAILING: submitted exchange-managed trailing stop "
                f"(id={order.client_order_id})",
                color=LogColor.RED,
            )
        else:
            # fail-fast (out-of-range trailing_pct) left the OPEN position with NO
            # protective trailing stop -- this must be logged loudly, never silent;
            # the submitter already logged the specific rejection reason at error level.
            s.log.error(
                f"[{ctx.pair}] NATIVE_TRAILING: submitter rejected config — open position "
                f"has NO protective trailing stop. Verify trailing_pct in [0.001, 0.10].",
                color=LogColor.RED,
            )
        return order
