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
        for _ in range(2):
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
        for message in messages:
            await message.ack()
        for sub in subscriptions:
            await sub.unsubscribe()
    finally:
        if created:
            await stream.delete_stream(SIGNAL_STREAM_NAME)
        await client.close()
