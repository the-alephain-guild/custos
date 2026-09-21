"""What kind of order the venue just refused.

Deciding this and acting on it are two jobs. They used to share one method, where
the answer lived in four separate booleans consumed at different depths and the
kinds stayed mutually exclusive only because of the order the ``return`` statements
happened to be in -- the entry branch was not an ``elif``, it was reached by falling
past the reduce-only branch. Naming the kinds makes the exclusivity structural, and
makes the decision testable without a strategy.

Why the rejection happened is a separate question again, answered by
``custos_toolkit.risk.exchange_errors``: this module says *which order*, that one
says *how bad*.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import Enum
from typing import Any, Protocol

from custos_toolkit_nautilus.adapter.runtime_types import Order


class RejectedOrderKind(str, Enum):  # noqa: UP042 - match the str(Enum) style of SLTPMode
    """Which order the venue refused, from this strategy's point of view."""

    TRACKED_STOP = "tracked_stop"
    TRACKED_TAKE_PROFIT = "tracked_take_profit"
    REDUCE_ONLY_CLOSE = "reduce_only_close"
    ENTRY = "entry"
    UNKNOWN = "unknown"


class _Tracker(Protocol):
    """The order-tracker surface this decision reads. Nothing here writes."""

    @property
    def sl_order_ids(self) -> Collection[Any]: ...

    @property
    def exchange_sl_order_ids(self) -> Collection[Any]: ...

    @property
    def tp_order_ids(self) -> Collection[Any]: ...

    @property
    def entry_order_id(self) -> Any: ...


def classify_rejected_order(
    client_order_id: Any,
    order: Order | None,
    tracker: _Tracker,
) -> RejectedOrderKind:
    """Which of this strategy's orders the rejection is about.

    ``order`` is what the cache holds, which can be nothing; the tracker is what the
    strategy believes it has out. Protective lots are checked before the reduce-only
    test because a resting stop is also reduce-only, and treating it as a failed close
    attempt would cancel the other protective lots along with it.

    ``UNKNOWN`` is a real answer: the venue's report may be about an order this
    strategy never placed, or one it has already forgotten.
    """
    if client_order_id in {*tracker.sl_order_ids, *tracker.exchange_sl_order_ids}:
        return RejectedOrderKind.TRACKED_STOP
    if client_order_id in set(tracker.tp_order_ids):
        return RejectedOrderKind.TRACKED_TAKE_PROFIT
    if order is not None and bool(order.is_reduce_only):
        return RejectedOrderKind.REDUCE_ONLY_CLOSE
    if tracker.entry_order_id is not None and tracker.entry_order_id == client_order_id:
        return RejectedOrderKind.ENTRY
    return RejectedOrderKind.UNKNOWN


__all__ = ["RejectedOrderKind", "classify_rejected_order"]
