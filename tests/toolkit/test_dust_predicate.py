"""What counts as dust, and how much a spot exit may sell.

Dust is a holding no order can take off the book: below the pair's minimum order
quantity, or -- when a price is known -- worth less than its minimum notional. The
runner treats it as flat and the toolkit leaves it alone, so the two sides must agree
on the boundary exactly. Both read ``tests/fixtures/dust_vectors.json``; these cases
are the toolkit's reading of it.

The exit cap answers the other half: a spot venue sells from the account's free base
balance, which a base-currency fee leaves below the position NautilusTrader reports,
and the part of the balance that was below one step when the strategy started is not
this run's to sell.

Pure functions over ``Decimal``, so these run in the base profile.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from custos_toolkit.position.dust import (
    classify_dust,
    sellable_quantity,
    starting_remainder,
    tradable_minimum,
)

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "dust_vectors.json").read_text()
)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _minimum(name: str):
    spec = _VECTORS["instruments"][name]
    return tradable_minimum(
        size_increment=Decimal(spec["size_increment"]),
        min_quantity=_decimal(spec["min_quantity"]),
        min_notional=_decimal(spec["min_notional"]),
    )


@pytest.mark.parametrize("vector", _VECTORS["dust"], ids=lambda v: v["name"])
def test_dust_vectors(vector) -> None:
    verdict = classify_dust(
        position_quantity=Decimal(vector["position_quantity"]),
        base_balance=_decimal(vector["base_balance"]),
        minimum=_minimum(vector["instrument"]),
        price=_decimal(vector["price"]),
    )

    if not vector["dust"]:
        assert verdict is None, f"a real holding was called dust: {verdict}"
        return
    assert verdict is not None, "dust was called a real holding"
    assert verdict.quantity_source == vector["expected_quantity_source"]
    assert verdict.reason == vector["reason"]
    assert verdict.minimum.quantity == Decimal(vector["threshold"])
    assert verdict.minimum.quantity_source == vector["threshold_source"]
    side = vector["expected_quantity_source"]
    expected_quantity = vector["position_quantity" if side == "position" else "base_balance"]
    assert verdict.quantity == Decimal(expected_quantity), "the record names what decided it"
    assert verdict.price == _decimal(vector["price"])


@pytest.mark.parametrize("vector", _VECTORS["sellable"], ids=lambda v: v["name"])
def test_sellable_vectors(vector) -> None:
    assert sellable_quantity(
        wanted=Decimal(vector["wanted"]),
        free_balance=_decimal(vector["free_balance"]),
        reserved=Decimal(vector["reserved"]),
        size_increment=Decimal(vector["size_increment"]),
    ) == Decimal(vector["sellable"])


@pytest.mark.parametrize("vector", _VECTORS["starting_remainder"], ids=lambda v: f"{v['balance']}")
def test_starting_remainder_vectors(vector) -> None:
    assert starting_remainder(
        Decimal(vector["balance"]), size_increment=Decimal(vector["size_increment"])
    ) == Decimal(vector["remainder"])


def test_the_vectors_cover_each_rule_both_ways() -> None:
    """A vector file that only ever says "dust" cannot catch a predicate that always does."""
    reasons = {(v["dust"], v.get("reason")) for v in _VECTORS["dust"]}
    assert {(True, "quantity"), (True, "notional"), (False, None)} <= reasons
    sources = {v.get("expected_quantity_source") for v in _VECTORS["dust"] if v["dust"]}
    assert sources == {"position", "balance"}


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_a_price_that_is_no_price_is_treated_as_unknown(price) -> None:
    """Dust is exempt from what a real holding must satisfy, so a bad price exempts nothing."""
    minimum = _minimum("sodex_vbtc_vusdc")

    assert (
        classify_dust(
            position_quantity=Decimal("0.00004"),
            base_balance=Decimal("0.00004"),
            minimum=minimum,
            price=price,
        )
        is None
    )


def test_a_float_is_refused() -> None:
    """Money arithmetic is Decimal end to end; a float is a caller's mistake, said loudly."""
    with pytest.raises(TypeError):
        classify_dust(
            position_quantity=0.00001,  # type: ignore[arg-type]
            base_balance=None,
            minimum=_minimum("sodex_vbtc_vusdc"),
            price=None,
        )
    with pytest.raises(TypeError):
        tradable_minimum(size_increment=0.00001)  # type: ignore[arg-type]


def test_a_step_that_is_not_positive_is_refused() -> None:
    with pytest.raises(ValueError):
        tradable_minimum(size_increment=Decimal("0"))
