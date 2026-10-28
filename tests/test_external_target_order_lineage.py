"""Source delivery, order and fills remain distinct after a runner restart."""

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)
from tests.test_external_target_inbox import FIRST, NOW, SECOND, directive


def queued(inbox: ExternalTargetInbox, authority):
    delivery = directive(authority)
    inbox.accept(delivery, received_at=NOW)
    assert inbox.begin_delivery(delivery.delivery_id, authority=authority, at=NOW)
    assert inbox.mark_handoff(delivery.delivery_id, authority=authority, at=NOW)
    return delivery


def test_two_deployments_keep_same_client_order_id_separate(tmp_path: Path) -> None:
    path = tmp_path / "target.sqlite"
    with ThreadPoolExecutor(max_workers=2) as workers:
        inbox, _ = workers.map(lambda _: ExternalTargetInbox(path), range(2))
    first, second = queued(inbox, FIRST), queued(inbox, SECOND)
    assert inbox.record_order_initialized(
        FIRST,
        first.delivery_id,
        client_order_id="O-1",
        instrument="ETH-USDT-SWAP.OKX",
        event_id="init-first",
        at=NOW,
    )
    assert inbox.record_order_initialized(
        SECOND,
        second.delivery_id,
        client_order_id="O-1",
        instrument="ETHUSDT-PERP.BINANCE",
        event_id="init-second",
        at=NOW,
    )
    assert inbox.record_order_fill(
        FIRST,
        client_order_id="O-1",
        event_id="fill-first",
        venue_order_id="V-1",
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    restarted = ExternalTargetInbox(path)
    first_link = restarted.order_execution(FIRST, "O-1")
    second_link = restarted.order_execution(SECOND, "O-1")
    assert first_link is not None and second_link is not None
    assert first_link.delivery_id == first.delivery_id
    assert second_link.delivery_id == second.delivery_id
    assert first_link.source_event_id == second_link.source_event_id
    assert first_link.filled_quantity == "0.4"
    assert first_link.venue_trade_ids == ("T-1",)
    assert first_link.venue_order_ids == ("V-1",)
    assert second_link.filled_quantity == "0"

    first_report = restarted.target_execution(FIRST, first.delivery_id)
    second_report = restarted.target_execution(SECOND, second.delivery_id)
    assert first_report is not None and second_report is not None
    assert first_report.status == second_report.status == "queued"
    assert first_report.local_fill_count == 1
    assert second_report.local_fill_count == 0
    assert first_report.orders == (first_link,)
    assert second_report.orders == (second_link,)
    assert restarted.target_execution(SECOND, first.delivery_id) is None


def test_replay_is_idempotent_but_conflicting_order_or_fill_is_refused(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    delivery = queued(inbox, FIRST)
    arguments = dict(
        client_order_id="O-1", instrument="ETH-USDT-SWAP.OKX", event_id="init-1", at=NOW
    )
    assert inbox.record_order_initialized(FIRST, delivery.delivery_id, **arguments)
    assert not inbox.record_order_initialized(FIRST, delivery.delivery_id, **arguments)
    with pytest.raises(ExternalTargetInboxError, match="conflicts"):
        inbox.record_order_initialized(
            FIRST, delivery.delivery_id, **{**arguments, "event_id": "init-2"}
        )
    assert inbox.record_order_fill(
        FIRST,
        client_order_id="O-1",
        event_id="fill-1",
        venue_order_id="V-1",
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    assert not inbox.record_order_fill(
        FIRST,
        client_order_id="O-1",
        event_id="fill-1",
        venue_order_id="V-1",
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ExternalTargetInboxError, match="conflicts"):
        inbox.record_order_fill(
            FIRST,
            client_order_id="O-1",
            event_id="fill-1",
            venue_order_id="V-1",
            venue_trade_id="T-1",
            quantity="0.5",
            at=NOW + timedelta(seconds=1),
        )


def test_wrong_authority_pair_and_flat_target_cannot_claim_order(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    delivery = queued(inbox, FIRST)
    with pytest.raises(ExternalTargetInboxError, match="scope"):
        inbox.record_order_initialized(
            replace(FIRST, generation=2),
            delivery.delivery_id,
            client_order_id="O-1",
            instrument="ETH-USDT-SWAP.OKX",
            event_id="init-1",
            at=NOW,
        )
    with pytest.raises(ExternalTargetInboxError, match="instrument"):
        inbox.record_order_initialized(
            FIRST,
            delivery.delivery_id,
            client_order_id="O-1",
            instrument="BTC-USDT-SWAP.OKX",
            event_id="init-1",
            at=NOW,
        )
    risk = directive(FIRST, target="FLAT", risk_action="FLATTEN")
    inbox.accept(risk, received_at=NOW)
    assert inbox.begin_delivery(risk.delivery_id, authority=FIRST, at=NOW)
    assert inbox.mark_handoff(risk.delivery_id, authority=FIRST, at=NOW)
    with pytest.raises(ExternalTargetInboxError, match="entry"):
        inbox.record_order_initialized(
            FIRST,
            risk.delivery_id,
            client_order_id="O-1",
            instrument="ETH-USDT-SWAP.OKX",
            event_id="init-1",
            at=NOW,
        )


def test_unattributed_fill_is_never_assigned_to_a_target(tmp_path: Path) -> None:
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    queued(inbox, FIRST)
    assert not inbox.record_order_fill(
        FIRST,
        client_order_id="external-order",
        event_id="fill-1",
        venue_order_id="V-1",
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    assert inbox.order_execution(FIRST, "external-order") is None


def test_existing_fill_table_gains_venue_order_identity(tmp_path: Path) -> None:
    path = tmp_path / "predecessor.sqlite"
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE external_target_fill_link (
                 deployment_instance_id TEXT NOT NULL,
                 fill_event_id TEXT NOT NULL,
                 client_order_id TEXT NOT NULL,
                 delivery_id TEXT NOT NULL,
                 venue_trade_id TEXT NOT NULL,
                 quantity TEXT NOT NULL,
                 filled_at_ns INTEGER NOT NULL,
                 PRIMARY KEY (deployment_instance_id, fill_event_id)
               )"""
        )
    with ThreadPoolExecutor(max_workers=2) as workers:
        inbox, _ = workers.map(lambda _: ExternalTargetInbox(path), range(2))
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(external_target_fill_link)")
        }
    assert "venue_order_id" in columns
    delivery = queued(inbox, FIRST)
    assert inbox.record_order_initialized(
        FIRST,
        delivery.delivery_id,
        client_order_id="O-1",
        instrument="ETH-USDT-SWAP.OKX",
        event_id="init-1",
        at=NOW,
    )
    assert inbox.record_order_fill(
        FIRST,
        client_order_id="O-1",
        event_id="fill-1",
        venue_order_id="V-1",
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
