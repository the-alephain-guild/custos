from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_command_intake import (
    COMMAND_FINGERPRINT_DOMAIN,
    DOMAIN_EVENT_ENCODING,
    DOMAIN_EVENT_SIGNATURE_PROFILE,
    CommandDeliveryPolicy,
    CommandIdentityDecision,
    CommandIntakeCoordinator,
    CommandIntakeStatus,
    CommandTerminalOutcome,
    CommandVerificationError,
    CrucibleRunnerCommandAuthenticator,
    DesiredCommandRecord,
    DurableCommandOutcome,
    InboundCommandDisposition,
    UntrustedCommandReason,
    VerifiedRunnerCommand,
    classify_command_identity,
    compute_command_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "docs/authority/runner-deployment-command-golden-v1.json"
VECTOR_PATH = ROOT / "tests/fixtures/runner_command/runner_command_fingerprint_v1.json"
KEY_A = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
KEY_B = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _compact(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _recursively_sorted(value: object) -> object:
    if isinstance(value, dict):
        return {key: _recursively_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_recursively_sorted(item) for item in value]
    return value


def _signed_fixture(
    *,
    private_key: Ed25519PrivateKey = KEY_A,
    key_id: str = "crucible-command-key-a",
    generation: int | None = None,
    mutate_event: Any = None,
    mutate_envelope: Any = None,
) -> tuple[bytes, str]:
    fixture = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    case = fixture["cases"][0]
    subject = case["subject"]
    event = copy.deepcopy(case["event_document"])
    if generation is not None:
        event["aggregate_version"] = generation
        event["payload"]["generation"] = generation
    if mutate_event is not None:
        mutate_event(event)
    event["payload"] = _recursively_sorted(event["payload"])
    event_bytes = _compact(event)
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
        "signature_key_id": key_id,
        "event_bytes": _encode(event_bytes),
        "signature": _encode(private_key.sign(framed)),
    }
    if mutate_envelope is not None:
        mutate_envelope(envelope)
    return _compact(envelope), subject


def _authenticator(
    *,
    keys: dict[str, Ed25519PrivateKey] | None = None,
    tenant: str = "acme",
    runner_id: str = "10000000-0000-4000-8000-000000000001",
    modes: frozenset[str] = frozenset({"sandbox"}),
) -> CrucibleRunnerCommandAuthenticator:
    private_keys = keys or {"crucible-command-key-a": KEY_A}
    return CrucibleRunnerCommandAuthenticator(
        expected_tenant_id=tenant,
        expected_runner_id=UUID(runner_id),
        allowed_trading_modes=modes,
        signature_keys={key_id: key.public_key() for key_id, key in private_keys.items()},
    )


@dataclass
class _Delivery:
    data: bytes
    subject: str
    delivery_id: str = "delivery-1"
    delivered_count: int = 1
    events: list[str] = field(default_factory=list)
    fail_term_once: bool = False

    async def ack(self) -> None:
        self.events.append("ack")

    async def nak(self, delay: float | None = None) -> None:
        self.events.append(f"nak:{delay}")

    async def term(self) -> None:
        if self.fail_term_once:
            self.fail_term_once = False
            self.events.append("term-crash")
            raise RuntimeError("process crashed after durable commit")
        self.events.append("term")

    async def in_progress(self) -> None:
        self.events.append("in_progress")


class _DurabilityPort:
    """Test-only persistent fake for the T4 port; it is not production storage."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.current: dict[UUID, tuple[int, str]] = {}
        self.terminal: dict[tuple[UUID, int, str], InboundCommandDisposition] = {}
        self.untrusted: dict[str, DurableCommandOutcome] = {}
        self.fail_record = False
        self.fail_terminal = False
        self.fail_untrusted = False
        self.record_calls = 0

    async def record_desired_command(
        self,
        *,
        command,
        command_fingerprint: str,
        verification_receipt,
    ) -> DesiredCommandRecord:
        del verification_receipt
        self.record_calls += 1
        self.events.append("record_desired")
        if self.fail_record:
            raise OSError("SQLite temporarily unavailable")
        current = self.current.get(command.deployment_instance_id)
        decision = classify_command_identity(
            current_generation=current[0] if current else None,
            current_fingerprint=current[1] if current else None,
            incoming_generation=command.generation,
            incoming_fingerprint=command_fingerprint,
        )
        if decision is CommandIdentityDecision.NEWER:
            self.current[command.deployment_instance_id] = (
                command.generation,
                command_fingerprint,
            )
        replay = self.terminal.get(
            (command.deployment_instance_id, command.generation, command_fingerprint),
            InboundCommandDisposition.NONE,
        )
        return DesiredCommandRecord(
            deployment_instance_id=command.deployment_instance_id,
            generation=command.generation,
            command_fingerprint=command_fingerprint,
            decision=decision,
            committed=True,
            replay_disposition=replay
            if decision is CommandIdentityDecision.IDEMPOTENT
            else InboundCommandDisposition.NONE,
        )

    async def commit_verified_terminal_outcome(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        outcome: str,
        reason_code: str,
    ) -> DurableCommandOutcome:
        del reason_code
        self.events.append(f"commit:{outcome}")
        if self.fail_terminal:
            raise OSError("terminal transaction failed")
        identity = (
            verified.command.deployment_instance_id,
            verified.command.generation,
            verified.command_fingerprint,
        )
        self.terminal[identity] = InboundCommandDisposition.TERM
        return DurableCommandOutcome(
            outcome_id=f"{delivery_id}:{outcome}",
            outcome=CommandTerminalOutcome(outcome),
            durable_disposition=InboundCommandDisposition.TERM,
            committed=True,
        )

    async def commit_untrusted_command_rejection(
        self,
        *,
        delivery_id: str,
        exact_subject: str,
        raw_envelope_digest: str,
        reason_code: str,
    ) -> DurableCommandOutcome:
        del exact_subject, reason_code
        self.events.append("commit:untrusted_rejection")
        if self.fail_untrusted:
            raise OSError("untrusted rejection transaction failed")
        outcome = self.untrusted.setdefault(
            raw_envelope_digest,
            DurableCommandOutcome(
                outcome_id=f"{delivery_id}:untrusted",
                outcome=CommandTerminalOutcome.UNTRUSTED_REJECTION,
                durable_disposition=InboundCommandDisposition.TERM,
                committed=True,
            ),
        )
        return outcome


def _coordinator(
    durability: _DurabilityPort,
    *,
    authenticator: CrucibleRunnerCommandAuthenticator | None = None,
    policy: CommandDeliveryPolicy | None = None,
) -> CommandIntakeCoordinator:
    return CommandIntakeCoordinator(
        authenticator=authenticator or _authenticator(),
        durability=durability,
        policy=policy or CommandDeliveryPolicy(),
    )


def test_cross_language_fingerprint_vector_uses_exact_signed_event_bytes() -> None:
    vector = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    envelope_bytes, subject = _signed_fixture()
    envelope = json.loads(envelope_bytes)
    event_bytes = _decode(envelope["event_bytes"])

    assert COMMAND_FINGERPRINT_DOMAIN.decode() == vector["domain_utf8"]
    assert subject == vector["subject"]
    assert len(event_bytes) == vector["verified_exact_event_bytes_size"]
    assert hashlib.sha256(event_bytes).hexdigest() == vector["verified_exact_event_bytes_sha256"]
    assert (
        compute_command_fingerprint(
            subject=subject,
            verified_exact_event_bytes=event_bytes,
        )
        == vector["command_fingerprint"]
    )
    fixture = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert vector["command_fingerprint"] == fixture["cases"][0]["command_fingerprint"]


def test_signature_rotation_does_not_change_command_fingerprint() -> None:
    first_bytes, subject = _signed_fixture(private_key=KEY_A, key_id="key-a")
    second_bytes, _ = _signed_fixture(private_key=KEY_B, key_id="key-b")
    keys = {"key-a": KEY_A, "key-b": KEY_B}
    authenticator = _authenticator(keys=keys)

    first = authenticator.verify(subject=subject, signed_envelope_bytes=first_bytes)
    second = authenticator.verify(subject=subject, signed_envelope_bytes=second_bytes)

    assert first.command_fingerprint == second.command_fingerprint
    assert first.command.command_fingerprint == second.command.command_fingerprint
    assert (
        first.verification_receipt.signature_key_id != second.verification_receipt.signature_key_id
    )
    assert "signature" not in asdict(first.verification_receipt)


@pytest.mark.parametrize(
    (
        "current_generation",
        "current_fingerprint",
        "incoming_generation",
        "incoming_fingerprint",
        "expected",
    ),
    [
        (None, None, 1, "a" * 64, CommandIdentityDecision.NEWER),
        (1, "a" * 64, 2, "b" * 64, CommandIdentityDecision.NEWER),
        (2, "a" * 64, 1, "a" * 64, CommandIdentityDecision.STALE),
        (1, "a" * 64, 1, "a" * 64, CommandIdentityDecision.IDEMPOTENT),
        (1, "a" * 64, 1, "b" * 64, CommandIdentityDecision.CONFLICT),
    ],
)
def test_generation_and_exact_bytes_identity_matrix(
    current_generation,
    current_fingerprint,
    incoming_generation,
    incoming_fingerprint,
    expected,
) -> None:
    assert (
        classify_command_identity(
            current_generation=current_generation,
            current_fingerprint=current_fingerprint,
            incoming_generation=incoming_generation,
            incoming_fingerprint=incoming_fingerprint,
        )
        is expected
    )


@pytest.mark.parametrize(
    "authenticator",
    [
        _authenticator(tenant="other-tenant"),
        _authenticator(runner_id="10000000-0000-4000-8000-000000000099"),
        _authenticator(modes=frozenset({"live"})),
    ],
    ids=["tenant", "runner", "mode"],
)
def test_local_authority_must_match_before_any_ack(authenticator) -> None:
    envelope_bytes, subject = _signed_fixture()
    with pytest.raises(CommandVerificationError) as caught:
        authenticator.verify(subject=subject, signed_envelope_bytes=envelope_bytes)
    assert caught.value.reason_code is UntrustedCommandReason.INVALID_SCHEMA


@pytest.mark.asyncio
async def test_invalid_signature_commits_typed_rejection_before_term() -> None:
    envelope_bytes, subject = _signed_fixture(
        mutate_envelope=lambda envelope: envelope.__setitem__("signature", _encode(b"x" * 64))
    )
    events: list[str] = []
    delivery = _Delivery(envelope_bytes, subject, events=events)
    result = await _coordinator(_DurabilityPort(events)).process(delivery)

    assert result.status is CommandIntakeStatus.TERMINAL_UNTRUSTED_REJECTION
    assert result.reason_code == "invalid_signature"
    assert events == ["commit:untrusted_rejection", "term"]


@pytest.mark.asyncio
async def test_deployment_spec_tenant_binding_failure_is_durable_poison_before_term() -> None:
    def invalidate_deployment_spec_tenant(event: dict[str, Any]) -> None:
        event["payload"]["deployment_spec"]["tenant_id"] = "other-tenant"

    envelope_bytes, subject = _signed_fixture(mutate_event=invalidate_deployment_spec_tenant)
    events: list[str] = []
    result = await _coordinator(_DurabilityPort(events)).process(
        _Delivery(envelope_bytes, subject, events=events)
    )

    assert result.reason_code == "invalid_schema"
    assert events == ["commit:untrusted_rejection", "term"]


@pytest.mark.asyncio
async def test_unknown_envelope_version_is_durable_poison_before_term() -> None:
    envelope_bytes, subject = _signed_fixture(
        mutate_envelope=lambda envelope: envelope.__setitem__("schema_version", 2)
    )
    events: list[str] = []
    result = await _coordinator(_DurabilityPort(events)).process(
        _Delivery(envelope_bytes, subject, events=events)
    )

    assert result.reason_code == "unsupported_version"
    assert events == ["commit:untrusted_rejection", "term"]


@pytest.mark.asyncio
async def test_untrusted_rejection_commit_failure_naks_and_never_terms() -> None:
    envelope_bytes, subject = _signed_fixture(
        mutate_envelope=lambda envelope: envelope.__setitem__("signature", _encode(b"x" * 64))
    )
    durability = _DurabilityPort()
    durability.fail_untrusted = True
    delivery = _Delivery(envelope_bytes, subject, delivered_count=2)

    result = await _coordinator(durability).process(delivery)

    assert result.status is CommandIntakeStatus.RETRY_SCHEDULED
    assert delivery.events == ["nak:30.0"]


@pytest.mark.asyncio
async def test_crash_after_untrusted_commit_is_terminally_replayed_after_restart() -> None:
    envelope_bytes, subject = _signed_fixture(
        mutate_envelope=lambda envelope: envelope.__setitem__("signature", _encode(b"x" * 64))
    )
    durability = _DurabilityPort()
    first = _Delivery(envelope_bytes, subject, fail_term_once=True)
    with pytest.raises(RuntimeError, match="crashed after durable commit"):
        await _coordinator(durability).process(first)

    second = _Delivery(
        envelope_bytes, subject, delivery_id="delivery-redelivery", delivered_count=2
    )
    result = await _coordinator(durability).process(second)

    assert len(durability.untrusted) == 1
    assert result.status is CommandIntakeStatus.TERMINAL_UNTRUSTED_REJECTION
    assert first.events == ["term-crash"]
    assert second.events == ["term"]


@pytest.mark.asyncio
async def test_same_generation_same_exact_bytes_is_idempotent_without_early_ack() -> None:
    envelope_bytes, subject = _signed_fixture()
    durability = _DurabilityPort()
    coordinator = _coordinator(durability)
    first = _Delivery(envelope_bytes, subject)
    second = _Delivery(envelope_bytes, subject, delivery_id="delivery-2", delivered_count=2)

    first_result = await coordinator.process(first)
    second_result = await coordinator.process(second)

    assert first_result.status is CommandIntakeStatus.PREPARED_FOR_APPLY
    assert second_result.status is CommandIntakeStatus.IDEMPOTENT_PENDING
    assert first.events == []
    assert second.events == []


@pytest.mark.asyncio
async def test_same_generation_different_exact_bytes_commits_conflict_before_term() -> None:
    initial_bytes, subject = _signed_fixture()
    conflict_bytes, _ = _signed_fixture(
        mutate_event=lambda event: event.__setitem__(
            "occurred_at", "2026-07-15T00:00:01.000000000Z"
        )
    )
    durability = _DurabilityPort()
    await _coordinator(durability).process(_Delivery(initial_bytes, subject))
    events: list[str] = []
    durability.events = events
    delivery = _Delivery(conflict_bytes, subject, delivery_id="conflict", events=events)

    result = await _coordinator(durability).process(delivery)

    assert result.status is CommandIntakeStatus.TERMINAL_CONFLICT
    assert events == ["record_desired", "commit:conflict", "term"]


@pytest.mark.asyncio
async def test_older_generation_commits_stale_before_term() -> None:
    newer_bytes, subject = _signed_fixture(generation=2)
    older_bytes, _ = _signed_fixture(generation=1)
    durability = _DurabilityPort()
    await _coordinator(durability).process(_Delivery(newer_bytes, subject))
    events: list[str] = []
    durability.events = events

    result = await _coordinator(durability).process(
        _Delivery(older_bytes, subject, delivery_id="stale", events=events)
    )

    assert result.status is CommandIntakeStatus.TERMINAL_STALE
    assert events == ["record_desired", "commit:stale", "term"]


@pytest.mark.asyncio
async def test_terminal_conflict_redelivery_replays_term_without_duplicate_apply() -> None:
    initial_bytes, subject = _signed_fixture()
    conflict_bytes, _ = _signed_fixture(
        mutate_event=lambda event: event.__setitem__(
            "occurred_at", "2026-07-15T00:00:01.000000000Z"
        )
    )
    durability = _DurabilityPort()
    coordinator = _coordinator(durability)
    await coordinator.process(_Delivery(initial_bytes, subject))
    await coordinator.process(_Delivery(conflict_bytes, subject, delivery_id="conflict"))
    redelivery = _Delivery(
        conflict_bytes,
        subject,
        delivery_id="conflict-redelivery",
        delivered_count=2,
    )

    result = await _coordinator(durability).process(redelivery)

    assert result.status is CommandIntakeStatus.TERMINAL_CONFLICT
    assert redelivery.events == ["term"]


@pytest.mark.asyncio
async def test_transient_store_failure_naks_with_bounded_backoff() -> None:
    envelope_bytes, subject = _signed_fixture()
    durability = _DurabilityPort()
    durability.fail_record = True
    delivery = _Delivery(envelope_bytes, subject, delivered_count=2)

    result = await _coordinator(durability).process(delivery)

    assert result.status is CommandIntakeStatus.RETRY_SCHEDULED
    assert delivery.events == ["nak:30.0"]


@pytest.mark.asyncio
async def test_retry_exhaustion_commits_terminal_outcome_before_term() -> None:
    envelope_bytes, subject = _signed_fixture()
    events: list[str] = []
    durability = _DurabilityPort(events)
    durability.fail_record = True
    delivery = _Delivery(envelope_bytes, subject, delivered_count=5, events=events)

    result = await _coordinator(durability).process(delivery)

    assert result.status is CommandIntakeStatus.TERMINAL_RETRY_EXHAUSTED
    assert events == ["record_desired", "commit:retry_exhausted", "term"]


@pytest.mark.asyncio
async def test_retry_exhaustion_commit_failure_still_naks_and_never_terms() -> None:
    envelope_bytes, subject = _signed_fixture()
    durability = _DurabilityPort()
    durability.fail_record = True
    durability.fail_terminal = True
    delivery = _Delivery(envelope_bytes, subject, delivered_count=5)

    result = await _coordinator(durability).process(delivery)

    assert result.status is CommandIntakeStatus.RETRY_SCHEDULED
    assert delivery.events == ["nak:300.0"]


@pytest.mark.asyncio
async def test_long_operation_renews_inbound_lease_without_ack_or_puback() -> None:
    envelope_bytes, subject = _signed_fixture()
    delivery = _Delivery(envelope_bytes, subject)
    policy = CommandDeliveryPolicy(
        ack_wait_seconds=0.1,
        max_deliver=2,
        backoff_seconds=(0.08, 0.1),
        in_progress_interval_seconds=0.005,
        quarantine_after_deliveries=2,
    )

    async def operation() -> str:
        await asyncio.sleep(0.018)
        return "ready"

    result = await _coordinator(_DurabilityPort(), policy=policy).run_with_in_progress(
        delivery, operation()
    )

    assert result == "ready"
    assert delivery.events.count("in_progress") >= 2
    assert not {"ack", "term"} & set(delivery.events)
    assert not any(event.startswith("nak:") for event in delivery.events)


def test_delivery_policy_rejects_unbounded_or_late_progress_configuration() -> None:
    with pytest.raises(ValueError, match="quarantine boundary"):
        CommandDeliveryPolicy(max_deliver=5, quarantine_after_deliveries=4)
    with pytest.raises(ValueError, match="first ACK deadline"):
        CommandDeliveryPolicy(in_progress_interval_seconds=10.0)


def test_inbound_disposition_vocabulary_cannot_alias_outbound_puback() -> None:
    assert {item.value for item in InboundCommandDisposition} == {"none", "ack", "nak", "term"}
    assert all("pub" not in item.value for item in InboundCommandDisposition)
