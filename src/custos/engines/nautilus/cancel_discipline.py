"""What the safety path may ask a venue, and when, once the venue has said no.

Containment and both shutdown policies withdraw orders by asking the venue to
cancel them. How a venue says no differs per venue, and so does how much an
account may ask, so both are declared by the venue modules:

- ``cancel_rejection_kind(reason)`` sorts a refusal text into one of the kinds
  below, or answers ``None`` when the venue has no measured classification;
- ``safety_request_budget()`` names how many safety-path requests one account
  partition may make, or ``None`` when the venue states no per-account figure.

Nothing in this module imports NautilusTrader, so the venue modules and the
drift checks can name these types on any install.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "NOT_FOUND",
    "RATE_LIMITED",
    "REFUSAL_KINDS",
    "REFUSED",
    "TRANSPORT",
    "UNKNOWN",
    "SafetyRequestBudget",
]

# The venue has no such order. Cancelling again cannot help; the order's real
# state is a question for reconciliation.
NOT_FOUND: Final = "not_found"
# The account is over its request allowance. Every request from the partition
# makes it worse, so the whole partition waits.
RATE_LIMITED: Final = "rate_limited"
# The request never reached the venue. The partition waits, since whatever broke
# the connection breaks the next request too.
TRANSPORT: Final = "transport"
# The venue answered and refused this order's cancel for a reason of its own.
REFUSED: Final = "refused"
# Anything the venue module could not classify, and everything on a venue that
# declares no classification at all.
UNKNOWN: Final = "unknown"

REFUSAL_KINDS: Final = frozenset({NOT_FOUND, RATE_LIMITED, TRANSPORT, REFUSED, UNKNOWN})


@dataclass(frozen=True, slots=True)
class SafetyRequestBudget:
    """A token bucket over one account partition's safety-path requests.

    Cancels, closes and order queries made by containment and shutdown all draw
    on it. ``capacity`` requests may go at once; after that, ``refill_per_second``.
    """

    capacity: int
    refill_per_second: float

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.refill_per_second <= 0:
            raise ValueError("a safety request budget must allow at least one request")
