"""Persist source target order lineage from native Nautilus callbacks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from custos_toolkit_nautilus.adapter.signal_correlation import (
    TARGET_DELIVERY_ID_TAG_PREFIX,
    extract_target_delivery_id_from_tags,
)

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)


def _event_time(event: Any) -> datetime:
    value = getattr(event, "ts_event", None)
    if type(value) is not int or value <= 0:
        raise ExternalTargetInboxError("native order event time is invalid")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value // 1_000)


class ExternalTargetOrderRecorder:
    """Only tagged entry orders can claim a source target; fills join by client ID."""

    def __init__(self, inbox: ExternalTargetInbox, authority: EngineLifecycleAuthority) -> None:
        self.inbox = inbox
        self.authority = authority

    def on_order_event(self, event: Any) -> None:
        kind = type(event).__name__
        if kind == "OrderInitialized":
            tags = getattr(event, "tags", None)
            if tags is None:
                return
            if not isinstance(tags, list | tuple):
                raise ExternalTargetInboxError("native order tags are malformed")
            target_tags = [
                tag for tag in tags if str(tag).startswith(TARGET_DELIVERY_ID_TAG_PREFIX)
            ]
            if not target_tags:
                return
            delivery_id = extract_target_delivery_id_from_tags(tags)
            if delivery_id is None:
                raise ExternalTargetInboxError("native order target tag is malformed")
            self.inbox.record_order_initialized(
                self.authority,
                delivery_id,
                client_order_id=str(event.client_order_id),
                instrument=str(event.instrument_id),
                event_id=str(event.event_id),
                at=_event_time(event),
            )
        elif kind == "OrderFilled":
            self.inbox.record_order_fill(
                self.authority,
                client_order_id=str(event.client_order_id),
                event_id=str(event.event_id),
                venue_trade_id=str(event.trade_id),
                quantity=str(event.last_qty),
                at=_event_time(event),
            )
