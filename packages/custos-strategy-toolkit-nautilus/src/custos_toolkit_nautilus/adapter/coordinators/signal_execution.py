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

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from custos_toolkit.signals.types import SignalDirection
from nautilus_trader.common import LogColor
from nautilus_trader.model import Bar

from custos_toolkit_nautilus.adapter.execution import ExecutionManager
from custos_toolkit_nautilus.adapter.runtime_types import Indicator
from custos_toolkit_nautilus.adapter.orders import _CLOSE_INFLIGHT_TIMEOUT_NS
from custos_toolkit_nautilus.adapter.signal_correlation import make_signal_tag
from custos_toolkit_nautilus.adapter.sizing import notional_from_quantity
from custos_toolkit_nautilus.adapter.strategy_core import CloseAttempt, plan_close_attempt

if TYPE_CHECKING:
    from custos_toolkit.signals.types import Signal

    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


@dataclass(frozen=True, slots=True)
class EntrySizing:
    """How big an entry is once a possible reversal's close leg is counted in."""

    size: Decimal
    is_reversal: bool
    close_quantity: Decimal


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """An entry that has passed every check that could still refuse it.

    The split this represents is load-bearing, not cosmetic. Clearing the way for a
    reversal cancels the old position's protection; doing that before the last
    refusal leaves the old position open with no stop and no replacement on the way.
    That ordering used to be held by a comment in the middle of one long method.
    Now it is the boundary between :meth:`SignalExecutionCoordinator._plan_entry`,
    which only reads, and :meth:`SignalExecutionCoordinator._commit_entry`, which
    cannot refuse because it is only ever called with one of these.

    ``reserved_capital`` is already spent by the time this exists: the allocator is
    the last gate, and passing it takes the money. The commit phase owns returning
    it if the local execution gate refuses the order before it reaches the venue.
    """

    order: object
    size: Decimal
    order_type: str
    is_reversal: bool
    reversal_close_quantity: Decimal
    signal_id: str | None
    reserved_capital: Decimal


class SignalExecutionCoordinator:
    """Bar-driven signal execution path (entry / exit / manage).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def _cancel_pending_entry_order(self, ctx: PairContext) -> bool:
        """
        Cancel any pending entry order for this pair.

        Called at the start of execute_entry_for_pair() to ensure that
        when signals change (e.g., ENTER_LONG → ENTER_SHORT), the previous
        pending entry order is cancelled before submitting a new one.

        Note: We don't clear the tracker here - wait for on_order_canceled
        or on_order_cancel_rejected events to clean up. This handles the
        race condition where the order fills on the exchange before our
        cancel request arrives.

        Returns True while the previous entry is still live at the venue, i.e. the
        cancel has been requested but not confirmed. The tracker holds one entry
        identity, so submitting a replacement in that window would overwrite the
        old order's identity while it can still fill.
        """
        s = self._strategy
        entry_order_id = ctx.order_tracker.entry_order_id
        if entry_order_id is None:
            return False

        order = s.cache.order(entry_order_id)

        # If order is still open, send cancel request
        if order and order.is_open:
            s.cancel_order(order)
            s.log.info(
                f"[{ctx.pair}] Cancelling pending entry order: {entry_order_id}",
                color=LogColor.YELLOW,
            )
            # Don't clear tracker here - wait for on_order_canceled or on_order_cancel_rejected
            return True

        # Order is not open (already filled/cancelled) - clear tracker immediately
        # This handles the case where fill event arrived before we tried to cancel
        ctx.order_tracker.clear_entry_order()
        return False

    def execute_entry_for_pair(
        self, ctx: PairContext, signal: Signal, size: Decimal, bar: Bar
    ) -> None:
        """Execute entry trade for a specific pair.

        Two phases with a hard boundary: decide, then commit. Everything that can
        still refuse this entry lives in :meth:`_plan_entry`, which only reads.
        Once it hands back a plan, nothing may refuse -- see :class:`EntryPlan`.
        """
        plan = self._plan_entry(ctx, signal, size, bar)
        if plan is None:
            return
        self._commit_entry(ctx, signal, bar, plan)

    def _plan_entry(
        self, ctx: PairContext, signal: Signal, size: Decimal, bar: Bar
    ) -> EntryPlan | None:
        """Decide whether this entry goes out, and how big. Reads only.

        Returns ``None`` when anything refuses (having logged why). The one thing
        here that is not a pure read is the capital allocation at the end: it is the
        last gate, and passing it takes the money.

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
        if self._cancel_pending_entry_order(ctx):
            # The cancel is out but unconfirmed. Entering now would point the single
            # tracked identity at the new order while the old one can still fill --
            # that fill would then look external and receive no protection. The signal
            # is re-evaluated every bar, so this waits rather than forfeits.
            s.log.warning(
                f"[{ctx.pair}] Holding this entry until the previous order's "
                "cancellation is confirmed",
                color=LogColor.YELLOW,
            )
            return None

        # Check position limits
        pos_config = s.config.position
        if pos_config.limits.max_total_positions:
            if len(s.cache.positions_open()) >= pos_config.limits.max_total_positions:
                s.log.warning(f"[{ctx.pair}] Max open positions reached")
                return None

        # Signal Override: amount > calculated size
        final_size = signal.amount if signal.amount is not None else size

        # Reversal sizing only reads the position. Clearing the way for a reversal
        # cancels the old position's protection, and that belongs to the commit phase.
        ctx.pending_entry_is_reversal = False
        sizing = self._size_against_open_position(ctx, signal, bar, final_size)
        if sizing is None:
            return None
        final_size = sizing.size
        is_reversal = sizing.is_reversal
        reversal_close_quantity = sizing.close_quantity

        # A computed size <= 0 (e.g. fixed_risk with no valid stop-loss, or check_limits
        # below min_order_size) must not become an exchange-rejected make_qty(0) — skip this
        # entry. Reversal sizing already added the close quantity, so a real reversal keeps
        # final_size > 0.
        if final_size <= 0:
            s.log.warning(
                f"[{ctx.pair}] computed entry size <= 0 ({final_size}); skipping entry",
                color=LogColor.YELLOW,
            )
            return None

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
            return None

        # Reserve the capital before recording anything. allocate() returning False is
        # a refusal -- the pair tier or the total cash cannot cover this size -- and
        # submitting anyway spends capital the allocator has not granted, while leaving
        # the context claiming a reservation the allocator never made.
        if s._capital_allocator and not s._capital_allocator.allocate(ctx.pair, final_size):
            s.log.warning(
                f"[{ctx.pair}] Capital allocation refused for size={final_size:.4f} "
                f"(available={s._capital_allocator.get_available_capital(ctx.pair):.4f}); "
                "skipping entry",
                color=LogColor.YELLOW,
            )
            return None

        return EntryPlan(
            order=order,
            size=final_size,
            order_type=order_type,
            is_reversal=is_reversal,
            reversal_close_quantity=reversal_close_quantity,
            signal_id=_sig_id,
            reserved_capital=final_size,
        )

    def _size_against_open_position(
        self, ctx: PairContext, signal: Signal, bar: Bar, base_size: Decimal
    ) -> EntrySizing | None:
        """How big this entry has to be, counting a close leg if it reverses.

        A netting account reverses by closing and opening in one order, so the entry
        carries the old position's notional too. Returns ``None`` when the instrument
        needed to convert that close quantity into notional is missing -- sizing a
        reversal off base quantity would be adding BTC to USDT.
        """
        s = self._strategy
        flat = EntrySizing(base_size, is_reversal=False, close_quantity=Decimal("0"))
        positions = s.cache.positions_open(instrument_id=ctx.instrument_id)
        if not positions:
            return flat
        position = positions[0]
        reverses = (signal.direction == SignalDirection.ENTER_LONG and position.is_short) or (
            signal.direction == SignalDirection.ENTER_SHORT and position.is_long
        )
        if not reverses:
            return flat

        instrument = s.cache.instrument(ctx.instrument_id)
        if instrument is None:
            s.log.error(
                f"[{ctx.pair}] Instrument not found; cannot size the reversal",
                color=LogColor.RED,
            )
            return None
        close_quantity = Decimal(str(position.quantity))
        close_notional = notional_from_quantity(instrument, close_quantity, Decimal(str(bar.close)))
        s.log.info(
            f"[{ctx.pair}] Reversal sizing: base={base_size:.3f} USDT, "
            f"close_qty={close_quantity} ({close_notional:.3f} USDT), "
            f"total={base_size + close_notional:.3f} USDT"
        )
        return EntrySizing(
            base_size + close_notional, is_reversal=True, close_quantity=close_quantity
        )

    def _commit_entry(self, ctx: PairContext, signal: Signal, bar: Bar, plan: EntryPlan) -> None:
        """Put a decided entry into the world. Nothing here may refuse it.

        Reaching this means every check has passed, so it is finally safe to take
        down the old position's protection for a reversal.
        """
        s = self._strategy
        order = plan.order
        final_size = plan.size
        is_reversal = plan.is_reversal
        reversal_close_quantity = plan.reversal_close_quantity
        order_type = plan.order_type
        _sig_id = plan.signal_id

        # Past every refusal. Only now is it safe to take down the old position's
        # protection, because this entry is going out.
        if is_reversal:
            ctx.pending_entry_is_reversal = True
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

        # Use context's position_tracker
        ctx.position_tracker.record_entry(Decimal(str(bar.close)), final_size)

        # Track allocated capital for correct release on position close
        ctx.allocated_capital += final_size

        # Store entry ATR
        atr = cast(Indicator | None, ctx.indicators.get("atr"))
        entry_atr = Decimal(str(atr.value)) if atr and atr.initialized else None
        ctx.position_tracker.set_pending_signal(signal, entry_atr)

        dispatched = cast(bool | None, s.submit_order(order))
        if dispatched is False:
            # The local gate refused it: nothing reached the venue, so nothing was
            # entered and nothing was spent. The exit path already reads this
            # refusal; leaving the entry path to record a position for an order
            # that does not exist would strand the reservation until restart.
            if s._capital_allocator:
                s._capital_allocator.release(ctx.pair, final_size)
            ctx.allocated_capital = max(ctx.allocated_capital - final_size, Decimal("0"))
            ctx.position_tracker.clear_pending_signal()
            if not s.cache.positions_open(instrument_id=ctx.instrument_id):
                ctx.position_tracker.reset()
            ctx.pending_entry_is_reversal = False
            s.log.warning(
                f"[{ctx.pair}] ENTRY was refused locally before dispatch; "
                f"returned the {final_size:.4f} it had reserved",
                color=LogColor.YELLOW,
            )
            return

        # Persist order→signal mapping (MARKET orders lose tags after fill in cache)
        if _sig_id:
            s._order_signal_map[str(order.client_order_id)] = _sig_id
        # Record the open-position signal id so subsequent SL/TP orders link to this signal
        ctx.active_signal_id = _sig_id

        # Track the entry order ID + direction for potential cancellation if the signal
        # changes (direction lets a trend gate tell this entry from a stale opposite one).
        entry_side = 1 if signal.direction == SignalDirection.ENTER_LONG else -1
        ctx.order_tracker.set_entry_order(
            order.client_order_id,
            entry_side,
            exposure_offset_quantity=reversal_close_quantity,
            reserved_capital=final_size,
            order_quantity=order.quantity,
        )

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
        # Reduce-only is the protective default. A venue that specifically refused that
        # form while the position is genuinely open (Binance's demo engine does) will
        # refuse it again, so the escape hatch is a plain order of the position's size.
        #
        # Two limits keep the hatch from becoming the hazard it protects against, because
        # a plain order can open a reverse position rather than close anything:
        #   - it needs positive evidence that reduce-only itself was refused, not merely a
        #     rejection that was not recognised as a server error;
        #   - it is one attempt per position. Later bars keep emitting an exit while the
        #     position looks open, and re-sending a plain order on each of them is how a
        #     stale view turns into a new position.
        attempt = plan_close_attempt(
            reduce_only_refused=ctx.order_tracker.reduce_only_refused,
            plain_close_submitted=ctx.order_tracker.plain_close_submitted,
        )
        if attempt is CloseAttempt.PLAIN_ALREADY_SPENT:
            # This path runs once per bar, so unlike the one-shot containment paths it
            # sends nothing rather than falling back to the refused form -- re-sending on
            # every bar is the flood shape, and the plain form is not an option twice.
            s.log.warning(
                f"[{ctx.pair}] The single plain close for this position has already been "
                f"submitted; not re-sending. If the position is still open, its state at "
                f"the venue needs a look rather than another order",
            )
            return
        use_reduce_only = attempt is not CloseAttempt.PLAIN
        order = execution_manager.create_exit_order(
            instrument_id=ctx.instrument_id,
            signal=signal,
            size=Decimal(str(position.quantity)),
            reduce_only=use_reduce_only,
        )
        if order:
            _sig_id = cast(
                str | None, signal.metadata.get("_signal_id") if signal.metadata else None
            )
            dispatched = cast(bool | None, s.submit_order(order))
            if dispatched is False:
                s.log.warning(
                    f"[{ctx.pair}] EXIT was refused locally before dispatch; "
                    "the close attempt remains available",
                )
                return
            if _sig_id:
                s._order_signal_map[str(order.client_order_id)] = _sig_id
            if not use_reduce_only:
                ctx.order_tracker.mark_plain_close_submitted()
                s.log.warning(
                    f"[{ctx.pair}] Closing without reduce_only: the venue refused the "
                    f"reduce-only form for this position; size={position.quantity} taken from "
                    f"the open position. This is the one plain attempt",
                )
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
