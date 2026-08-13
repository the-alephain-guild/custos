"""SL/TP execution mode taxonomy with per-mode behavior.

Consolidates the previously scattered ``sl_tp_mode`` string dispatch (~22 call
sites across the strategy base class, OrderReconciler, and ConfigSummaryLogger)
into a single typed enum. Capability properties replace the boolean guard
branches; ``on_entry_filled`` is the single colocated dispatch for the post-fill
protection differences between modes.

The string values match ``SL_TP_MODES`` in ``custos_toolkit_nautilus.adapter.config.risk`` so a
mode can be built straight from config: ``SLTPMode(config.risk.trade.sl_tp_mode)``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from custos_toolkit.signals.types import SignalDirection

if TYPE_CHECKING:
    from custos_toolkit.signals.types import Signal
    from nautilus_trader.model.objects import Quantity

    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    from .runtime_types import Position


class SLTPMode(str, Enum):  # noqa: UP042 - preserve pre-T4b str(Enum) runtime semantics
    """How stop-loss / take-profit protection is placed and managed.

    - ``EXCHANGE``: SL and TP both submitted as resting exchange orders.
    - ``TICK``: protection handled by tick-level monitoring (no exchange SL).
    - ``HYBRID``: safety SL on the exchange, TP / trailing via tick monitoring.
    - ``NATIVE_TRAILING``: a single exchange-managed trailing stop is the
      protective order; no independent tick SL, take-profit, or break-even.
    """

    EXCHANGE = "exchange"
    TICK = "tick"
    HYBRID = "hybrid"
    NATIVE_TRAILING = "native_trailing"

    @property
    def uses_native_trailing(self) -> bool:
        """Whether this mode places an exchange-managed trailing stop."""
        return self is SLTPMode.NATIVE_TRAILING

    @property
    def uses_tick_monitor(self) -> bool:
        """Whether this mode builds and feeds a tick monitor (init + process)."""
        return self in (SLTPMode.TICK, SLTPMode.HYBRID)

    @property
    def subscribes_tick_stream(self) -> bool:
        """Whether tick stream subscription is worthwhile.

        native_trailing exits are venue-managed (tick handlers early-return), so
        subscribing the tick stream would only waste bandwidth.
        """
        return self is not SLTPMode.NATIVE_TRAILING

    @property
    def allows_break_even(self) -> bool:
        """Whether break-even stop adjustment applies.

        native_trailing's trailing stop is itself a dynamic stop; a separate
        break-even stop_market would be untracked and conflict with it.
        """
        return self is not SLTPMode.NATIVE_TRAILING

    @property
    def uses_exchange_sl(self) -> bool:
        """Whether this mode keeps a resting exchange stop-loss order."""
        return self in (SLTPMode.EXCHANGE, SLTPMode.HYBRID)

    def on_entry_filled(
        self,
        strategy: NautilusTradingStrategy,
        ctx: PairContext,
        signal: Signal,
        position: Position | None,
        entry_px: Decimal | float,
        entry_atr: Decimal | float | None,
        *,
        protection_quantity: Quantity | Decimal | None = None,
        initialize_position: bool = True,
    ) -> None:
        """Submit post-fill protection for an entry, per this mode.

        ``entry_px`` is the raw fill price; it is converted to ``Decimal`` only on
        the tick/hybrid path that seeds the tick monitor, matching the original
        lazy-conversion behavior (exchange/native_trailing never touch it).
        """
        quantity_kwargs = {} if protection_quantity is None else {"quantity": protection_quantity}
        if self is SLTPMode.EXCHANGE:
            strategy._sltp_coordinator.submit_stop_loss(ctx, signal, **quantity_kwargs)
            strategy._sltp_coordinator.submit_take_profit(ctx, signal, **quantity_kwargs)
        elif self is SLTPMode.TICK and initialize_position:
            _init_tick_position(ctx, signal, position, entry_px, entry_atr)
        elif self is SLTPMode.HYBRID:
            strategy._sltp_coordinator.submit_safety_stop_loss(ctx, signal, **quantity_kwargs)
            if initialize_position:
                _init_tick_position(ctx, signal, position, entry_px, entry_atr)
        elif self is SLTPMode.NATIVE_TRAILING:
            strategy._sltp_coordinator.submit_native_trailing(ctx, signal, **quantity_kwargs)


def _init_tick_position(
    ctx: PairContext,
    signal: Signal,
    position: Position | None,
    entry_px: Decimal | float,
    entry_atr: Decimal | float | None,
) -> None:
    """Seed the tick monitor with the just-opened position (tick/hybrid)."""
    if position is None or ctx.tick_monitor is None:
        return
    is_long = signal.direction == SignalDirection.ENTER_LONG
    ctx.tick_monitor.init_position(
        entry_price=Decimal(str(entry_px)), is_long=is_long, entry_atr=entry_atr
    )
