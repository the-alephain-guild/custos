"""Position-sizing component.

Holds the position-size computation: the default notional paradigm (PositionSizer
base size + signal-strength + limits) and the native fixed-risk path
(FixedRiskSizer off the stop-loss distance). Injects a strategy reference and
reaches ``config`` / ``cache`` / ``log`` / ``_position_sizer`` /
``_order_calculator`` / ``_get_effective_capital`` / ``_base_size_factor`` through
it.

The ``calculate_position_size`` hook stays on the Strategy class (subclasses
override it); its default body delegates to ``default_position_size`` here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

import msgspec
from nautilus_trader.common import LogColor

from custos_toolkit_nautilus.adapter.sizing import compute_fixed_risk_qty

if TYPE_CHECKING:
    from custos_toolkit.signals.types import Signal

    from custos_toolkit_nautilus.adapter.pair_context import PairContext
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class _ValueIndicator(Protocol):
    @property
    def value(self) -> object: ...


class SizingCoordinator:
    """Position-size computation (default notional + fixed-risk paths).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def default_position_size(self, ctx: PairContext, signal: Signal) -> Decimal:
        """Calculate position size using shared PositionSizer.

        Note: This method does not currently support per-pair entry count scaling.
        Override calculate_position_size() for pair-specific sizing logic.
        """
        s = self._strategy
        pos_config = s.config.position
        # Position scale: fixed_risk sizes off the stop-loss distance via the native
        # FixedRiskSizer; percentage/fixed/kelly use the notional paradigm.
        if pos_config.size_type == "fixed_risk":
            return self._fixed_risk_position_size(ctx, signal)

        equity = s._get_effective_capital()
        base_size = s._position_sizer.calculate_base_size(equity)
        # Signal strength is still float from Signal class, convert at boundary
        adjusted_size = s._position_sizer.apply_signal_strength(
            base_size, Decimal(str(signal.strength))
        )

        # Apply per-pair size reduction factor (set by pair filters,
        # avoids cross-pair contamination)
        adjusted_size = adjusted_size * Decimal(str(ctx.size_reduction_factor))
        ctx.size_reduction_factor = s._base_size_factor  # Reset to base factor

        # TODO: entry_count scaling requires pair context, not implemented in default
        # Subclasses can override calculate_position_size(ctx, signal) for pair-specific logic

        limits = msgspec.structs.asdict(pos_config.limits)
        return s._position_sizer.check_limits(adjusted_size, equity, limits)

    def _fixed_risk_position_size(self, ctx: PairContext, signal: Signal) -> Decimal:
        """Size via native FixedRiskSizer (risk_pct of equity off stop-loss distance).

        Returns the position **notional** (quote currency) so it integrates with the
        existing entry pipeline (``create_entry_order`` recovers the base qty by
        dividing by price). Runtime guards return 0 on missing instrument/price/SL;
        the hard config guarantee (fixed_risk requires a stop-loss) is enforced by
        the on_start fail-fast.

        Risk uses ``signal.price`` as the entry estimate. When the actual fill
        differs (limit-order offset, or ``signal.price`` != bar close), the
        realized risk deviates from ``risk_pct`` by the price gap -- a bounded
        approximation this sizing mode accepts.
        """
        s = self._strategy
        instrument = s.cache.instrument(ctx.instrument_id)
        if instrument is None:
            s.log.warning(f"[{ctx.pair}] fixed_risk: instrument unavailable; size=0")
            return Decimal(0)

        entry_price = Decimal(str(signal.price))
        if entry_price <= 0:
            s.log.warning(f"[{ctx.pair}] fixed_risk: invalid entry price; size=0")
            return Decimal(0)

        atr = None
        if "atr" in ctx.indicators:
            atr_value = cast(_ValueIndicator, ctx.indicators["atr"]).value
            atr = Decimal(str(atr_value)) if atr_value else None

        sl_price = s._order_calculator.calculate_stop_loss(entry_price, signal.direction, atr)
        if sl_price is None or sl_price <= 0:
            s.log.warning(f"[{ctx.pair}] fixed_risk: no stop-loss price; size=0")
            return Decimal(0)

        equity = s._get_effective_capital()
        pos_config = s.config.position
        risk_pct = pos_config.fixed_risk.risk_pct
        qty = compute_fixed_risk_qty(instrument, entry_price, sl_price, equity, risk_pct)

        # Express the base qty as notional (quote) for the existing entry pipeline.
        notional = Decimal(str(qty)) * entry_price

        # Apply the same position-limit safety caps as the notional paradigm
        # (max_position_pct / max_trade_size / min_order_size) so a tight stop-loss
        # can't produce an oversized position (operational safety).
        limits = msgspec.structs.asdict(pos_config.limits)
        notional = s._position_sizer.check_limits(notional, equity, limits)

        s.log.info(
            f"[{ctx.pair}] fixed_risk: risk={risk_pct} entry={entry_price} "
            f"sl={sl_price} qty={qty} notional={notional:.2f}",
            color=LogColor.CYAN,
        )
        return notional
