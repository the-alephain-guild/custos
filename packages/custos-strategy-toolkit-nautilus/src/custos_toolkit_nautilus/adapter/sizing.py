"""Position sizing -- nautilus-specific sizing helpers.

Platform-agnostic sizing abstractions live in ``shared/position/``; this module
carries the implementation that needs nautilus ``FixedRiskSizer``/``Instrument``.
Platform-agnostic modules are not allowed to pull in nautilus dependencies, so
fixed-risk sizing lives in ``shared/nautilus/`` rather than ``shared/position/``.
"""

from decimal import Decimal
from typing import cast

from nautilus_trader.model import Money, Quantity
from nautilus_trader.risk import FixedRiskSizer

from .runtime_types import Instrument


def compute_fixed_risk_qty(
    instrument: Instrument,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    equity: Decimal,
    risk_pct: float,
) -> Quantity:
    """Compute a base-currency Quantity via the native FixedRiskSizer.

    Sizes the position so that hitting ``stop_loss_price`` loses ``risk_pct``
    (a decimal fraction: 0.01 == 1%) of ``equity``.

    CRITICAL: the native ``FixedRiskSizer.calculate`` default
    ``unit_batch_size=1`` floors sub-1 crypto quantities to 0 -- the instrument's
    ``size_increment`` must be passed, otherwise e.g. 0.1 BTC would round down to 0.

    Args:
        instrument: NautilusTrader Instrument (provides Price/Money/Quantity + sizer).
        entry_price: Entry price.
        stop_loss_price: Stop-loss price.
        equity: Account equity (quote currency).
        risk_pct: Fraction of equity to risk (decimal, 0.01 == 1%).

    Returns:
        Quantity (base currency). Returns 0 when the SL distance is 0.
    """
    entry = instrument.make_price(entry_price)
    stop_loss = instrument.make_price(stop_loss_price)
    equity_money = Money(cast(float, equity), instrument.quote_currency)
    risk = Decimal(str(risk_pct))
    return FixedRiskSizer(instrument).calculate(
        entry=entry,
        stop_loss=stop_loss,
        equity=equity_money,
        risk=risk,
        unit_batch_size=instrument.size_increment.as_decimal(),
    )


def quantity_from_notional(instrument: Instrument, notional: Decimal, price: Decimal) -> Decimal:
    """Convert quote notional to native linear contract units without oversizing."""
    multiplier = Decimal(str(getattr(instrument, "multiplier", 1)))
    if bool(getattr(instrument, "is_inverse", False)):
        raise ValueError("inverse contract sizing is not supported")
    if not multiplier.is_finite() or multiplier <= 0 or price <= 0 or not price.is_finite():
        raise ValueError("instrument multiplier and price must be finite and positive")
    return notional / (price * multiplier)
