from decimal import Decimal as D

import pytest

from custos.core.reservation_math import (
    PositionCostLot,
    allocate_fifo_cost,
    notional_limit_breach,
    released_position_cost,
    remaining_order_reservation,
)


@pytest.mark.parametrize(
    "reserved,fill,quantity,leaves,expected",
    [
        ("100", "80", "1", "0", "0"),
        ("100", "40", "1", "1", "50"),
        ("100", "40", "1", None, "60"),
        ("100", "120", "1", None, "0"),
    ],
)
def test_fill_releases_only_the_proven_share_of_reserved_capacity(
    reserved, fill, quantity, leaves, expected
):
    assert remaining_order_reservation(
        D(reserved), D(fill), D(quantity), None if leaves is None else D(leaves)
    ) == D(expected)


def test_fifo_reduction_releases_entry_cost_instead_of_exit_proceeds():
    lots = (PositionCostLot("first", D("2"), D("100")), PositionCostLot("second", D("3"), D("240")))
    changes = allocate_fifo_cost(lots, D("3"))
    assert [(item.lot_id, item.quantity, item.cost) for item in changes] == [
        ("first", D("2"), D("100")),
        ("second", D("1"), D("80")),
    ]
    assert released_position_cost(D("240"), D("3"), D("3")) == D("240")
    assert sum((item.cost for item in allocate_fifo_cost(lots, D("5"))), D(0)) == D("340")


def test_a_reduction_cannot_consume_more_than_the_proven_position():
    with pytest.raises(ValueError, match="exceeds"):
        allocate_fifo_cost((PositionCostLot("first", D("1"), D("50")),), D("2"))


@pytest.mark.parametrize(
    "order,total,expected",
    [("100", "1000", None), ("100.01", "1000", "per_order"), ("100", "1000.01", "aggregate")],
)
def test_notional_limits_are_inclusive_decimal_boundaries(order, total, expected):
    assert notional_limit_breach(D(order), D(total), D("100"), D("1000")) == expected


@pytest.mark.parametrize("amount", [float("nan"), D("NaN"), D("Infinity"), D("-1"), True])
def test_invalid_money_is_rejected_before_it_can_release_capacity(amount):
    with pytest.raises(ValueError):
        remaining_order_reservation(amount, D("1"), D("1"), D("0"))
