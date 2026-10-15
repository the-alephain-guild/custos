"""Dust, and how much of a spot holding an exit may sell.

Dust is a holding no order can take off the book. The venue states the smallest order
it accepts in two ways -- a minimum quantity (its step, or a larger minimum order
quantity) and, on some pairs, a minimum notional -- and a holding below either cannot
be sold. The runner treats dust as flat and the toolkit leaves it alone: no stop, no
take-profit, no exit. Both sides call this module, so they cannot disagree about
where the boundary is.

The exit cap answers a neighbouring question. A spot venue sells from the account's
free base balance, and a buy whose fee is taken in the base currency leaves that
balance below the position quantity NautilusTrader reports, which it rounds to the
step. Selling the reported quantity then either fails or takes the shortfall from
base currency that is not this strategy's. The cap sells no more than is free, and
leaves alone the part of the balance that was below one step when the run started.

Pure functions over ``Decimal``. A ``float`` is refused rather than converted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class TradableMinimum:
    """The smallest order the venue accepts on a pair, as its instrument states it."""

    quantity: Decimal
    """``max(size_increment, min_quantity)``: no order below it can be placed."""
    quantity_source: Literal["size_increment", "min_quantity"]
    notional: Decimal | None
    """The minimum notional in the quote currency, or None when the venue states none."""


@dataclass(frozen=True, slots=True)
class Dust:
    """Why a holding is dust: which quantity decided it, and against which minimum."""

    quantity: Decimal
    quantity_source: Literal["position", "balance"]
    reason: Literal["quantity", "notional"]
    minimum: TradableMinimum
    price: Decimal | None
    """The price the notional was judged at; None when none was known."""


def tradable_minimum(
    *,
    size_increment: Decimal,
    min_quantity: Decimal | None = None,
    min_notional: Decimal | None = None,
) -> TradableMinimum:
    """The pair's smallest acceptable order. A stated minimum of zero is no minimum."""
    _require_decimal(size_increment, min_quantity, min_notional)
    if not size_increment.is_finite() or size_increment <= 0:
        raise ValueError(f"size_increment must be positive, got {size_increment}")
    notional = min_notional if min_notional is not None and min_notional > 0 else None
    if min_quantity is not None and min_quantity > size_increment:
        return TradableMinimum(
            quantity=min_quantity, quantity_source="min_quantity", notional=notional
        )
    return TradableMinimum(
        quantity=size_increment, quantity_source="size_increment", notional=notional
    )


def classify_dust(
    *,
    position_quantity: Decimal,
    base_balance: Decimal | None,
    minimum: TradableMinimum,
    price: Decimal | None,
) -> Dust | None:
    """The reason a holding is dust, or None when it is a real holding.

    A holding is dust when its position quantity, or -- on a cash account, where
    ``base_balance`` is the account's total in the base currency -- that balance, is
    below the minimum quantity, or when a price is known and either is worth less than
    the minimum notional. The balance is read as well as the position because
    NautilusTrader rounds the position to the step: a 0.00000935 balance reports as a
    0.00001 position, which is not below the minimum, and only the balance shows that
    there is nothing to sell. Without a balance (a margin account) the position alone
    decides.

    A price that is not a positive finite number is no price: dust is exempt from what
    a real holding must satisfy, so an unknown or bad price exempts nothing beyond what
    the quantity alone shows.
    """
    _require_decimal(position_quantity, base_balance, price)
    known_price = price if price is not None and price.is_finite() and price > 0 else None
    candidates: list[tuple[Decimal, Literal["position", "balance"]]] = [
        (position_quantity, "position")
    ]
    if base_balance is not None:
        candidates.append((base_balance, "balance"))
    for quantity, source in candidates:
        reason = _below(quantity, minimum, known_price)
        if reason is not None:
            return Dust(
                quantity=quantity,
                quantity_source=source,
                reason=reason,
                minimum=minimum,
                price=known_price,
            )
    return None


def below_tradable_minimum(
    quantity: Decimal, minimum: TradableMinimum, price: Decimal | None
) -> Literal["quantity", "notional"] | None:
    """Why an order of this quantity would be refused for its size, or None if it would not."""
    _require_decimal(quantity, price)
    known_price = price if price is not None and price.is_finite() and price > 0 else None
    return _below(quantity, minimum, known_price)


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    """The largest multiple of ``step`` not above ``quantity``; never negative."""
    _require_decimal(quantity, step)
    if quantity <= 0:
        return Decimal("0")
    steps = (quantity / step).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step).quantize(step)


def starting_remainder(balance: Decimal, *, size_increment: Decimal) -> Decimal:
    """The part of a balance below one step: what no exit of this run may sell.

    Taken once, when the strategy starts. Only the sub-step part is reserved, so a
    restart that finds a whole-step holding of its own previous run can still close it.
    """
    _require_decimal(balance, size_increment)
    return balance - floor_to_step(balance, size_increment)


def sellable_quantity(
    *,
    wanted: Decimal,
    free_balance: Decimal | None,
    reserved: Decimal,
    size_increment: Decimal,
) -> Decimal:
    """How much of ``wanted`` an exit may sell, floored to the step.

    ``free_balance`` is the account's free base balance on a cash account, or None
    where there is no such balance to respect (a margin account). ``reserved`` is the
    starting remainder. The result is zero when nothing may be sold.
    """
    _require_decimal(wanted, free_balance, reserved, size_increment)
    available = wanted if free_balance is None else min(wanted, free_balance - reserved)
    return floor_to_step(available, size_increment)


def _below(
    quantity: Decimal, minimum: TradableMinimum, price: Decimal | None
) -> Literal["quantity", "notional"] | None:
    if quantity < minimum.quantity:
        return "quantity"
    if price is not None and minimum.notional is not None and quantity * price < minimum.notional:
        return "notional"
    return None


def _require_decimal(*values: Decimal | None) -> None:
    for value in values:
        if value is not None and not isinstance(value, Decimal):
            raise TypeError(f"expected Decimal, got {type(value).__name__}: {value!r}")
