"""Direct Custos client for the Crucible runner NATS transport authority."""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import nkeys

from custos.core.machine_credential_vault import (
    MachineCredential,
    MachineCredentialHttpClient,
)
from custos.core.nats_transport import (
    RUNNER_NATS_TRANSPORT_SCHEMA_VERSION,
    RunnerNatsTransportCredential,
    RunnerNatsTransportError,
    RunnerNatsTransportPendingOperation,
    generate_runner_user_nkey,
)

RUNNER_NATS_OPERATION_PATH = "/api/v1/runner-nats/transport-operations"
RUNNER_NATS_OPERATION_RESULT_PATH = "/api/v1/runner-nats/transport-operation-results"
RUNNER_NATS_USER_KEY_POSSESSION_PROFILE = "crucible.runner.nats-key-possession.v1"

_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = {
    "pending_signing",
    "pending_provisioning",
    "pending_readback",
    "pending_revocation",
}


def runner_nats_user_key_possession_payload_v1(
    operation: RunnerNatsTransportPendingOperation,
    machine_credential: MachineCredential,
    machine_request_id: UUID,
) -> bytes:
    """Return the exact cross-language User NKey possession preimage."""

    expected_generation = (
        "-"
        if operation.expected_active_generation is None
        else str(operation.expected_active_generation)
    )
    return "\n".join(
        (
            RUNNER_NATS_USER_KEY_POSSESSION_PROFILE,
            f"tenant_id={operation.tenant_id}",
            f"runner_id={operation.runner_id}",
            f"credential_id={machine_credential.credential_id}",
            f"credential_version={machine_credential.credential_version}",
            f"machine_request_id={machine_request_id}",
            f"authorization_intent_id={operation.authorization_intent_id}",
            f"operation_id={operation.operation_id}",
            f"trading_mode={operation.trading_mode}",
            f"operation_kind={operation.operation_kind}",
            f"expected_active_generation={expected_generation}",
            f"user_public_key={operation.user_public_key}",
        )
    ).encode("utf-8")


def _sign_user_key(seed: bytes, payload: bytes) -> str:
    seed_buffer = bytearray(seed)
    pair = nkeys.from_seed(seed_buffer)
    try:
        return base64.b64encode(pair.sign(payload)).decode("ascii")
    finally:
        pair.wipe()
        for index in range(len(seed_buffer)):
            seed_buffer[index] = 0


@dataclass(frozen=True, slots=True)
class RunnerNatsTransportOperationCompletion:
    operation_id: UUID
    operation_kind: str
    target_generation: int
    request_fingerprint: str
    credential: RunnerNatsTransportCredential | None
    revocation_receipt_digest: str | None


class RunnerNatsTransportAuthorityClient:
    """Machine-authenticated begin-and-poll client with local seed custody."""

    def __init__(
        self,
        crucible_url: str,
        machine_credential: MachineCredential,
    ) -> None:
        self.machine_credential = machine_credential
        self.http = MachineCredentialHttpClient(crucible_url, machine_credential)

    def prepare_initial(
        self,
        *,
        authorization_intent_id: UUID,
        trading_mode: str,
        expected_issuer_public_key: str,
        now: datetime | None = None,
    ) -> RunnerNatsTransportPendingOperation:
        seed, public_key = generate_runner_user_nkey()
        return RunnerNatsTransportPendingOperation(
            schema_version=RUNNER_NATS_TRANSPORT_SCHEMA_VERSION,
            authorization_intent_id=authorization_intent_id,
            operation_id=uuid4(),
            tenant_id=self.machine_credential.tenant_id,
            runner_id=self.machine_credential.runner_id,
            trading_mode=trading_mode,
            operation_kind="issue",
            expected_active_generation=None,
            user_public_key=public_key,
            user_seed=seed,
            expected_issuer_public_key=expected_issuer_public_key,
            created_at=(now or datetime.now(UTC)).astimezone(UTC),
        )

    def prepare_rotation(
        self,
        active: RunnerNatsTransportCredential,
        *,
        authorization_intent_id: UUID,
        now: datetime | None = None,
    ) -> RunnerNatsTransportPendingOperation:
        self._assert_credential_binding(active)
        seed, public_key = generate_runner_user_nkey()
        return RunnerNatsTransportPendingOperation(
            schema_version=RUNNER_NATS_TRANSPORT_SCHEMA_VERSION,
            authorization_intent_id=authorization_intent_id,
            operation_id=uuid4(),
            tenant_id=active.tenant_id,
            runner_id=active.runner_id,
            trading_mode=active.trading_mode,
            operation_kind="rotate",
            expected_active_generation=active.credential_generation,
            user_public_key=public_key,
            user_seed=seed,
            expected_issuer_public_key=active.issuer_public_key,
            created_at=(now or datetime.now(UTC)).astimezone(UTC),
        )

    def prepare_revocation(
        self,
        active: RunnerNatsTransportCredential,
        *,
        authorization_intent_id: UUID,
        now: datetime | None = None,
    ) -> RunnerNatsTransportPendingOperation:
        self._assert_credential_binding(active)
        return RunnerNatsTransportPendingOperation(
            schema_version=RUNNER_NATS_TRANSPORT_SCHEMA_VERSION,
            authorization_intent_id=authorization_intent_id,
            operation_id=uuid4(),
            tenant_id=active.tenant_id,
            runner_id=active.runner_id,
            trading_mode=active.trading_mode,
            operation_kind="revoke",
            expected_active_generation=active.credential_generation,
            user_public_key=active.user_public_key,
            user_seed=active.user_seed,
            expected_issuer_public_key=active.issuer_public_key,
            created_at=(now or datetime.now(UTC)).astimezone(UTC),
        )

    def execute(
        self,
        operation: RunnerNatsTransportPendingOperation,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.25,
    ) -> RunnerNatsTransportOperationCompletion:
        self._assert_operation_binding(operation)
        if timeout_seconds <= 0:
            raise RunnerNatsTransportError("runner NATS operation timeout must be positive")
        if poll_interval_seconds <= 0:
            raise RunnerNatsTransportError("runner NATS operation poll interval must be positive")
        request_fingerprint = self._begin(operation)
        deadline = time.monotonic() + timeout_seconds
        while True:
            completion = self._read_result(operation, request_fingerprint)
            if completion is not None:
                return completion
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerNatsTransportError(
                    "runner NATS authority operation did not complete before timeout"
                )
            time.sleep(min(poll_interval_seconds, remaining))

    def _begin(self, operation: RunnerNatsTransportPendingOperation) -> str:
        machine_request_id = uuid4()
        possession_payload = runner_nats_user_key_possession_payload_v1(
            operation,
            self.machine_credential,
            machine_request_id,
        )
        body = {
            "tenant_id": operation.tenant_id,
            "runner_id": str(operation.runner_id),
            "credential_id": str(self.machine_credential.credential_id),
            "credential_version": self.machine_credential.credential_version,
            "machine_request_id": str(machine_request_id),
            "authorization_intent_id": str(operation.authorization_intent_id),
            "operation_id": str(operation.operation_id),
            "trading_mode": operation.trading_mode,
            "operation_kind": operation.operation_kind,
            "expected_active_generation": operation.expected_active_generation,
            "user_public_key": operation.user_public_key,
            "user_key_possession_signature_base64": _sign_user_key(
                operation.user_seed,
                possession_payload,
            ),
        }
        response = self.http.post(
            RUNNER_NATS_OPERATION_PATH,
            body,
            canonical_path=RUNNER_NATS_OPERATION_PATH,
            correlation_id=machine_request_id,
        )
        expected_fields = {
            "operation_id",
            "target_generation",
            "request_fingerprint",
            "phase",
            "replayed",
        }
        if set(response) != expected_fields:
            raise RunnerNatsTransportError("runner NATS operation begin response shape is invalid")
        if (
            response["operation_id"] != str(operation.operation_id)
            or response["target_generation"] != operation.target_generation
            or response["phase"] not in _PHASES
            or type(response["replayed"]) is not bool
        ):
            raise RunnerNatsTransportError("runner NATS operation begin response binding mismatch")
        fingerprint = str(response["request_fingerprint"])
        if not _LOWER_SHA256.fullmatch(fingerprint):
            raise RunnerNatsTransportError("runner NATS operation request fingerprint is invalid")
        return fingerprint

    def _read_result(
        self,
        operation: RunnerNatsTransportPendingOperation,
        request_fingerprint: str,
    ) -> RunnerNatsTransportOperationCompletion | None:
        machine_request_id = uuid4()
        body = {
            "tenant_id": operation.tenant_id,
            "runner_id": str(operation.runner_id),
            "credential_id": str(self.machine_credential.credential_id),
            "credential_version": self.machine_credential.credential_version,
            "machine_request_id": str(machine_request_id),
            "operation_id": str(operation.operation_id),
        }
        response = self.http.post(
            RUNNER_NATS_OPERATION_RESULT_PATH,
            body,
            canonical_path=RUNNER_NATS_OPERATION_RESULT_PATH,
            correlation_id=machine_request_id,
        )
        expected_fields = {
            "schema_version",
            "operation_id",
            "trading_mode",
            "operation_kind",
            "target_generation",
            "phase",
            "outcome",
            "request_fingerprint",
            "authority",
            "revocation_receipt_digest",
        }
        if set(response) != expected_fields:
            raise RunnerNatsTransportError("runner NATS operation result shape is invalid")
        if (
            response["schema_version"] != RUNNER_NATS_TRANSPORT_SCHEMA_VERSION
            or response["operation_id"] != str(operation.operation_id)
            or response["trading_mode"] != operation.trading_mode
            or response["operation_kind"] != operation.operation_kind
            or response["target_generation"] != operation.target_generation
            or response["phase"] not in _PHASES
            or response["request_fingerprint"] != request_fingerprint
        ):
            raise RunnerNatsTransportError("runner NATS operation result binding mismatch")
        outcome = response["outcome"]
        authority = response["authority"]
        receipt = response["revocation_receipt_digest"]
        if outcome == "pending":
            if authority is not None or receipt is not None:
                raise RunnerNatsTransportError(
                    "pending runner NATS operation exposed completion material"
                )
            return None
        if outcome != "succeeded":
            raise RunnerNatsTransportError("runner NATS operation outcome is unsupported")

        credential: RunnerNatsTransportCredential | None = None
        if operation.operation_kind in {"issue", "rotate"}:
            if not isinstance(authority, Mapping):
                raise RunnerNatsTransportError("successful runner NATS operation has no authority")
            credential = RunnerNatsTransportCredential.from_authority_document(
                authority,
                user_seed=operation.user_seed,
                expected_tenant_id=operation.tenant_id,
                expected_runner_id=operation.runner_id,
                expected_trading_mode=operation.trading_mode,
                expected_issuer_public_key=operation.expected_issuer_public_key,
            )
            if (
                credential.operation_id != operation.operation_id
                or credential.credential_generation != operation.target_generation
                or credential.user_public_key != operation.user_public_key
            ):
                raise RunnerNatsTransportError(
                    "completed runner NATS authority operation binding mismatch"
                )
        elif authority is not None:
            raise RunnerNatsTransportError(
                "successful runner NATS revocation returned an authority"
            )

        if operation.operation_kind in {"rotate", "revoke"}:
            if not isinstance(receipt, str) or not _LOWER_SHA256.fullmatch(receipt):
                raise RunnerNatsTransportError("runner NATS revocation receipt digest is invalid")
        elif receipt is not None:
            raise RunnerNatsTransportError(
                "initial runner NATS issue returned a revocation receipt"
            )
        return RunnerNatsTransportOperationCompletion(
            operation_id=operation.operation_id,
            operation_kind=operation.operation_kind,
            target_generation=operation.target_generation,
            request_fingerprint=request_fingerprint,
            credential=credential,
            revocation_receipt_digest=receipt,
        )

    def _assert_operation_binding(
        self,
        operation: RunnerNatsTransportPendingOperation,
    ) -> None:
        if (
            operation.tenant_id != self.machine_credential.tenant_id
            or operation.runner_id != self.machine_credential.runner_id
        ):
            raise RunnerNatsTransportError(
                "pending NATS operation has wrong machine authority binding"
            )

    def _assert_credential_binding(
        self,
        credential: RunnerNatsTransportCredential,
    ) -> None:
        if (
            credential.tenant_id != self.machine_credential.tenant_id
            or credential.runner_id != self.machine_credential.runner_id
        ):
            raise RunnerNatsTransportError("NATS credential has wrong machine authority binding")


__all__ = [
    "RUNNER_NATS_OPERATION_PATH",
    "RUNNER_NATS_OPERATION_RESULT_PATH",
    "RUNNER_NATS_USER_KEY_POSSESSION_PROFILE",
    "RunnerNatsTransportAuthorityClient",
    "RunnerNatsTransportOperationCompletion",
    "runner_nats_user_key_possession_payload_v1",
]
