"""One stream's backlog must not hold the publisher's only page.

``pending()`` reads a bounded page ordered by stream. A stream whose batches fill
that page keeps every other stream out of the window, and the sender used to stop
at the page boundary, so the healthy streams were never reached on any round. The
tests below fill a page with a stream the broker rejects and then require the rest
to go out in the same round.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
)

_RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
_BLOCKED_INSTANCE = UUID("20000000-0000-4000-8000-000000000002")
_HEALTHY_INSTANCE = UUID("20000000-0000-4000-8000-000000000003")
_SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
_STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
_CAPABILITY_ID = UUID("50000000-0000-4000-8000-000000000005")
_DIGEST = "a" * 64

# The sender reads one page of this size; the backlog has to exceed it for the
# starvation to show up at all.
_PAGE_SIZE = 64


def _authority(
    *,
    trading_mode: str = "sandbox",
    deployment_instance_id: UUID = _BLOCKED_INSTANCE,
) -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="acme",
        trading_mode=trading_mode,
        runner_id=_RUNNER_ID,
        deployment_instance_id=deployment_instance_id,
        deployment_spec_id=_SPEC_ID,
        deployment_spec_digest=_DIGEST,
        generation=1,
        strategy_id=_STRATEGY_ID,
        capability_version_id=_CAPABILITY_ID,
        capability_version=1,
        capability_manifest_digest="b" * 64,
    )


def _identity() -> RunnerFactIdentity:
    return RunnerFactIdentity(Ed25519PrivateKey.generate(), "starvation-key")


def _fact(authority: RunnerFactAuthority, *, generation: int) -> dict[str, Any]:
    return RunnerDeploymentLifecycleFact.observed(
        authority,
        generation=generation,
        lifecycle_state="running",
        command_fingerprint=_DIGEST,
        outcome="applied",
    ).to_wire()


async def _fill_page(outbox: RunnerFactOutbox, authority: RunnerFactAuthority) -> None:
    """Enqueue one more batch than the sender's page holds."""
    identity = _identity()
    for generation in range(1, _PAGE_SIZE + 2):
        assert (
            await outbox.enqueue(authority, identity, [_fact(authority, generation=generation)])
            is not None
        )


class _PubAck:
    stream = "RUNNER_FACTS"
    seq = 41
    domain = "SIM"
    duplicate = False


class _SignalPubAck:
    stream = "CRUCIBLE_STRATEGY_SIGNAL_V1"
    seq = 9
    domain = "SIM"
    duplicate = False


class _SelectiveJetStream:
    """A broker that refuses one deployment instance and serves every other."""

    def __init__(self, *, refuses: UUID, ack: object | None = None) -> None:
        self._refuses = str(refuses)
        self._ack = ack or _PubAck()
        self.accepted: list[str] = []

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> object:
        del headers, timeout
        document = json.loads(payload)
        instance = _instance_of(document)
        if instance == self._refuses:
            raise RuntimeError("broker refuses this deployment instance")
        self.accepted.append(subject)
        return self._ack


def _instance_of(document: Any) -> str | None:
    """Both the batch envelope and the signal envelope carry the instance at the top."""
    if not isinstance(document, dict):
        return None
    return str(document.get("deployment_instance_id"))


class _ConnectedNats:
    is_connected = True


class _AnyTransport:
    def assert_active(self) -> None:
        return None

    def assert_publish_subject(self, subject: str) -> None:
        assert subject.startswith("crucible.runner.")


def _publisher(
    outbox: RunnerFactOutbox,
    jetstreams: dict[str, Any],
) -> RunnerFactJetStreamPublisher:
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={mode: _AnyTransport() for mode in jetstreams},
        outbox=outbox,
        runner_id=_RUNNER_ID,
        authority_guard=lambda: None,
    )
    publisher._nats = {mode: _ConnectedNats() for mode in jetstreams}  # noqa: SLF001 - no broker.
    publisher._jetstreams = dict(jetstreams)  # noqa: SLF001 - no broker.
    return publisher


@pytest.mark.asyncio
async def test_a_full_page_of_one_stream_still_leaves_the_other_mode_a_turn(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    blocked = _authority(trading_mode="sandbox")
    healthy = _authority(trading_mode="testnet", deployment_instance_id=_HEALTHY_INSTANCE)
    await _fill_page(outbox, blocked)
    assert await outbox.enqueue(healthy, _identity(), [_fact(healthy, generation=1)]) is not None

    sandbox = _SelectiveJetStream(refuses=_BLOCKED_INSTANCE)
    testnet = _SelectiveJetStream(refuses=_BLOCKED_INSTANCE)

    delivered = await _publisher(outbox, {"sandbox": sandbox, "testnet": testnet}).drain_once()

    assert delivered == 1
    assert testnet.accepted == [healthy.subject]
    remaining = await outbox.pending()
    assert all(batch.stream_key == blocked.stream_key for batch in remaining)


@pytest.mark.asyncio
async def test_a_blocked_instance_does_not_starve_its_sibling_in_the_same_mode(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    blocked = _authority()
    sibling = _authority(deployment_instance_id=_HEALTHY_INSTANCE)
    assert blocked.stream_key < sibling.stream_key, "the blocked stream must own the first page"
    await _fill_page(outbox, blocked)
    assert await outbox.enqueue(sibling, _identity(), [_fact(sibling, generation=1)]) is not None

    jetstream = _SelectiveJetStream(refuses=_BLOCKED_INSTANCE)

    delivered = await _publisher(outbox, {"sandbox": jetstream}).drain_once()

    assert delivered == 1
    assert jetstream.accepted == [sibling.subject]


@pytest.mark.asyncio
async def test_the_blocked_stream_is_charged_one_attempt_not_a_whole_page(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    blocked = _authority()
    sibling = _authority(deployment_instance_id=_HEALTHY_INSTANCE)
    await _fill_page(outbox, blocked)
    assert await outbox.enqueue(sibling, _identity(), [_fact(sibling, generation=1)]) is not None

    await _publisher(
        outbox, {"sandbox": _SelectiveJetStream(refuses=_BLOCKED_INSTANCE)}
    ).drain_once()

    attempted = [batch for batch in await outbox.pending() if batch.attempts > 0]
    assert len(attempted) == 1, "only the head of the blocked stream may be retried per round"


@pytest.mark.asyncio
async def test_the_order_inside_the_unblocked_stream_is_preserved(tmp_path: Path) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    blocked = _authority()
    sibling = _authority(deployment_instance_id=_HEALTHY_INSTANCE)
    await _fill_page(outbox, blocked)
    identity = _identity()
    for generation in range(1, 4):
        assert (
            await outbox.enqueue(sibling, identity, [_fact(sibling, generation=generation)])
            is not None
        )

    class _RecordingJetStream(_SelectiveJetStream):
        def __init__(self) -> None:
            super().__init__(refuses=_BLOCKED_INSTANCE)
            self.generations: list[int] = []

        async def publish(self, subject, payload, *, headers, timeout):  # type: ignore[no-untyped-def]
            ack = await super().publish(subject, payload, headers=headers, timeout=timeout)
            document = json.loads(payload)
            self.generations.append(int(document["facts"][0]["generation"]))
            return ack

    jetstream = _RecordingJetStream()
    delivered = await _publisher(outbox, {"sandbox": jetstream}).drain_once()

    assert delivered == 3
    assert jetstream.generations == [1, 2, 3]


@pytest.mark.asyncio
async def test_every_stream_blocked_ends_the_round_without_spinning(tmp_path: Path) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    first = _authority()
    second = _authority(deployment_instance_id=_HEALTHY_INSTANCE)
    await _fill_page(outbox, first)
    assert await outbox.enqueue(second, _identity(), [_fact(second, generation=1)]) is not None

    class _RefuseEverything(_SelectiveJetStream):
        def __init__(self) -> None:
            super().__init__(refuses=_BLOCKED_INSTANCE)
            self.calls = 0

        async def publish(self, subject, payload, *, headers, timeout):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("broker is down")

    jetstream = _RefuseEverything()
    delivered = await _publisher(outbox, {"sandbox": jetstream}).drain_once()

    assert delivered == 0
    assert jetstream.calls == 2, "one attempt per stream, then the round ends"


@pytest.mark.asyncio
async def test_a_blocked_signal_stream_does_not_starve_the_healthy_one(tmp_path: Path) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-state.sqlite3")
    blocked = _authority()
    sibling = _authority(deployment_instance_id=_HEALTHY_INSTANCE)
    identity = _identity()
    for index in range(_PAGE_SIZE + 1):
        assert (
            await outbox.enqueue_strategy_signal(
                blocked,
                identity,
                fact_id=UUID(f"60000000-0000-4000-8000-{index:012d}"),
                instrument="BTC-USDT",
                timeframe="1m",
                direction="long",
                occurred_at=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
                input_digest="c" * 64,
                strategy_version="supertrend-v1",
                trace_id=UUID(f"70000000-0000-4000-8000-{index:012d}"),
            )
            is not None
        )
    healthy_fact = UUID("61000000-0000-4000-8000-000000000001")
    assert (
        await outbox.enqueue_strategy_signal(
            sibling,
            identity,
            fact_id=healthy_fact,
            instrument="ETH-USDT",
            timeframe="1m",
            direction="short",
            occurred_at=datetime(2026, 8, 13, 2, 1, tzinfo=UTC),
            input_digest="d" * 64,
            strategy_version="supertrend-v1",
            trace_id=UUID("71000000-0000-4000-8000-000000000001"),
        )
        is not None
    )

    jetstream = _SelectiveJetStream(refuses=_BLOCKED_INSTANCE, ack=_SignalPubAck())
    delivered = await _publisher(outbox, {"sandbox": jetstream}).drain_once()

    assert delivered == 1
    assert len(jetstream.accepted) == 1
    receipt = await outbox.strategy_signal_publication_receipt(healthy_fact)
    assert receipt is not None
