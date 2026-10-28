"""Exact collateral loss arithmetic over already verified financial observations."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Literal


def _decimal(value: Decimal, *, positive: bool = False) -> Fraction:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("loss inputs require finite Decimal values")
    parts = value.as_tuple()
    if value.adjusted() >= 78 or not isinstance(parts.exponent, int) or parts.exponent < -18:
        raise ValueError("loss input exceeds the supported exact numeric range")
    if positive and value <= 0:
        raise ValueError("collateral amounts and prices must be positive")
    return Fraction(value)


def adverse_collateral_pnl(
    *,
    margin: Decimal,
    leverage: int,
    entry_price: Decimal,
    mark_price: Decimal,
    side: Literal["BUY", "SELL"],
) -> Fraction:
    """Value only the loss side using original collateral, without quantity truncation."""
    collateral = _decimal(margin, positive=True)
    entry = _decimal(entry_price, positive=True)
    mark = _decimal(mark_price, positive=True)
    if type(leverage) is not int or not 1 <= leverage <= 1000:
        raise ValueError("collateral leverage must be an exact supported integer")
    if type(side) is not str or side not in {"BUY", "SELL"}:
        raise ValueError("collateral side must be BUY or SELL")
    change = mark / entry - 1
    pnl = collateral * leverage * change * (1 if side == "BUY" else -1)
    return min(pnl, Fraction(0))


def conservative_loss(
    *,
    realized_net_pnl: Decimal,
    adverse_pnl: tuple[Fraction, ...],
) -> Fraction:
    """Combine net settlements and negative open PnL, counting included fees once."""
    realized = _decimal(realized_net_pnl)
    if type(adverse_pnl) is not tuple or len(adverse_pnl) > 64:
        raise ValueError("open adverse PnL must be a bounded immutable collection")
    if any(type(pnl) is not Fraction or pnl > 0 for pnl in adverse_pnl):
        raise ValueError("adverse PnL must contain exact nonpositive fractions")
    return max(Fraction(0), -realized - sum(adverse_pnl, Fraction(0)))


def loss_limit_reached(loss: Fraction, limit: Decimal) -> bool:
    """Trip at the exact threshold, retaining sub-cent and repeating-fraction losses."""
    if type(loss) is not Fraction or loss < 0:
        raise ValueError("loss must be an exact nonnegative fraction")
    return loss >= _decimal(limit, positive=True)
