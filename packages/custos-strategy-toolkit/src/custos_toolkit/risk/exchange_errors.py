"""Exchange order-rejection classification (plan 13).

Platform-neutral pure logic: classifies an order-rejection reason string into a
backoff tier so the strategy can react differently to transient server/rate-limit
errors vs. business-logic rejections.

- ``"server"``: gateway/timeout/rate-limit errors (5xx, -1007, -1003, -1015, ...).
  These mean the venue couldn't process the request; the right reaction is a long
  backoff / circuit-break, and NOT hammering the endpoint (which burns order quota).
- ``"logic"``: business rejections (e.g. -2022 ReduceOnly rejected, -2019 margin).
  These are deterministic; a short backoff (after clearing the blocking condition)
  is appropriate.

Kept under ``shared/risk`` (no nautilus/msgspec deps) so it is unit-testable in any
environment, per the platform-neutral module rules.
"""

from __future__ import annotations

# Substrings (lower-cased) that mark a transient server-side / rate-limit error.
# Binance numeric codes are matched as substrings of the reason payload string.
_SERVER_ERROR_MARKERS: tuple[str, ...] = (
    "-1000",  # UNKNOWN — internal error
    "-1001",  # DISCONNECTED — internal disconnect
    "-1003",  # TOO_MANY_REQUESTS — rate limited
    "-1007",  # TIMEOUT — backend timeout, execution status unknown
    "-1015",  # TOO_MANY_ORDERS — order rate limited
    "-1016",  # SERVICE_SHUTTING_DOWN
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "timeout",
    "too many",
    "<!doctype",
    "<html",
)


def classify_rejection_reason(reason: str | None) -> str:
    """Classify an order-rejection reason into a backoff tier.

    Returns ``"server"`` for transient gateway/timeout/rate-limit errors that
    warrant a long backoff / circuit-break, otherwise ``"logic"`` (the safe
    default for deterministic business rejections and unrecognized reasons).
    """
    if not reason:
        return "logic"
    lowered = reason.lower()
    if any(marker in lowered for marker in _SERVER_ERROR_MARKERS):
        return "server"
    return "logic"


# Positive markers for "the venue refused reduce-only itself". Deliberately narrow:
# this is the only evidence that may drop reduce_only from a close order, and dropping
# it makes the order capable of opening a reverse position.
_REDUCE_ONLY_REFUSAL_MARKERS = ("-2022", "reduceonly", "reduce only", "reduce-only")


def is_reduce_only_refusal(reason: str | None) -> bool:
    """Whether the venue specifically refused the reduce-only form of an order.

    ``classify_rejection_reason`` answers a different question: it buckets a rejection
    for backoff purposes, and its ``"logic"`` bucket is the documented default for
    *unrecognised* reasons. That default is safe for deciding how long to wait, and
    unsafe for deciding to drop reduce_only -- an empty or unknown reason would arm an
    escape hatch on no evidence at all, which is how an order that may already have
    filled turns into a new position in the opposite direction.

    So this requires positive evidence, and says False when it does not have it. A
    margin rejection (-2019) shares the ``"logic"`` bucket and is not one.
    """
    if not reason:
        return False
    lowered = reason.lower()
    return any(marker in lowered for marker in _REDUCE_ONLY_REFUSAL_MARKERS)
