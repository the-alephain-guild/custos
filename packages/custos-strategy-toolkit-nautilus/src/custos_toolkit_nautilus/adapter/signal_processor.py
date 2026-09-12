"""
Signal processor strategy for external signal handling.

Provides a NautilusTradingStrategy subclass that processes external signals,
supporting bidirectional signal flow:
- Receive external signals -> resolve fields -> execute trades
- Internal signals -> convert to OKX format -> emit for external consumption
"""

from collections import deque
from decimal import Decimal

from custos_toolkit.signals import Signal, SignalResolver
from nautilus_trader.model import Bar

from custos_toolkit_nautilus.adapter.pair_context import PairContext
from custos_toolkit_nautilus.adapter.trading_config import NautilusTradingStrategyConfig
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class SignalProcessorConfig(NautilusTradingStrategyConfig, frozen=True):
    """
    Configuration for SignalProcessorStrategy.

    Inherits all base strategy configuration and uses the signal section
    for OKX compatibility settings.
    """

    pass


class SignalProcessorStrategy(NautilusTradingStrategy):
    """
    External signal processor strategy.

    This strategy processes signals from external sources (e.g., TradingView, OKX Signal Bot)
    and can also emit internal signals to external consumers.

    Responsibilities:
    1. Receive external signals -> resolve missing fields -> execute trades
    2. Emit internal signals -> convert to OKX format -> provide for external use

    Usage:
        # Create strategy
        config = SignalProcessorConfig(...)
        strategy = SignalProcessorStrategy(config)

        # Receive external signal (e.g., from webhook)
        strategy.receive_external_signal(Signal.enter_long(100, pair="BTC-USDT"))

        # Or emit signal to OKX format
        signal = Signal.enter_long(100, pair="BTC-USDT")
        okx_json = strategy.emit_signal(signal)
    """

    def __init__(self, config: SignalProcessorConfig):
        """
        Initialize signal processor strategy.

        Args:
            config: Strategy configuration with signal settings
        """
        super().__init__(config)

        # Signal resolver for field completion and format conversion
        self._signal_resolver = SignalResolver(
            signal_config=config.signal if hasattr(config, "signal") else None,
            position_config=config.position if hasattr(config, "position") else None,
        )

        # Queue for pending external signals (per pair)
        self._pending_signals: dict[str, deque[Signal]] = {}

        # Last emitted signal for external consumption
        self._last_emitted_signal: dict[str, dict[str, object]] = {}

    # ABSTRACT METHOD IMPLEMENTATIONS

    def on_strategy_start(self) -> None:
        """Initialize signal queues for each pair."""
        for pair in self._configured_pairs():
            self._pending_signals[pair] = deque()
        self.log.info("SignalProcessorStrategy started, ready to receive external signals")

    def on_strategy_stop(self) -> None:
        """Clean up signal queues."""
        self._pending_signals.clear()
        self._last_emitted_signal.clear()
        self.log.info("SignalProcessorStrategy stopped")

    def on_reset(self) -> None:
        """Reset signal queues."""
        for queue in self._pending_signals.values():
            queue.clear()
        self._last_emitted_signal.clear()

    def _configured_pairs(self) -> list[str]:
        """Trading pairs source: config.trading.pairs (single source of truth).

        The base class no longer caches _pairs; pairs are derived from config. A
        leftover reference to the deleted field here used to raise AttributeError in
        on_strategy_start.
        """
        return list(self.config.trading.pairs)

    def calculate_signal(self, ctx: PairContext, bar: Bar) -> Signal:
        """
        Get next external signal for this pair's context.

        If there's a pending external signal, resolve and return it.
        Otherwise, return neutral signal.

        Args:
            ctx: PairContext (has .pair / .instrument_id; the external signal queue is
                keyed by ctx.pair)
            bar: Current bar data

        Returns:
            Resolved signal or neutral signal
        """
        pair = ctx.pair
        queue = self._pending_signals.get(pair)
        if queue and len(queue) > 0:
            signal = queue.popleft()
            # Ensure pair matches
            if signal.pair == pair or not signal.pair:
                resolved = self._signal_resolver.resolve(signal)
                # Update pair if not set
                if not resolved.pair:
                    resolved = Signal(
                        direction=resolved.direction,
                        price=resolved.price or Decimal(str(bar.close)),
                        strength=resolved.strength,
                        timestamp=resolved.timestamp,
                        pair=pair,
                        metadata=resolved.metadata,
                        investment_type=resolved.investment_type,
                        amount=resolved.amount,
                        order_type=resolved.order_type,
                        order_price_offset=resolved.order_price_offset,
                        max_lag=resolved.max_lag,
                        signal_token=resolved.signal_token,
                    )
                self.log.info(f"[{pair}] Processing external signal: {resolved.direction.name}")
                return resolved

        return Signal.neutral(price=Decimal(str(bar.close)), pair=pair)

    def get_indicator_history(self) -> dict[str, object]:
        """Return empty indicator history (no indicators in this strategy)."""
        return {}

    # EXTERNAL SIGNAL INTERFACE

    def receive_external_signal(self, signal: Signal) -> None:
        """
        Receive an external signal for processing.

        The signal will be queued and processed on the next bar event.
        Missing fields will be resolved using config defaults.

        Args:
            signal: External signal to process

        Example:
            # From webhook handler
            signal = Signal.enter_long(100, pair="BTC-USDT", amount=Decimal("500"))
            strategy.receive_external_signal(signal)
        """
        # Determine target pair
        pair = signal.pair
        pairs = self._configured_pairs()
        if not pair and pairs:
            pair = pairs[0]  # Default to first configured pair

        if pair not in self._pending_signals:
            self.log.warning(f"Received signal for unconfigured pair: {pair}")
            return

        self._pending_signals[pair].append(signal)
        self.log.info(
            f"[{pair}] External signal queued: {signal.direction.name}, "
            f"queue_size={len(self._pending_signals[pair])}"
        )

    def receive_okx_signal(self, data: dict[str, object]) -> None:
        """
        Receive a signal in OKX JSON format.

        The signal will be parsed, converted to internal format, and queued.

        Args:
            data: Signal data in OKX format

        Example:
            # From webhook handler
            data = {
                "action": "ENTER_LONG",
                "instrument": "BTC-USDT-SWAP",
                "signalToken": "...",
                "amount": "100",
                "investmentType": "percentage_investment"
            }
            strategy.receive_okx_signal(data)
        """
        try:
            signal = self._signal_resolver.from_okx_format(data)
            self.receive_external_signal(signal)
        except ValueError as e:
            self.log.error(f"Failed to parse OKX signal: {e}")

    # SIGNAL OUTPUT INTERFACE

    def emit_signal(self, signal: Signal) -> dict[str, object]:
        """
        Convert signal to OKX format for external consumption.

        Args:
            signal: Internal signal to convert

        Returns:
            Signal in OKX JSON format

        Example:
            signal = Signal.enter_long(100, pair="BTC-USDT", amount=Decimal("500"))
            okx_data = strategy.emit_signal(signal)
            # okx_data can be sent to OKX Signal Bot API
        """
        okx_data = self._signal_resolver.to_okx_format(signal)

        # Store for later retrieval
        pairs = self._configured_pairs()
        pair = signal.pair or (pairs[0] if pairs else "unknown")
        self._last_emitted_signal[pair] = okx_data

        self.log.info(f"[{pair}] Signal emitted: {okx_data['action']}")
        return okx_data

    def get_last_emitted_signal(self, pair: str | None = None) -> dict[str, object] | None:
        """
        Get the last emitted signal for a pair.

        Args:
            pair: Trading pair (uses first pair if None)

        Returns:
            Last emitted signal in OKX format, or None
        """
        if pair is None:
            pairs = self._configured_pairs()
            pair = pairs[0] if pairs else None
        if pair is None:
            return None
        return self._last_emitted_signal.get(pair)

    def get_pending_signal_count(self, pair: str | None = None) -> int:
        """
        Get number of pending signals for a pair.

        Args:
            pair: Trading pair (uses first pair if None)

        Returns:
            Number of pending signals
        """
        if pair is None:
            pairs = self._configured_pairs()
            pair = pairs[0] if pairs else None
        if pair is None:
            return 0
        queue = self._pending_signals.get(pair)
        return len(queue) if queue else 0
