"""Machine-authenticated bootstrap of the current signed Runner safety policy."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from custos.contracts.crucible_runner_safety_policy import (
    CrucibleRunnerSafetyPolicyAuthenticator,
    RunnerSafetyPolicyVerificationError,
    VerifiedRunnerSafetyPolicy,
)
from custos.core.machine_credential_vault import (
    MachineCredential,
    MachineCredentialError,
    MachineCredentialHttpClient,
    MachineCredentialRejectedError,
    MachineCredentialTransportError,
)

RUNNER_SAFETY_POLICY_RESOLUTION_PATH = "/api/v1/runner-safety-policy/resolve"
_RESPONSE_FIELDS = {
    "schema_version",
    "tenant_id",
    "runner_id",
    "trading_mode",
    "exact_subject",
    "signed_envelope_base64url",
}
_TRADING_MODES = frozenset({"live", "sandbox", "testnet"})


class RunnerSafetyPolicyAuthorityError(RuntimeError):
    """The current owner policy could not be safely bootstrapped."""


class RunnerSafetyPolicyAuthorityUnavailableError(RunnerSafetyPolicyAuthorityError):
    """The owner authority could not be reached or returned a server failure."""


class RunnerSafetyPolicyAuthorityRejectedError(RunnerSafetyPolicyAuthorityError):
    """The owner authority or signature contract rejected the requested policy."""


class RunnerSafetyPolicyAuthorityClient:
    """Resolve the exact signed event already published by Crucible's outbox."""

    def __init__(
        self,
        crucible_url: str,
        machine_credential: MachineCredential,
        authenticator: CrucibleRunnerSafetyPolicyAuthenticator,
    ) -> None:
        self.machine_credential = machine_credential
        self.authenticator = authenticator
        self.http = MachineCredentialHttpClient(crucible_url, machine_credential)

    async def resolve_current(self, trading_mode: str) -> VerifiedRunnerSafetyPolicy:
        if trading_mode not in _TRADING_MODES:
            raise RunnerSafetyPolicyAuthorityRejectedError(
                "runner safety policy trading mode is invalid"
            )
        correlation_id = uuid4()
        body = {
            "tenant_id": self.machine_credential.tenant_id,
            "runner_id": str(self.machine_credential.runner_id),
            "credential_id": str(self.machine_credential.credential_id),
            "credential_version": self.machine_credential.credential_version,
            "correlation_id": str(correlation_id),
            "trading_mode": trading_mode,
        }
        try:
            response = await asyncio.to_thread(
                self.http.post,
                RUNNER_SAFETY_POLICY_RESOLUTION_PATH,
                body,
                canonical_path=RUNNER_SAFETY_POLICY_RESOLUTION_PATH,
                correlation_id=correlation_id,
            )
        except MachineCredentialTransportError as error:
            raise RunnerSafetyPolicyAuthorityUnavailableError(
                "runner safety policy authority is unavailable"
            ) from error
        except (MachineCredentialRejectedError, MachineCredentialError) as error:
            raise RunnerSafetyPolicyAuthorityRejectedError(
                "runner safety policy authority rejected the machine binding"
            ) from error
        return parse_runner_safety_policy_resolution(
            response,
            authenticator=self.authenticator,
            expected_tenant_id=self.machine_credential.tenant_id,
            expected_runner_id=self.machine_credential.runner_id,
            expected_trading_mode=trading_mode,
        )


def parse_runner_safety_policy_resolution(
    response: Mapping[str, Any],
    *,
    authenticator: CrucibleRunnerSafetyPolicyAuthenticator,
    expected_tenant_id: str,
    expected_runner_id: UUID,
    expected_trading_mode: str,
) -> VerifiedRunnerSafetyPolicy:
    if set(response) != _RESPONSE_FIELDS or response.get("schema_version") != 1:
        raise RunnerSafetyPolicyAuthorityRejectedError(
            "runner safety policy authority response shape is invalid"
        )
    expected = {
        "tenant_id": expected_tenant_id,
        "runner_id": str(expected_runner_id),
        "trading_mode": expected_trading_mode,
    }
    if any(response.get(field) != value for field, value in expected.items()):
        raise RunnerSafetyPolicyAuthorityRejectedError(
            "runner safety policy authority response scope differs from the Runner"
        )
    subject = response.get("exact_subject")
    if not isinstance(subject, str) or not subject:
        raise RunnerSafetyPolicyAuthorityRejectedError(
            "runner safety policy subject is invalid"
        )
    envelope = _decode_base64url(
        response.get("signed_envelope_base64url"),
        "runner safety policy signed envelope",
    )
    try:
        verified = authenticator.verify(
            subject=subject,
            signed_envelope_bytes=envelope,
        )
    except RunnerSafetyPolicyVerificationError as error:
        raise RunnerSafetyPolicyAuthorityRejectedError(
            "runner safety policy signature or exact-byte binding is invalid"
        ) from error
    if verified.policy.trading_mode != expected_trading_mode:
        raise RunnerSafetyPolicyAuthorityRejectedError(
            "verified runner safety policy mode differs from the request"
        )
    return verified


def _decode_base64url(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise RunnerSafetyPolicyAuthorityRejectedError(f"{label} is not canonical base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as error:
        raise RunnerSafetyPolicyAuthorityRejectedError(
            f"{label} is not canonical base64url"
        ) from error
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise RunnerSafetyPolicyAuthorityRejectedError(f"{label} is not canonical base64url")
    return decoded


__all__ = [
    "RUNNER_SAFETY_POLICY_RESOLUTION_PATH",
    "RunnerSafetyPolicyAuthorityClient",
    "RunnerSafetyPolicyAuthorityError",
    "RunnerSafetyPolicyAuthorityRejectedError",
    "RunnerSafetyPolicyAuthorityUnavailableError",
    "parse_runner_safety_policy_resolution",
]
