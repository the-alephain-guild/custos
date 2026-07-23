from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.contracts.crucible_runner_safety_policy import (
    RunnerAggregateCapPolicyV1,
    VerifiedRunnerSafetyPolicy,
)
from custos.contracts.deployment import (
    DOMAIN_EVENT_ENCODING,
    DOMAIN_EVENT_SIGNATURE_PROFILE,
)
from custos.core.runner_fact import (
    RUNNER_STATE_SCHEMA_VERSION,
    RunnerFactAuthority,
    RunnerFactIdentity,
    RunnerFactOutbox,
    RunnerStateAuthorityError,
    RunnerStateMigrationError,
)

RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
CAPABILITY_ID = UUID("50000000-0000-4000-8000-000000000005")
SHA_A = "a" * 64
SHA_B = "b" * 64
ARTIFACT_IDENTITY_DIGEST = "c" * 64
ARTIFACT_AUTHORITY_DIGEST = "d" * 64
POLICY_ID = UUID("60000000-0000-4000-8000-000000000006")


def _runner_policy() -> VerifiedRunnerSafetyPolicy:
    policy = RunnerAggregateCapPolicyV1(
        schema_version=1,
        authority_coordinate="crucible.runner-aggregate-cap-policy.v1",
        policy_id=POLICY_ID,
        runner_id=RUNNER_ID,
        tenant_id="acme",
        trading_mode="sandbox",
        revision=1,
        settlement_currency="USDT",
        max_order_notional="100",
        max_total_notional="1000",
        exposure_model="filled_plus_active_reservations",
        breach_action="freeze_risk_increasing",
        risk_reducing_orders="always_permitted",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        status="active",
        previous=None,
        policy_digest="c" * 64,
    )
    return VerifiedRunnerSafetyPolicy(
        policy=policy,
        exact_subject=f"crucible.runner.policy.v1.acme.{RUNNER_ID}.sandbox",
        exact_event_bytes=b'{"verified":"test-only"}',
        exact_signed_envelope_bytes=b'{"signature":"test-only"}',
        signature_key_id="crucible-policy-key",
        fingerprint="d" * 64,
        verified_event_bytes_sha256="e" * 64,
    )


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="acme",
        trading_mode="sandbox",
        runner_id=RUNNER_ID,
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=SPEC_ID,
        deployment_spec_digest=SHA_A,
        generation=1,
        strategy_id=STRATEGY_ID,
        capability_version_id=CAPABILITY_ID,
        capability_version=1,
        capability_manifest_digest=SHA_B,
    )


def _identity() -> RunnerFactIdentity:
    return RunnerFactIdentity(Ed25519PrivateKey.generate(), "runner-fact-test-key")


def _fact(label: str) -> dict[str, str]:
    del label
    return {
        "kind": "heartbeat",
        "event_id": str(uuid4()),
        "status": "online",
        "observed_at": "2026-07-15T08:00:00Z",
    }


def _document(payload: bytes) -> dict:
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def test_existing_outbox_database_upgrades_in_place_to_single_state_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-facts.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE runner_fact_stream (
                stream_key TEXT PRIMARY KEY,
                next_sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE runner_fact_seen_event (
                event_id TEXT PRIMARY KEY,
                stream_key TEXT NOT NULL,
                source_sequence INTEGER NOT NULL,
                batch_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE runner_fact_outbox (
                batch_id TEXT PRIMARY KEY,
                stream_key TEXT NOT NULL,
                subject TEXT NOT NULL,
                source_seq_start INTEGER NOT NULL,
                source_seq_end INTEGER NOT NULL,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                UNIQUE(stream_key, source_seq_start, source_seq_end)
            );
            """
        )

    RunnerFactOutbox(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = connection.execute(
            "SELECT schema_version FROM runner_state_schema WHERE singleton = 1"
        ).fetchone()[0]
    assert version == RUNNER_STATE_SCHEMA_VERSION
    assert {
        "desired_deployments",
        "applied_deployments",
        "command_outcomes",
        "command_in_progress_lease",
        "artifact_activation",
        "runner_cap_policy",
        "runner_cap_policy_head",
        "order_reservation",
        "runner_exposure_checkpoint",
        "runner_fact_outbox",
    } <= tables


def test_newer_database_schema_is_never_silently_downgraded(tmp_path: Path) -> None:
    database = tmp_path / "runner-facts.sqlite3"
    RunnerFactOutbox(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runner_state_schema SET schema_version = ? WHERE singleton = 1",
            (RUNNER_STATE_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(RunnerStateMigrationError, match="canonical first-production V1"):
        RunnerFactOutbox(database)


@pytest.mark.asyncio
async def test_instance_stream_continues_across_spec_and_generation_changes(
    tmp_path: Path,
) -> None:
    outbox = RunnerFactOutbox(tmp_path / "runner-facts.sqlite3")
    identity = _identity()
    first = _authority()
    second = replace(
        first,
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000099"),
        deployment_spec_digest="c" * 64,
        generation=2,
    )

    await outbox.enqueue(first, identity, [_fact("first")])
    await outbox.enqueue(second, identity, [_fact("second")])
    pending = await outbox.pending()
    documents = [_document(batch.payload) for batch in pending]

    assert first.stream_key == second.stream_key
    assert first.subject == second.subject
    assert str(first.deployment_spec_id) not in first.stream_key
    assert first.deployment_spec_digest not in first.subject
    assert [document["source_seq_start"] for document in documents] == [1, 2]
    assert [document["generation"] for document in documents] == [1, 2]
    assert [document["deployment_spec_id"] for document in documents] == [
        str(first.deployment_spec_id),
        str(second.deployment_spec_id),
    ]


COMMAND_GOLDEN_PATH = (
    Path(__file__).resolve().parents[1] / "docs/authority/runner-deployment-command-golden-v1.json"
)
COMMAND_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
OTHER_COMMAND_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
RUNNER_ID = RUNNER_ID


def _encode_base64url(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _compact_json(value: object) -> bytes:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _signed_command_fixture(
    *,
    private_key: Ed25519PrivateKey = COMMAND_SIGNING_KEY,
    mutate_event=None,
) -> tuple[bytes, str]:
    import copy
    import json

    fixture = json.loads(COMMAND_GOLDEN_PATH.read_text(encoding="utf-8"))
    case = fixture["cases"][0]
    subject = case["subject"]
    event = copy.deepcopy(case["event_document"])
    if mutate_event is not None:
        mutate_event(event)
    event_bytes = _compact_json(event)
    subject_bytes = subject.encode()
    framed = b"".join(
        (
            b"CRUCIBLE-DOMAIN-EVENT-V1\0",
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )
    envelope = {
        "schema_version": 1,
        "signature_profile": DOMAIN_EVENT_SIGNATURE_PROFILE,
        "event_encoding": DOMAIN_EVENT_ENCODING,
        "event_bytes": _encode_base64url(event_bytes),
        "signature_key_id": "crucible-command-key-a",
        "signature": _encode_base64url(private_key.sign(framed)),
    }
    return _compact_json(envelope), subject


def _command_authenticator():
    from custos.core.runner_command_intake import CrucibleRunnerCommandAuthenticator

    return CrucibleRunnerCommandAuthenticator(
        expected_tenant_id="acme",
        expected_runner_id=RUNNER_ID,
        allowed_trading_modes=frozenset({"sandbox"}),
        signature_keys={"crucible-command-key-a": COMMAND_SIGNING_KEY.public_key()},
    )


def _verified_command(*, mutate_event=None):
    raw, subject = _signed_command_fixture(mutate_event=mutate_event)
    verified = _command_authenticator().verify(
        subject=subject,
        signed_envelope_bytes=raw,
    )
    return raw, subject, verified


def _runner_authority(verified):
    command = verified.command
    return RunnerFactAuthority(
        tenant_id=command.tenant_id,
        trading_mode=command.trading_mode,
        runner_id=command.runner_id,
        deployment_instance_id=command.deployment_instance_id,
        deployment_spec_id=command.deployment_spec_id,
        deployment_spec_digest=command.deployment_spec_digest,
        generation=command.generation,
        strategy_id=UUID("11000000-0000-4000-8000-000000000011"),
        capability_version_id=UUID("12000000-0000-4000-8000-000000000012"),
        capability_version=1,
        capability_manifest_digest="b" * 64,
    )


def _runner_fact_store(
    database: Path,
    *,
    tenant_id: str = "acme",
    identity=None,
):
    from custos.core.runner_fact import RunnerStateStore

    outbox = RunnerFactOutbox(database)
    signing_identity = identity or RunnerFactIdentity(
        Ed25519PrivateKey.from_private_bytes(bytes(range(65, 97))),
        "runner-state-key",
    )
    store = RunnerStateStore(
        outbox=outbox,
        identity=signing_identity,
        tenant_id=tenant_id,
        runner_id=RUNNER_ID,
        authority_resolver=_runner_authority,
    )
    return outbox, store


@pytest.mark.asyncio
async def test_desired_command_persists_exact_bytes_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, verified = _verified_command()

    first = await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    second = await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )

    assert first.decision.value == "newer"
    assert second.decision.value == "idempotent"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT exact_subject, command_fingerprint, exact_event_bytes,
                   verified_event_bytes_digest, verification_receipt
            FROM desired_deployments
            WHERE deployment_instance_id = ?
            """,
            (str(verified.command.deployment_instance_id),),
        ).fetchone()
    assert row is not None
    assert row[0] == verified.command.verified_subject
    assert row[1] == verified.command_fingerprint
    assert row[2] == verified.command.exact_signed_event_bytes
    assert row[3] == verified.verification_receipt.verified_event_bytes_sha256
    assert verified.verification_receipt.signature_key_id in row[4]


@pytest.mark.asyncio
async def test_applied_commit_is_atomic_with_lifecycle_outbox_and_restart_replay(
    tmp_path: Path,
) -> None:
    import time

    database = tmp_path / "runner-state.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    await store.record_in_progress_lease(
        delivery_id="delivery-applied",
        verified=verified,
        lease_until_ns=time.time_ns() + 60_000_000_000,
    )
    await store.record_artifact_activation(
        verified=verified,
        activation_id="activation-1",
        artifact_identity_digest=ARTIFACT_IDENTITY_DIGEST,
        artifact_authority_digest=ARTIFACT_AUTHORITY_DIGEST,
    )
    await store.record_verified_runner_safety_policy(_runner_policy())
    result = await store.commit_applied_and_enqueue_lifecycle(
        delivery_id="delivery-applied",
        verified=verified,
        engine_handle="engine-1",
        observed_status="running",
        artifact_activation_id="activation-1",
        artifact_policy_id="artifact-policy-1",
    )

    assert result.committed is True
    assert result.durable_disposition.value == "ack"
    assert result.lifecycle_batch_id is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM applied_deployments").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM command_outcomes").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM command_in_progress_lease").fetchone()[0] == 0
        )
        assert connection.execute("SELECT count(*) FROM artifact_activation").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runner_cap_policy").fetchone()[0] == 1

    _, restarted_store = _runner_fact_store(database)
    replay = await restarted_store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    repeated = await restarted_store.commit_applied_and_enqueue_lifecycle(
        delivery_id="delivery-applied-redelivery",
        verified=verified,
        engine_handle="engine-1",
        observed_status="running",
        artifact_activation_id="activation-1",
        artifact_policy_id="artifact-policy-1",
    )
    assert replay.replay_disposition.value == "ack"
    assert repeated.committed is False
    assert repeated.lifecycle_batch_id == result.lifecycle_batch_id
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM command_outcomes").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_signing_failure_rolls_back_applied_state_and_lifecycle_outbox(
    tmp_path: Path,
) -> None:
    import time

    class FailingIdentity(RunnerFactIdentity):
        def sign_batch_payload(self, canonical_payload: bytes) -> str:
            del canonical_payload
            raise RuntimeError("simulated signer crash")

    database = tmp_path / "runner-state.sqlite3"
    identity = FailingIdentity(Ed25519PrivateKey.generate(), "failing-key")
    _, store = _runner_fact_store(database, identity=identity)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    await store.record_in_progress_lease(
        delivery_id="delivery-crash",
        verified=verified,
        lease_until_ns=time.time_ns() + 60_000_000_000,
    )

    with pytest.raises(RuntimeError, match="simulated signer crash"):
        await store.commit_applied_and_enqueue_lifecycle(
            delivery_id="delivery-crash",
            verified=verified,
            engine_handle="engine-1",
            observed_status="running",
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM applied_deployments").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM command_outcomes").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM command_in_progress_lease").fetchone()[0] == 1
        )


@pytest.mark.asyncio
async def test_same_generation_different_exact_bytes_is_terminal_and_quarantined(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, first = _verified_command()
    _, _, conflicting = _verified_command(
        mutate_event=lambda event: event.__setitem__("occurred_at", "2026-07-15T12:00:01Z")
    )
    await store.record_desired_command(
        command=first.command,
        command_fingerprint=first.command_fingerprint,
        verification_receipt=first.verification_receipt,
    )
    conflict = await store.record_desired_command(
        command=conflicting.command,
        command_fingerprint=conflicting.command_fingerprint,
        verification_receipt=conflicting.verification_receipt,
    )
    outcome = await store.commit_verified_terminal_outcome(
        delivery_id="delivery-conflict",
        verified=conflicting,
        outcome="conflict",
        reason_code="same_generation_different_exact_bytes",
    )
    replay = await store.commit_verified_terminal_outcome(
        delivery_id="delivery-conflict-redelivery",
        verified=conflicting,
        outcome="conflict",
        reason_code="same_generation_different_exact_bytes",
    )

    assert conflict.decision.value == "conflict"
    assert outcome.durable_disposition.value == "term"
    assert replay.committed is False
    with sqlite3.connect(database) as connection:
        desired_status = connection.execute(
            "SELECT desired_status FROM desired_deployments"
        ).fetchone()[0]
        assert desired_status == "quarantined"
        assert connection.execute("SELECT count(*) FROM command_outcomes").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 1


class _RunnerFactDelivery:
    def __init__(self, *, data: bytes, subject: str, database: Path) -> None:
        self.data = data
        self.subject = subject
        self.database = database
        self.delivery_id = "delivery-invalid-signature"
        self.delivered_count = 1
        self.events: list[str] = []

    async def ack(self) -> None:
        self.events.append("ack")

    async def nak(self, delay: float | None = None) -> None:
        self.events.append(f"nak:{delay}")

    async def term(self) -> None:
        with sqlite3.connect(self.database) as connection:
            assert connection.execute("SELECT count(*) FROM command_outcomes").fetchone()[0] == 1
        self.events.append("term")

    async def in_progress(self) -> None:
        self.events.append("in_progress")


@pytest.mark.asyncio
async def test_untrusted_rejection_is_durable_before_terminal_disposition(
    tmp_path: Path,
) -> None:
    from custos.core.runner_command_intake import CommandDeliveryPolicy, CommandIntakeCoordinator

    database = tmp_path / "runner-state.sqlite3"
    _, store = _runner_fact_store(database)
    raw, subject = _signed_command_fixture(private_key=OTHER_COMMAND_SIGNING_KEY)
    delivery = _RunnerFactDelivery(data=raw, subject=subject, database=database)
    coordinator = CommandIntakeCoordinator(
        authenticator=_command_authenticator(),
        durability=store,
        policy=CommandDeliveryPolicy(),
    )

    result = await coordinator.process(delivery)

    assert result.status.value == "terminal_untrusted_rejection"
    assert delivery.events == ["term"]


@pytest.mark.asyncio
async def test_local_reservation_exposure_and_cross_tenant_guards(tmp_path: Path) -> None:
    database = tmp_path / "runner-state.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    await store.record_verified_runner_safety_policy(_runner_policy())
    await store.reserve_order_notional(
        event_id="reserve-order-1",
        deployment_instance_id=verified.command.deployment_instance_id,
        client_order_id="order-1",
        policy_id=POLICY_ID,
        requested_notional=Decimal("100"),
    )
    await store.record_order_fill(
        event_id="fill-order-1",
        deployment_instance_id=verified.command.deployment_instance_id,
        client_order_id="order-1",
        fill_notional=Decimal("40"),
    )
    await store.record_exposure_checkpoint_reference(
        policy_id=str(POLICY_ID),
        open_exposure="100.00",
        reconstructed_at_ns=10,
        source_digest="d" * 64,
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM order_reservation").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM runner_exposure_checkpoint").fetchone()[0] == 1
        )

    _, wrong_tenant_store = _runner_fact_store(
        tmp_path / "other-tenant.sqlite3",
        tenant_id="other-tenant",
    )
    with pytest.raises(RunnerStateAuthorityError):
        await wrong_tenant_store.record_desired_command(
            command=verified.command,
            command_fingerprint=verified.command_fingerprint,
            verification_receipt=verified.verification_receipt,
        )


@pytest.mark.asyncio
async def test_engine_restart_budget_survives_reopen_and_applied_commit(
    tmp_path: Path,
) -> None:
    import time

    database = tmp_path / "runner-state.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    deadline = time.time_ns() + 60_000_000_000
    await store.record_in_progress_lease(
        delivery_id="delivery-restart",
        verified=verified,
        lease_until_ns=deadline,
    )
    assert (
        await store.record_engine_restart(
            delivery_id="delivery-restart",
            verified=verified,
            reason_code="engine_ready_timeout",
            lease_until_ns=deadline,
        )
        == 1
    )
    assert (
        await store.record_engine_restart(
            delivery_id="delivery-restart",
            verified=verified,
            reason_code="zombie_disconnect",
            lease_until_ns=deadline,
        )
        == 2
    )

    _, reopened = _runner_fact_store(database)
    before = await reopened.load_engine_lifecycle_state(verified)
    assert before.restart_count == 2
    assert before.applied_generation is None

    await reopened.commit_applied_and_enqueue_lifecycle(
        delivery_id="delivery-restart",
        verified=verified,
        engine_handle="engine-handle",
        observed_status="ready",
    )
    after = await reopened.load_engine_lifecycle_state(verified)
    assert after.restart_count == 2
    assert after.applied_generation == verified.command.generation
    assert after.observed_status == "ready"
