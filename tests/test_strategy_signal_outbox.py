from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
)


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="tenant-a",
        trading_mode="testnet",
        runner_id=UUID("10000000-0000-4000-8000-000000000001"),
        deployment_instance_id=UUID("20000000-0000-4000-8000-000000000001"),
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000001"),
        deployment_spec_digest="a" * 64,
        generation=3,
        strategy_id=UUID("40000000-0000-4000-8000-000000000001"),
        capability_version_id=UUID("50000000-0000-4000-8000-000000000001"),
        capability_version=2,
        capability_manifest_digest="b" * 64,
    )


def _identity() -> RunnerFactIdentity:
    return RunnerFactIdentity.from_private_bytes(
        bytes(range(32)),
        "ed25519-56475aa75463474c0285df5dbf2bcab7",
    )


@pytest.mark.asyncio
async def test_signal_survives_restart_with_exact_capability_scope(tmp_path) -> None:
    database = tmp_path / "runner-state.sqlite3"
    outbox = RunnerFactOutbox(database)
    fact_id = UUID("60000000-0000-4000-8000-000000000001")

    inserted = await outbox.enqueue_strategy_signal(
        _authority(),
        _identity(),
        fact_id=fact_id,
        instrument="BTC-USDT",
        timeframe="1m",
        direction="long",
        occurred_at=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
        input_digest="c" * 64,
        strategy_version="supertrend-v1",
        trace_id=UUID("70000000-0000-4000-8000-000000000001"),
    )
    assert inserted == fact_id

    pending = await RunnerFactOutbox(database).pending_strategy_signals()
    assert len(pending) == 1
    document = json.loads(pending[0].payload)
    assert document["source_sequence"] == 1
    assert document["capability_version_id"] == "50000000-0000-4000-8000-000000000001"
    assert document["capability_version"] == 2
    assert document["capability_manifest_digest"] == "b" * 64
    assert pending[0].subject == document["subject"]


@pytest.mark.asyncio
async def test_signal_replay_does_not_allocate_a_second_sequence(tmp_path) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-state.sqlite3")
    values = dict(
        fact_id=UUID("60000000-0000-4000-8000-000000000001"),
        instrument="BTC-USDT",
        timeframe="1m",
        direction="long",
        occurred_at=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
        input_digest="c" * 64,
        strategy_version="supertrend-v1",
        trace_id=UUID("70000000-0000-4000-8000-000000000001"),
    )
    assert await outbox.enqueue_strategy_signal(_authority(), _identity(), **values)
    assert await outbox.enqueue_strategy_signal(_authority(), _identity(), **values) is None
    assert len(await outbox.pending_strategy_signals()) == 1


class _SignalPubAck:
    stream = "CRUCIBLE_STRATEGY_SIGNAL_V1"
    seq = 9
    domain = "SIM"
    duplicate = False


class _SignalJetStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> _SignalPubAck:
        del timeout
        self.calls.append((subject, payload, headers))
        return _SignalPubAck()


class _ConnectedNats:
    is_connected = True


class _SignalTransport:
    def assert_active(self) -> None:
        return None

    def assert_publish_subject(self, subject: str) -> None:
        assert subject.startswith("crucible.runner.strategy-signal.v1.")


@pytest.mark.asyncio
async def test_signal_is_retired_only_after_jetstream_puback(tmp_path) -> None:
    database = tmp_path / "runner-state.sqlite3"
    outbox = RunnerFactOutbox(database)
    fact_id = await outbox.enqueue_strategy_signal(
        _authority(),
        _identity(),
        fact_id=UUID("60000000-0000-4000-8000-000000000001"),
        instrument="BTC-USDT",
        timeframe="1m",
        direction="long",
        occurred_at=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
        input_digest="c" * 64,
        strategy_version="supertrend-v1",
        trace_id=UUID("70000000-0000-4000-8000-000000000001"),
    )
    assert fact_id is not None
    jetstream = _SignalJetStream()
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"testnet": _SignalTransport()},
        outbox=outbox,
        runner_id=_authority().runner_id,
        authority_guard=lambda: None,
    )
    publisher._nats = {"testnet": _ConnectedNats()}  # noqa: SLF001
    publisher._jetstreams = {"testnet": jetstream}  # noqa: SLF001

    assert await publisher.drain_once() == 1
    assert await outbox.pending_strategy_signals() == []
    receipt = await RunnerFactOutbox(database).strategy_signal_publication_receipt(fact_id)
    assert receipt is not None
    assert receipt.broker_stream == "CRUCIBLE_STRATEGY_SIGNAL_V1"
    assert receipt.broker_sequence == 9
    assert jetstream.calls[0][2] == {"Nats-Msg-Id": str(fact_id)}
