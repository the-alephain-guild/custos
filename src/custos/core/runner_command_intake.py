"""Verified Crucible command identity and inbound delivery policy.

This module intentionally stops before engine apply and before SQLite storage.
The command intake boundary implements :class:`CommandIntakeDurability` with the existing
RunnerFact SQLite deep module.  The coordinator only permits ACK or TERM after
that port returns a typed durable receipt.  JetStream PubAck is an unrelated
outbound RunnerFact concern and has no surface here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Protocol, TypeVar, cast
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from custos.contracts.crucible_runner_command import CrucibleRunnerDeploymentCommandV1
from custos.contracts.deployment import (
    DOMAIN_EVENT_ENCODING,
    DOMAIN_EVENT_SIGNATURE_CONTEXT,
    DOMAIN_EVENT_SIGNATURE_PROFILE,
)
from custos.core.log import get_logger

_log = get_logger("custos.runner_command_intake")

COMMAND_FINGERPRINT_DOMAIN = b"CRUCIBLE-RUNNER-COMMAND-FINGERPRINT-V1\0"
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "signature_profile",
        "event_encoding",
        "event_bytes",
        "signature_key_id",
        "signature",
    }
)
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TRADING_MODES = frozenset({"sandbox", "testnet", "live"})


class UntrustedCommandReason(StrEnum):
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_SCHEMA = "invalid_schema"
    UNSUPPORTED_VERSION = "unsupported_version"


class CommandIdentityDecision(StrEnum):
    NEWER = "newer"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    STALE = "stale"


class InboundCommandDisposition(StrEnum):
    NONE = "none"
    ACK = "ack"
    NAK = "nak"
    TERM = "term"


class CommandTerminalOutcome(StrEnum):
    CONFLICT = "conflict"
    STALE = "stale"
    RETRY_EXHAUSTED = "retry_exhausted"
    UNTRUSTED_REJECTION = "untrusted_rejection"


class CommandIntakeStatus(StrEnum):
    PREPARED_FOR_APPLY = "prepared_for_apply"
    IDEMPOTENT_PENDING = "idempotent_pending"
    IDEMPOTENT_TERMINAL_REPLAY = "idempotent_terminal_replay"
    TERMINAL_CONFLICT = "terminal_conflict"
    TERMINAL_STALE = "terminal_stale"
    TERMINAL_RETRY_EXHAUSTED = "terminal_retry_exhausted"
    TERMINAL_UNTRUSTED_REJECTION = "terminal_untrusted_rejection"
    RETRY_SCHEDULED = "retry_scheduled"


@dataclass(frozen=True, slots=True)
class CommandDeliveryPolicy:
    """Bounded inbound JetStream policy, independent from outbound PubAck."""

    ack_wait_seconds: float = 30.0
    max_deliver: int = 5
    backoff_seconds: tuple[float, ...] = (10.0, 30.0, 60.0, 120.0, 300.0)
    in_progress_interval_seconds: float = 5.0
    quarantine_after_deliveries: int = 5

    def __post_init__(self) -> None:
        if self.ack_wait_seconds <= 0:
            raise ValueError("command ack_wait_seconds must be positive")
        if type(self.max_deliver) is not int or self.max_deliver < 2:
            raise ValueError("command max_deliver must be an integer >= 2")
        if not self.backoff_seconds or len(self.backoff_seconds) > self.max_deliver:
            raise ValueError("command backoff must contain 1..max_deliver entries")
        if any(delay <= 0 for delay in self.backoff_seconds):
            raise ValueError("command backoff values must be positive")
        first_ack_deadline = min(self.ack_wait_seconds, self.backoff_seconds[0])
        if not 0 < self.in_progress_interval_seconds < first_ack_deadline:
            raise ValueError("command in_progress interval must precede the first ACK deadline")
        if self.quarantine_after_deliveries != self.max_deliver:
            raise ValueError("command quarantine boundary must equal max_deliver")

    def backoff_for(self, delivered_count: int) -> float:
        if type(delivered_count) is not int or delivered_count < 1:
            raise ValueError("delivered_count must be a positive integer")
        return self.backoff_seconds[min(delivered_count - 1, len(self.backoff_seconds) - 1)]


@dataclass(frozen=True, slots=True)
class CommandVerificationReceipt:
    signature_key_id: str
    signature_profile: str
    exact_subject: str
    verified_event_bytes_sha256: str
    command_fingerprint: str
    schema_version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class VerifiedRunnerCommand:
    command: CrucibleRunnerDeploymentCommandV1
    command_fingerprint: str
    verification_receipt: CommandVerificationReceipt


@dataclass(frozen=True, slots=True)
class DesiredCommandRecord:
    deployment_instance_id: UUID
    generation: int
    command_fingerprint: str
    decision: CommandIdentityDecision
    committed: bool
    replay_disposition: InboundCommandDisposition = InboundCommandDisposition.NONE


@dataclass(frozen=True, slots=True)
class DurableCommandOutcome:
    outcome_id: str
    outcome: CommandTerminalOutcome
    durable_disposition: InboundCommandDisposition
    committed: bool


@dataclass(frozen=True, slots=True)
class CommandIntakeResult:
    status: CommandIntakeStatus
    disposition: InboundCommandDisposition
    verified: VerifiedRunnerCommand | None = None
    reason_code: str | None = None


class CommandVerificationError(ValueError):
    def __init__(
        self,
        reason_code: UntrustedCommandReason,
        message: str,
        *,
        raw_envelope_digest: str,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.raw_envelope_digest = raw_envelope_digest


class InboundCommandDelivery(Protocol):
    delivery_id: str
    subject: str
    data: bytes
    delivered_count: int

    async def ack(self) -> None: ...

    async def nak(self, delay: float | None = None) -> None: ...

    async def term(self) -> None: ...

    async def in_progress(self) -> None: ...


class CommandIntakeDurability(Protocol):
    """Intake port backed by the sole RunnerFact SQLite store.

    ``commit_verified_terminal_outcome`` must delegate to the RunnerFact store's atomic
    command-outcome plus lifecycle-fact transaction.  A second database or
    outbox is forbidden.
    """

    async def record_desired_command(
        self,
        *,
        command: CrucibleRunnerDeploymentCommandV1,
        command_fingerprint: str,
        verification_receipt: CommandVerificationReceipt,
    ) -> DesiredCommandRecord: ...

    async def commit_verified_terminal_outcome(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        outcome: Literal["conflict", "stale", "retry_exhausted"],
        reason_code: str,
    ) -> DurableCommandOutcome: ...

    async def commit_untrusted_command_rejection(
        self,
        *,
        delivery_id: str,
        exact_subject: str,
        raw_envelope_digest: str,
        reason_code: Literal["invalid_signature", "invalid_schema", "unsupported_version"],
    ) -> DurableCommandOutcome: ...


@dataclass(frozen=True, slots=True)
class _SignatureMaterial:
    key_id: str
    event_bytes: bytes
    signature: bytes


@dataclass(frozen=True, slots=True)
class CrucibleRunnerCommandAuthenticator:
    """Authenticate runner-command bytes, then invoke the sole command consumer model."""

    expected_tenant_id: str
    expected_runner_id: UUID
    allowed_trading_modes: frozenset[str]
    signature_keys: Mapping[str, Ed25519PublicKey]

    def __post_init__(self) -> None:
        if not self.expected_tenant_id.strip():
            raise ValueError("expected tenant id is required")
        if self.expected_runner_id.int == 0:
            raise ValueError("expected runner id must not be nil")
        modes = frozenset(self.allowed_trading_modes)
        if not modes or not modes <= _TRADING_MODES:
            raise ValueError("allowed trading modes must be a non-empty known subset")
        keys = dict(self.signature_keys)
        if not keys or any(
            not key_id.strip() or not isinstance(key, Ed25519PublicKey)
            for key_id, key in keys.items()
        ):
            raise ValueError("at least one named Ed25519 authority key is required")
        object.__setattr__(self, "allowed_trading_modes", modes)
        object.__setattr__(self, "signature_keys", MappingProxyType(keys))

    def verify(self, *, subject: str, signed_envelope_bytes: bytes) -> VerifiedRunnerCommand:
        raw_digest = hashlib.sha256(signed_envelope_bytes).hexdigest()
        try:
            material = _signature_material(signed_envelope_bytes)
        except _UnsupportedEnvelopeVersion as exc:
            raise CommandVerificationError(
                UntrustedCommandReason.UNSUPPORTED_VERSION,
                str(exc),
                raw_envelope_digest=raw_digest,
            ) from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SCHEMA,
                f"signed command envelope is invalid: {exc}",
                raw_envelope_digest=raw_digest,
            ) from exc

        authority_key = self.signature_keys.get(material.key_id)
        if authority_key is None:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SIGNATURE,
                "signed command key id is not trusted by this runner",
                raw_envelope_digest=raw_digest,
            )
        framed = _framed(DOMAIN_EVENT_SIGNATURE_CONTEXT, subject, material.event_bytes)
        try:
            authority_key.verify(material.signature, framed)
        except InvalidSignature as exc:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SIGNATURE,
                "Crucible runner command signature verification failed",
                raw_envelope_digest=raw_digest,
            ) from exc

        try:
            command = CrucibleRunnerDeploymentCommandV1.from_verified_signed_envelope(
                subject=subject,
                signed_envelope_bytes=signed_envelope_bytes,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SCHEMA,
                f"Crucible runner command schema or binding is invalid: {exc}",
                raw_envelope_digest=raw_digest,
            ) from exc
        if command.tenant_id != self.expected_tenant_id:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SCHEMA,
                "Crucible runner command tenant differs from runner authority",
                raw_envelope_digest=raw_digest,
            )
        if command.runner_id != self.expected_runner_id:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SCHEMA,
                "Crucible runner command runner id differs from runner authority",
                raw_envelope_digest=raw_digest,
            )
        if command.trading_mode not in self.allowed_trading_modes:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SCHEMA,
                "Crucible runner command mode is not enabled by this runner",
                raw_envelope_digest=raw_digest,
            )

        command_fingerprint = compute_command_fingerprint(
            subject=command.verified_subject,
            verified_exact_event_bytes=command.exact_signed_event_bytes,
        )
        if command.command_fingerprint != command_fingerprint:
            raise CommandVerificationError(
                UntrustedCommandReason.INVALID_SCHEMA,
                "Crucible runner command fingerprint differs from exact event bytes",
                raw_envelope_digest=raw_digest,
            )
        receipt = CommandVerificationReceipt(
            signature_key_id=material.key_id,
            signature_profile=DOMAIN_EVENT_SIGNATURE_PROFILE,
            exact_subject=command.verified_subject,
            verified_event_bytes_sha256=hashlib.sha256(
                command.exact_signed_event_bytes
            ).hexdigest(),
            command_fingerprint=command_fingerprint,
        )
        return VerifiedRunnerCommand(
            command=command,
            command_fingerprint=command_fingerprint,
            verification_receipt=receipt,
        )


class CommandIntakeCoordinator:
    """Apply bounded inbound dispositions around a RunnerFact durability port."""

    def __init__(
        self,
        *,
        authenticator: CrucibleRunnerCommandAuthenticator,
        durability: CommandIntakeDurability,
        policy: CommandDeliveryPolicy,
    ) -> None:
        self._authenticator = authenticator
        self._durability = durability
        self.policy = policy

    async def process(self, delivery: InboundCommandDelivery) -> CommandIntakeResult:
        _validate_delivery(delivery)
        try:
            verified = self._authenticator.verify(
                subject=delivery.subject,
                signed_envelope_bytes=delivery.data,
            )
        except CommandVerificationError as exc:
            try:
                outcome = await self._durability.commit_untrusted_command_rejection(
                    delivery_id=delivery.delivery_id,
                    exact_subject=delivery.subject,
                    raw_envelope_digest=exc.raw_envelope_digest,
                    reason_code=exc.reason_code.value,
                )
                _require_durable_outcome(
                    outcome,
                    expected=CommandTerminalOutcome.UNTRUSTED_REJECTION,
                    disposition=InboundCommandDisposition.TERM,
                )
            except Exception as durable_error:  # noqa: BLE001 - fail closed on any store failure
                _log.error(
                    "runner_command_untrusted_rejection_commit_failed",
                    delivery_id=delivery.delivery_id,
                    reason_code=exc.reason_code.value,
                    error_type=type(durable_error).__name__,
                )
                return await self._nak(delivery, reason_code="durable_rejection_failed")
            await delivery.term()
            return CommandIntakeResult(
                status=CommandIntakeStatus.TERMINAL_UNTRUSTED_REJECTION,
                disposition=InboundCommandDisposition.TERM,
                reason_code=exc.reason_code.value,
            )

        try:
            record = await self._durability.record_desired_command(
                command=verified.command,
                command_fingerprint=verified.command_fingerprint,
                verification_receipt=verified.verification_receipt,
            )
            _require_bound_record(record, verified)
        except Exception as durable_error:  # noqa: BLE001 - local durability is retryable
            _log.error(
                "runner_command_desired_record_failed",
                delivery_id=delivery.delivery_id,
                deployment_instance_id=str(verified.command.deployment_instance_id),
                generation=verified.command.generation,
                error_type=type(durable_error).__name__,
            )
            return await self.handle_transient_failure(
                delivery,
                verified=verified,
                reason_code="desired_record_failed",
            )

        if record.decision is CommandIdentityDecision.NEWER:
            return CommandIntakeResult(
                status=CommandIntakeStatus.PREPARED_FOR_APPLY,
                disposition=InboundCommandDisposition.NONE,
                verified=verified,
            )
        if record.decision is CommandIdentityDecision.IDEMPOTENT:
            if record.replay_disposition is InboundCommandDisposition.NONE:
                return CommandIntakeResult(
                    status=CommandIntakeStatus.IDEMPOTENT_PENDING,
                    disposition=InboundCommandDisposition.NONE,
                    verified=verified,
                )
            await _apply_inbound_disposition(delivery, record.replay_disposition)
            return CommandIntakeResult(
                status=CommandIntakeStatus.IDEMPOTENT_TERMINAL_REPLAY,
                disposition=record.replay_disposition,
                verified=verified,
            )

        terminal = {
            CommandIdentityDecision.CONFLICT: (
                CommandTerminalOutcome.CONFLICT,
                CommandIntakeStatus.TERMINAL_CONFLICT,
                "same_generation_different_exact_bytes",
            ),
            CommandIdentityDecision.STALE: (
                CommandTerminalOutcome.STALE,
                CommandIntakeStatus.TERMINAL_STALE,
                "older_generation",
            ),
        }[record.decision]
        return await self._commit_verified_terminal(
            delivery,
            verified=verified,
            outcome=terminal[0],
            status=terminal[1],
            reason_code=terminal[2],
        )

    async def handle_transient_failure(
        self,
        delivery: InboundCommandDelivery,
        *,
        verified: VerifiedRunnerCommand,
        reason_code: str,
    ) -> CommandIntakeResult:
        """NAK with backoff, or TERM only after a durable exhausted outcome."""

        if delivery.delivered_count < self.policy.max_deliver:
            return await self._nak(delivery, reason_code=reason_code, verified=verified)
        return await self._commit_verified_terminal(
            delivery,
            verified=verified,
            outcome=CommandTerminalOutcome.RETRY_EXHAUSTED,
            status=CommandIntakeStatus.TERMINAL_RETRY_EXHAUSTED,
            reason_code=reason_code,
        )

    async def run_with_in_progress(
        self,
        delivery: InboundCommandDelivery,
        operation: Awaitable[_T],
    ) -> _T:
        """Renew only the inbound command ACK lease while an operation waits."""

        task = asyncio.ensure_future(operation)
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task}, timeout=self.policy.in_progress_interval_seconds
                )
                if task in done:
                    return await task
                await delivery.in_progress()
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            raise

    async def _commit_verified_terminal(
        self,
        delivery: InboundCommandDelivery,
        *,
        verified: VerifiedRunnerCommand,
        outcome: CommandTerminalOutcome,
        status: CommandIntakeStatus,
        reason_code: str,
    ) -> CommandIntakeResult:
        try:
            receipt = await self._durability.commit_verified_terminal_outcome(
                delivery_id=delivery.delivery_id,
                verified=verified,
                outcome=cast(Literal["conflict", "stale", "retry_exhausted"], outcome.value),
                reason_code=reason_code,
            )
            _require_durable_outcome(
                receipt,
                expected=outcome,
                disposition=InboundCommandDisposition.TERM,
            )
        except Exception as durable_error:  # noqa: BLE001 - fail closed on any store failure
            _log.error(
                "runner_command_terminal_outcome_commit_failed",
                delivery_id=delivery.delivery_id,
                deployment_instance_id=str(verified.command.deployment_instance_id),
                generation=verified.command.generation,
                outcome=outcome.value,
                error_type=type(durable_error).__name__,
            )
            return await self._nak(
                delivery,
                reason_code="durable_terminal_outcome_failed",
                verified=verified,
            )
        await delivery.term()
        return CommandIntakeResult(
            status=status,
            disposition=InboundCommandDisposition.TERM,
            verified=verified,
            reason_code=reason_code,
        )

    async def _nak(
        self,
        delivery: InboundCommandDelivery,
        *,
        reason_code: str,
        verified: VerifiedRunnerCommand | None = None,
    ) -> CommandIntakeResult:
        await delivery.nak(delay=self.policy.backoff_for(delivery.delivered_count))
        return CommandIntakeResult(
            status=CommandIntakeStatus.RETRY_SCHEDULED,
            disposition=InboundCommandDisposition.NAK,
            verified=verified,
            reason_code=reason_code,
        )


def compute_command_fingerprint(*, subject: str, verified_exact_event_bytes: bytes) -> str:
    """Freeze command identity without including outer signature bytes."""

    if not isinstance(subject, str) or not subject:
        raise ValueError("command fingerprint subject is required")
    if type(verified_exact_event_bytes) is not bytes or not verified_exact_event_bytes:
        raise ValueError("command fingerprint requires non-empty exact event bytes")
    return hashlib.sha256(
        _framed(COMMAND_FINGERPRINT_DOMAIN, subject, verified_exact_event_bytes)
    ).hexdigest()


def classify_command_identity(
    *,
    current_generation: int | None,
    current_fingerprint: str | None,
    incoming_generation: int,
    incoming_fingerprint: str,
) -> CommandIdentityDecision:
    """Classify generation/fingerprint without mutating durable state."""

    _require_generation(incoming_generation, "incoming_generation")
    _require_fingerprint(incoming_fingerprint, "incoming_fingerprint")
    if current_generation is None and current_fingerprint is None:
        return CommandIdentityDecision.NEWER
    if current_generation is None or current_fingerprint is None:
        raise ValueError("current generation and fingerprint must both be present or absent")
    _require_generation(current_generation, "current_generation")
    _require_fingerprint(current_fingerprint, "current_fingerprint")
    if incoming_generation > current_generation:
        return CommandIdentityDecision.NEWER
    if incoming_generation < current_generation:
        return CommandIdentityDecision.STALE
    if hmac.compare_digest(incoming_fingerprint, current_fingerprint):
        return CommandIdentityDecision.IDEMPOTENT
    return CommandIdentityDecision.CONFLICT


def _signature_material(signed_envelope_bytes: bytes) -> _SignatureMaterial:
    if type(signed_envelope_bytes) is not bytes or not signed_envelope_bytes:
        raise ValueError("signed command envelope bytes are required")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    envelope = json.loads(signed_envelope_bytes, object_pairs_hook=reject_duplicates)
    if not isinstance(envelope, dict) or frozenset(envelope) != _ENVELOPE_FIELDS:
        raise ValueError("signed command envelope field set differs")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise _UnsupportedEnvelopeVersion("signed command envelope schema version is unsupported")
    if envelope["signature_profile"] != DOMAIN_EVENT_SIGNATURE_PROFILE:
        raise _UnsupportedEnvelopeVersion("signed command signature profile is unsupported")
    if envelope["event_encoding"] != DOMAIN_EVENT_ENCODING:
        raise _UnsupportedEnvelopeVersion("signed command event encoding is unsupported")
    key_id = envelope["signature_key_id"]
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("signed command key id is invalid")
    event_bytes = _decode_base64url(envelope["event_bytes"], "event_bytes")
    signature = _decode_base64url(envelope["signature"], "signature")
    if not event_bytes:
        raise ValueError("signed command event bytes are empty")
    if len(signature) != 64:
        raise ValueError("signed command Ed25519 signature must contain 64 bytes")
    return _SignatureMaterial(key_id=key_id, event_bytes=event_bytes, signature=signature)


def _decode_base64url(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_PATTERN.fullmatch(value):
        raise ValueError(f"signed command {field} is not unpadded base64url")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"signed command {field} is invalid base64url") from exc


def _framed(domain: bytes, subject: str, event_bytes: bytes) -> bytes:
    subject_bytes = subject.encode("utf-8")
    return b"".join(
        (
            domain,
            len(subject_bytes).to_bytes(4, "big"),
            subject_bytes,
            len(event_bytes).to_bytes(8, "big"),
            event_bytes,
        )
    )


def _require_generation(value: int, field: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} must be a positive integer")


def _require_fingerprint(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _validate_delivery(delivery: InboundCommandDelivery) -> None:
    if not delivery.delivery_id.strip():
        raise ValueError("command delivery id is required")
    if not delivery.subject:
        raise ValueError("command delivery subject is required")
    if type(delivery.data) is not bytes or not delivery.data:
        raise ValueError("command delivery data must be non-empty bytes")
    if type(delivery.delivered_count) is not int or delivery.delivered_count < 1:
        raise ValueError("command delivered_count must be a positive integer")


def _require_bound_record(
    record: DesiredCommandRecord,
    verified: VerifiedRunnerCommand,
) -> None:
    command = verified.command
    if not record.committed:
        raise RuntimeError("desired command record is not durable")
    if (
        record.deployment_instance_id != command.deployment_instance_id
        or record.generation != command.generation
        or record.command_fingerprint != verified.command_fingerprint
    ):
        raise RuntimeError("desired command record differs from verified identity")
    if record.decision is not CommandIdentityDecision.IDEMPOTENT:
        if record.replay_disposition is not InboundCommandDisposition.NONE:
            raise RuntimeError("only an idempotent record may carry a replay disposition")
    elif record.replay_disposition not in {
        InboundCommandDisposition.NONE,
        InboundCommandDisposition.ACK,
        InboundCommandDisposition.TERM,
    }:
        raise RuntimeError("idempotent replay disposition is invalid")


def _require_durable_outcome(
    outcome: DurableCommandOutcome,
    *,
    expected: CommandTerminalOutcome,
    disposition: InboundCommandDisposition,
) -> None:
    if (
        not outcome.committed
        or outcome.outcome is not expected
        or outcome.durable_disposition is not disposition
        or not outcome.outcome_id.strip()
    ):
        raise RuntimeError("command outcome is not the required durable disposition")


async def _apply_inbound_disposition(
    delivery: InboundCommandDelivery,
    disposition: InboundCommandDisposition,
) -> None:
    if disposition is InboundCommandDisposition.ACK:
        await delivery.ack()
        return
    if disposition is InboundCommandDisposition.TERM:
        await delivery.term()
        return
    raise RuntimeError("only durable ACK or TERM replay may finalize a command")


class _UnsupportedEnvelopeVersion(ValueError):
    pass


_T = TypeVar("_T")
