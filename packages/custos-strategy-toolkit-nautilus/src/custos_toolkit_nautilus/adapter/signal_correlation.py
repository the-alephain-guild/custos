"""Ties an order back to the signal that asked for it.

A signal produces an entry order, and that position later grows protective orders.
They are separate orders with separate client order ids, so nothing in the execution
stream says which entry a given stop-loss is protecting. This carries that link: the
signal gets an id, the id rides on the orders as a tag, and the strategy keeps a map
for the case the tag does not survive (a market order loses its tags in the cache once
filled -- the venue's fill event carries none).

Nothing reads it back yet. It is kept because the link is what the execution-analytics
contract asks for by name -- ``signal_fact_id`` on both the order and the TCA record --
and rebuilding it later would mean rediscovering the fill-loses-its-tags part the hard
way. The lane that used to read it (a msgbus topic to a Redis stream to a sidecar) is
retired; the signed RunnerFact stream is where it is headed instead.

The prefix is shared with nothing: the old consumer inlined its own copy rather than
import across a package boundary, and that consumer is gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

__all__ = [
    "SIGNAL_ID_TAG_PREFIX",
    "extract_signal_id_from_tags",
    "generate_signal_id",
    "make_signal_tag",
]

SIGNAL_ID_TAG_PREFIX = "signal_id:"


def generate_signal_id() -> str:
    """A fresh signal id."""
    return str(uuid.uuid4())


def make_signal_tag(signal_id: str) -> str:
    """The order tag carrying a signal id."""
    return f"{SIGNAL_ID_TAG_PREFIX}{signal_id}"


def extract_signal_id_from_tags(tags: Iterable[object] | None) -> str | None:
    """The signal id an order's tags carry, or None.

    ``tags`` is whatever nautilus put on the order, so it is read defensively: the
    absence of a tag is an ordinary answer here, not a failure.
    """
    if not tags:
        return None
    for tag in tags:
        tag_str = str(tag)
        if tag_str.startswith(SIGNAL_ID_TAG_PREFIX):
            return tag_str[len(SIGNAL_ID_TAG_PREFIX) :]
    return None
