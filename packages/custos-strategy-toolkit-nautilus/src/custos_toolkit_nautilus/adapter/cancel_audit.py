"""Countable records for a cancel request and whatever came back.

A cancel is asked for and then, separately, either confirmed, refused, or never heard
about again. Only the first of those was visible: the bulk call names an instrument
rather than orders, so nothing recorded how many cancels the venue had been asked for,
and a request that simply evaporated left no trace at all.

That gap is why the four orphaned stops of 2026-07-30 could not be explained. Three
causes fit the evidence and none could be ruled out. These records exist so the next run
answers the question instead of adding another anecdote.

The format is deliberately dull: a fixed leading event name and ``key=value`` pairs, so a
run's totals come out of ``grep -c`` rather than out of reading. NautilusTrader's logger
takes a message rather than key-value pairs, which is why this is a convention here and
not structlog.

Reading the numbers: ``requested`` is not meant to equal ``confirmed``. An order can fill
in the window between asking and the venue acting, and a refusal is its own outcome. What
Plan 29 is looking for is a *persistent* shortfall alongside orders still resting at the
venue afterwards.

Nothing here may raise. These calls sit directly in front of a cancel on the money path,
and an observability failure must never be the reason a stop-loss stays alive.
"""

from __future__ import annotations

from typing import Any

CANCEL_REQUESTED = "cancel_requested"
CANCEL_CONFIRMED = "cancel_confirmed"
CANCEL_REFUSED = "cancel_refused"


def _emit(log: Any, message: str) -> None:
    try:
        log.info(message)
    except Exception:  # noqa: BLE001 — see module docstring: never cost a cancel
        pass


def record_cancel_requested(log: Any, *, order_id: Any, instrument_id: Any) -> None:
    """One line per order the venue is about to be asked to cancel."""
    _emit(log, f"{CANCEL_REQUESTED} order_id={order_id} instrument={instrument_id}")


def record_cancel_confirmed(log: Any, *, order_id: Any, instrument_id: Any) -> None:
    """The venue confirmed the cancel. This is the right-hand side of the count."""
    _emit(log, f"{CANCEL_CONFIRMED} order_id={order_id} instrument={instrument_id}")


def record_cancel_refused(log: Any, *, order_id: Any, instrument_id: Any, reason: Any) -> None:
    """The venue refused the cancel -- a third outcome, and its own bucket.

    Worth separating because the current handler reads every refusal as "the order is
    already gone" and does nothing at all for a stop-loss. Whether that reading holds is
    Plan 29's question; seeing that it happened comes first.
    """
    _emit(
        log,
        f"{CANCEL_REFUSED} order_id={order_id} instrument={instrument_id} reason={reason}",
    )
