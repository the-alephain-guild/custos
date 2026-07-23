"""Typed engine readiness, restart and terminal supervision over the RunnerFact store."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
from custos.core.engine_protocol import (
    ActivatedEngineArtifactV1,
    EngineLifecycleAuthority,
    EngineReadyReceipt,
    EngineTerminalEvent,
    ExecutionEngineProtocol,
)
from custos.core.runner_command_intake import VerifiedRunnerCommand
from custos.core.runner_fact import CommandOutcomeCommitResult, EngineLifecycleDurableState


class EngineLifecycleError(RuntimeError):
    """Base error for the corrected engine lifecycle adapter."""


class EngineLifecycleBlocked(EngineLifecycleError):
    """A required artifact or mode capability is not authorized."""


class EngineLifecycleQuarantined(EngineLifecycleError):
    """The durable restart budget is exhausted or a terminal event is final."""


@dataclass(frozen=True, slots=True)
class EngineLifecycleConfig:
    readiness_timeout_secs: float = 30.0
    restart_budget: int = 3
    restart_backoff_initial_secs: float = 1.0
    restart_backoff_max_secs: float = 30.0
    live_execution_enabled: bool = False

    def __post_init__(self) -> None:
        if self.readiness_timeout_secs <= 0:
            raise ValueError("readiness timeout must be positive")
        if type(self.restart_budget) is not int or self.restart_budget < 0:
            raise ValueError("restart budget must be a non-negative integer")
        if self.restart_backoff_initial_secs <= 0:
            raise ValueError("restart backoff must be positive")
        if self.restart_backoff_max_secs < self.restart_backoff_initial_secs:
            raise ValueError("restart backoff maximum must not be smaller than its initial value")


class EngineLifecycleStateStore(Protocol):
    async def load_engine_lifecycle_state(
        self, verified: VerifiedRunnerCommand
    ) -> EngineLifecycleDurableState: ...

    async def record_in_progress_lease(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        lease_until_ns: int,
    ) -> None: ...

    async def record_engine_restart(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        reason_code: str,
        lease_until_ns: int,
    ) -> int: ...

    async def commit_applied_and_enqueue_lifecycle(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        engine_handle: str | None,
        observed_status: str,
        artifact_activation_id: str | None = None,
        artifact_policy_id: str | None = None,
    ) -> CommandOutcomeCommitResult: ...

    async def commit_recovered_engine_ready(
        self,
        *,
        verified: VerifiedRunnerCommand,
        engine_handle: str,
        observed_status: str,
        artifact_activation_id: str | None = None,
        artifact_policy_id: str | None = None,
    ) -> None: ...

    async def commit_verified_command_outcome_and_enqueue_fact(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        outcome: Literal["applied", "conflict", "stale", "retry_exhausted"],
        reason_code: str,
        engine_handle: str | None,
        observed_status: str,
        lifecycle_state: str,
        artifact_activation_id: str | None = None,
        artifact_policy_id: str | None = None,
    ) -> CommandOutcomeCommitResult: ...


class EngineLifecycleSupervisor:
    """Apply one verified command without bypassing RunnerFact atomic lifecycle durability.

    The live engine lifecycle remains fail closed until every external capability
    receipt is present and the team daemon composition gate opens.
    """

    def __init__(
        self,
        *,
        engine: ExecutionEngineProtocol,
        state_store: EngineLifecycleStateStore,
        artifact_capability: ArtifactRuntimeCapabilityV1,
        config: EngineLifecycleConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._engine = engine
        self._store = state_store
        self._artifact_capability = artifact_capability
        self._config = config or EngineLifecycleConfig()
        self._sleep = sleep
        self._clock_ns = clock_ns

    async def apply(
        self,
        *,
        delivery_id: str,
        verified: Any,
        runtime_spec: dict[str, Any],
        credential: dict[str, Any],
        artifact: ActivatedEngineArtifactV1,
        artifact_policy_id: str | None = None,
    ) -> EngineReadyReceipt:
        authority = self._require_authorized_runtime(verified, runtime_spec, credential)
        artifact_activation_id = artifact.activation_id
        state = await self._store.load_engine_lifecycle_state(verified)
        recovered_applied = False
        if state.desired_status == "quarantined":
            raise EngineLifecycleQuarantined(
                state.quarantine_reason or "deployment lifecycle is durably quarantined"
            )
        if self._matches_ready_applied(state, verified):
            try:
                receipt = await self._await_ready(authority)
            except Exception:  # noqa: BLE001 - a lost prior engine enters bounded restart
                await self._engine.stop(str(authority.deployment_instance_id))
                restart_count = await self._record_restart(
                    delivery_id=delivery_id,
                    verified=verified,
                    reason_code="engine_missing_after_restart",
                )
                recovered_applied = True
            else:
                self._require_ready_identity(receipt, authority)
                return receipt
        else:
            restart_count = state.restart_count
        return await self._start_with_budget(
            delivery_id=delivery_id,
            verified=verified,
            authority=authority,
            runtime_spec=runtime_spec,
            credential=credential,
            artifact=artifact,
            artifact_activation_id=artifact_activation_id,
            artifact_policy_id=artifact_policy_id,
            restart_count=restart_count,
            recovered_applied=recovered_applied,
        )

    async def apply_non_running(
        self,
        *,
        delivery_id: str,
        verified: Any,
    ) -> None:
        """Apply a signed fail-safe lifecycle state without touching artifact bytes."""

        lifecycle_state = str(verified.command.lifecycle_state)
        if lifecycle_state not in {"paused", "stopped", "archived"}:
            raise EngineLifecycleBlocked(
                "non-running lifecycle application requires paused, stopped or archived"
            )
        authority = EngineLifecycleAuthority.from_verified_command(verified)
        if not self._engine.supports_trading_mode(authority.trading_mode):
            raise EngineLifecycleBlocked("engine does not support the signed trading mode")
        await self._store.load_engine_lifecycle_state(verified)
        await self._store.record_in_progress_lease(
            delivery_id=delivery_id,
            verified=verified,
            lease_until_ns=self._lease_deadline_ns(),
        )
        await self._engine.stop(str(authority.deployment_instance_id))
        await self._store.commit_applied_and_enqueue_lifecycle(
            delivery_id=delivery_id,
            verified=verified,
            engine_handle=None,
            observed_status=lifecycle_state,
            artifact_activation_id=None,
            artifact_policy_id=None,
        )

    async def supervise_once(
        self,
        *,
        delivery_id: str,
        verified: Any,
        runtime_spec: dict[str, Any],
        credential: dict[str, Any],
        artifact: ActivatedEngineArtifactV1,
        artifact_policy_id: str | None = None,
    ) -> EngineReadyReceipt | None:
        authority = self._require_authorized_runtime(verified, runtime_spec, credential)
        artifact_activation_id = artifact.activation_id
        event = await self._engine.wait_terminal(authority)
        self._require_terminal_identity(event, authority)
        await self._engine.stop(str(authority.deployment_instance_id))
        if not event.retryable:
            await self._quarantine(
                delivery_id=delivery_id,
                verified=verified,
                reason_code=event.reason_code,
                artifact_activation_id=artifact_activation_id,
                artifact_policy_id=artifact_policy_id,
            )
        await self._store.record_in_progress_lease(
            delivery_id=delivery_id,
            verified=verified,
            lease_until_ns=self._lease_deadline_ns(),
        )
        restart_count = await self._record_restart(
            delivery_id=delivery_id,
            verified=verified,
            reason_code=event.reason_code,
        )
        if restart_count > self._config.restart_budget:
            await self._quarantine(
                delivery_id=delivery_id,
                verified=verified,
                reason_code=f"restart_budget_exhausted:{event.reason_code}",
                artifact_activation_id=artifact_activation_id,
                artifact_policy_id=artifact_policy_id,
            )
        await self._sleep(self._backoff(restart_count))
        return await self._start_with_budget(
            delivery_id=delivery_id,
            verified=verified,
            authority=authority,
            runtime_spec=runtime_spec,
            credential=credential,
            artifact=artifact,
            artifact_activation_id=artifact_activation_id,
            artifact_policy_id=artifact_policy_id,
            restart_count=restart_count,
        )

    async def _start_with_budget(
        self,
        *,
        delivery_id: str,
        verified: Any,
        authority: EngineLifecycleAuthority,
        runtime_spec: dict[str, Any],
        credential: dict[str, Any],
        artifact: ActivatedEngineArtifactV1,
        artifact_activation_id: str,
        artifact_policy_id: str | None,
        restart_count: int,
        recovered_applied: bool = False,
    ) -> EngineReadyReceipt:
        while True:
            await self._store.record_in_progress_lease(
                delivery_id=delivery_id,
                verified=verified,
                lease_until_ns=self._lease_deadline_ns(),
            )
            handle: str | None = None
            try:
                handle = await self._engine.deploy(runtime_spec, credential, artifact)
                receipt = await self._await_ready(authority)
                self._require_ready_identity(receipt, authority)
            except Exception as exc:  # noqa: BLE001 - typed terminal mapping below
                if handle is not None:
                    await self._engine.stop(str(authority.deployment_instance_id))
                reason_code = (
                    "engine_ready_timeout"
                    if isinstance(exc, TimeoutError)
                    else "engine_start_failed"
                )
                if restart_count >= self._config.restart_budget:
                    await self._quarantine(
                        delivery_id=delivery_id,
                        verified=verified,
                        reason_code=reason_code,
                        artifact_activation_id=artifact_activation_id,
                        artifact_policy_id=artifact_policy_id,
                    )
                restart_count = await self._record_restart(
                    delivery_id=delivery_id,
                    verified=verified,
                    reason_code=reason_code,
                )
                await self._sleep(self._backoff(restart_count))
                continue
            if recovered_applied:
                await self._store.commit_recovered_engine_ready(
                    verified=verified,
                    engine_handle=handle,
                    observed_status="ready",
                    artifact_activation_id=artifact_activation_id,
                    artifact_policy_id=artifact_policy_id,
                )
            else:
                await self._store.commit_applied_and_enqueue_lifecycle(
                    delivery_id=delivery_id,
                    verified=verified,
                    engine_handle=handle,
                    observed_status="ready",
                    artifact_activation_id=artifact_activation_id,
                    artifact_policy_id=artifact_policy_id,
                )
            return receipt

    async def _quarantine(
        self,
        *,
        delivery_id: str,
        verified: Any,
        reason_code: str,
        artifact_activation_id: str,
        artifact_policy_id: str | None,
    ) -> None:
        await self._store.commit_verified_command_outcome_and_enqueue_fact(
            delivery_id=delivery_id,
            verified=verified,
            outcome="retry_exhausted",
            reason_code=reason_code,
            engine_handle=None,
            observed_status="quarantined",
            lifecycle_state=str(verified.command.lifecycle_state),
            artifact_activation_id=artifact_activation_id,
            artifact_policy_id=artifact_policy_id,
        )
        raise EngineLifecycleQuarantined(reason_code)

    async def _record_restart(
        self,
        *,
        delivery_id: str,
        verified: Any,
        reason_code: str,
    ) -> int:
        return await self._store.record_engine_restart(
            delivery_id=delivery_id,
            verified=verified,
            reason_code=reason_code,
            lease_until_ns=self._lease_deadline_ns(),
        )

    async def _await_ready(self, authority: EngineLifecycleAuthority) -> EngineReadyReceipt:
        return await asyncio.wait_for(
            self._engine.wait_ready(
                authority,
                timeout_secs=self._config.readiness_timeout_secs,
            ),
            timeout=self._config.readiness_timeout_secs,
        )

    def _require_authorized_runtime(
        self,
        verified: Any,
        runtime_spec: dict[str, Any],
        credential: dict[str, Any],
    ) -> EngineLifecycleAuthority:
        if not self._artifact_capability.ready:
            raise EngineLifecycleBlocked("artifact runtime capability is not READY")
        authority = EngineLifecycleAuthority.from_verified_command(verified)
        mode = str(runtime_spec.get("trading_mode") or "")
        if mode != authority.trading_mode:
            raise EngineLifecycleBlocked("runtime spec mode differs from signed command")
        if not self._engine.supports_trading_mode(mode):
            raise EngineLifecycleBlocked(f"execution engine does not support signed mode {mode}")
        connector = str(runtime_spec.get("connector") or "")
        if not connector or not self._engine.supports_venue(connector):
            raise EngineLifecycleBlocked("execution engine does not support the signed venue")
        if mode in {"testnet", "live"} and credential.get("permission_scope") != (
            "trade_no_withdraw"
        ):
            raise EngineLifecycleBlocked(
                "real-venue credential must be scoped to trade_no_withdraw"
            )
        if mode == "live":
            if not self._config.live_execution_enabled:
                raise EngineLifecycleBlocked(
                    "live execution is disabled until the immutable runtime receipt is accepted"
                )
            if not runtime_spec.get("promotion_id") or not runtime_spec.get(
                "promotion_evidence_digest"
            ):
                raise EngineLifecycleBlocked(
                    "live execution lacks signed Crucible promotion evidence"
                )
        return authority

    @staticmethod
    def _matches_ready_applied(state: EngineLifecycleDurableState, verified: Any) -> bool:
        return (
            state.applied_generation == verified.command.generation
            and state.applied_command_fingerprint == verified.command_fingerprint
            and state.observed_status == "ready"
            and state.engine_handle is not None
        )

    @staticmethod
    def _require_ready_identity(
        receipt: EngineReadyReceipt, authority: EngineLifecycleAuthority
    ) -> None:
        if (
            receipt.deployment_instance_id != authority.deployment_instance_id
            or receipt.deployment_spec_id != authority.deployment_spec_id
            or receipt.deployment_spec_digest != authority.deployment_spec_digest
            or receipt.generation != authority.generation
        ):
            raise EngineLifecycleError("engine ready receipt differs from signed command authority")

    @staticmethod
    def _require_terminal_identity(
        event: EngineTerminalEvent, authority: EngineLifecycleAuthority
    ) -> None:
        if (
            event.deployment_instance_id != authority.deployment_instance_id
            or event.deployment_spec_id != authority.deployment_spec_id
            or event.generation != authority.generation
        ):
            raise EngineLifecycleError(
                "engine terminal event differs from signed command authority"
            )

    def _lease_deadline_ns(self) -> int:
        seconds = self._config.readiness_timeout_secs + self._config.restart_backoff_max_secs
        return self._clock_ns() + max(1, int(seconds * 1_000_000_000))

    def _backoff(self, restart_count: int) -> float:
        delay = self._config.restart_backoff_initial_secs * (2 ** max(0, restart_count - 1))
        return min(delay, self._config.restart_backoff_max_secs)


__all__ = [
    "EngineLifecycleBlocked",
    "EngineLifecycleConfig",
    "EngineLifecycleDurableState",
    "EngineLifecycleError",
    "EngineLifecycleQuarantined",
    "EngineLifecycleSupervisor",
]
