"""Which currency a deployment settles in, derived from the pairs it trades.

Equity only has a single answer when a deployment settles in a single currency. A
funded futures account normally holds several currencies at once, so nothing can read
"the" equity off the account alone -- the caller has to say which currency it means.

The pairs are where that comes from. They are required and non-empty on the spec, and
unlike the open positions they are known before anything is traded, which is precisely
when the startup guards need an answer.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["SettlementCurrencyError", "settlement_currency_for_pairs"]


class SettlementCurrencyError(ValueError):
    """A deployment whose pairs do not settle in exactly one currency.

    Deliberately not called "ambiguous": that word already names a different
    condition in this codebase (an account holding several currencies while the
    caller declared none), and reusing it would send a reader to the wrong cause.
    """


def settlement_currency_for_pairs(pairs: Iterable[object]) -> str:
    """Return the one currency these pairs settle in.

    Raises:
        SettlementCurrencyError: if the pairs are empty, or span more than one
            settlement currency -- in which case a deployment-wide equity figure
            does not exist and inventing one would be worse than refusing.
    """
    currencies = {str(pair).upper().replace("/", "-").split("-")[-1] for pair in pairs}
    currencies.discard("")
    if len(currencies) != 1:
        raise SettlementCurrencyError(
            "a deployment must settle in exactly one settlement currency; "
            f"these pairs settle in {sorted(currencies) or 'none'}"
        )
    return next(iter(currencies))
