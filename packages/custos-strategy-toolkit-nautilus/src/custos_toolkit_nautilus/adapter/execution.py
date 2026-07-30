"""
Execution manager for NautilusTrader order creation.

Provides a clean interface for creating entry and exit orders
based on trading signals, separating order creation logic from
strategy implementation.
"""

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import cast

from custos_toolkit.signals.types import Signal, SignalDirection
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price

from .runtime_types import Cache, Logger, Order, OrderFactory


def align_to_tick_size(price: Decimal, tick_size: Decimal, side: OrderSide) -> Decimal:
    """
    Align price to tick size.

    For BUY orders, round down (more conservative limit - willing to pay less).
    For SELL orders, round up (more conservative limit - want to receive more).

    Args:
        price: The price to align
        tick_size: The tick size (price increment)
        side: Order side (BUY or SELL)

    Returns:
        Price aligned to tick size
    """
    if side == OrderSide.BUY:
        # Round down for BUY - more conservative (lower price)
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size
    else:
        # Round up for SELL - more conservative (higher price)
        return (price / tick_size).quantize(Decimal("1"), rounding=ROUND_UP) * tick_size


class ExecutionManager:
    """
    Manage order creation and execution.

    This class encapsulates the logic for translating trading signals
    into Nautilus orders, handling both market and limit order types.

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

    def create_entry_order(
        self,
        instrument_id: InstrumentId,
        signal: Signal,
        size: Decimal,
        bar: Bar,
        order_type: str = "market",
        slippage_tolerance: Decimal = Decimal("0.001"),
        price_offset: Decimal | None = None,
        tags: list[str] | None = None,
    ) -> Order | None:
        """
        Create entry order based on signal.

        Translates signal direction to order side:
        - ENTER_LONG -> OrderSide.BUY
        - ENTER_SHORT -> OrderSide.SELL
        - Other directions -> return None

        For limit orders, price is offset by price_offset (if provided) or slippage_tolerance:
        - BUY: limit_price = price * (1 - offset)
        - SELL: limit_price = price * (1 + offset)

        Args:
            instrument_id: The instrument to trade
            signal: Trading signal with direction
            size: Order size in quote currency (e.g., USDT notional value)
            bar: Current bar for price reference
            order_type: "market" or "limit"
            slippage_tolerance: Default price offset for limit orders (Decimal, e.g., 0.001 = 0.1%)
            price_offset: Override price offset from Signal (Decimal, e.g., 0.05 = 0.05%)
                          Takes precedence over slippage_tolerance when provided.
            tags: Optional list of string tags to attach to the order
                  (e.g., ["signal_id:abc-123"] for signal-to-order correlation)

        Returns:
            Order object or None if signal is not an entry signal
            or instrument is not found
        """
        # Determine order side from signal direction
        if signal.direction == SignalDirection.ENTER_LONG:
            side = OrderSide.BUY
        elif signal.direction == SignalDirection.ENTER_SHORT:
            side = OrderSide.SELL
        else:
            return None

        # Get instrument for quantity and price precision
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {instrument_id}")
            return None

        # Convert quote currency (USDT) notional to base currency (BTC) quantity
        # size is in USDT, we need to convert to BTC: qty = notional / price
        price = Decimal(str(bar.close))
        contract_qty = size / price

        # Round down to instrument precision to avoid exceeding limits
        rounded_qty = contract_qty.quantize(
            Decimal(10) ** -instrument.size_precision, rounding=ROUND_DOWN
        )
        quantity = instrument.make_qty(rounded_qty)

        if order_type == "market":
            return self._order_factory.market(
                instrument_id=instrument_id,
                order_side=side,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
                tags=tags,
            )
        elif order_type == "limit":
            # Use price_offset if provided, otherwise fall back to slippage_tolerance
            # price_offset is typically a percentage (e.g., 0.05 = 0.05%), convert to decimal
            effective_offset = (
                price_offset / Decimal("100") if price_offset is not None else slippage_tolerance
            )
            if side == OrderSide.BUY:
                limit_price = price * (Decimal("1") - effective_offset)
            else:
                limit_price = price * (Decimal("1") + effective_offset)

            # Align limit price to tick size
            tick_size = Decimal(str(instrument.price_increment))
            aligned_price = align_to_tick_size(limit_price, tick_size, side)

            return self._order_factory.limit(
                instrument_id=instrument_id,
                order_side=side,
                quantity=quantity,
                price=Price(cast(float, aligned_price), instrument.price_precision),
                time_in_force=TimeInForce.GTC,
                tags=tags,
            )

        return None

    def create_exit_order(
        self,
        instrument_id: InstrumentId,
        signal: Signal,
        size: Decimal,
        reduce_only: bool = True,
    ) -> Order | None:
        """
        Create exit order (always market; reduce-only unless the venue refused it).

        reduce_only protects the money path: if an exit is re-submitted (e.g. after the
        close gate's in-flight window) while the local cache still lags a fill that
        already closed the position, the venue rejects the duplicate instead of opening
        a reverse position.

        Callers pass ``reduce_only=False`` only after the venue has already refused the
        reduce-only form for this position on the logic tier. Binance's demo engine does
        that while the position is genuinely open, and retrying the refused form cannot
        succeed -- so the plain order is the only way to close. That trade is deliberate:
        it drops the protection described above, which is why the caller must first
        establish that the position is still open.

        Translates signal direction to order side:
        - EXIT_LONG -> OrderSide.SELL (close long position)
        - EXIT_SHORT -> OrderSide.BUY (close short position)
        - Other directions -> return None

        Args:
            instrument_id: The instrument to trade
            signal: Trading signal with direction
            size: Order size (Decimal)

        Returns:
            Market order or None if signal is not an exit signal
            or instrument is not found
        """
        # Determine order side from signal direction
        if signal.direction == SignalDirection.EXIT_LONG:
            side = OrderSide.SELL
        elif signal.direction == SignalDirection.EXIT_SHORT:
            side = OrderSide.BUY
        else:
            return None

        # Get instrument for quantity precision
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Instrument not found: {instrument_id}")
            return None

        # Round down to instrument precision to avoid exceeding limits
        rounded_size = size.quantize(Decimal(10) ** -instrument.size_precision, rounding=ROUND_DOWN)
        quantity = instrument.make_qty(rounded_size)

        return self._order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
        )
