"""Order recovery / reconciliation / orphan cleanup / reject breaker component.

Holds the defense-in-depth order-protection cluster: restart position recovery,
exchange SL / native_trailing claim, per-bar orphan reconciliation sweep, and
severity-tiered reject breaker. Injects a strategy reference and reaches
``cache`` / ``clock`` / ``log`` / ``_contexts`` / ``_mode`` / ``cancel_all_orders`` /
``cancel_order`` / ``_order_signal_map`` /
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
from nautilus_trader.common import LogColor
from nautilus_trader.model import OrderCancelRejected, OrderRejected, OrderSide, OrderType

from custos_toolkit_nautilus.adapter.coordinators.entry_reservation import (
    release_unfilled_entry,
)
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
                    quantity=Decimal(str(position.quantity)),
                )
                bars = s.cache.bars(ctx.bar_type)
                if bars:
                    current_price = Decimal(str(bars[-1].close))
                    # Observe, do not check: the return value is discarded here, and a
                    # check would spend a scaled level on an exit nobody submits.
                    ctx.tick_monitor.observe(current_price)

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
        existing_sls = self.find_existing_sl_orders(ctx, position)

        newly_reclaimed = 0
        if existing_sls:
            # A partially filled entry owns one protective lot per fill. Reclaim
            # every matching order after restart, not only the first one, otherwise
            # close cleanup would orphan the remaining lots.
            for existing_sl in existing_sls:
                if s._mode is SLTPMode.HYBRID:
                    if existing_sl.client_order_id not in ctx.order_tracker.exchange_sl_order_ids:
                        newly_reclaimed += 1
                    ctx.order_tracker.add_exchange_sl_order(
                        existing_sl.client_order_id, existing_sl.quantity
                    )
                else:
                    if existing_sl.client_order_id not in ctx.order_tracker.sl_order_ids:
                        newly_reclaimed += 1
                    ctx.order_tracker.add_sl_order(
                        existing_sl.client_order_id, existing_sl.quantity
                    )
            if newly_reclaimed:
                s.log.info(
                    f"[{ctx.pair}] Reclaimed {newly_reclaimed} existing SL order lot(s)",
                    color=LogColor.GREEN,
                )

        exchange_managed = s._mode is SLTPMode.HYBRID
        protected_quantity = ctx.order_tracker.protected_quantity(exchange_managed=exchange_managed)
        missing_quantity = max(
            Decimal(str(position.quantity)) - protected_quantity,
            Decimal("0"),
        )
        if missing_quantity <= 0:
            return

        # No SL order, or partial-fill coverage is short: create only the missing
        # quantity. Never cancel/replace an accepted lot and introduce a protection gap.
        s.log.warning(
            f"[{ctx.pair}] SL protection short by {missing_quantity} - creating delta lot",
        )

        # Create a synthetic signal based on position direction
        direction = SignalDirection.ENTER_LONG if position.is_long else SignalDirection.ENTER_SHORT
        signal = Signal(direction=direction, price=Decimal(str(position.avg_px_open)))

        if s._mode is SLTPMode.HYBRID:
            s._sltp_coordinator.submit_safety_stop_loss(ctx, signal, quantity=missing_quantity)
            return

        # Exchange mode. The repair owns no entry of its own, so it hands the sizing
        # ATR to the submitter directly. Writing a synthetic signal into the pending
        # entry state instead would overwrite the context of an entry that is still
        # filling -- and, with a blank ATR, leave an ATR-based stop unpriceable.
        placed = s._sltp_coordinator.submit_stop_loss(
            ctx, signal, quantity=missing_quantity, atr=self._repair_atr(ctx)
        )
        if placed is None:
            s.log.error(
                f"[{ctx.pair}] Stop-loss repair produced no order; {missing_quantity} of "
                "the position remains unprotected",
                color=LogColor.RED,
            )

    @staticmethod
    def _repair_atr(ctx: PairContext) -> Decimal | None:
        """The ATR a repair may size a stop with.

        An entry still in flight already carries the ATR its protection was planned
        against; prefer it. Otherwise the live reading is the best available input.
        None means the data genuinely is not there, which the caller reports rather
        than passing off as a completed repair.
        """
        pending_atr = ctx.position_tracker.pending_entry_atr
        if pending_atr is not None:
            return pending_atr
        indicator = ctx.indicators.get("atr")
        if indicator and indicator.initialized:
            return Decimal(str(indicator.value))
        return None

    def ensure_exchange_sl_protection(self, ctx: PairContext) -> None:
        """Per-bar repair of missing standard/hybrid stop coverage."""
        s = self._strategy
        if not s._mode.uses_exchange_sl:
            return
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        if not positions:
            return
        self._prune_closed_protection(ctx, exchange_managed=s._mode is SLTPMode.HYBRID)
        position = positions[0]
        expected = Decimal(str(position.quantity))
        covered = ctx.order_tracker.protected_quantity(exchange_managed=s._mode is SLTPMode.HYBRID)
        if covered >= expected:
            return
        now_ns = s.clock.timestamp_ns()
        if now_ns < ctx.exchange_sl_rebuild_deadline_ns:
            return
        ctx.exchange_sl_rebuild_deadline_ns = now_ns + _NATIVE_TRAILING_REBUILD_COOLDOWN_NS
        s.log.error(
            f"[{ctx.pair}] Exchange SL coverage {covered} is below position {expected} — "
            "rebuilding missing delta",
            color=LogColor.RED,
        )
        self.ensure_exchange_sl_exists(ctx, position)

    def find_existing_sl_order(self, ctx: PairContext, position: Position) -> Order | None:
        """Return the first matching stop for legacy single-order callers."""
        orders = self.find_existing_sl_orders(ctx, position)
        return orders[0] if orders else None

    def find_existing_sl_orders(self, ctx: PairContext, position: Position) -> list[Order]:
        """
        Find all existing stop-loss lots for the position on the exchange.

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

        # Get all open orders for this instrument
        open_orders = self._strategy.cache.orders_open(instrument_id=ctx.instrument_id)

        expected_side = OrderSide.SELL if position.is_long else OrderSide.BUY

        matches = []
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
                matches.append(order)

        return matches

    def ensure_native_trailing_exists(self, ctx: PairContext, position: Position) -> None:
        """Ensure an exchange-managed trailing stop exists for the position.

        After a restart, reconciliation re-discovers venue orders as EXTERNAL.
        If a protective TRAILING_STOP_MARKET is already open for this position,
        re-claim it; otherwise submit a fresh one (native_trailing recovery).
        """
        s = self._strategy
        existing_orders = self.find_existing_trailing_orders(ctx, position)
        newly_reclaimed = 0
        if existing_orders:
            for existing in existing_orders:
                if existing.client_order_id not in ctx.order_tracker.exchange_sl_order_ids:
                    newly_reclaimed += 1
                ctx.order_tracker.add_exchange_sl_order(existing.client_order_id, existing.quantity)
            if newly_reclaimed:
                s.log.info(
                    f"[{ctx.pair}] Reclaimed {newly_reclaimed} existing trailing stop lot(s)",
                    color=LogColor.GREEN,
                )

        protected_quantity = ctx.order_tracker.protected_quantity(exchange_managed=True)
        missing_quantity = max(
            Decimal(str(position.quantity)) - protected_quantity,
            Decimal("0"),
        )
        if missing_quantity <= 0:
            return

        s.log.warning(
            f"[{ctx.pair}] Trailing-stop protection short by {missing_quantity} - "
            "creating delta lot",
        )
        direction = SignalDirection.ENTER_LONG if position.is_long else SignalDirection.ENTER_SHORT
        signal = Signal(direction=direction, price=Decimal(str(position.avg_px_open)))
        s._sltp_coordinator.submit_native_trailing(ctx, signal, quantity=missing_quantity)

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

        self._prune_closed_protection(ctx, exchange_managed=True)
        position = positions[0]
        expected = Decimal(str(position.quantity))
        covered = ctx.order_tracker.protected_quantity(exchange_managed=True)
        if covered >= expected:
            return

        now_ns = s.clock.timestamp_ns()
        if now_ns < ctx.native_trailing_rebuild_deadline_ns:
            return  # rate guard: still cooling down from a recent rebuild attempt

        ctx.native_trailing_rebuild_deadline_ns = now_ns + _NATIVE_TRAILING_REBUILD_COOLDOWN_NS
        s.log.error(
            f"[{ctx.pair}] NATIVE_TRAILING coverage {covered} is below position {expected} "
            "(reject/loss) — rebuilding missing delta",
            color=LogColor.RED,
        )
        self.ensure_native_trailing_exists(ctx, position)

    def _prune_closed_protection(
        self,
        ctx: PairContext,
        *,
        exchange_managed: bool,
    ) -> None:
        """Drop only terminal cached stops; unknown orders remain in-flight coverage."""
        s = self._strategy
        order_ids = (
            ctx.order_tracker.exchange_sl_order_ids
            if exchange_managed
            else ctx.order_tracker.sl_order_ids
        )
        for order_id in order_ids:
            order = s.cache.order(order_id)
            if order is not None and order.is_closed:
                ctx.order_tracker.remove_order(order_id)

    def find_existing_trailing_order(self, ctx: PairContext, position: Position) -> Order | None:
        """Return the first matching trailing stop for legacy single-order callers."""
        orders = self.find_existing_trailing_orders(ctx, position)
        return orders[0] if orders else None

    def find_existing_trailing_orders(self, ctx: PairContext, position: Position) -> list[Order]:
        """Find an open exchange-managed trailing stop for the position.

        Looks for a reduce-only TRAILING_STOP_MARKET on the protective side
        (SELL for long, BUY for short).
        """

        open_orders = self._strategy.cache.orders_open(instrument_id=ctx.instrument_id)
        expected_side = OrderSide.SELL if position.is_long else OrderSide.BUY

        matches = []
        for order in open_orders:
            if (
                order.order_type == OrderType.TRAILING_STOP_MARKET
                and order.is_reduce_only
                and order.side == expected_side
            ):
                matches.append(order)

        return matches

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
                *tracker.sl_order_ids,
                *tracker.exchange_sl_order_ids,
                tracker.entry_order_id,
                *tracker.tp_order_ids,
            )
            if oid is not None
        }
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position_is_long = positions[0].is_long if positions else None
        sl_is_tracked = bool(tracker.sl_order_ids or tracker.exchange_sl_order_ids)
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
        is_tracked_stop = event.client_order_id in {
            *ctx.order_tracker.sl_order_ids,
            *ctx.order_tracker.exchange_sl_order_ids,
        }
        is_tracked_take_profit = event.client_order_id in set(ctx.order_tracker.tp_order_ids)

        # A rejected order will never fill, so its signal link goes.
        s._order_signal_map.pop(str(event.client_order_id), None)

        if is_tracked_stop:
            # A protective lot is not an active close attempt. Cancel-all/clear here
            # would erase other accepted lots and a still-partially-filling entry.
            # Remove only the rejected lot, pause new risk, and let the per-bar
            # quantity reconciler rebuild the exact missing delta under cooldown.
            ctx.order_tracker.remove_order(event.client_order_id)
            ctx.native_trailing_rebuild_deadline_ns = 0
            ctx.exchange_sl_rebuild_deadline_ns = 0
            s.pause()
            s.log.error(
                f"[{ctx.pair}] Protective stop rejected ({reason}); preserving other "
                "protection and pending entry, pausing new risk until coverage is repaired",
                color=LogColor.RED,
            )
            return

        if is_tracked_take_profit:
            # A rejected profit-taking lot does not consume stop coverage and must not
            # enter the full-close -2022 escape path.
            ctx.order_tracker.remove_order(event.client_order_id)
            if ctx.tick_monitor is not None:
                # Nothing was taken, so the scaled level it carried is still owed.
                ctx.tick_monitor.release_level_order(event.client_order_id)
            s.log.warning(
                f"[{ctx.pair}] Take-profit lot rejected ({reason}); stop coverage retained",
                color=LogColor.YELLOW,
            )
            return

        # Rejected active close order -> severity-tiered breaker: break the tight loop without
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
            release_unfilled_entry(s, ctx)
            ctx.order_tracker.clear_entry_order()
            ctx.position_tracker.clear_pending_signal()
            ctx.pending_entry_is_reversal = False
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

        # A refused cancel is not evidence that the order is gone. It commonly means
        # exactly the opposite -- the venue would not cancel it because it is filling.
        # Ask the cache for the order's actual state instead of reading the refusal as
        # a disappearance: releasing ownership of an order that can still fill makes
        # our own fill arrive as an untracked one, and an untracked fill gets no
        # protection.
        order = s.cache.order(event.client_order_id)
        still_live = order is not None and not order.is_closed
        is_tracked_entry = (
            ctx.order_tracker.entry_order_id is not None
            and ctx.order_tracker.entry_order_id == event.client_order_id
        )

        if still_live:
            if is_tracked_entry:
                s.log.warning(
                    f"[{ctx.pair}] Entry order cancel refused while the order is still "
                    f"live: {event.client_order_id}, reason={event.reason}. Keeping "
                    "ownership so a later fill is recognised as ours",
                )
            return

        # Confirmed terminal: the order really is gone, so its signal link goes too.
        s._order_signal_map.pop(str(event.client_order_id), None)

        if is_tracked_entry:
            ctx.order_tracker.clear_entry_order()
            s.log.warning(
                f"[{ctx.pair}] Entry order cancel rejected (order no longer exists): "
                f"{event.client_order_id}, reason={event.reason}",
            )
