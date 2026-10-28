"""Compare locally attributed fills with an independent venue ledger interval."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from custos.core.runner_fact_producer import VenueLedgerEvidence
from custos.engines.nautilus.external_target_inbox import TargetDeliveryExecution


@dataclass(frozen=True, slots=True)
class TargetVenueFillComparison:
    """Only known venue order IDs are compared; this is not a final order state."""

    status: Literal["unknown", "disagreement", "matched_known_order_fills"]
    matched_trade_ids: tuple[str, ...] = ()
    local_only_trade_ids: tuple[str, ...] = ()
    venue_only_trade_ids: tuple[str, ...] = ()
    reason: str | None = None


def _utc_ns(value: datetime) -> int | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def compare_known_order_fills(
    target: TargetDeliveryExecution,
    evidence: VenueLedgerEvidence,
    *,
    closed_at: datetime,
) -> TargetVenueFillComparison:
    """Match all fills on known venue order IDs within a trusted collection interval.

    The caller must obtain ``evidence`` from this deployment's own venue reader
    using the same ``closed_at``. No order with zero local fills can be proven
    final here because its venue order ID is not yet known to this journal.
    """
    expected_venue = {"okx_perpetual": "OKX", "binance_perpetual": "BINANCE"}.get(target.connector)
    start_ns = _utc_ns(evidence.coverage_from)
    through_ns = _utc_ns(evidence.observed_through)
    close_ns = _utc_ns(closed_at)
    if (
        expected_venue is None
        or evidence.venue != expected_venue
        or evidence.source != "venue_api"
        or evidence.completeness.get("fills_complete") is not True
        or start_ns is None
        or through_ns is None
        or close_ns is None
        or start_ns >= close_ns
        or through_ns < close_ns
    ):
        return TargetVenueFillComparison("unknown", reason="venue_coverage_unavailable")
    if not target.orders or any(not order.venue_order_ids for order in target.orders):
        return TargetVenueFillComparison("unknown", reason="venue_order_identity_unavailable")
    if any(
        start_ns > order.initialized_at_ns
        or any(fill_ns > close_ns for fill_ns in order.fill_at_ns)
        for order in target.orders
    ):
        return TargetVenueFillComparison("unknown", reason="target_interval_not_covered")

    local = [
        (order.instrument, venue_order_id, venue_trade_id)
        for order in target.orders
        for venue_order_id, venue_trade_id in zip(
            order.venue_order_ids, order.venue_trade_ids, strict=True
        )
    ]
    if len(set(local)) != len(local):
        return TargetVenueFillComparison("disagreement", reason="duplicate_local_trade")
    known_orders = {(instrument, order_id) for instrument, order_id, _ in local}
    venue: list[tuple[str, str, str]] = []
    for row in evidence.fills:
        if not isinstance(row, Mapping):
            return TargetVenueFillComparison("unknown", reason="malformed_venue_fills")
        instrument = row.get("instrument")
        order_id = row.get("venue_order_id")
        trade_id = row.get("venue_trade_id")
        if not all(isinstance(value, str) and value for value in (instrument, order_id, trade_id)):
            return TargetVenueFillComparison("unknown", reason="malformed_venue_fills")
        if (instrument, order_id) in known_orders:
            venue.append((instrument, order_id, trade_id))
    if len(set(venue)) != len(venue):
        return TargetVenueFillComparison("disagreement", reason="duplicate_venue_trade")
    local_only = set(local) - set(venue)
    venue_only = set(venue) - set(local)
    matched = set(local) & set(venue)
    if local_only or venue_only:
        return TargetVenueFillComparison(
            "disagreement",
            matched_trade_ids=tuple(sorted(key[2] for key in matched)),
            local_only_trade_ids=tuple(sorted(key[2] for key in local_only)),
            venue_only_trade_ids=tuple(sorted(key[2] for key in venue_only)),
        )
    return TargetVenueFillComparison(
        "matched_known_order_fills",
        matched_trade_ids=tuple(sorted(key[2] for key in matched)),
    )
