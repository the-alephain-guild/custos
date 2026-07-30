"""Bar-driven signal execution component.

Holds the per-bar signal execution cluster: submitting entry/exit orders from an
actionable signal, managing stop loss / break-even for open positions, and
cancelling a stale pending entry order before a new one. Injects a strategy
reference and reaches ``cache`` / ``log`` / ``config`` / ``submit_order`` /
``cancel_order`` / ``cancel_all_orders`` plus ``_mode`` / ``_capital_allocator`` /
``_risk_manager`` / ``_sltp_coordinator`` / ``_order_signal_map`` through it.

The ``_process_bar`` pipeline stays on the Strategy class as the orchestration layer;
it delegates the signal-execution steps (entry/exit/manage) to this component.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

from custos_toolkit.signals.types import SignalDirection
from nautilus_trader.common.enums import LogColor

from custos_toolkit_nautilus.adapter.event_publisher import make_signal_tag
from custos_toolkit_nautilus.adapter.execution import ExecutionManager
from custos_toolkit_nautilus.adapter.orders import _CLOSE_INFLIGHT_TIMEOUT_NS

if TYPE_CHECKING:
    from custos_toolkit.signals.types import Signal
    from nautilus_trader.model.data import Bar

    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class _Indicator(Protocol):
    @property
    def initialized(self) -> bool: ...
    @property
    def value(self) -> object: ...


class SignalExecutionCoordinator:
    """Bar-driven signal execution path (entry / exit / manage).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def _cancel_pending_entry_order(self, ctx: PairContext) -> None:
        """
        Cancel any pending entry order for this pair.

        Called at the start of execute_entry_for_pair() to ensure that
        when signals change (e.g., ENTER_LONG → ENTER_SHORT), the previous
        pending entry order is cancelled before submitting a new one.

        Note: We don't clear the tracker here - wait for on_order_canceled
        or on_order_cancel_rejected events to clean up. This handles the
        race condition where the order fills on the exchange before our
        cancel request arrives.
        """
        s = self._strategy
        entry_order_id = ctx.order_tracker.entry_order_id
        if entry_order_id is None:
            return

        order = s.cache.order(entry_order_id)

        # If order is still open, send cancel request
        if order and order.is_open:
            s.cancel_order(order)
            s.log.info(
                f"[{ctx.pair}] Cancelling pending entry order: {entry_order_id}",
                color=LogColor.YELLOW,
            )
            # Don't clear tracker here - wait for on_order_canceled or on_order_cancel_rejected
            return

        # Order is not open (already filled/cancelled) - clear tracker immediately
        # This handles the case where fill event arrived before we tried to cancel
        ctx.order_tracker.clear_entry_order()

    def execute_entry_for_pair(
        self, ctx: PairContext, signal: Signal, size: Decimal, bar: Bar
    ) -> None:
        """Execute entry trade for a specific pair.

        Signal Override Layer:
            - signal.amount: Overrides the calculated size
            - signal.order_type: Overrides config order_type
            - signal.order_price_offset: Used for limit order price offset

        Reversal Sizing:
            In netting accounts, reversing from Short to Long (or vice versa)
            requires closing the current position + opening new position.
            This method automatically adds current position quantity to achieve
            proper reversal (close + open = base_size + current_qty).
        """
        s = self._strategy
        # Cancel any previous pending entry order before submitting new one
        self._cancel_pending_entry_order(ctx)

        # Check position limits
        pos_config = s.config.position
        if pos_config.limits.max_total_positions:
            if len(s.cache.positions_open()) >= pos_config.limits.max_total_positions:
                s.log.warning(f"[{ctx.pair}] Max open positions reached")
                return

        # Signal Override: amount > calculated size
        final_size = signal.amount if signal.amount is not None else size

        # Reversal sizing: add current position size for close+open in netting accounts
        # Default False; only the reversal branch below sets it True.
        ctx.pending_entry_is_reversal = False
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        if positions:
            position = positions[0]
            is_reversal_to_long = (
                signal.direction == SignalDirection.ENTER_LONG and position.is_short
            )
            is_reversal_to_short = (
                signal.direction == SignalDirection.ENTER_SHORT and position.is_long
            )
            if is_reversal_to_long or is_reversal_to_short:
                ctx.pending_entry_is_reversal = True
                # Convert current position quantity (base currency, e.g., BTC) to notional value
                # (quote currency, e.g., USDT) to ensure we're adding USDT + USDT, not USDT + BTC
                current_qty = Decimal(str(position.quantity))
                current_price = Decimal(str(bar.close))
                current_value_usdt = current_qty * current_price
                final_size = final_size + current_value_usdt
                s.log.info(
                    f"[{ctx.pair}] Reversal sizing: base={size:.3f} USDT, "
                    f"close_qty={current_qty} ({current_value_usdt:.3f} USDT), "
                    f"total={final_size:.3f} USDT"
                )
                # Cancel ALL open orders for this instrument before reversal.
                # Using cancel_all_orders instead of tracker-based cancellation to also cover
                # untracked orders (e.g., TP orders not recovered after strategy restart).
                # NOTE: this is an async fire-and-forget command — a venue failure leaves
                # orphans behind; OrderReconciler.sweep_stale_orders_for_pair() reconciles
                # them per bar.
                s.cancel_all_orders(ctx.instrument_id)
                ctx.order_tracker.clear()
                s.log.info(
                    f"[{ctx.pair}] Requested cancel of all open orders before reversal",
                    color=LogColor.YELLOW,
                )

        # A computed size <= 0 (e.g. fixed_risk with no valid stop-loss, or check_limits
        # below min_order_size) must not become an exchange-rejected make_qty(0) — skip this
        # entry. Reversal sizing already added the close quantity, so a real reversal keeps
        # final_size > 0.
        if final_size <= 0:
            s.log.warning(
                f"[{ctx.pair}] computed entry size <= 0 ({final_size}); skipping entry",
                color=LogColor.YELLOW,
            )
            return

        # Signal Override: order_type > config
        order_type = signal.order_type or s.config.trading.order_type

        # Signal Override: order_price_offset for limit orders
        price_offset = signal.order_price_offset

        # Propagate signal_id through order tags
        _tags = None
        _sig_id = cast(str | None, signal.metadata.get("_signal_id") if signal.metadata else None)
        if _sig_id:
            _tags = [make_signal_tag(_sig_id)]

        # Use context's execution_manager
        execution_manager = cast(ExecutionManager, ctx.execution_manager)
        order = execution_manager.create_entry_order(
            instrument_id=ctx.instrument_id,
            signal=signal,
            size=final_size,
            bar=bar,
            order_type=order_type,
            price_offset=price_offset,
            tags=_tags,
        )
        if order is None:
            return

        # Use context's position_tracker
        ctx.position_tracker.record_entry(Decimal(str(bar.close)), final_size)

        # Track allocated capital for correct release on position close
        ctx.allocated_capital += final_size

        # Allocate from capital allocator if available
        if s._capital_allocator:
            s._capital_allocator.allocate(ctx.pair, final_size)

        # Store entry ATR
        atr = cast(_Indicator | None, ctx.indicators.get("atr"))
        entry_atr = Decimal(str(atr.value)) if atr and atr.initialized else None
        ctx.position_tracker.set_pending_signal(signal, entry_atr)

        s.submit_order(order)

        # Persist order→signal mapping (MARKET orders lose tags after fill in cache)
        if _sig_id:
            s._order_signal_map[str(order.client_order_id)] = _sig_id
        # Record the open-position signal id so subsequent SL/TP orders link to this signal
        ctx.active_signal_id = _sig_id

        # Track the entry order ID + direction for potential cancellation if the signal
        # changes (direction lets a trend gate tell this entry from a stale opposite one).
        entry_side = 1 if signal.direction == SignalDirection.ENTER_LONG else -1
        ctx.order_tracker.set_entry_order(order.client_order_id, entry_side)

        s.log.info(
            f"[{ctx.pair}] ENTRY: {signal.direction.name} | "
            f"size={final_size:.4f} | order_type={order_type}",
            color=LogColor.BLUE,
        )

    def execute_exit_for_pair(self, ctx: PairContext, signal: Signal, bar: Bar) -> None:
        """Execute exit trade for a specific pair."""
        s = self._strategy
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position = positions[0] if positions else None
        if position is None or position.is_closed:
            return

        # In-flight gate: the exit is a market IOC order, but its fill event lags back to
        # the local cache, so within that window the decoupled reversal-EXIT path (which
        # emits an exit every bar while the position is open) could re-submit the same
        # close. The gate (together with create_exit_order's reduce_only) caps it to one
        # close in flight per position, shared with the tick exit path.
        now_ns = s.clock.timestamp_ns()
        if not ctx.order_tracker.can_submit_close(now_ns):
            return

        execution_manager = cast(ExecutionManager, ctx.execution_manager)
        # Reduce-only is the protective default, but a venue that has already refused it
        # on the logic tier (Binance's demo engine does this with the position genuinely
        # open) will refuse it again -- retrying the refused form is retrying what cannot
        # work. From the second attempt on, close with a plain order of the position's
        # size. The count is raised only by logic-tier rejections and is cleared on a
        # confirmed close, so a server error or rate limit does not drop the protection.
        first_attempt = ctx.order_tracker.close_reject_count == 0
        order = execution_manager.create_exit_order(
            instrument_id=ctx.instrument_id,
            signal=signal,
            size=Decimal(str(position.quantity)),
            reduce_only=first_attempt,
        )
        if not first_attempt:
            s.log.warning(
                f"[{ctx.pair}] Closing without reduce_only after "
                f"{ctx.order_tracker.close_reject_count} logical refusal(s) of the "
                f"reduce-only close; size={position.quantity} taken from the open position",
            )
        if order:
            _sig_id = cast(
                str | None, signal.metadata.get("_signal_id") if signal.metadata else None
            )
            if _sig_id:
                s._order_signal_map[str(order.client_order_id)] = _sig_id
            s.submit_order(order)
            ctx.order_tracker.mark_closing(now_ns, _CLOSE_INFLIGHT_TIMEOUT_NS)
            s.log.info(
                f"[{ctx.pair}] EXIT: {signal.direction.name}",
                color=LogColor.MAGENTA,
            )

    def manage_positions_for_pair(self, ctx: PairContext, bar: Bar) -> None:
        """Manage stop loss and take profit for open positions of a specific pair."""
        s = self._strategy
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        position = positions[0] if positions else None
        if position is None or position.is_closed:
            return

        trade_risk = s.config.risk.trade
        current_price = Decimal(str(bar.close))

        # Check trailing stop and break-even
        entry_price = ctx.position_tracker.first_entry_price
        if (
            trade_risk.stop_loss.break_even.enabled
            and entry_price > 0
            and not ctx.break_even_applied
            # native_trailing's TrailingStopMarketOrder is itself a dynamic stop;
            # adding a break-even stop_market on top would be untracked (reduce-only
            # capacity risk) and conflict with native_trailing semantics.
            and s._mode.allows_break_even
        ):
            if s._risk_manager.should_move_to_break_even(
                entry_price,
                current_price,
                position.is_long,
                trade_risk.stop_loss.break_even.activation_pct,
            ):
                s._sltp_coordinator.move_stop_to_break_even(ctx, position, entry_price)
