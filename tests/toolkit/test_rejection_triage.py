"""Which order the venue refused, decided without a strategy.

This used to be four booleans inside a 128-line handler, only testable by driving a
whole strategy, and mutually exclusive only because of the order the returns happened
to be in. These tests hold the exclusivity that the enum now makes structural.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from custos_toolkit_nautilus.adapter.coordinators.rejection_triage import (  # noqa: E402
    RejectedOrderKind,
    classify_rejected_order,
)


def _tracker(
    *,
    sl: tuple[str, ...] = (),
    exchange_sl: tuple[str, ...] = (),
    tp: tuple[str, ...] = (),
    entry: str | None = None,
) -> NS:
    return NS(
        sl_order_ids=sl,
        exchange_sl_order_ids=exchange_sl,
        tp_order_ids=tp,
        entry_order_id=entry,
    )


def _order(*, reduce_only: bool) -> NS:
    return NS(is_reduce_only=reduce_only)


def test_a_tracked_stop_is_a_stop_even_though_it_is_also_reduce_only() -> None:
    """The ordering that used to be implicit: a resting stop is not a failed close.

    Reading it as a close would send it down the -2022 escape path, which cancels
    every order on the instrument -- taking the other protective lots with it.
    """
    kind = classify_rejected_order("stop-1", _order(reduce_only=True), _tracker(sl=("stop-1",)))

    assert kind is RejectedOrderKind.TRACKED_STOP


def test_an_exchange_side_stop_counts_as_a_tracked_stop_too() -> None:
    kind = classify_rejected_order(
        "stop-2", _order(reduce_only=True), _tracker(exchange_sl=("stop-2",))
    )

    assert kind is RejectedOrderKind.TRACKED_STOP


def test_a_tracked_take_profit_is_not_a_close_attempt() -> None:
    kind = classify_rejected_order("tp-1", _order(reduce_only=True), _tracker(tp=("tp-1",)))

    assert kind is RejectedOrderKind.TRACKED_TAKE_PROFIT


def test_an_untracked_reduce_only_order_is_a_close_attempt() -> None:
    kind = classify_rejected_order("close-1", _order(reduce_only=True), _tracker())

    assert kind is RejectedOrderKind.REDUCE_ONLY_CLOSE


def test_the_tracked_entry_order_is_the_entry() -> None:
    kind = classify_rejected_order("entry-1", _order(reduce_only=False), _tracker(entry="entry-1"))

    assert kind is RejectedOrderKind.ENTRY


def test_a_close_attempt_is_never_also_read_as_the_entry() -> None:
    """The entry branch used to be reached by falling past the reduce-only branch.

    With an exclusive verdict a reduce-only order cannot also be answered as the
    entry, whatever the tracker happens to hold.
    """
    kind = classify_rejected_order("close-1", _order(reduce_only=True), _tracker(entry="close-1"))

    assert kind is RejectedOrderKind.REDUCE_ONLY_CLOSE


def test_an_order_this_strategy_never_placed_is_unknown() -> None:
    """A live venue stream is account-wide; not every rejection is ours."""
    kind = classify_rejected_order("someone-else", _order(reduce_only=False), _tracker())

    assert kind is RejectedOrderKind.UNKNOWN


def test_a_missing_cache_entry_does_not_become_a_close_attempt() -> None:
    """The cache can miss the order. Absent is not the same as reduce-only."""
    kind = classify_rejected_order("gone", None, _tracker())

    assert kind is RejectedOrderKind.UNKNOWN


@pytest.mark.parametrize(
    ("tracker", "order", "expected"),
    [
        (_tracker(sl=("x",)), _order(reduce_only=True), RejectedOrderKind.TRACKED_STOP),
        (_tracker(tp=("x",)), _order(reduce_only=True), RejectedOrderKind.TRACKED_TAKE_PROFIT),
        (_tracker(), _order(reduce_only=True), RejectedOrderKind.REDUCE_ONLY_CLOSE),
        (_tracker(entry="x"), _order(reduce_only=False), RejectedOrderKind.ENTRY),
        (_tracker(), _order(reduce_only=False), RejectedOrderKind.UNKNOWN),
    ],
)
def test_every_kind_is_reachable(tracker: NS, order: NS, expected: RejectedOrderKind) -> None:
    """A verdict nothing can produce would be a branch nobody exercises."""
    assert classify_rejected_order("x", order, tracker) is expected
