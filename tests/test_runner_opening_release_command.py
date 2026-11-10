from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_command_intake import (
    CommandVerificationError,
    CrucibleRunnerCommandAuthenticator,
    UntrustedCommandReason,
)

KEY_ID = "opening-release-fixture"
KEY = Ed25519PrivateKey.from_private_bytes(bytes([104]) * 32)
ROOT = Path(__file__).resolve().parents[1]


def uid(value: int) -> str:
    return str(UUID(int=value))


def json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def signed(
    subject: str, document: dict, key: Ed25519PrivateKey = KEY, key_id: str = KEY_ID
) -> bytes:
    event = dict(document)
    event["payload"] = json.loads(json.dumps(event["payload"], sort_keys=True))
    raw = json_bytes(event)
    topic = subject.encode()
    frame = (
        b"CRUCIBLE-DOMAIN-EVENT-V1\0"
        + len(topic).to_bytes(4, "big")
        + topic
        + len(raw).to_bytes(8, "big")
        + raw
    )
    return json_bytes(
        {
            "schema_version": 1,
            "signature_profile": "crucible-domain-event-v1-exact-bytes",
            "event_encoding": "application/json;base64url",
            "event_bytes": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
            "signature_key_id": key_id,
            "signature": base64.urlsafe_b64encode(key.sign(frame)).rstrip(b"=").decode(),
        }
    )


def release_document() -> dict:
    at = "2026-11-10T05:00:00Z"
    return {
        "schema_version": 1,
        "event_id": uid(2),
        "tenant_id": "tenant-a",
        "event_plane": {"kind": "mode", "trading_mode": "sandbox"},
        "bounded_context": "deployment",
        "aggregate_type": "runner_opening_release",
        "aggregate_id": uid(1),
        "aggregate_version": 1,
        "event_type": "RunnerOpeningReleaseCommand.v1",
        "payload": {
            "schema_version": 1,
            "release_id": uid(1),
            "tenant_id": "tenant-a",
            "trading_mode": "sandbox",
            "runner_id": uid(3),
            "deployment_instance_id": uid(4),
            "deployment_spec_id": uid(5),
            "deployment_spec_digest": "a" * 64,
            "generation": 17,
            "credential_scope_id": uid(6),
            "credential_scope_digest": "b" * 64,
            "basis_amount": "40000.00000001",
            "currency": "USDT",
            "basis_version_digest": "d" * 64,
            "opening_valuation_event_id": uid(8),
            "issued_at": at,
            "account_identity": {
                "kind": "sandbox_account",
                "incarnation": uid(7),
                "client_id": "SIM",
                "account_id": "SIM-01",
                "venue": "SIM",
                "account_type": "CASH",
                "configuration_sha256": "c" * 64,
            },
        },
        "correlation_id": uid(9),
        "actor_assertion_jti": None,
        "occurred_at": at,
    }


def release_subject(payload: dict) -> str:
    return f"crucible.runner.opening-release.v1.{payload['tenant_id']}.{payload['runner_id']}.{payload['trading_mode']}"


def test_verifies_exact_crucible_release_fixture_and_rejects_subject_substitution() -> None:
    document = release_document()
    subject = release_subject(document["payload"])
    raw = signed(subject, document)
    auth = CrucibleRunnerCommandAuthenticator(
        "tenant-a", UUID(int=3), frozenset({"sandbox"}), {KEY_ID: KEY.public_key()}
    )
    verified = auth.verify_opening_release(subject=subject, signed_envelope_bytes=raw)
    assert verified.command.generation == 17
    assert verified.command.basis_amount == "40000.00000001"
    assert verified.command.release_id == uid(1)
    assert json.loads(verified.exact_signed_event_bytes) == document
    with pytest.raises(CommandVerificationError) as error:
        auth.verify_opening_release(
            subject=subject.replace("opening-release", "command"), signed_envelope_bytes=raw
        )
    assert error.value.reason_code is UntrustedCommandReason.INVALID_SIGNATURE
    fixture = ROOT / "tests/fixtures/runner_command/opening_release_v1_exact_vector.json"
    expected = json.loads(fixture.read_text())
    assert (
        expected["event_bytes_sha256"]
        == hashlib.sha256(verified.exact_signed_event_bytes).hexdigest()
    )
    assert expected["signature"] == json.loads(raw)["signature"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("basis_amount", 40000.00000001),
        ("basis_amount", "-1"),
        ("basis_amount", "01"),
        ("generation", 0),
        ("generation", True),
        ("release_id", uid(11)),
        ("credential_scope_id", uid(0)),
        ("issued_at", "2026-11-10T05:00:01Z"),
    ],
)
def test_refuses_authenticated_bytes_that_violate_the_release_contract(
    field: str, value: object
) -> None:
    document = release_document()
    document["payload"][field] = value
    subject = release_subject(document["payload"])
    auth = CrucibleRunnerCommandAuthenticator(
        "tenant-a", UUID(int=3), frozenset({"sandbox"}), {KEY_ID: KEY.public_key()}
    )
    with pytest.raises(CommandVerificationError) as error:
        auth.verify_opening_release(
            subject=subject, signed_envelope_bytes=signed(subject, document)
        )
    assert error.value.reason_code is UntrustedCommandReason.INVALID_SCHEMA


def test_binds_only_the_verified_running_desired_generation_and_exact_account() -> None:
    cases = json.loads(
        (ROOT / "docs/authority/runner-deployment-command-golden-v1.json").read_text()
    )["cases"]
    first = next(
        case for case in cases if case["event_document"]["payload"]["lifecycle_state"] == "running"
    )
    old = first["event_document"]["payload"]
    command_key_id = "fixture-domain-key-v1"
    command_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    auth = CrucibleRunnerCommandAuthenticator(
        old["tenant_id"],
        UUID(old["runner_id"]),
        frozenset({old["mode"]}),
        {
            KEY_ID: KEY.public_key(),
            command_key_id: command_key.public_key(),
        },
    )
    desired = auth.verify(
        subject=first["subject"],
        signed_envelope_bytes=signed(
            first["subject"], first["event_document"], command_key, command_key_id
        ),
    ).command
    document = release_document()
    payload = document["payload"]
    for field in (
        "tenant_id",
        "runner_id",
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
    ):
        payload[field] = old[field]
    payload["trading_mode"] = old["mode"]
    payload["credential_scope_id"] = old["deployment_spec"]["credential_scope"]["scope_id"]
    payload["credential_scope_digest"] = old["deployment_spec"]["credential_scope"]["scope_digest"]
    document["tenant_id"] = payload["tenant_id"]
    document["event_plane"]["trading_mode"] = payload["trading_mode"]
    subject = release_subject(payload)
    command = auth.verify_opening_release(
        subject=subject, signed_envelope_bytes=signed(subject, document)
    ).command
    command.require_current_target(desired, command.account_identity)
    with pytest.raises(ValueError, match="current running desired target"):
        command.require_current_target(
            desired.model_copy(update={"generation": desired.generation + 1}),
            command.account_identity,
        )
    with pytest.raises(ValueError, match="current running desired target"):
        command.require_current_target(
            desired.model_copy(update={"lifecycle_state": "paused"}), command.account_identity
        )
    other = deepcopy(command.account_identity).model_copy(update={"account_id": "SIM-02"})
    with pytest.raises(ValueError, match="current running desired target"):
        command.require_current_target(desired, other)


def local_release(desired, release_id: str = uid(1)):
    document = release_document()
    payload = document["payload"]
    for name in ("tenant_id", "trading_mode", "deployment_spec_digest", "generation"):
        payload[name] = getattr(desired.command, name)
    for name in ("runner_id", "deployment_instance_id", "deployment_spec_id"):
        payload[name] = str(getattr(desired.command, name))
    credentials = desired.command.deployment_spec["credential_scope"]
    payload["credential_scope_id"] = credentials["scope_id"]
    payload["credential_scope_digest"] = credentials["scope_digest"]
    payload["release_id"] = document["aggregate_id"] = release_id
    document["tenant_id"] = payload["tenant_id"]
    document["event_plane"]["trading_mode"] = payload["trading_mode"]
    subject = release_subject(payload)
    auth = CrucibleRunnerCommandAuthenticator(
        desired.command.tenant_id,
        desired.command.runner_id,
        frozenset({desired.command.trading_mode}),
        {KEY_ID: KEY.public_key()},
    )
    return auth, subject, document


async def record_desired(store, verified):
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )


async def test_durable_opening_release_replays_across_restart_with_the_gate_closed(tmp_path):
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.runner_fact import RunnerStateAuthorityError
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner.sqlite3"
    outbox, store = _runner_fact_store(database)
    _, _, desired = _verified_command()
    await record_desired(store, desired)
    authority = EngineLifecycleAuthority.from_verified_command(desired)
    before = outbox.platform_storage_snapshot()
    auth, subject, document = local_release(desired)
    wire = signed(subject, document)
    assert (
        await store.record_opening_release(auth, subject=subject, signed_envelope=wire)
        == "recorded"
    )
    assert outbox.platform_storage_snapshot().mutation_seq == before.mutation_seq + 1
    assert store.load_opening_gate_state_sync(authority) == "awaiting_release"
    _, restarted = _runner_fact_store(database)
    reopened = outbox.platform_storage_snapshot()
    receipt = restarted.load_pending_opening_release(desired.command.deployment_instance_id, auth)
    assert receipt is not None
    assert receipt.command.release_id == uid(1)
    assert (
        receipt.exact_signed_event_bytes
        == auth.verify_opening_release(
            subject=subject, signed_envelope_bytes=wire
        ).exact_signed_event_bytes
    )
    assert (
        await restarted.record_opening_release(auth, subject=subject, signed_envelope=wire)
        == "replayed"
    )
    assert outbox.platform_storage_snapshot().mutation_seq == reopened.mutation_seq
    assert restarted.load_opening_gate_state_sync(authority) == "awaiting_release"

    conflict = deepcopy(document)
    conflict["payload"]["basis_amount"] = "10"
    with pytest.raises(RunnerStateAuthorityError, match="different signed bytes"):
        await restarted.record_opening_release(
            auth, subject=subject, signed_envelope=signed(subject, conflict)
        )
    _, _, other = local_release(desired, uid(20))
    with pytest.raises(RunnerStateAuthorityError, match="another opening release"):
        await restarted.record_opening_release(
            auth, subject=subject, signed_envelope=signed(subject, other)
        )
    assert (
        restarted.load_pending_opening_release(desired.command.deployment_instance_id, auth)
        == receipt
    )


@pytest.mark.parametrize("lifecycle", ["paused", "stopped"])
async def test_a_new_desired_command_retires_pending_release_and_rejects_late_delivery(
    tmp_path, lifecycle
):
    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.runner_fact import RunnerStateAuthorityError
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, running = _verified_command()
    await record_desired(store, running)
    auth, subject, document = local_release(running)
    wire = signed(subject, document)
    assert (
        await store.record_opening_release(auth, subject=subject, signed_envelope=wire)
        == "recorded"
    )

    def change(event):
        event["aggregate_version"] = 2
        event["payload"]["generation"] = 2
        event["payload"]["lifecycle_state"] = lifecycle

    _, _, next_desired = _verified_command(mutate_event=change)
    await record_desired(store, next_desired)
    _, restarted = _runner_fact_store(database)
    assert (
        restarted.load_pending_opening_release(running.command.deployment_instance_id, auth) is None
    )
    assert (
        await restarted.record_opening_release(auth, subject=subject, signed_envelope=wire)
        == "retired"
    )
    assert (
        restarted.load_opening_gate_state_sync(
            EngineLifecycleAuthority.from_verified_command(next_desired)
        )
        == "revoked"
    )
    _, _, late = local_release(running, uid(21))
    with pytest.raises(RunnerStateAuthorityError, match="current running desired target"):
        await restarted.record_opening_release(
            auth, subject=subject, signed_envelope=signed(subject, late)
        )
    assert (
        restarted.load_pending_opening_release(running.command.deployment_instance_id, auth) is None
    )


async def test_retained_release_bytes_are_reauthenticated_after_restart(tmp_path):
    import sqlite3

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, desired = _verified_command()
    await record_desired(store, desired)
    auth, subject, document = local_release(desired)
    assert (
        await store.record_opening_release(
            auth, subject=subject, signed_envelope=signed(subject, document)
        )
        == "recorded"
    )
    tampered = json.loads(signed(subject, document))
    tampered["signature"] = "A" * 86
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runner_opening_release_inbox SET signed_envelope = ?", (json_bytes(tampered),)
        )
    _, restarted = _runner_fact_store(database)
    with pytest.raises(CommandVerificationError) as error:
        restarted.load_pending_opening_release(desired.command.deployment_instance_id, auth)
    assert error.value.reason_code is UntrustedCommandReason.INVALID_SIGNATURE
    assert (
        restarted.load_opening_gate_state_sync(
            EngineLifecycleAuthority.from_verified_command(desired)
        )
        == "awaiting_release"
    )


@pytest.mark.parametrize("inventory_case", ["unavailable", "pending_order"])
async def test_signed_release_refusal_commits_with_the_outbox_and_replays_exact_bytes(
    tmp_path, inventory_case
):
    import sqlite3
    from dataclasses import replace

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.opening_inventory_contract import unavailable_opening_inventory
    from custos.core.runner_fact import (
        OPENING_RELEASE_FACT_KIND,
        RUNNER_FACT_SIGNING_DOMAIN,
        RunnerStateAuthorityError,
        _canonical_json_bytes,
        runner_fact_signing_header,
    )
    from tests.test_runner_fact_store import (
        _runner_authority,
        _runner_fact_store,
        _verified_command,
    )

    database = tmp_path / "runner.sqlite3"
    outbox, store = _runner_fact_store(database)
    _, _, desired = _verified_command()
    await record_desired(store, desired)
    auth, subject, document = local_release(desired)
    manifest = {
        "fact_kind_projectors": {OPENING_RELEASE_FACT_KIND: "risk"},
        "runner_fact_contracts": {"risk": {"opening_release": "v1"}},
    }
    manifest_digest = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    store._authority_resolver = lambda verified: replace(
        _runner_authority(verified), capability_manifest_digest=manifest_digest
    )
    authority = store._authority_resolver(desired)
    gate = EngineLifecycleAuthority.from_verified_command(desired)
    inventory = unavailable_opening_inventory(authority, "binance")
    amount = "0"
    if inventory_case == "pending_order":
        from tests.test_opening_inventory_contract import _cash_candidate

        inventory, read = _cash_candidate(authority)
        inventory["observations"]["positions"] = read([])
        inventory["observations"]["liabilities"] = read(None)
        inventory["observations"]["liabilities"].update(
            status="not_applicable", reason="model_not_applicable"
        )
        inventory["observations"]["ordinary_orders"] = read(
            [
                {
                    "order_id": "C-1",
                    "client_order_id": "C-1",
                    "venue_order_id": None,
                    "instrument": "BTCUSDT.BINANCE",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "status": "SUBMITTED",
                    "original_quantity": "1",
                    "executed_quantity": "0",
                    "price": None,
                    "trigger_price": None,
                    "in_matching_engine": False,
                    "pending": True,
                }
            ]
        )
        amount = "1"
        document["occurred_at"] = document["payload"]["issued_at"] = inventory["equity_binding"][
            "observed_at"
        ]
        document["payload"]["account_identity"] = inventory["observations"]["identity"]["rows"][0]
        document["payload"]["basis_amount"] = amount
    wire = signed(subject, document)
    assert (
        await store.record_opening_release(auth, subject=subject, signed_envelope=wire)
        == "recorded"
    )
    observation = dict(
        release_id=UUID(int=1),
        capability_manifest=manifest,
        amount=amount,
        currency="USDT",
        observed_at=document["occurred_at"],
        inventory=inventory,
    )
    with pytest.raises(RunnerStateAuthorityError, match="capability is not registered"):
        await store.commit_refused_opening_release(
            auth, **{**observation, "capability_manifest": {}}
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TRIGGER test_refusal_outbox_failure BEFORE INSERT ON runner_fact_outbox
            BEGIN SELECT RAISE(ABORT, 'injected refusal outbox failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected refusal outbox failure"):
        await store.commit_refused_opening_release(auth, **observation)
    assert (
        store.load_pending_opening_release(desired.command.deployment_instance_id, auth) is not None
    )
    assert store.load_opening_gate_state_sync(gate) == "awaiting_release"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runner_fact_seen_event").fetchone()[0] == 0
        connection.execute("DROP TRIGGER test_refusal_outbox_failure")

    batch_id, payload = await store.commit_refused_opening_release(auth, **observation)
    envelope = json.loads(payload)
    (fact,) = envelope["facts"]
    assert fact["seq"] == 1
    assert fact["kind"] == OPENING_RELEASE_FACT_KIND
    assert fact["release_id"] == uid(1)
    assert fact["release_command_event_id"] == document["event_id"]
    assert fact["basis_version_digest"] == document["payload"]["basis_version_digest"]
    assert (fact["outcome"], fact["reason"]) == ("refused", "inventory_unproven")
    signature = base64.urlsafe_b64decode(
        envelope["signature"] + "=" * (-len(envelope["signature"]) % 4)
    )
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(65, 97))).public_key()
    key.verify(
        signature,
        RUNNER_FACT_SIGNING_DOMAIN + _canonical_json_bytes(runner_fact_signing_header(envelope)),
    )
    assert store.load_pending_opening_release(desired.command.deployment_instance_id, auth) is None
    assert store.load_opening_gate_state_sync(gate) == "awaiting_release"

    await outbox.commit_puback(
        batch_id,
        broker_stream="CRUCIBLE_RUNNER_FACTS",
        broker_sequence=1,
        broker_domain=None,
        duplicate=False,
    )
    _, reopened = _runner_fact_store(database)
    assert await reopened.commit_refused_opening_release(auth, **observation) == (batch_id, payload)
    assert (
        await reopened.record_opening_release(auth, subject=subject, signed_envelope=wire)
        == "replayed"
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runner_fact_seen_event").fetchone()[0] == 1
    _, _, replacement = local_release(desired, uid(42))
    assert (
        await reopened.record_opening_release(
            auth, subject=subject, signed_envelope=signed(subject, replacement)
        )
        == "recorded"
    )


@pytest.mark.parametrize(
    ("scenario", "disposition"),
    [
        ("exact", "ack"),
        ("bad_signature", "term"),
        ("other_mode", "term"),
        ("legacy_profile", "term"),
        ("commit_failure", "nak"),
    ],
)
async def test_control_consumer_binds_release_and_acks_only_after_commit(
    tmp_path, scenario, disposition
):
    import asyncio
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.nats_client import CrucibleNatsClient
    from custos.core.runner_control_consumer import RunnerControlConsumerV1
    from tests.test_runner_fact_store import _runner_fact_store, _verified_command

    database = tmp_path / "runner.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, desired = _verified_command()
    await record_desired(store, desired)
    auth, subject, document = local_release(desired)
    mode = desired.command.trading_mode
    tenant = desired.command.tenant_id
    runner = desired.command.runner_id
    scope = f"{tenant}.{runner}.{mode}"
    filters = [f"crucible.runner.command.v1.{scope}", f"crucible.runner.policy.v1.{scope}"]
    if scenario != "legacy_profile":
        filters.append(subject)
    profile = SimpleNamespace(
        tenant_id=tenant,
        runner_id=runner,
        trading_mode=mode,
        durable_config={"filter_subjects": filters},
        assert_active=lambda: None,
    )
    machine = SimpleNamespace(tenant_id=tenant, runner_id=runner, assert_active=lambda: None)
    client = CrucibleNatsClient(
        connection_profile=profile,
        tenant_id=tenant,
        runner_id=str(runner),
        machine_credential=machine,
    )
    command_runtime = SimpleNamespace(process=AsyncMock())
    consumer = RunnerControlConsumerV1(
        command_runtime=command_runtime,
        policy_authenticator=object(),
        state_store=store,
        opening_release_authenticator=auth,
    )
    delivery_subject = subject
    wire = signed(subject, document)
    if scenario == "bad_signature":
        tampered = json.loads(wire)
        tampered["signature"] = "A" * 86
        wire = json_bytes(tampered)
    if scenario == "other_mode":
        document["event_plane"]["trading_mode"] = document["payload"]["trading_mode"] = "live"
        document["payload"]["account_identity"] = {
            "kind": "sodex_account",
            "main_wallet": "0x" + "1" * 40,
            "account_id": "1",
        }
        delivery_subject = release_subject(document["payload"])
        wire = signed(delivery_subject, document)
    if scenario == "commit_failure":
        with sqlite3.connect(database) as connection:
            connection.execute(
                """CREATE TRIGGER fail_release_commit BEFORE INSERT ON runner_opening_release_inbox
                BEGIN SELECT RAISE(ABORT, 'injected receipt write failure'); END"""
            )

    async def require_durable_before_ack():
        saved = store.load_pending_opening_release(desired.command.deployment_instance_id, auth)
        assert saved == auth.verify_opening_release(subject=subject, signed_envelope_bytes=wire)

    delivery = SimpleNamespace(
        subject=delivery_subject,
        data=wire,
        ack=AsyncMock(side_effect=require_durable_before_ack),
        term=AsyncMock(),
        nak=AsyncMock(),
    )

    async def messages():
        yield delivery

    await consumer.run(
        client=client, subscription=SimpleNamespace(messages=messages()), stop=asyncio.Event()
    )
    assert (delivery.ack.await_count, delivery.term.await_count, delivery.nak.await_count) == (
        int(disposition == "ack"),
        int(disposition == "term"),
        int(disposition == "nak"),
    )
    command_runtime.process.assert_not_awaited()
    if disposition != "ack":
        assert (
            store.load_pending_opening_release(desired.command.deployment_instance_id, auth) is None
        )
    assert (
        store.load_opening_gate_state_sync(EngineLifecycleAuthority.from_verified_command(desired))
        == "awaiting_release"
    )


@pytest.mark.parametrize("case", ("balance_mismatch", "matching", "retired"))
async def test_pending_release_observation_records_only_a_fresh_refusal(tmp_path, case):
    import sqlite3
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.core.opening_observation import OpeningObservationBundle
    from custos.core.runner_fact import OPENING_RELEASE_FACT_KIND, _canonical_json_bytes
    from custos.core.runner_fact_producer import (
        RunnerFactDeployment,
        observe_pending_opening_release,
    )
    from tests.test_runner_fact_opening_bundle import _future_native_bundle
    from tests.test_runner_fact_store import (
        _runner_authority,
        _runner_fact_store,
        _verified_command,
    )

    database = tmp_path / "runner.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, desired = _verified_command()
    await record_desired(store, desired)
    auth, subject, event = local_release(desired)
    manifest = {
        "fact_kind_projectors": {OPENING_RELEASE_FACT_KIND: "risk"},
        "runner_fact_contracts": {"risk": {"opening_release": "v1"}},
    }
    store._authority_resolver = lambda verified: replace(
        _runner_authority(verified),
        capability_manifest_digest=hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest(),
    )
    authority = store._authority_resolver(desired)
    source = _future_native_bundle(authority=authority)
    token = replace(source.token, command_fingerprint=desired.command_fingerprint)
    bundle = OpeningObservationBundle(
        authority,
        token,
        source.independent,
        source.trading_gate,
        source.inventory,
        source.native,
        source.currency,
    )
    event["occurred_at"] = event["payload"]["issued_at"] = "2026-11-04T00:00:00Z"
    event["payload"]["account_identity"] = bundle.inventory["observations"]["identity"]["rows"][0]
    event["payload"]["basis_amount"] = "1" if case == "matching" else "100"
    wire = signed(subject, event)
    await store.record_opening_release(auth, subject=subject, signed_envelope=wire)
    deployment = RunnerFactDeployment(
        authority,
        str(authority.deployment_instance_id),
        str(authority.deployment_spec_id),
        authority.deployment_spec_digest,
        "BINANCE",
        "binance",
        "USDT",
        False,
        "1.0.0",
        "1m",
    )

    async def read_after_intake(instance, currency):
        assert (instance, currency) == (deployment.deployment_instance_id, deployment.currency)
        assert (
            store.load_pending_opening_release(authority.deployment_instance_id, auth) is not None
        )
        return bundle

    host = SimpleNamespace(
        deployment_ready=AsyncMock(return_value=True),
        runner_fact_opening_bundle=AsyncMock(side_effect=read_after_intake),
        runner_fact_observation_token=AsyncMock(
            side_effect=[token, replace(token, observation_epoch=2) if case == "retired" else token]
        ),
    )
    await observe_pending_opening_release(
        deployment, host=host, state_store=store, authenticator=auth, manifest=manifest
    )
    host.runner_fact_opening_bundle.assert_awaited_once()
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT payload FROM runner_fact_outbox").fetchall()
    if case == "balance_mismatch":
        (row,) = rows
        (fact,) = json.loads(row[0])["facts"]
        assert (fact["amount"], fact["outcome"], fact["reason"]) == (
            "1",
            "refused",
            "balance_mismatch",
        )
        assert store.load_pending_opening_release(authority.deployment_instance_id, auth) is None
    else:
        assert not rows
        assert (
            store.load_pending_opening_release(authority.deployment_instance_id, auth) is not None
        )
    assert (
        store.load_opening_gate_state_sync(EngineLifecycleAuthority.from_verified_command(desired))
        == "awaiting_release"
    )
