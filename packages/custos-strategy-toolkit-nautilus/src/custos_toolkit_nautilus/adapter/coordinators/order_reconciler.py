"""Order recovery / reconciliation / orphan cleanup / reject breaker component.

Holds the defense-in-depth order-protection cluster: restart position recovery,
exchange SL / native_trailing claim, per-bar orphan reconciliation sweep, and
severity-tiered reject breaker. Injects a strategy reference and reaches
``cache`` / ``clock`` / ``log`` / ``_contexts`` / ``_mode`` / ``cancel_all_orders`` /
``cancel_order`` / ``_event_publisher`` / ``_order_signal_map`` /
``_get_context_from_instrument`` through it, plus SL/TP submission via
``_sltp_coordinator.submit_*``.

The nautilus event callbacks ``on_order_rejected`` / ``on_order_cancel_rejected`` stay
on the Strategy class (the engine dispatches them by name, the callback contract
requires them); their bodies delegate to this component's ``handle_*`` methods.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from custos_toolkit.risk.exchange_errors import (
    classify_rejection_reason,
    is_reduce_only_refusal,
)
from custos_toolkit.signals.types import Signal, SignalDirection
from nautilus_trader.common.enums import LogColor
from nautilus_trader.model.events import OrderCancelRejected, OrderRejected

from custos_toolkit_nautilus.adapter.event_publisher import extract_signal_id_from_tags
from custos_toolkit_nautilus.adapter.orders import STALE_SWEEP_RETRY_COOLDOWN_NS, is_stale_order
from custos_toolkit_nautilus.adapter.runtime_types import Order, Position
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

# Backoff windows (ns) for the close-reject breaker.
# _CLOSE_REJECT_COOLDOWN_NS    — short backoff after a logical reject (-2022).
# _CLOSE_SERVER_ERROR_BACKOFF_NS — long breaker backoff for server errors / rate limits
#          (5xx/-1007/-1003/-1015), so we don't hammer a failing/throttled order
#          endpoint every 2s and burn the quota.
_CLOSE_REJECT_COOLDOWN_NS: int = 2_000_000_000  # 2s
_CLOSE_SERVER_ERROR_BACKOFF_NS: int = 60_000_000_000  # 60s
# Consecutive logical close-rejects (-2022 etc.) after which the strategy is paused for
# manual intervention. A position the venue can never reduce reaches this on the bar-driven
# reject path (~5 rejects ≈ a few minutes) and halts instead of spamming the venue forever.
_CLOSE_REJECT_HALT_THRESHOLD: int = 5
# Rate guard for the per-bar native_trailing protection rebuild
# (prevents a reject->rebuild->reject flood when the venue keeps rejecting).
_NATIVE_TRAILING_REBUILD_COOLDOWN_NS: int = 60_000_000_000  # 60s


class OrderReconciler:
    """Order recovery / reconciliation / orphan cleanup / reject breaker.

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def recover_from_existing_positions(self) -> None:
        """Recover from existing positions on restart.

        For each open position:
        1. Restore position tracker state
        2. Restore tick monitor state (tick/hybrid mode)
        3. Check and recreate exchange SL orders if missing (exchange/hybrid mode)
        """
        s = self._strategy
        for ctx in s._contexts.values():
            pair = ctx.pair
            positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
            if not positions:
                continue

            position = positions[0]
            if position.is_closed:
                continue

            s.log.info(
                f"[{pair}] Recovering from existing position: "
                f"side={position.side}, qty={position.quantity}",
                color=LogColor.YELLOW,
            )

            # 1. Restore position tracker
            ctx.position_tracker.record_entry(
                Decimal(str(position.avg_px_open)), Decimal(str(position.quantity))
            )

            # 2. Restore tick monitor (tick/hybrid mode)
            if s._mode.uses_tick_monitor and ctx.tick_monitor:
                ctx.tick_monitor.init_position(
                    entry_price=Decimal(str(position.avg_px_open)),
                    is_long=position.is_long,
                )
                bars = s.cache.bars(ctx.bar_type)
                if bars:
                    current_price = Decimal(str(bars[-1].close))
                    ctx.tick_monitor.check(current_price)

            # 3. Check and recreate exchange SL orders (exchange/hybrid mode)
            if s._mode.uses_exchange_sl:
                self.ensure_exchange_sl_exists(ctx, position)

            # 4. Re-claim or resubmit exchange-managed trailing stop (native_trailing)
            if s._mode.uses_native_trailing:
                self.ensure_native_trailing_exists(ctx, position)

    def ensure_exchange_sl_exists(self, ctx: PairContext, position: Position) -> None:
        """
        Ensure exchange stop loss order exists for the position.

        Checks if there's an existing SL order on the exchange for this position.
        If not found, creates a new one.

        For 'exchange' mode: Creates standard SL based on config
        For 'hybrid' mode: Creates safety net SL at max_loss_pct

        Args:
            ctx: PairContext for the trading pair
            position: The open position to protect
        """
        s = self._strategy
        # Check for existing SL orders on the exchange
        existing_sl = self.find_existing_sl_order(ctx, position)

        if existing_sl:
            # Found existing SL order - track it
            if s._mode is SLTPMode.HYBRID:
                ctx.order_tracker.set_exchange_sl_order(existing_sl.client_order_id)
            else:
                ctx.order_tracker.set_sl_order(existing_sl.client_order_id)
            s.log.info(
                f"[{ctx.pair}] Found existing SL order: {existing_sl.client_order_id}",
                color=LogColor.GREEN,
            )
            return

        # No SL order found - create one
        s.log.warning(
            f"[{ctx.pair}] No SL order found for existing position - creating new one",
        )

        # Create a synthetic signal based on position direction
        direction = SignalDirection.ENTER_LONG if position.is_long else SignalDirection.ENTER_SHORT
        signal = Signal(direction=direction, price=Decimal(str(position.avg_px_open)))

        if s._mode is SLTPMode.HYBRID:
            s._sltp_coordinator.submit_safety_stop_loss(ctx, signal)
        else:  # exchange mode
            # Set entry price and ATR for SL calculation
            ctx.position_tracker.set_pending_signal(signal, entry_atr=None)
            s._sltp_coordinator.submit_stop_loss(ctx, signal)

    def find_existing_sl_order(self, ctx: PairContext, position: Position) -> Order | None:
        """
        Find existing stop loss order for the position on the exchange.

        Looks for:
        - Stop market orders (reduce_only=True)
        - Matching instrument
        - Opposite side to position (SELL for long, BUY for short)

        Args:
            ctx: PairContext for the trading pair
            position: The open position

        Returns:
            Order object if found, None otherwise
        """
        from nautilus_trader.model.enums import OrderSide, OrderType

        # Get all open orders for this instrument
        open_orders = self._strategy.cache.orders_open(instrument_id=ctx.instrument_id)

        expected_side = OrderSide.SELL if position.is_long else OrderSide.BUY

        for order in open_orders:
            # Check if this looks like a SL order:
            # 1. Stop market order
            # 2. Reduce only
            # 3. Correct side (opposite to position)
            if (
                order.order_type == OrderType.STOP_MARKET
                and order.is_reduce_only
                and order.side == expected_side
            ):
                return order

        return None

    def ensure_native_trailing_exists(self, ctx: PairContext, position: Position) -> None:
        """Ensure an exchange-managed trailing stop exists for the position.

        After a restart, reconciliation re-discovers venue orders as EXTERNAL.
        If a protective TRAILING_STOP_MARKET is already open for this position,
        re-claim it; otherwise submit a fresh one (native_trailing recovery).
        """
        s = self._strategy
        existing = self.find_existing_trailing_order(ctx, position)
        if existing:
            ctx.order_tracker.set_exchange_sl_order(existing.client_order_id)
            s.log.info(
                f"[{ctx.pair}] Found existing trailing stop: {existing.client_order_id}",
                color=LogColor.GREEN,
            )
            return

        s.log.warning(
            f"[{ctx.pair}] No trailing stop found for existing position - creating new one",
        )
        direction = SignalDirection.ENTER_LONG if position.is_long else SignalDirection.ENTER_SHORT
        signal = Signal(direction=direction, price=Decimal(str(position.avg_px_open)))
        s._sltp_coordinator.submit_native_trailing(ctx, signal)

    def ensure_native_trailing_protection(self, ctx: PairContext) -> None:
        """Per-bar self-heal: rebuild the trailing stop if an open position lost it.

        native_trailing's trailing order is reduce_only; when the venue rejects it,
        the generic on_order_rejected handler cancels + clears the tracker WITHOUT
        rebuilding, and the native tick/bar paths don't re-arm — leaving the open
        position unprotected. This reconciliation re-arms it,
        rate-guarded (``_NATIVE_TRAILING_REBUILD_COOLDOWN_NS``) so a venue that keeps
        rejecting cannot drive a reject->rebuild->reject flood. Runs every bar
        (including while paused — protection hygiene, same tier as the sweep).
        """
        s = self._strategy
        if not s._mode.uses_native_trailing:
            return
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        if not positions:
            return

        # Already protected by a live tracked trailing order? Use `not is_closed`
        # (covers open AND in-flight SUBMITTED/PENDING) so a just-submitted trailing
        # still being accepted by the venue is not mistaken for "unprotected" and
        # rebuilt into a duplicate (avoids racing a just-submitted trailing order).
        sl_id = ctx.order_tracker.exchange_sl_order_id
        if sl_id is not None:
            order = s.cache.order(sl_id)
            if order is not None and not order.is_closed:
                return

        now_ns = s.clock.timestamp_ns()
        if now_ns < ctx.native_trailing_rebuild_deadline_ns:
            return  # rate guard: still cooling down from a recent rebuild attempt

        ctx.native_trailing_rebuild_deadline_ns = now_ns + _NATIVE_TRAILING_REBUILD_COOLDOWN_NS
        s.log.error(
            f"[{ctx.pair}] NATIVE_TRAILING: open position has no protective trailing stop "
            f"(reject/loss) — rebuilding",
            color=LogColor.RED,
        )
        self.ensure_native_trailing_exists(ctx, positions[0])

    def find_existing_trailing_order(self, ctx: PairContext, position: Position) -> Order | None:
        """Find an open exchange-managed trailing stop for the position.

        Looks for a reduce-only TRAILING_STOP_MARKET on the protective side
        (SELL for long, BUY for short).
        """
        from nautilus_trader.model.enums import OrderSide, OrderType

        open_orders = self._strategy.cache.orders_open(instrument_id=ctx.instrument_id)
        expected_side = OrderSide.SELL if position.is_long else OrderSide.BUY

        for order in open_orders:
            if (
                order.order_type == OrderType.TRAILING_STOP_MARKET
                and order.is_reduce_only
                and order.side == expected_side
            ):
                return order

        return None

    def sweep_stale_orders_for_pair(self, ctx: PairContext) -> int:
        """Cancel stale (orphaned) reduce-only orders for this pair.

        Root cause: the reversal path's cancel_all_orders() is fire-and-forget -- when
        the venue call fails (e.g. a demo-fapi transport error) the old SL stays resting
        on the exchange while the tracker is already cleared, and Nautilus sends the
        strategy no cancel-failure event, so the orphan has no backstop. This method
        reconciles the open orders in the cache every bar and cancels the reduce-only
        orders the strategy no longer claims.

        Returns:
            Number of cancel requests issued in this sweep.
        """
        s = self._strategy
        open_orders = s.cache.orders_open(instrument_id=ctx.instrument_id)
        if not open_orders:
            if ctx.stale_cancel_attempts:
                ctx.stale_cancel_attempts.clear()
            return 0

        tracker = ctx.order_tracker
        tracked_ids = {
            oid
            for oid in (
                tracker.sl_order_id,
                tracker.exchange_sl_order_id,
                tracker.entry_order_id,
                *tracker.tp_order_ids,
            )
            if oid is not None
        }
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position_is_long = positions[0].is_long if positions else None
        sl_is_tracked = tracker.sl_order_id is not None or tracker.exchange_sl_order_id is not None
        now_ns = s.clock.timestamp_ns()

        # Prune rate-guard entries for orders no longer open
        open_ids = {o.client_order_id for o in open_orders}
        for oid in list(ctx.stale_cancel_attempts):
            if oid not in open_ids:
                del ctx.stale_cancel_attempts[oid]

        cancelled = 0
        for order in open_orders:
            if not is_stale_order(
                order,
                position_is_long=position_is_long,
                tracked_ids=tracked_ids,
                sl_is_tracked=sl_is_tracked,
                now_ns=now_ns,
            ):
                continue
            # rate guard — per-order cooldown between cancel attempts
            last_attempt = ctx.stale_cancel_attempts.get(order.client_order_id, 0)
            if now_ns - last_attempt < STALE_SWEEP_RETRY_COOLDOWN_NS:
                continue
            ctx.stale_cancel_attempts[order.client_order_id] = now_ns
            s.log.warning(
                f"[{ctx.pair}] Stale order sweep: cancelling orphaned "
                f"{order.side} {order.order_type} {order.client_order_id}",
            )
            s.cancel_order(order)
            cancelled += 1
        return cancelled

    def handle_order_rejected(self, event: OrderRejected) -> None:
        """Handle order rejected event.

        Break the tight loop when the venue rejects an order (e.g. Binance -2022
        ReduceOnly rejected):
        - rejected close order (reduce_only) -> set a reject backoff cooldown, turning
          per-tick resubmission into a controlled slow retry (the next submit is allowed
          only after the cooldown), so a single close fills once resting orders clear.
        - rejected entry order -> clear the entry tracker.

        The thin ``on_order_rejected`` shell on the Strategy owns the top-level
        try/except (a callback exception must not take down the strategy process); this
        method only carries the logic.
        """
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

        order = s.cache.order(event.client_order_id)
        reason = str(getattr(event, "reason", "")) or "unknown"
        is_reduce_only = bool(getattr(order, "is_reduce_only", False)) if order else False

        # Event emission: order rejected (same shape as cancel_rejected).
        if s._event_publisher.enabled:
            _oid = str(event.client_order_id)
            _sig_id = s._order_signal_map.pop(_oid, None) or extract_signal_id_from_tags(
                order.tags if order else None
            )
            s._event_publisher.publish_order_event(
                event=event,
                order=order,
                signal_id=_sig_id,
                side=order.side.name if order and hasattr(order, "side") else "UNKNOWN",
                order_type=order.order_type.name if order else "UNKNOWN",
                quantity=str(order.quantity) if order else "0",
                status="rejected",
                fill_price=None,
                venue_from_event=False,
            )

        # Rejected close order -> severity-tiered breaker: break the tight loop without
        # hammering a throttled endpoint.
        if is_reduce_only:
            now_ns = s.clock.timestamp_ns()
            tier = classify_rejection_reason(reason)
            if tier == "server":
                # Server error / rate limit (5xx/-1007/-1003/-1015): long breaker backoff,
                # **no cancel_all** (don't keep pressuring a failing/throttled order
                # endpoint and burning quota). -1007 execution-status-unknown lands here
                # too -- during the long backoff NautilusTrader keeps reconciling the real
                # position state, avoiding a repeat close of an order that may have filled.
                ctx.order_tracker.set_close_cooldown(now_ns, _CLOSE_SERVER_ERROR_BACKOFF_NS)
                s.log.warning(
                    f"[{ctx.pair}] Reduce-only order rejected by venue "
                    f"(server/ratelimit: {reason}); circuit-breaking "
                    f"{_CLOSE_SERVER_ERROR_BACKOFF_NS // 1_000_000_000}s before retry",
                    color=LogColor.RED,
                )
            else:
                # Logical reject (-2022 etc.): clear venue orphans (leftover reduce_only
                # SL/TP that reconciliation missed and the cache can't see fill the
                # reduce capacity) + short backoff.
                s.cancel_all_orders(ctx.instrument_id)
                ctx.order_tracker.clear()
                ctx.order_tracker.set_close_cooldown(now_ns, _CLOSE_REJECT_COOLDOWN_NS)
                s.log.warning(
                    f"[{ctx.pair}] Reduce-only order rejected (logic: {reason}); "
                    f"cancelling all venue orders (clear orphans) and backing off "
                    f"{_CLOSE_REJECT_COOLDOWN_NS // 1_000_000_000}s before retry",
                    color=LogColor.RED,
                )
                # A close the venue can never reduce keeps getting rejected every bar;
                # count consecutive logical rejects and halt once the threshold is hit, so
                # we stop spamming the venue and hand off to a human. The count survives the
                # clear() above and resets only on a confirmed close
                # (TradeEventHandler.handle_position_closed).
                ctx.order_tracker.record_close_reject()
                # Arming the escape hatch needs the venue to have refused reduce-only
                # itself. The count above also rises on rejections whose reason we could
                # not read, and this tier is the classifier's default for those -- an
                # unknown reason is not evidence, and acting on it would drop reduce_only
                # for an order that may already have filled.
                if is_reduce_only_refusal(reason):
                    ctx.order_tracker.record_reduce_only_refusal()
                if ctx.order_tracker.close_reject_count >= _CLOSE_REJECT_HALT_THRESHOLD:
                    s.pause()
                    s.log.error(
                        f"[{ctx.pair}] Close order rejected "
                        f"{ctx.order_tracker.close_reject_count} consecutive times "
                        f"(likely un-reducible at venue); pausing strategy for manual "
                        f"intervention",
                        color=LogColor.RED,
                    )

        # Rejected entry order -> clear the tracker.
        if (
            ctx.order_tracker.entry_order_id is not None
            and ctx.order_tracker.entry_order_id == event.client_order_id
        ):
            ctx.order_tracker.clear_entry_order()
            s.log.warning(
                f"[{ctx.pair}] Entry order rejected: {event.client_order_id} ({reason})",
                color=LogColor.RED,
            )

    def handle_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        """
        Handle order cancel rejected event - clean up tracker anyway.

        This handles the race condition where an order is filled on the exchange
        before our cancel request arrives. The order is no longer pending, so
        we should clean up our tracker regardless.

        The thin ``on_order_cancel_rejected`` shell on the Strategy owns the top-level
        try/except; this method only carries the logic.
        """
        s = self._strategy
        ctx = s._get_context_from_instrument(event.instrument_id)
        if ctx is None:
            return

        # Event emission: order cancel rejected
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
                side=_order.side.name if _order and hasattr(_order, "side") else "UNKNOWN",
                order_type=_order.order_type.name if _order else "UNKNOWN",
                quantity=str(_order.quantity) if _order else "0",
                status="rejected",
                fill_price=None,
                venue_from_event=False,
            )

        # Clean up entry order tracker - the order is no longer on exchange
        # (either filled, expired, or already cancelled)
        if (
            ctx.order_tracker.entry_order_id is not None
            and ctx.order_tracker.entry_order_id == event.client_order_id
        ):
            ctx.order_tracker.clear_entry_order()
            s.log.warning(
                f"[{ctx.pair}] Entry order cancel rejected (order no longer exists): "
                f"{event.client_order_id}, reason={event.reason}",
            )
