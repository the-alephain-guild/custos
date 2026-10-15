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

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final

from custos.core.log import get_logger

__all__ = [
    "CancelDiscipline",
    "NOT_FOUND",
    "RATE_LIMITED",
    "REFUSAL_KINDS",
    "REFUSED",
    "TRANSPORT",
    "UNKNOWN",
    "PartitionGate",
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


# An order whose cancel has gone unanswered this long is asked about once. Five
# seconds is when nautilus's own in-flight check starts asking; this is a later,
# single backstop for a venue that answered neither.
IN_FLIGHT_QUERY_AFTER_SECS: Final = 15.0
# After a not-found refusal the order is queried at once, then after each of these
# gaps. When the last gap passes with the order still open, the venue and the
# cache disagree and only reconciliation can settle it.
NOT_FOUND_QUERY_GAPS_SECS: Final = (10.0, 20.0, 40.0)
# (first wait, longest wait) for the partition-wide pauses, doubling in between.
RATE_LIMITED_PAUSE_SECS: Final = (60.0, 600.0)
TRANSPORT_PAUSE_SECS: Final = (5.0, 120.0)
# (first wait, longest wait) for one order's back-off after a refusal of its own.
ORDER_BACKOFF_SECS: Final = (2.0, 60.0)
# How many times an order's cancel is re-sent after a refusal of each kind before
# it is given up on. A rate-limited cancel is bounded by time, not by count.
RETRY_LIMITS: Final = {TRANSPORT: 10, REFUSED: 5, UNKNOWN: 5}

_HANDLED_EVENTS: Final = frozenset(
    {"OrderPendingCancel", "OrderCancelRejected", "OrderCanceled", "OrderRejected", "OrderFilled"}
)

_log = get_logger("custos.nautilus_host.cancel_discipline")


def _doubling(bounds: tuple[float, float], attempt: int) -> float:
    first, longest = bounds
    return min(first * 2 ** (attempt - 1), longest)


class PartitionGate:
    """The request allowance and pauses of one account partition.

    One partition is one exchange account, which is what a venue rate-limits.
    Every instance on it shares this gate, and it outlives them: an account that
    was told to slow down is still that account when the next generation starts.
    """

    def __init__(self, budget: SafetyRequestBudget | None, clock: Callable[[], float]) -> None:
        self._budget = budget
        self._clock = clock
        self._tokens = float(budget.capacity) if budget is not None else 0.0
        self._refilled_at = clock()
        self._paused_until: float | None = None
        self._rate_limited_streak = 0
        self._transport_streak = 0

    def paused(self) -> bool:
        return self._paused_until is not None and self._clock() < self._paused_until

    def take(self) -> bool:
        """Spend one request, or say there is none to spend right now."""
        if self.paused():
            return False
        if self._budget is None:
            return True
        now = self._clock()
        self._tokens = min(
            float(self._budget.capacity),
            self._tokens + (now - self._refilled_at) * self._budget.refill_per_second,
        )
        self._refilled_at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def refused(self, kind: str) -> float | None:
        """Account for a refusal; returns the pause it started, if any."""
        if kind == RATE_LIMITED:
            self._rate_limited_streak += 1
            return self._pause(_doubling(RATE_LIMITED_PAUSE_SECS, self._rate_limited_streak))
        self._rate_limited_streak = 0
        if kind == TRANSPORT:
            self._transport_streak += 1
            return self._pause(_doubling(TRANSPORT_PAUSE_SECS, self._transport_streak))
        self._transport_streak = 0
        return None

    def answered(self) -> None:
        """The venue answered a request: whatever was throttling the account has passed."""
        self._rate_limited_streak = 0
        self._transport_streak = 0

    def _pause(self, seconds: float) -> float:
        until = self._clock() + seconds
        self._paused_until = max(until, self._paused_until or until)
        return seconds


@dataclass(slots=True)
class _OrderState:
    kind: str | None = None
    retries: int = 0
    next_cancel_at: float = 0.0
    # A cancel was sent, or the order was seen pending cancel, and nothing has
    # answered since. Nothing is re-sent while this is set.
    awaiting_since: float | None = None
    in_flight_queried: bool = False
    queries: int = 0
    next_query_at: float | None = None
    contradicted: bool = False
    abandoned: bool = False


class CancelDiscipline:
    """One instance's memory of what the venue said about each cancel.

    Held by the host for as long as the node lives, not by the shutdown or
    containment coroutine asking: those are cancelled and restarted by the guard
    on every tick, and what the venue said has to survive that.
    """

    def __init__(
        self,
        *,
        deployment_instance_id: str,
        classify: Callable[[str], str | None],
        partition: PartitionGate,
        clock: Callable[[], float],
    ) -> None:
        self._deployment_instance_id = deployment_instance_id
        self._classify = classify
        self._partition = partition
        self._clock = clock
        self._orders: dict[str, _OrderState] = {}

    def on_order_event(self, event: Any) -> None:
        """The order sink. Never raises: a failure here must not degrade the deployment."""
        try:
            self._observe(event)
        except Exception as exc:  # noqa: BLE001 - reported here; the forwarder must not see it
            _log.error(
                "cancel_discipline_event_failed",
                deployment_instance_id=self._deployment_instance_id,
                event_type=type(event).__name__,
                error_type=type(exc).__name__,
            )

    def cancel(self, strategy: Any, order: Any) -> bool:
        """Ask for this order's cancel if the venue's last answer allows it."""
        order_id = str(order.client_order_id)
        if bool(getattr(order, "is_closed", False)):
            self._orders.pop(order_id, None)
            return False
        now = self._clock()
        pending = bool(getattr(order, "is_pending_cancel", False))
        state = self._orders.get(order_id)
        if state is None and pending:
            state = self._orders[order_id] = _OrderState(awaiting_since=now)
        if state is not None:
            if state.abandoned or state.contradicted:
                return False
            if state.kind == NOT_FOUND:
                self._follow_up_not_found(strategy, order, state, now)
                return False
            if state.awaiting_since is not None:
                self._follow_up_in_flight(strategy, order, state, now, pending)
                return False
            if now < state.next_cancel_at:
                return False
        if not self._partition.take():
            return False
        state = self._orders.setdefault(order_id, _OrderState())
        # Set before the request: an answer can arrive inside it.
        state.awaiting_since = now
        state.in_flight_queried = False
        # 2.0 cancels by id; passing the order raises before anything is withdrawn.
        strategy.cancel_order(order.client_order_id)
        return True

    def close(self, request: Callable[[], None]) -> bool:
        """Send one close request if the partition allows one now."""
        if not self._partition.take():
            return False
        request()
        return True

    def residual(self, orders: Iterable[Any]) -> dict[str, int]:
        """Why each of these still-open orders is still open, by category."""
        now = self._clock()
        categories: Counter[str] = Counter()
        for order in orders:
            state = self._orders.get(str(order.client_order_id))
            if state is None:
                categories["paused" if self._partition.paused() else "untried"] += 1
            elif state.contradicted:
                categories["venue_state_contradicted"] += 1
            elif state.abandoned:
                categories["cancel_abandoned"] += 1
            elif state.kind == NOT_FOUND:
                categories["not_found"] += 1
            elif state.awaiting_since is not None:
                categories["in_flight"] += 1
            elif self._partition.paused():
                categories["paused"] += 1
            elif now < state.next_cancel_at:
                categories["backing_off"] += 1
            else:
                categories["untried"] += 1
        return dict(categories)

    def _observe(self, event: Any) -> None:
        name = type(event).__name__
        if name not in _HANDLED_EVENTS:
            return
        order_id = str(event.client_order_id)
        now = self._clock()
        if name == "OrderPendingCancel":
            state = self._orders.setdefault(order_id, _OrderState())
            if state.awaiting_since is None:
                state.awaiting_since = now
        elif name == "OrderCancelRejected":
            kind = self._classify(str(event.reason))
            self._refused(order_id, kind if kind in REFUSAL_KINDS else UNKNOWN, now)
        elif name in {"OrderCanceled", "OrderRejected"}:
            self._orders.pop(order_id, None)
            self._partition.answered()
        else:
            # A fill is the venue speaking about the order: whatever made it look
            # missing is void, and its cancel starts over from what it is now.
            state = self._orders.get(order_id)
            if state is not None and state.kind == NOT_FOUND:
                self._orders.pop(order_id, None)

    def _refused(self, order_id: str, kind: str, now: float) -> None:
        state = self._orders.setdefault(order_id, _OrderState())
        state.awaiting_since = None
        state.in_flight_queried = False
        pause = self._partition.refused(kind)
        if kind == NOT_FOUND:
            if state.kind != NOT_FOUND:
                state.kind = NOT_FOUND
                state.next_query_at = now
            _log.warning(
                "nt_cancel_refused",
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=order_id,
                kind=kind,
            )
            return
        state.kind = kind
        limit = RETRY_LIMITS.get(kind)
        if limit is not None and state.retries >= limit:
            state.abandoned = True
            _log.error(
                "nt_cancel_abandoned",
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=order_id,
                kind=kind,
                retries=state.retries,
            )
            return
        state.retries += 1
        # A partition-wide refusal is waited out by the partition; the order itself
        # may go again as soon as the partition does.
        state.next_cancel_at = (
            now if pause is not None else now + _doubling(ORDER_BACKOFF_SECS, state.retries)
        )
        _log.warning(
            "nt_cancel_refused",
            deployment_instance_id=self._deployment_instance_id,
            client_order_id=order_id,
            kind=kind,
            retries=state.retries,
            partition_pause_secs=pause,
            next_cancel_in_secs=state.next_cancel_at - now,
        )

    def _follow_up_not_found(
        self, strategy: Any, order: Any, state: _OrderState, now: float
    ) -> None:
        if state.next_query_at is None or now < state.next_query_at:
            return
        if state.queries >= len(NOT_FOUND_QUERY_GAPS_SECS):
            state.contradicted = True
            _log.error(
                "nt_cancel_venue_state_contradicted",
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=str(order.client_order_id),
                queries=state.queries,
            )
            return
        if self._query(strategy, order):
            state.queries += 1
            state.next_query_at = now + NOT_FOUND_QUERY_GAPS_SECS[state.queries - 1]

    def _follow_up_in_flight(
        self, strategy: Any, order: Any, state: _OrderState, now: float, pending: bool
    ) -> None:
        if state.in_flight_queried:
            if not pending:
                # The order is no longer pending, yet no answer explained why: the
                # cancel was lost somewhere, and the order is open again.
                self._refused(str(order.client_order_id), UNKNOWN, now)
            return
        awaiting_since = state.awaiting_since or now
        if now - awaiting_since >= IN_FLIGHT_QUERY_AFTER_SECS and self._query(strategy, order):
            state.in_flight_queried = True

    def _query(self, strategy: Any, order: Any) -> bool:
        query = getattr(strategy, "query_order", None)
        if not callable(query):
            return False
        if not self._partition.take():
            return False
        query(order)
        _log.info(
            "nt_cancel_order_queried",
            deployment_instance_id=self._deployment_instance_id,
            client_order_id=str(order.client_order_id),
        )
        return True
