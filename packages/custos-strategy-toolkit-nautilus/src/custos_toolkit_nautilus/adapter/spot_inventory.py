"""What a spot holding is, read from the account rather than from the position.

NautilusTrader rounds a position to the instrument's step, and a spot venue that takes
the buy fee in the base currency leaves the account holding less than that rounded
quantity. These readers take the account's balances into account and hand them to the
platform-neutral rules in ``custos_toolkit.position.dust``: whether a holding is dust,
and how much of it an exit may sell.

Every reader answers for a cash account only and returns its "no constraint" value
anywhere else, so a margin account behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from custos_toolkit.position.dust import (
    Dust,
    TradableMinimum,
    below_tradable_minimum,
    classify_dust,
    sellable_quantity,
    starting_remainder,
    tradable_minimum,
)

from .strategy_core import read_cash_account

if TYPE_CHECKING:
    from nautilus_trader.model import InstrumentId

    from .runtime_types import Account, Cache, Instrument


def read_tradable_minimum(instrument: Instrument) -> TradableMinimum:
    """The smallest order the venue accepts, from the instrument the adapter loaded.

    A minimum notional stated in anything but the quote currency cannot be compared
    with a quote price, so it is left out rather than guessed at.
    """
    min_quantity = instrument.min_quantity
    min_notional = instrument.min_notional
    return tradable_minimum(
        size_increment=instrument.size_increment.as_decimal(),
        min_quantity=None if min_quantity is None else min_quantity.as_decimal(),
        min_notional=(
            min_notional.as_decimal()
            if min_notional is not None and min_notional.currency == instrument.quote_currency
            else None
        ),
    )


def _already_said(_message: str) -> None:
    """A warning sink for a read whose caller has already reported the same failure."""


def _cash_account(
    cache: Cache, instrument_id: InstrumentId, warn: Callable[[str], None]
) -> Account | None:
    if read_cash_account(cache, instrument_id, warn) is not True:
        return None
    return cache.account_for_venue(instrument_id.venue)


def read_dust(
    cache: Cache,
    instrument_id: InstrumentId,
    position_quantity: Decimal,
    price: Decimal | None,
    warn: Callable[[str], None],
) -> Dust | None:
    """Why this position is dust, or None when it is a real holding.

    On a cash account the base balance decides as well as the position. An instrument
    missing from the cache makes nothing dust: an exemption needs the venue's own
    minimums to stand on.
    """
    instrument = cache.instrument(instrument_id)
    if instrument is None:
        return None
    balance: Decimal | None = None
    account = _cash_account(cache, instrument_id, warn)
    if account is not None:
        total = account.balance_total(instrument.base_currency)
        balance = None if total is None else total.as_decimal()
    return classify_dust(
        position_quantity=position_quantity,
        base_balance=balance,
        minimum=read_tradable_minimum(instrument),
        price=price,
    )


def read_starting_remainder(
    cache: Cache, instrument_id: InstrumentId, warn: Callable[[str], None]
) -> Decimal | None:
    """The sub-step part of the free base balance a cash account holds now.

    Read once at start: no exit of this run sells it. None when the account is not a
    readable cash account, where there is no balance to reserve from.
    """
    instrument = cache.instrument(instrument_id)
    account = _cash_account(cache, instrument_id, warn)
    if instrument is None or account is None:
        return None
    free = account.balance_free(instrument.base_currency)
    held = Decimal("0") if free is None else free.as_decimal()
    return starting_remainder(held, size_increment=instrument.size_increment.as_decimal())


@dataclass(frozen=True, slots=True)
class SpotExit:
    """How much an exit sells, and why it sells less than asked when it does."""

    quantity: Decimal
    note: str | None
    """Set when the quantity differs from what was asked: a countable log line."""

    @property
    def refused(self) -> bool:
        """Nothing is sent: the exit is too small for the venue to accept, or there is none."""
        return self.quantity <= 0

    def announce(
        self, pair: str, *, warning: Callable[[str], None], info: Callable[[str], None]
    ) -> None:
        """Say why the exit differs from what was asked: a warning when nothing goes out."""
        if self.note is not None:
            (warning if self.refused else info)(f"[{pair}] {self.note}")


def size_spot_exit(
    cache: Cache,
    instrument_id: InstrumentId,
    wanted: Decimal,
    reserved: Decimal | None,
    price: Decimal | None,
) -> SpotExit:
    """Cap an exit on a cash account at what it may sell, or refuse one too small to place.

    The cap is the free base balance less the starting remainder, floored to the step.
    A capped quantity the venue would refuse for its size -- below the minimum quantity,
    or under the minimum notional at ``price`` -- is not sent at all. Anywhere but a
    readable cash account the exit is sized as asked.

    The caller has already read the account to decide the order's form, and said so
    if it could not; this reads it again without repeating that, so the warning stays
    one line per order.
    """
    instrument = cache.instrument(instrument_id)
    account = _cash_account(cache, instrument_id, _already_said)
    if instrument is None or account is None:
        return SpotExit(quantity=wanted, note=None)
    free_money = account.balance_free(instrument.base_currency)
    free = Decimal("0") if free_money is None else free_money.as_decimal()
    held_back = Decimal("0") if reserved is None else reserved
    quantity = sellable_quantity(
        wanted=wanted,
        free_balance=free,
        reserved=held_back,
        size_increment=instrument.size_increment.as_decimal(),
    )
    minimum = read_tradable_minimum(instrument)
    refusal = below_tradable_minimum(quantity, minimum, price)
    facts = (
        f"instrument={instrument_id} wanted={wanted} free={free} reserved={held_back} "
        f"sellable={quantity}"
    )
    if refusal is not None:
        return SpotExit(
            quantity=Decimal("0"),
            note=(
                f"spot_exit_not_sent {facts} reason=below_minimum_{refusal} "
                f"minimum_quantity={minimum.quantity} minimum_notional={minimum.notional} "
                f"price={price}"
            ),
        )
    if quantity != wanted:
        return SpotExit(quantity=quantity, note=f"spot_exit_capped {facts}")
    return SpotExit(quantity=quantity, note=None)
