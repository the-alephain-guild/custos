"""Source delivery, order and fills remain distinct after a runner restart."""

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
    inbox = ExternalTargetInbox(path)
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
    assert second_link.filled_quantity == "0"


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
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    assert not inbox.record_order_fill(
        FIRST,
        client_order_id="O-1",
        event_id="fill-1",
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ExternalTargetInboxError, match="conflicts"):
        inbox.record_order_fill(
            FIRST,
            client_order_id="O-1",
            event_id="fill-1",
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
        venue_trade_id="T-1",
        quantity="0.4",
        at=NOW + timedelta(seconds=1),
    )
    assert inbox.order_execution(FIRST, "external-order") is None
