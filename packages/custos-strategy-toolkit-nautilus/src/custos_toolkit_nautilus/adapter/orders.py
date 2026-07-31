# shared/nautilus/orders.py
"""
Order submitters for NautilusTrader stop loss and take profit orders.

Provides clean interfaces for creating SL/TP orders based on trading signals
and position state, separating order creation logic from strategy implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import TYPE_CHECKING, cast

from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit.signals.types import Signal, SignalDirection
from nautilus_trader.model.enums import (
    OrderSide,
    OrderType,
    TimeInForce,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from .runtime_types import Cache, Logger, Order, OrderFactory, Position

if TYPE_CHECKING:
    from .config.risk import ScaledTakeProfitConfig, StopLossTrailingConfig

# In-flight window (ns) that blocks re-submitting a full close after one is sent,
# a safety net against losing a fill and getting stuck. Shared by every close
# submission site (tick exit + bar exit) so a single position has one close in flight.
_CLOSE_INFLIGHT_TIMEOUT_NS: int = 5_000_000_000  # 5s


@dataclass
class OrderTracker:
    """
    Tracks pending entry and SL/TP order IDs for position management.

    Centralizes order ID tracking that was previously scattered across
    base_strategy.py state variables.
    """

    _sl_order_id: ClientOrderId | None = field(default=None, repr=False)
    _tp_order_ids: list[ClientOrderId] = field(default_factory=list, repr=False)
    _exchange_sl_order_id: ClientOrderId | None = field(default=None, repr=False)
    _entry_order_id: ClientOrderId | None = field(default=None, repr=False)
    # Direction of the pending entry order: 1 = long, -1 = short, 0 = none. Lets callers
    # tell a trend-aligned pending entry from a stale opposite one.
    _entry_side: int = field(default=0, repr=False)
    # Full-close in-flight / cooldown gate. 0 = a close may be submitted; > 0 = blocked
    # until this timestamp (ns). Set to now+timeout after submitting a close (in-flight
    # protection) and to now+cooldown after a close is rejected (backoff retry). Prevents
    # a reduce-only close order from flooding every tick and triggering -2022.
    _closing_deadline_ns: int = field(default=0, repr=False)
    # Consecutive logical-reject count for reduce-only close orders (-2022 etc.).
    # Incremented per logical reject; reset only on confirmed position close. A position the
    # venue can never reduce accumulates rejects until the halt threshold pauses the strategy
    # for manual intervention. Not touched by clear()/clear_closing() (those clear order-id
    # tracking after a reject/reversal, not on a real close) so the count survives to accumulate.
    _close_reject_count: int = field(default=0, repr=False)
    # Set only on positive evidence that the venue refused reduce-only itself, and only
    # this may drop reduce_only from the next close. Cleared on a confirmed close.
    _reduce_only_refused: bool = field(default=False, repr=False)
    # The plain close is one attempt per position, so this records that it was spent.
    _plain_close_submitted: bool = field(default=False, repr=False)

    def set_sl_order(self, order_id: ClientOrderId) -> None:
        """Set the stop loss order ID."""
        self._sl_order_id = order_id

    def add_tp_order(self, order_id: ClientOrderId) -> None:
        """Add a take profit order ID."""
        self._tp_order_ids.append(order_id)

    def set_tp_orders(self, order_ids: list[ClientOrderId]) -> None:
        """Set all take profit order IDs at once."""
        self._tp_order_ids = list(order_ids)

    def set_exchange_sl_order(self, order_id: ClientOrderId) -> None:
        """Set the exchange-managed stop loss order ID."""
        self._exchange_sl_order_id = order_id

    def set_entry_order(self, order_id: ClientOrderId, side: int = 0) -> None:
        """Set the pending entry order ID and its direction (1=long, -1=short)."""
        self._entry_order_id = order_id
        self._entry_side = side

    def clear_entry_order(self) -> None:
        """Clear only the entry order ID (e.g., after fill or cancel)."""
        self._entry_order_id = None
        self._entry_side = 0

    def clear(self) -> None:
        """Clear all tracked order IDs."""
        self._sl_order_id = None
        self._tp_order_ids = []
        self._exchange_sl_order_id = None
        self._entry_order_id = None
        self._entry_side = 0
        self._closing_deadline_ns = 0

    # Full-close in-flight / cooldown gate.

    def can_submit_close(self, now_ns: int) -> bool:
        """Return True when a new full-close order may be submitted.

        Returns False while blocked (in-flight not yet timed out / post-reject cooldown),
        so a reduce-only close order can't flood every tick.
        """
        return now_ns >= self._closing_deadline_ns

    def mark_closing(self, now_ns: int, timeout_ns: int) -> None:
        """Mark a full-close order in-flight; block re-submission until timeout.

        The timeout is a safety net: if a close order neither fills nor is rejected (a
        lost order), a retry is allowed after it, so the position can't get stuck unclosed.
        """
        self._closing_deadline_ns = now_ns + timeout_ns

    def set_close_cooldown(self, now_ns: int, cooldown_ns: int) -> None:
        """Apply a post-rejection backoff before the next close attempt."""
        self._closing_deadline_ns = now_ns + cooldown_ns

    def clear_closing(self) -> None:
        """Clear the close gate (e.g. on confirmed position close)."""
        self._closing_deadline_ns = 0

    # Consecutive close-reject count (feeds the halt threshold).

    def record_close_reject(self) -> None:
        """Increment the consecutive close-order reject count."""
        self._close_reject_count += 1

    def reset_close_rejects(self) -> None:
        """Reset close-rejection state (on confirmed position close).

        This clears the escape hatch too: the position it was opened for is gone, so a
        later close must start from the protective form again.
        """
        self._close_reject_count = 0
        self._reduce_only_refused = False
        self._plain_close_submitted = False

    # Escape hatch for a venue that refuses reduce-only on a position that is really
    # there. Kept separate from the reject count above, because that count also rises on
    # rejections whose reason is unknown, and an unknown reason is not evidence.

    def record_reduce_only_refusal(self) -> None:
        """Record that the venue specifically refused the reduce-only form."""
        self._reduce_only_refused = True

    @property
    def reduce_only_refused(self) -> bool:
        """Whether reduce-only was refused for the position currently open."""
        return self._reduce_only_refused

    def mark_plain_close_submitted(self) -> None:
        """Record that the one permitted plain close order has gone out."""
        self._plain_close_submitted = True

    @property
    def plain_close_submitted(self) -> bool:
        """Whether the single plain close attempt has already been made.

        A plain close can open a reverse position, so it is one attempt per position,
        not one per bar: every later bar still emits an exit while the position looks
        open, and repeating an order that no longer has anything to reduce is how a
        stale view becomes a new position.
        """
        return self._plain_close_submitted

    @property
    def close_reject_count(self) -> int:
        """Consecutive close-order rejects since the last confirmed position close."""
        return self._close_reject_count

    def remove_order(self, order_id: ClientOrderId) -> None:
        """Remove an order ID from tracking."""
        if self._sl_order_id is not None and self._sl_order_id == order_id:
            self._sl_order_id = None
        if order_id in self._tp_order_ids:
            self._tp_order_ids.remove(order_id)
        if self._exchange_sl_order_id is not None and self._exchange_sl_order_id == order_id:
            self._exchange_sl_order_id = None
        if self._entry_order_id is not None and self._entry_order_id == order_id:
            self._entry_order_id = None
            self._entry_side = 0

    @property
    def sl_order_id(self) -> ClientOrderId | None:
        """Get the stop loss order ID."""
        return self._sl_order_id

    @property
    def tp_order_ids(self) -> list[ClientOrderId]:
        """Get a copy of take profit order IDs."""
        return self._tp_order_ids.copy()

    @property
    def exchange_sl_order_id(self) -> ClientOrderId | None:
        """Get the exchange-managed stop loss order ID."""
        return self._exchange_sl_order_id

    @property
    def entry_order_id(self) -> ClientOrderId | None:
        """Get the pending entry order ID."""
        return self._entry_order_id

    @property
    def entry_side(self) -> int:
        """Direction of the pending entry order (1=long, -1=short, 0=none)."""
        return self._entry_side

    @property
    def has_pending_orders(self) -> bool:
        """Check if any orders are being tracked."""
        return (
            self._sl_order_id is not None
            or len(self._tp_order_ids) > 0
            or self._exchange_sl_order_id is not None
            or self._entry_order_id is not None
        )


# Stale-order sweep thresholds:
# cancel_all_orders() during reversal is fire-and-forget — a venue failure
# (e.g. demo-fapi transport error) leaves the old SL resting with no retry,
# while the tracker has already been cleared. The per-bar sweep reconciles
# cache state and cancels orphaned reduce-only orders.
STALE_ORDER_MIN_AGE_NS: int = 30_000_000_000  # 30s — don't race in-flight orders
STALE_SWEEP_RETRY_COOLDOWN_NS: int = 120_000_000_000  # 120s — per-order cancel rate guard


def is_stale_order(
    order: Order,
    *,
    position_is_long: bool | None,
    tracked_ids: set[ClientOrderId],
    sl_is_tracked: bool,
    now_ns: int,
    min_age_ns: int = STALE_ORDER_MIN_AGE_NS,
) -> bool:
    """Classify whether an open order is a stale orphan that should be cancelled.

    Scope: reduce-only orders (SL/TP) only — pending entry orders are managed
    by SignalExecutionCoordinator._cancel_pending_entry_order().

    Rules:
    - tracked by OrderTracker or younger than min_age_ns -> keep (in-flight)
    - no open position -> any untracked reduce-only order is an orphan -> cancel
    - wrong side (cannot reduce the current position) -> cancel
      (the reversal-orphan case: position flipped, old SL side is now invalid)
    - protective-side STOP_MARKET or TRAILING_STOP_MARKET while an SL is already
      tracked -> duplicate -> cancel (failed/duplicate trailing-SL replacement;
      reduce-only capacity hazard)
    - protective-side stop with no tracked SL -> keep (restart recovery:
      OrderReconciler.ensure_exchange_sl_exists() re-tracks it); protective-side
      LIMIT (TP) -> keep (restart TPs are intentionally preserved)

    Args:
        order: Open order (duck-typed: client_order_id, side, order_type,
            is_reduce_only, ts_init)
        position_is_long: Direction of the open position, None when flat
        tracked_ids: client_order_ids currently tracked by OrderTracker
        sl_is_tracked: Whether the tracker holds an SL (sl or exchange_sl)
        now_ns: Current timestamp (ns)
        min_age_ns: Minimum order age before it may be swept

    Returns:
        True when the order should be cancelled as a stale orphan
    """
    if not getattr(order, "is_reduce_only", False):
        return False
    if order.client_order_id in tracked_ids:
        return False
    if now_ns - getattr(order, "ts_init", 0) < min_age_ns:
        return False
    if position_is_long is None:
        return True
    protective_side = OrderSide.SELL if position_is_long else OrderSide.BUY
    if order.side != protective_side:
        return True
    if (
        order.order_type in (OrderType.STOP_MARKET, OrderType.TRAILING_STOP_MARKET)
        and sl_is_tracked
    ):
        return True
    return False


def align_stop_price_to_tick(price: Decimal, tick_size: Decimal, side: OrderSide) -> Decimal:
    """
    Align stop trigger price to tick size.

    For stop orders, we want to trigger earlier (more conservative):
    - SELL stop (closing long): round DOWN so stop triggers earlier
    - BUY stop (closing short): round UP so stop triggers earlier

    Args:
        price: The stop trigger price to align
        tick_size: The tick size (price increment)
        side: Order side (the stop order side, not position direction)

    Returns:
        Price aligned to tick size
    """
    if side == OrderSide.SELL:
        # Round down for SELL stop - triggers earlier when price falls
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size
    else:
        # Round up for BUY stop - triggers earlier when price rises
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_UP) * tick_size


def align_limit_price_to_tick(price: Decimal, tick_size: Decimal, side: OrderSide) -> Decimal:
    """
    Align limit price to tick size.

    For limit orders (take profit):
    - SELL limit: round UP (want to receive more)
    - BUY limit: round DOWN (willing to pay less)

    Args:
        price: The limit price to align
        tick_size: The tick size (price increment)
        side: Order side

    Returns:
        Price aligned to tick size
    """
    if side == OrderSide.SELL:
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_UP) * tick_size
    else:
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size


class StopLossSubmitter:
    """
    Create stop loss orders for open positions.

    Uses OrderPriceCalculator to determine stop price based on
    fixed percentage or ATR-based calculations.

    Attributes:
        _order_factory: Nautilus order factory for creating orders
        _cache: Nautilus cache for accessing instruments
        _log: Logger for error/info messages
        _order_calculator: Calculator for stop loss prices
    """

    def __init__(
        self,
        order_factory: OrderFactory,
        cache: Cache,
        log: Logger,
        order_calculator: OrderPriceCalculator,
    ) -> None:
        """
        Initialize with Nautilus components.

        Args:
            order_factory: Nautilus order factory
            cache: Nautilus cache for instrument lookup
            log: Logger instance
            order_calculator: Calculator for stop loss prices
        """
        self._order_factory = order_factory
        self._cache = cache
        self._log = log
        self._order_calculator = order_calculator

    def create_order(
        self,
        instrument_id: InstrumentId,
        signal: Signal,
        entry_price: Decimal,
        atr: Decimal | None,
        position: Position,
        tags: list[str] | None = None,
    ) -> Order | None:
        """
        Create stop loss order.

        Calculates stop price using order_calculator and creates a stop market order.
        - Long position -> OrderSide.SELL stop
        - Short position -> OrderSide.BUY stop

        Args:
            instrument_id: The instrument to trade
            signal: Trading signal with direction
            entry_price: Entry price for the position (Decimal)
            atr: Current ATR value (Decimal, required for ATR-based stops)
            position: Open position to protect

        Returns:
            Stop market order or None if price is None or instrument not found
        """
        # Calculate stop price
        stop_price = self._order_calculator.calculate_stop_loss(
            entry_price=entry_price,
            direction=signal.direction,
            atr=atr,
        )

        if stop_price is None:
            return None

        # Determine side from signal direction (not position, which may be from previous trade)
        is_long = signal.direction == SignalDirection.ENTER_LONG
        side = OrderSide.SELL if is_long else OrderSide.BUY

        return self.create_order_from_price(
            instrument_id=instrument_id,
            side=side,
            quantity=position.quantity,
            stop_price=stop_price,
            tags=tags,  # pass-through signal_id tag
        )

    def create_order_from_price(
        self,
        instrument_id: InstrumentId,
        side: OrderSide,
        quantity: Quantity,
        stop_price: Decimal,
        tags: list[str] | None = None,
    ) -> Order | None:
        """Build a reduce-only stop-market order at a given (pre-computed) stop price.

        A shared order-building primitive that separates price calculation from order
        construction: each caller computes its own price (``create_order`` uses the
        order_calculator; break-even uses entry; safety uses max_loss_pct), and the
        construction part (instrument lookup + side-aware tick alignment + reduce-only
        stop_market) is unified here. Removes the break-even/safety SL inconsistency of
        hand-rolling ``order_factory.stop_market``.

        Args:
            instrument_id: The instrument
            side: Stop order side (SELL protects a long / BUY protects a short)
            quantity: Stop quantity (usually position.quantity)
            stop_price: Pre-computed trigger price (not yet aligned)
            tags: Pass-through signal_id tag

        Returns:
            Stop market order; None when the instrument is missing.
        """
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {instrument_id}")
            return None

        tick_size = Decimal(str(instrument.price_increment))
        aligned_stop_price = align_stop_price_to_tick(stop_price, tick_size, side)

        return self._order_factory.stop_market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=quantity,
            trigger_price=Price(cast(float, aligned_stop_price), instrument.price_precision),
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=tags,
        )


# Binance Futures callbackRate is bounded to [0.1%, 10%]; with offset in
# BASIS_POINTS, callbackRate = trailing_offset_bps / 100 = trailing_pct × 100,
# so trailing_pct must be in [0.001, 0.10] (adapters/binance
# execution.py:986-992 + common/constants.py:30-31). Out-of-range configs are
# rejected fail-fast (no silent clamp) — prefer reject over
# mis-configure.
NATIVE_TRAILING_MIN_PCT: Decimal = Decimal("0.001")
NATIVE_TRAILING_MAX_PCT: Decimal = Decimal("0.10")

# trigger_price_type (config) -> nautilus TriggerType. Binance Futures trailing
# accepts only DEFAULT / LAST_PRICE / MARK_PRICE; mark price is the
# default for contract protective orders to avoid last-price wicks.
_TRIGGER_PRICE_TYPE_MAP: dict[str, TriggerType] = {
    "default": TriggerType.DEFAULT,
    "last": TriggerType.LAST_PRICE,
    "mark": TriggerType.MARK_PRICE,
}


class NativeTrailingStopSubmitter:
    """
    Create exchange-managed TrailingStopMarketOrder for open positions.

    Submits a Binance-Futures-managed trailing stop (algo order) that the venue
    tracks server-side, replacing the hand-written per-tick trailing loop that
    was the reduce-only flood source. Fixed-percentage trailing only:

    - offset is BASIS_POINTS: ``trailing_offset = trailing_pct × 10000`` (bps),
      i.e. ``callbackRate = trailing_pct × 100`` (%).
    - callbackRate fail-fast: ``trailing_pct`` must be within [0.001, 0.10];
      out-of-range configs are rejected (returns None, logs error) rather than
      silently clamped.
    - ``activation_price`` is computed explicitly from entry
      (``entry × (1 ± activation_pct)``, tick-aligned) because the Binance
      adapter does NOT enforce it.
    - ``trigger_price`` is never passed (the adapter rejects it,
      INVALID_TRIGGER_PRICE); orders are ``reduce_only``.

    Note: parameter mapping from the hand-written
    ``TrailingStopManager`` is static; behavioral equivalence (activation-window,
    gap-through, long/short, LAST vs MARK) is gated on the empirical test matrix
    and NOT asserted here.

    Attributes:
        _order_factory: Nautilus order factory for creating orders
        _cache: Nautilus cache for accessing instruments
        _log: Logger for error/info messages
    """

    def __init__(self, order_factory: OrderFactory, cache: Cache, log: Logger) -> None:
        """
        Initialize with Nautilus components.

        Args:
            order_factory: Nautilus order factory
            cache: Nautilus cache for instrument lookup
            log: Logger instance
        """
        self._order_factory = order_factory
        self._cache = cache
        self._log = log

    def create_order(
        self,
        instrument_id: InstrumentId,
        signal: Signal,
        entry_price: Decimal,
        position: Position,
        trailing_cfg: StopLossTrailingConfig,
        tags: list[str] | None = None,
    ) -> Order | None:
        """
        Create an exchange-managed trailing stop market order.

        - Long position  -> OrderSide.SELL trailing stop
        - Short position -> OrderSide.BUY trailing stop

        Args:
            instrument_id: The instrument to trade
            signal: Trading signal with direction (used for side)
            entry_price: Entry price of the position (Decimal), used to derive
                activation_price
            position: Open position to protect (provides quantity)
            trailing_cfg: StopLossTrailingConfig-like object with
                ``trailing_pct``, ``activation_pct``, ``trigger_price_type``
            tags: Optional order tags (e.g. signal_id tag)

        Returns:
            TrailingStopMarketOrder, or None if trailing_pct is out of range
            (fail-fast) or the instrument is not found.
        """
        # fail-fast: callbackRate boundary (no silent clamp — prefer reject)
        trailing_pct = Decimal(str(trailing_cfg.trailing_pct))
        if trailing_pct < NATIVE_TRAILING_MIN_PCT or trailing_pct > NATIVE_TRAILING_MAX_PCT:
            self._log.error(
                f"native_trailing: trailing_pct={trailing_pct} out of Binance callbackRate "
                f"range [0.1%, 10%] (trailing_pct must be in [0.001, 0.10]); rejecting order "
                f"(fail-fast, no silent clamp)"
            )
            return None

        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {instrument_id}")
            return None

        # Side from signal direction (not position, which may be from a prior trade)
        is_long = signal.direction == SignalDirection.ENTER_LONG
        side = OrderSide.SELL if is_long else OrderSide.BUY

        # offset in BASIS_POINTS: trailing_pct × 10000 bps
        trailing_offset = trailing_pct * Decimal("10000")

        # activation_price = entry × (1 ± activation_pct), tick-aligned
        activation_pct = Decimal(str(trailing_cfg.activation_pct))
        entry = Decimal(str(entry_price))
        if is_long:
            raw_activation = entry * (Decimal("1") + activation_pct)
        else:
            raw_activation = entry * (Decimal("1") - activation_pct)
        tick_size = Decimal(str(instrument.price_increment))
        aligned_activation = align_stop_price_to_tick(raw_activation, tick_size, side)
        activation_price = Price(cast(float, aligned_activation), instrument.price_precision)

        # trigger_type: mark/last/default, fall back to MARK_PRICE on unknown value
        trigger_price_type = str(getattr(trailing_cfg, "trigger_price_type", "mark")).lower()
        trigger_type = _TRIGGER_PRICE_TYPE_MAP.get(trigger_price_type)
        if trigger_type is None:
            self._log.warning(
                f"native_trailing: invalid trigger_price_type={trigger_price_type!r}; "
                f"falling back to MARK_PRICE (allowed: default/last/mark)"
            )
            trigger_type = TriggerType.MARK_PRICE

        return self._order_factory.trailing_stop_market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=position.quantity,
            trailing_offset=trailing_offset,
            activation_price=activation_price,
            trigger_type=trigger_type,
            trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=tags,  # signal_id tag passthrough
        )


class TakeProfitSubmitter:
    """
    Create take profit orders for open positions.

    Supports both single take profit orders and scaled exits
    at multiple price levels.

    Attributes:
        _order_factory: Nautilus order factory for creating orders
        _cache: Nautilus cache for accessing instruments
        _log: Logger for error/info messages
        _order_calculator: Calculator for take profit prices
    """

    def __init__(
        self,
        order_factory: OrderFactory,
        cache: Cache,
        log: Logger,
        order_calculator: OrderPriceCalculator,
    ) -> None:
        """
        Initialize with Nautilus components.

        Args:
            order_factory: Nautilus order factory
            cache: Nautilus cache for instrument lookup
            log: Logger instance
            order_calculator: Calculator for take profit prices
        """
        self._order_factory = order_factory
        self._cache = cache
        self._log = log
        self._order_calculator = order_calculator

    def create_single_order(
        self,
        instrument_id: InstrumentId,
        signal: Signal,
        entry_price: Decimal,
        atr: Decimal | None,
        stop_loss: Decimal | None,
        position: Position,
        tags: list[str] | None = None,
    ) -> Order | None:
        """
        Create single take profit limit order.

        Calculates TP price using order_calculator and creates a limit order.
        - Long position -> OrderSide.SELL limit
        - Short position -> OrderSide.BUY limit

        Args:
            instrument_id: The instrument to trade
            signal: Trading signal with direction
            entry_price: Entry price for the position (Decimal)
            atr: Current ATR value (Decimal, for ATR-based TP)
            stop_loss: Stop loss price (Decimal, for risk/reward calculation)
            position: Open position to take profit on

        Returns:
            Limit order or None if price is None or instrument not found
        """
        # Calculate take profit price
        tp_price = self._order_calculator.calculate_take_profit(
            entry_price=entry_price,
            direction=signal.direction,
            atr=atr,
            stop_loss=stop_loss,
        )

        if tp_price is None:
            return None

        # Get instrument for price precision
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {instrument_id}")
            return None

        # Determine side from signal direction (not position, which may be from previous trade)
        is_long = signal.direction == SignalDirection.ENTER_LONG
        side = OrderSide.SELL if is_long else OrderSide.BUY

        # Align take profit price to tick size
        tick_size = Decimal(str(instrument.price_increment))
        aligned_tp_price = align_limit_price_to_tick(tp_price, tick_size, side)

        return self._order_factory.limit(
            instrument_id=instrument_id,
            order_side=side,
            quantity=position.quantity,
            price=Price(cast(float, aligned_tp_price), instrument.price_precision),
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=tags,  # pass-through signal_id tag
        )

    def create_scaled_orders(
        self,
        instrument_id: InstrumentId,
        signal: Signal,
        entry_price: Decimal,
        position: Position,
        scaled_config: ScaledTakeProfitConfig,
        tags: list[str] | None = None,
    ) -> list[Order]:
        """
        Create scaled take profit orders at multiple price levels.

        Creates multiple limit orders, each closing a portion of the position
        at progressively better prices.

        scaled_config should have:
        - levels: int (number of levels)
        - level_1, level_2, level_3: objects with target_pct, exit_pct

        For each level up to `levels`:
        - Calculate TP price: entry * (1 + target_pct) for long, (1 - target_pct) for short
        - Calculate qty: total_qty * exit_pct

        Args:
            instrument_id: The instrument to trade
            signal: Trading signal with direction
            entry_price: Entry price for the position (Decimal)
            position: Open position to take profit on
            scaled_config: Configuration with level details

        Returns:
            List of limit orders (may be empty if instrument not found)
        """
        # Get instrument for price precision
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {instrument_id}")
            return []

        is_long = signal.direction == SignalDirection.ENTER_LONG
        side = OrderSide.SELL if is_long else OrderSide.BUY
        total_qty = Decimal(str(position.quantity))

        # Get tick size for price alignment
        tick_size = Decimal(str(instrument.price_increment))

        # Build list from individual level fields
        scaled_levels = [
            scaled_config.level_1,
            scaled_config.level_2,
            scaled_config.level_3,
        ][: scaled_config.levels]

        # Validate target_pct ascending order
        prev_target = Decimal("0")
        for i, level in enumerate(scaled_levels, start=1):
            target_pct = Decimal(str(level.target_pct))
            if target_pct <= prev_target:
                self._log.error(
                    f"Invalid scaled TP: level_{i} target_pct ({target_pct}) "
                    f"must be > level_{i - 1} ({prev_target})"
                )
                return []
            prev_target = target_pct

        orders: list[Order] = []
        for level in scaled_levels:
            target_pct = Decimal(str(level.target_pct))
            exit_pct = Decimal(str(level.exit_pct))

            # Calculate price based on direction
            if is_long:
                tp_price = entry_price * (1 + target_pct)
            else:
                tp_price = entry_price * (1 - target_pct)

            # Align take profit price to tick size
            aligned_tp_price = align_limit_price_to_tick(tp_price, tick_size, side)

            # Calculate quantity for this level
            scale_qty = total_qty * exit_pct

            order = self._order_factory.limit(
                instrument_id=instrument_id,
                order_side=side,
                quantity=instrument.make_qty(scale_qty),
                price=Price(cast(float, aligned_tp_price), instrument.price_precision),
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
                tags=tags,  # pass-through signal_id tag
            )
            orders.append(order)

        return orders
