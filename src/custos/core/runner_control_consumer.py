"""Consume the one signed control-plane runner-control stream."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from custos.contracts.crucible_runner_safety_policy import (
    SUBJECT_PREFIX as RUNNER_POLICY_SUBJECT_PREFIX,
)
from custos.contracts.crucible_runner_safety_policy import (
    CrucibleRunnerSafetyPolicyAuthenticator,
    RunnerSafetyPolicyVerificationError,
)
from custos.core.log import get_logger
from custos.core.nats_client import CrucibleNatsClient
from custos.core.nats_transport import RunnerNatsTransportError
from custos.core.runner_command_runtime import RunnerCommandRuntimeCoordinator
from custos.core.runner_fact import RunnerStateStore

log = get_logger("custos.runner-control")


@dataclass(frozen=True, slots=True)
class JetStreamCommandDelivery:
    """Narrow inbound command delivery port over one nats-py message."""

    message: Any

    @property
    def subject(self) -> str:
        return str(self.message.subject)

    @property
    def data(self) -> bytes:
        return bytes(self.message.data)

    @property
    def delivered_count(self) -> int:
        return int(self.message.metadata.num_delivered)

    @property
    def delivery_id(self) -> str:
        metadata = self.message.metadata
        return f"{metadata.stream}:{metadata.consumer}:{metadata.sequence.stream}"

    async def ack(self) -> None:
        await self.message.ack()

    async def nak(self, delay: float | None = None) -> None:
        await self.message.nak(delay=delay)

    async def term(self) -> None:
        await self.message.term()

    async def in_progress(self) -> None:
        await self.message.in_progress()


class RunnerControlConsumerV1:
    """Dispatch commands and policies from the existing exact-mode durable."""

    def __init__(
        self,
        *,
        command_runtime: RunnerCommandRuntimeCoordinator,
        policy_authenticator: CrucibleRunnerSafetyPolicyAuthenticator,
        state_store: RunnerStateStore,
    ) -> None:
        self._command_runtime = command_runtime
        self._policy_authenticator = policy_authenticator
        self._state_store = state_store

    async def run(
        self,
        *,
        client: CrucibleNatsClient,
        subscription: Any,
        stop: asyncio.Event,
    ) -> None:
        async for message in subscription.messages:
            if stop.is_set():
                return
            if str(message.subject).startswith(f"{RUNNER_POLICY_SUBJECT_PREFIX}."):
                await self._process_policy(client, message)
                continue
            delivery = JetStreamCommandDelivery(message)
            result = await self._command_runtime.process(delivery)
            log.info(
                "runner_command_processed",
                status=result.status.value,
                reason_code=result.reason_code or "none",
                delivery_count=delivery.delivered_count,
            )

    async def _process_policy(self, client: CrucibleNatsClient, message: Any) -> None:
        try:
            verified = self._policy_authenticator.verify(
                subject=str(message.subject),
                signed_envelope_bytes=bytes(message.data),
            )
            client.assert_policy_binding(str(message.subject), verified.policy)
        except (RunnerSafetyPolicyVerificationError, RunnerNatsTransportError, ValueError) as exc:
            reason_code = getattr(exc, "reason_code", "transport_binding_invalid")
            log.error(
                "runner_safety_policy_rejected",
                reason_code=reason_code,
                error_type=type(exc).__name__,
            )
            await message.term()
            return
        try:
            await self._state_store.record_verified_runner_safety_policy(verified)
        except Exception as exc:  # noqa: BLE001 - local durability failure is retryable
            log.warning(
                "runner_safety_policy_commit_failed",
                error_type=type(exc).__name__,
            )
            await message.nak(delay=10.0)
            return
        await message.ack()


__all__ = ["JetStreamCommandDelivery", "RunnerControlConsumerV1"]
