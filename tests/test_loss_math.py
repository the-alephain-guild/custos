"""Raw collateral arithmetic must retain losses hidden by Native quantity truncation."""

from decimal import Decimal, localcontext
from fractions import Fraction
from importlib import import_module

import pytest


def api():
    return import_module("custos.core.loss_math")


@pytest.mark.parametrize("side,mark", [("BUY", "28200"), ("SELL", "31800")])
def test_exact_collateral_loss_reaches_the_thirty_dollar_boundary(side, mark):
    pnl = api().adverse_collateral_pnl(
        margin=Decimal("100"),
        leverage=5,
        entry_price=Decimal("30000"),
        mark_price=Decimal(mark),
        side=side,
    )
    assert pnl == Fraction(-30)
    loss = api().conservative_loss(realized_net_pnl=Decimal(0), adverse_pnl=(pnl,))
    assert loss == 30
    assert api().loss_limit_reached(loss, Decimal("30"))
    truncated_quantity = Decimal("0.0166666666666666")
    assert truncated_quantity * Decimal("1800") < 30


def test_rational_arithmetic_is_independent_of_decimal_context():
    with localcontext() as context:
        context.prec = 2
        pnl = api().adverse_collateral_pnl(
            margin=Decimal("100.123456789123456789"),
            leverage=5,
            entry_price=Decimal("3"),
            mark_price=Decimal("3.18"),
            side="SELL",
        )
        assert pnl == -Fraction(Decimal("100.123456789123456789")) * Fraction(3, 10)
        loss = api().conservative_loss(realized_net_pnl=Decimal("-0.01"), adverse_pnl=(pnl,))
        assert api().loss_limit_reached(loss, Decimal("30"))


@pytest.mark.parametrize("side,mark", [("BUY", "31000"), ("SELL", "29000")])
def test_unrealized_profit_never_offsets_another_open_loss(side, mark):
    gain = api().adverse_collateral_pnl(
        margin=Decimal("100"),
        leverage=5,
        entry_price=Decimal("30000"),
        mark_price=Decimal(mark),
        side=side,
    )
    assert gain == 0
    assert (
        api().conservative_loss(realized_net_pnl=Decimal(0), adverse_pnl=(gain, Fraction(-30)))
        == 30
    )


def test_realized_net_settlement_is_counted_once_and_profits_can_offset_losses():
    assert api().conservative_loss(realized_net_pnl=Decimal("-30"), adverse_pnl=()) == 30
    assert (
        api().conservative_loss(realized_net_pnl=Decimal("10"), adverse_pnl=(Fraction(-40),)) == 30
    )
    assert (
        api().conservative_loss(realized_net_pnl=Decimal("40"), adverse_pnl=(Fraction(-30),)) == 0
    )
    assert not api().loss_limit_reached(Fraction(29999999999999999999, 10**18), Decimal("30"))
    assert api().loss_limit_reached(Fraction(30000000000000000001, 10**18), Decimal("30"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("margin", 100.0),
        ("margin", Decimal("NaN")),
        ("margin", Decimal("Infinity")),
        ("margin", Decimal("0")),
        ("margin", Decimal("-1")),
        ("margin", Decimal("1e78")),
        ("margin", Decimal("1e-19")),
        ("leverage", True),
        ("leverage", 5.0),
        ("leverage", 0),
        ("leverage", 1001),
        ("entry_price", Decimal("0")),
        ("mark_price", Decimal("-1")),
        ("side", "buy"),
        ("side", True),
    ],
)
def test_collateral_inputs_are_exact_and_explicit(field, value):
    values = dict(
        margin=Decimal("100"),
        leverage=5,
        entry_price=Decimal("30000"),
        mark_price=Decimal("31800"),
        side="SELL",
    )
    values[field] = value
    with pytest.raises(ValueError):
        api().adverse_collateral_pnl(**values)


@pytest.mark.parametrize(
    "realized,adverse",
    [
        (1.5, ()),
        (Decimal("NaN"), ()),
        (Decimal(0), (Fraction(1),)),
        (Decimal(0), (Decimal(-1),)),
        (Decimal(0), (False,)),
        (Decimal(0), [Fraction(-1)]),
        (Decimal(0), (Fraction(-1),) * 65),
    ],
)
def test_loss_aggregation_rejects_guessed_or_positive_adverse_pnl(realized, adverse):
    with pytest.raises(ValueError):
        api().conservative_loss(realized_net_pnl=realized, adverse_pnl=adverse)


@pytest.mark.parametrize(
    "loss,limit",
    [
        (Fraction(-1), Decimal("30")),
        (30.0, Decimal("30")),
        (Fraction(0), Decimal(0)),
        (Fraction(0), Decimal("NaN")),
    ],
)
def test_threshold_requires_exact_nonnegative_loss_and_positive_limit(loss, limit):
    with pytest.raises(ValueError):
        api().loss_limit_reached(loss, limit)
