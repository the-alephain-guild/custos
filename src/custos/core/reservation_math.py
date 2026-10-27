"""Deterministic reservation and entry-cost accounting for execution stores."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


def _amount(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("risk amount must be a finite nonnegative Decimal")
    if positive and value == 0:
        raise ValueError("risk quantity must be positive")
    return value


def remaining_order_reservation(
    reserved: Decimal,
    fill_notional: Decimal,
    fill_quantity: Decimal,
    leaves_quantity: Decimal | None,
) -> Decimal:
    _amount(reserved)
    _amount(fill_notional, positive=True)
    _amount(fill_quantity, positive=True)
    if leaves_quantity is None:
        return max(reserved - fill_notional, Decimal(0))
    _amount(leaves_quantity)
    if leaves_quantity == 0:
        return Decimal(0)
    return reserved * leaves_quantity / (leaves_quantity + fill_quantity)


def released_position_cost(
    open_cost: Decimal, open_quantity: Decimal, closing_quantity: Decimal
) -> Decimal:
    _amount(open_cost)
    _amount(open_quantity, positive=True)
    _amount(closing_quantity)
    if closing_quantity > open_quantity:
        raise ValueError("position reduction exceeds durable filled quantity")
    if closing_quantity == open_quantity:
        return open_cost
    return open_cost * closing_quantity / open_quantity


@dataclass(frozen=True)
class PositionCostLot:
    lot_id: str
    quantity: Decimal
    cost: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.lot_id, str) or not self.lot_id:
            raise ValueError("position lot must have an identity")
        _amount(self.quantity, positive=True)
        _amount(self.cost)


@dataclass(frozen=True)
class PositionCostReduction:
    lot_id: str
    quantity: Decimal
    cost: Decimal


def allocate_fifo_cost(
    lots: tuple[PositionCostLot, ...], closing_quantity: Decimal
) -> tuple[PositionCostReduction, ...]:
    _amount(closing_quantity)
    if len({lot.lot_id for lot in lots}) != len(lots):
        raise ValueError("position lots must have distinct identities")
    if closing_quantity > sum((lot.quantity for lot in lots), Decimal(0)):
        raise ValueError("position reduction exceeds durable position lot quantity")
    remaining = closing_quantity
    reductions = []
    for lot in lots:
        if remaining == 0:
            break
        quantity = min(remaining, lot.quantity)
        reductions.append(
            PositionCostReduction(
                lot.lot_id, quantity, released_position_cost(lot.cost, lot.quantity, quantity)
            )
        )
        remaining -= quantity
    return tuple(reductions)


def notional_limit_breach(
    order_notional: Decimal,
    total_notional: Decimal,
    max_order_notional: Decimal,
    max_total_notional: Decimal,
) -> Literal["per_order", "aggregate"] | None:
    for value in (order_notional, total_notional, max_order_notional, max_total_notional):
        _amount(value)
    if order_notional > max_order_notional:
        return "per_order"
    if total_notional > max_total_notional:
        return "aggregate"
    return None
