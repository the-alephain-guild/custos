"""Two distinct consumers receive the same sanitized source event on local NATS."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import nats
import pytest
from nats.js.errors import NotFoundError

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_outbox import ExternalSourceOutbox
from custos.core.external_source_signal import make_source_event_id, parse_external_source_signal
from custos.core.external_source_transport import (
    SIGNAL_STREAM_NAME,
    SourcePublicationWorker,
    bind_signal_consumer,
    subscription_spec,
)
from custos.engines.nautilus.external_source_delivery import SourceSubscriptionBinding
from custos.engines.nautilus.external_source_subscriber import ExternalSourceSubscriber
from custos.engines.nautilus.external_target_dispatcher import ExternalTargetDispatcher
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from custos.engines.nautilus.host import ExternalSourceRuntimeScope


@pytest.mark.asyncio
async def test_two_deployments_receive_one_source_event(tmp_path: Path) -> None:
    url = os.getenv("CUSTOS_TEST_NATS_URL")
    if not url:
        pytest.skip("requires an isolated local NATS server via CUSTOS_TEST_NATS_URL")
    if urlparse(url).hostname not in {"127.0.0.1", "localhost"}:
        pytest.fail("external source fanout test requires a loopback NATS server")

    now = datetime.now(UTC)
    emitted = now - timedelta(seconds=1)
    bar_close = emitted.replace(minute=emitted.minute // 30 * 30, second=0, microsecond=0)
    tenant_id = f"synthetic-{uuid4().hex[:12]}"
    source_id = "synthetic-supertrend"
    event_id = make_source_event_id(
        source_id=source_id,
        strategy_revision="v0.5",
        symbol="ETH-USDT",
        timeframe="30",
        bar_close_time=bar_close,
        target="SHORT",
        risk_action=None,
    )
    signal = parse_external_source_signal(
        {
            "schemaVersion": 1,
            "sourceId": source_id,
            "strategyRevision": "v0.5",
            "symbol": "ETH-USDT",
            "timeframe": "30",
            "barCloseTime": bar_close.isoformat(),
            "timestamp": emitted.isoformat(),
            "maxLag": 60,
            "target": "SHORT",
            "eventId": event_id,
            "signalToken": "synthetic-token-never-published-0123456789",
        },
        tenant_id=tenant_id,
        expected_source_id=source_id,
        allowed_symbols=frozenset({"ETH-USDT"}),
        now=now,
    )
    outbox = ExternalSourceOutbox(tmp_path / "source.sqlite")
    outbox.accept(signal, received_at=now)

    client = await nats.connect(url, connect_timeout=2)
    stream = client.jetstream()
    created = False
    try:
        try:
            await stream.stream_info(SIGNAL_STREAM_NAME)
        except NotFoundError:
            pass
        else:
            pytest.fail("external source fanout test refuses an existing signal stream")
        await stream.add_stream(
            name=SIGNAL_STREAM_NAME,
            subjects=["custos.external-source.v1.>"],
        )
        created = True
        subscriptions = []
        receivers = []
        for connector in ("okx_perpetual", "binance_perpetual"):
            authority = EngineLifecycleAuthority(
                deployment_instance_id=uuid4(),
                deployment_spec_id=uuid4(),
                deployment_spec_digest="a" * 64,
                generation=1,
                trading_mode="testnet",
            )
            spec = subscription_spec(
                tenant_id=tenant_id,
                runner_id=uuid4(),
                authority=authority,
                source_id=source_id,
            )
            await stream.add_consumer(SIGNAL_STREAM_NAME, config=spec.consumer_config)
            subscriptions.append(await bind_signal_consumer(stream, spec))
            binding = SourceSubscriptionBinding(
                tenant_id=tenant_id,
                source_id=source_id,
                source_symbol="ETH-USDT",
                target_pair="ETH-USDT",
                authority=authority,
            )
            scope = ExternalSourceRuntimeScope(
                authority=authority,
                connector=connector,
                allowed_pairs=frozenset({"ETH-USDT"}),
                authorized_leverage=3,
            )
            target_inbox = ExternalTargetInbox(
                tmp_path / f"target-{authority.deployment_instance_id}.sqlite"
            )
            receivers.append(
                (
                    ExternalSourceSubscriber(
                        spec, binding, target_inbox, scope_provider=lambda scope=scope: scope
                    ),
                    target_inbox,
                    authority,
                    scope,
                )
            )

        assert await SourcePublicationWorker(outbox, stream, tenant_id=tenant_id).publish_once(
            at=now
        )
        messages = [await sub.next_msg(timeout=3) for sub in subscriptions]
        assert messages[0].data == messages[1].data
        assert b"signalToken" not in messages[0].data
        assert event_id.encode() in messages[0].data
        received = parse_external_source_signal(
            json.loads(messages[0].data),
            tenant_id=tenant_id,
            expected_source_id=source_id,
            allowed_symbols=frozenset({"ETH-USDT"}),
            now=datetime.now(UTC),
        )
        assert received.event_id == event_id
        delivery_ids = []
        for message, (receiver, target_inbox, authority, scope) in zip(
            messages, receivers, strict=True
        ):
            receipt = await receiver.handle_message(message, at=datetime.now(UTC))
            assert receipt is not None and receipt.status == "accepted"
            pending = target_inbox.pending(authority, at=datetime.now(UTC))
            assert len(pending) == 1 and pending[0].source_event_id == event_id
            delivery_ids.append(pending[0].delivery_id)

            class QueueOnlyHost:
                def __init__(self, authority, scope) -> None:
                    self.authority = authority
                    self.scope = scope
                    self.queued = []

                def external_source_runtime_scope(self, instance_id):
                    assert instance_id == str(self.authority.deployment_instance_id)
                    return self.scope

                def queue_external_source_signal(self, active_authority, signal):
                    assert active_authority == self.authority
                    self.queued.append(signal)
                    return True

            host = QueueOnlyHost(authority, scope)
            dispatched = await ExternalTargetDispatcher(target_inbox, host).process_once(
                authority, at=datetime.now(UTC)
            )
            assert dispatched is not None and dispatched.status == "queued"
            assert len(host.queued) == 1 and host.queued[0].amount is None
            assert host.queued[0].metadata["target_delivery_id"] == pending[0].delivery_id
        assert delivery_ids[0] != delivery_ids[1]
        for sub in subscriptions:
            await sub.unsubscribe()
    finally:
        if created:
            await stream.delete_stream(SIGNAL_STREAM_NAME)
        await client.close()
