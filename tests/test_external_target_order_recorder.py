"""Native order callbacks bind only tagged source entries to fills."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)
from custos.engines.nautilus.external_target_order_recorder import ExternalTargetOrderRecorder
from tests.test_external_target_inbox import FIRST, NOW, directive


@dataclass
class OrderInitialized:
    client_order_id: str
    instrument_id: str
    event_id: str
    ts_event: int
    tags: list[str] | None


@dataclass
class OrderFilled:
    client_order_id: str
    event_id: str
    venue_order_id: str
    trade_id: str
    last_qty: str
    ts_event: int


def setup(tmp_path: Path):
    path = tmp_path / "target.sqlite"
    inbox = ExternalTargetInbox(path)
    delivery = directive(FIRST)
    inbox.accept(delivery, received_at=NOW)
    assert inbox.begin_delivery(delivery.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(delivery.delivery_id, authority=FIRST, at=NOW)
    return inbox, delivery


def test_order_callbacks_persist_source_to_fill_lineage(tmp_path: Path) -> None:
    inbox, delivery = setup(tmp_path)
    recorder = ExternalTargetOrderRecorder(inbox, FIRST)
    init = OrderInitialized(
        client_order_id="O-1",
        instrument_id="ETH-USDT-SWAP.OKX",
        event_id="init-1",
        ts_event=int(NOW.timestamp() * 1_000_000_000),
        tags=[
            "signal_id:10000000-0000-4000-8000-000000000001",
            f"target_delivery_id:{delivery.delivery_id}",
        ],
    )
    recorder.on_order_event(init)
    recorder.on_order_event(init)
    fill = OrderFilled(
        client_order_id="O-1",
        event_id="fill-1",
        venue_order_id="V-1",
        trade_id="trade-1",
        last_qty="0.4",
        ts_event=int((NOW + timedelta(seconds=1)).timestamp() * 1_000_000_000),
    )
    recorder.on_order_event(fill)
    recorder.on_order_event(fill)
    restarted = ExternalTargetInbox(inbox.path)
    linked = restarted.order_execution(FIRST, "O-1")
    assert linked is not None
    assert linked.delivery_id == delivery.delivery_id
    assert linked.filled_quantity == "0.4"
    assert linked.fill_event_ids == ("fill-1",)
    assert linked.venue_order_ids == ("V-1",)


def test_untagged_order_is_not_attributed_and_bad_target_tag_faults(tmp_path: Path) -> None:
    inbox, _ = setup(tmp_path)
    recorder = ExternalTargetOrderRecorder(inbox, FIRST)
    init = OrderInitialized(
        client_order_id="O-1",
        instrument_id="ETH-USDT-SWAP.OKX",
        event_id="init-1",
        ts_event=int(NOW.timestamp() * 1_000_000_000),
        tags=None,
    )
    recorder.on_order_event(init)
    assert inbox.order_execution(FIRST, "O-1") is None
    init.tags = ["target_delivery_id:bad"]
    with pytest.raises(ExternalTargetInboxError, match="target tag"):
        recorder.on_order_event(init)
