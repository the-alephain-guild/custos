"""RunnerFact SQLite outbox characterization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    PendingRunnerFactBatch,
    RunnerFactAuthority,
    RunnerFactIdentity,
    RunnerFactJetStreamPublisher,
    RunnerFactOutbox,
)

_RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
_INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
_SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
_STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
_CAPABILITY_ID = UUID("50000000-0000-4000-8000-000000000005")
_SPEC_DIGEST = "a" * 64
_CAPABILITY_DIGEST = "b" * 64


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="acme",
        trading_mode="sandbox",
        runner_id=_RUNNER_ID,
        deployment_instance_id=_INSTANCE_ID,
        deployment_spec_id=_SPEC_ID,
        deployment_spec_digest=_SPEC_DIGEST,
        generation=1,
        strategy_id=_STRATEGY_ID,
        capability_version_id=_CAPABILITY_ID,
        capability_version=1,
        capability_manifest_digest=_CAPABILITY_DIGEST,
    )


def _identity() -> RunnerFactIdentity:
    return RunnerFactIdentity(Ed25519PrivateKey.generate(), "characterization-key")


def _fact(
    authority: RunnerFactAuthority,
    *,
    generation: int,
    lifecycle_state: str,
) -> dict[str, Any]:
    return RunnerDeploymentLifecycleFact.observed(
        authority,
        generation=generation,
        lifecycle_state=lifecycle_state,
        command_fingerprint=_SPEC_DIGEST,
        outcome="applied",
    ).to_wire()


def _document(batch: PendingRunnerFactBatch) -> dict[str, Any]:
    document = json.loads(batch.payload)
    assert isinstance(document, dict)
    return document


@pytest.mark.asyncio
async def test_sequences_are_monotonic_per_stream_and_duplicate_events_are_deduplicated(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    identity = _identity()
    first_authority = _authority()
    first_fact = _fact(first_authority, generation=1, lifecycle_state="running")
    second_fact = _fact(first_authority, generation=1, lifecycle_state="running")

    assert await outbox.enqueue(first_authority, identity, [first_fact]) is not None
    assert await outbox.enqueue(first_authority, identity, [first_fact]) is None
    assert await outbox.enqueue(first_authority, identity, [second_fact]) is None

    second_authority = replace(
        first_authority,
        deployment_instance_id=UUID("20000000-0000-4000-8000-000000000099"),
    )
    assert (
        await outbox.enqueue(
            second_authority,
            identity,
            [_fact(second_authority, generation=1, lifecycle_state="running")],
        )
        is not None
    )

    pending = await outbox.pending()
    first_stream = [batch for batch in pending if batch.stream_key == first_authority.stream_key]
    second_stream = [batch for batch in pending if batch.stream_key == second_authority.stream_key]

    assert [
        (_document(batch)["source_seq_start"], _document(batch)["source_seq_end"])
        for batch in first_stream
    ] == [(1, 1)]
    assert [_document(batch)["facts"][0]["seq"] for batch in first_stream] == [1]
    assert [_document(batch)["facts"][0]["seq"] for batch in second_stream] == [1]


@pytest.mark.asyncio
async def test_signed_pending_batch_survives_outbox_reopen(tmp_path: Path) -> None:
    database = tmp_path / "runner-facts.sqlite3"
    outbox = RunnerFactOutbox(database)
    authority = _authority()
    batch_id = await outbox.enqueue(
        authority,
        _identity(),
        [_fact(authority, generation=4, lifecycle_state="running")],
    )
    assert batch_id is not None

    before = (await outbox.pending())[0]
    document = _document(before)
    reopened = RunnerFactOutbox(database)
    after = (await reopened.pending())[0]

    assert after == before
    assert document["batch_id"] == str(batch_id)
    assert document["key_id"] == "characterization-key"
    assert len(document["payload_digest"]) == 64
    assert document["signature"]
    assert document["facts"][0]["generation"] == 4


@pytest.mark.asyncio
async def test_record_failure_retains_pending_batch_and_increments_attempts(tmp_path: Path) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    authority = _authority()
    batch_id = await outbox.enqueue(
        authority,
        _identity(),
        [_fact(authority, generation=1, lifecycle_state="running")],
    )
    assert batch_id is not None

    await outbox.record_failure(batch_id, RuntimeError("PubAck unavailable"))
    pending = await outbox.pending()

    assert len(pending) == 1
    assert pending[0].batch_id == batch_id
    assert pending[0].attempts == 1


class _JetStreamStub:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        ack: object | None = None,
    ) -> None:
        self.failure = failure
        self.ack = ack or _PubAckStub()
        self.calls: list[tuple[str, bytes]] = []

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> object:
        del headers, timeout
        self.calls.append((subject, payload))
        if self.failure is not None:
            raise self.failure
        return self.ack


class _PubAckStub:
    stream = "RUNNER_FACTS"
    seq = 41
    domain = "SIM"
    duplicate = False


class _MissingSequencePubAckStub:
    stream = "RUNNER_FACTS"


class _ConnectedNatsStub:
    is_connected = True


class _TransportProfile:
    def assert_active(self) -> None:
        return None

    def assert_publish_subject(self, subject: str) -> None:
        assert subject.startswith("crucible.runner.fact.v1.")


def _publisher(outbox: RunnerFactOutbox, jetstream: _JetStreamStub) -> RunnerFactJetStreamPublisher:
    publisher = RunnerFactJetStreamPublisher(
        connection_profiles={"sandbox": _TransportProfile()},
        outbox=outbox,
        runner_id=_RUNNER_ID,
        authority_guard=lambda: None,
    )
    publisher._nats = {"sandbox": _ConnectedNatsStub()}  # noqa: SLF001 - no live broker.
    publisher._jetstreams = {"sandbox": jetstream}  # noqa: SLF001 - characterize PubAck.
    return publisher


@pytest.mark.asyncio
async def test_failed_outbound_publish_retains_batch_without_outbox_acknowledge(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    authority = _authority()
    assert (
        await outbox.enqueue(
            authority,
            _identity(),
            [_fact(authority, generation=1, lifecycle_state="running")],
        )
        is not None
    )

    published = await _publisher(
        outbox, _JetStreamStub(failure=RuntimeError("no PubAck"))
    ).drain_once()
    pending = await outbox.pending()

    assert published == 0
    assert len(pending) == 1
    assert pending[0].attempts == 1


@pytest.mark.asyncio
async def test_successful_outbound_puback_acknowledges_and_deletes_pending_batch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-facts.sqlite3"
    outbox = RunnerFactOutbox(database)
    authority = _authority()
    batch_id = await outbox.enqueue(
        authority,
        _identity(),
        [_fact(authority, generation=1, lifecycle_state="running")],
    )
    assert batch_id is not None
    jetstream = _JetStreamStub()

    published = await _publisher(outbox, jetstream).drain_once()
    receipt = await RunnerFactOutbox(database).publication_receipt(batch_id)

    assert published == 1
    assert len(jetstream.calls) == 1
    assert await outbox.pending() == []
    assert receipt is not None
    assert receipt.batch_id == batch_id
    assert receipt.subject == authority.subject
    assert receipt.batch_payload_sha256
    assert receipt.broker_stream == "RUNNER_FACTS"
    assert receipt.broker_sequence == 41
    assert receipt.broker_domain == "SIM"
    assert receipt.duplicate is False
    assert receipt.publish_attempts == 0


@pytest.mark.asyncio
async def test_puback_without_broker_sequence_keeps_the_batch_pending(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    authority = _authority()
    batch_id = await outbox.enqueue(
        authority,
        _identity(),
        [_fact(authority, generation=1, lifecycle_state="running")],
    )
    assert batch_id is not None

    published = await _publisher(
        outbox,
        _JetStreamStub(ack=_MissingSequencePubAckStub()),
    ).drain_once()

    pending = await outbox.pending()
    assert published == 0
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert await outbox.publication_receipt(batch_id) is None
