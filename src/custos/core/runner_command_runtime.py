"""Sole V1 command-to-verified-artifact-to-engine coordination path."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from custos.artifacts.development_runtime import (
    ActivatedDevelopmentStrategyArtifact,
    DevelopmentArtifactRuntimeBlocked,
    DevelopmentStrategyArtifactRuntimeV1,
    PreparedDevelopmentStrategyArtifact,
)
from custos.artifacts.development_source import DevelopmentSourceVerificationError
from custos.artifacts.errors import ArtifactVerificationError
from custos.artifacts.release_resolver import (
    StrategyReleaseArtifactResolverV1,
    StrategyReleaseResolutionRejected,
    StrategyReleaseResolutionUnavailable,
)
from custos.artifacts.runtime import (
    ActivatedStrategyArtifact,
    ArtifactRuntimeActivationError,
    ArtifactRuntimeBlocked,
    PreparedStrategyArtifact,
    RuntimeEntryPointLoader,
    StrategyArtifactRuntimeV1,
)
from custos.core.engine_lifecycle import (
    EngineLifecycleBlocked,
    EngineLifecycleQuarantined,
    EngineLifecycleSupervisor,
)
from custos.core.engine_protocol import EngineReadyReceipt
from custos.core.runner_command_intake import (
    CommandDeliveryPolicy,
    CommandIntakeCoordinator,
    CommandIntakeDurability,
    CommandIntakeResult,
    CommandIntakeStatus,
    InboundCommandDelivery,
    VerifiedRunnerCommand,
)

logger = logging.getLogger(__name__)


class RunnerCredentialResolutionError(RuntimeError):
    """Base error for resolving a signed credential-scope reference."""


class RunnerCredentialResolutionUnavailable(RunnerCredentialResolutionError):
    """The local credential capability is temporarily unavailable."""


class RunnerCredentialResolutionRejected(RunnerCredentialResolutionError):
    """The signed scope does not resolve to an authorized local credential."""


class RunnerCredentialResolverV1(Protocol):
    async def resolve(
        self,
        verified: VerifiedRunnerCommand,
        credential_scope: object,
    ) -> dict[str, Any]: ...


class RunnerCommandRuntimeStatus(StrEnum):
    INTAKE_HANDLED = "intake_handled"
    APPLIED_ACKED = "applied_acked"
    RETRY_SCHEDULED = "retry_scheduled"
    TERMINAL_REJECTED = "terminal_rejected"
    TERMINAL_QUARANTINED = "terminal_quarantined"


@dataclass(frozen=True, slots=True)
class RunnerCommandRuntimeResult:
    status: RunnerCommandRuntimeStatus
    intake: CommandIntakeResult
    activation_id: str | None = None
    ready_receipt: EngineReadyReceipt | None = None
    reason_code: str | None = None


class RunnerCommandRuntimeCoordinator:
    """Coordinate the one first-production command path and its ACK boundary."""

    def __init__(
        self,
        *,
        intake: CommandIntakeCoordinator,
        durability: CommandIntakeDurability,
        release_resolver: StrategyReleaseArtifactResolverV1,
        artifact_runtime: StrategyArtifactRuntimeV1 | None,
        development_artifact_runtime: DevelopmentStrategyArtifactRuntimeV1 | None = None,
        entry_point_loader: RuntimeEntryPointLoader,
        credential_resolver: RunnerCredentialResolverV1,
        engine_lifecycle: EngineLifecycleSupervisor,
        delivery_policy: CommandDeliveryPolicy,
    ) -> None:
        self._intake = intake
        self._durability = durability
        self._release_resolver = release_resolver
        self._artifact_runtime = artifact_runtime
        self._development_artifact_runtime = development_artifact_runtime
        self._entry_point_loader = entry_point_loader
        self._credential_resolver = credential_resolver
        self._engine_lifecycle = engine_lifecycle
        self._policy = delivery_policy

    async def process(self, delivery: InboundCommandDelivery) -> RunnerCommandRuntimeResult:
        intake = await self._intake.process(delivery)
        if intake.status not in {
            CommandIntakeStatus.PREPARED_FOR_APPLY,
            CommandIntakeStatus.IDEMPOTENT_PENDING,
        }:
            return RunnerCommandRuntimeResult(
                status=RunnerCommandRuntimeStatus.INTAKE_HANDLED,
                intake=intake,
                reason_code=intake.reason_code,
            )
        verified = intake.verified
        if verified is None:
            raise RuntimeError("applicable command intake result lost verified authority")

        activated: ActivatedStrategyArtifact | ActivatedDevelopmentStrategyArtifact | None
        ready: EngineReadyReceipt | None
        try:
            if verified.command.lifecycle_state == "running":
                _prepared, activated, ready = await self._with_heartbeat(
                    delivery,
                    self._resolve_activate_apply(delivery.delivery_id, verified),
                )
            else:
                activated = None
                ready = None
                await self._with_heartbeat(
                    delivery,
                    self._engine_lifecycle.apply_non_running(
                        delivery_id=delivery.delivery_id,
                        verified=verified,
                    ),
                )
        except (
            StrategyReleaseResolutionRejected,
            ArtifactVerificationError,
            DevelopmentSourceVerificationError,
        ) as error:
            reason = self._reason_code("artifact_authority_rejected", error)
            return await self._terminal_rejection(delivery, intake, verified, reason)
        except (RunnerCredentialResolutionRejected, ArtifactRuntimeActivationError) as error:
            reason = self._reason_code("runtime_authority_rejected", error)
            return await self._terminal_rejection(delivery, intake, verified, reason)
        except EngineLifecycleQuarantined as error:
            await delivery.term()
            return RunnerCommandRuntimeResult(
                status=RunnerCommandRuntimeStatus.TERMINAL_QUARANTINED,
                intake=intake,
                reason_code=self._reason_code("engine_lifecycle_quarantined", error),
            )
        except (
            StrategyReleaseResolutionUnavailable,
            DevelopmentArtifactRuntimeBlocked,
            RunnerCredentialResolutionUnavailable,
            ArtifactRuntimeBlocked,
            EngineLifecycleBlocked,
        ) as error:
            return await self._retry_or_exhaust(
                delivery,
                intake,
                verified,
                self._reason_code("runtime_dependency_unavailable", error),
            )
        except Exception as error:  # noqa: BLE001 - bounded fail-closed retry
            logger.exception(
                "runner command apply failed",
                extra={
                    "delivery_id": delivery.delivery_id,
                    "deployment_instance_id": str(verified.command.deployment_instance_id),
                    "generation": verified.command.generation,
                },
            )
            return await self._retry_or_exhaust(
                delivery,
                intake,
                verified,
                self._reason_code("runtime_apply_failed", error),
            )

        await delivery.ack()
        return RunnerCommandRuntimeResult(
            status=RunnerCommandRuntimeStatus.APPLIED_ACKED,
            intake=intake,
            activation_id=activated.activation_id if activated is not None else None,
            ready_receipt=ready,
        )

    async def recover(self, verified: VerifiedRunnerCommand) -> EngineReadyReceipt:
        """Restore one durable running command without an inbound ACK boundary."""

        if verified.command.lifecycle_state != "running":
            raise RuntimeError("only a running durable command may recover an engine")
        _prepared, _activated, ready = await self._resolve_activate_apply(
            (
                "startup-recovery:"
                f"{verified.command.deployment_instance_id}:"
                f"{verified.command.generation}"
            ),
            verified,
        )
        return ready

    async def _resolve_activate_apply(
        self,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
    ) -> tuple[
        PreparedStrategyArtifact | PreparedDevelopmentStrategyArtifact,
        ActivatedStrategyArtifact | ActivatedDevelopmentStrategyArtifact,
        EngineReadyReceipt,
    ]:
        if verified.command.is_development_source:
            development_artifact_runtime = self._development_artifact_runtime
            if development_artifact_runtime is None:
                raise DevelopmentArtifactRuntimeBlocked(
                    "sandbox development artifact runtime is not composed"
                )
            prepared = await development_artifact_runtime.prepare(
                deployment_instance_id=verified.command.deployment_instance_id,
            )
            activated = await development_artifact_runtime.activate(
                prepared,
                loader=self._entry_point_loader,
            )
            artifact_policy_id = prepared.receipt.artifact_policy_id
        else:
            resolved = await self._release_resolver.resolve(verified)
            artifact_runtime = self._artifact_runtime
            if artifact_runtime is None:
                raise StrategyReleaseResolutionUnavailable(
                    "production StrategyRelease runtime is not composed"
                )
            prepared = await artifact_runtime.prepare(
                deployment_instance_id=verified.command.deployment_instance_id,
                release_authority=resolved.release_authority,
                release_statement_bytes=resolved.release_statement_bytes,
                detached_bundle_path=resolved.detached_bundle_path,
                member_paths=resolved.member_paths,
                verified_at=resolved.verified_at,
            )
            activated = await artifact_runtime.activate(
                prepared,
                loader=self._entry_point_loader,
            )
            artifact_policy_id = prepared.receipt.runner_local_policy_decision.policy_id
        runtime_spec_model = verified.command.to_runtime_spec()
        credential = await self._credential_resolver.resolve(
            verified,
            runtime_spec_model.credential_scope,
        )
        runtime_spec = runtime_spec_model.model_dump(mode="python")
        ready = await self._engine_lifecycle.apply(
            delivery_id=delivery_id,
            verified=verified,
            runtime_spec=runtime_spec,
            credential=credential,
            artifact=activated,
            artifact_policy_id=artifact_policy_id,
        )
        return prepared, activated, ready

    async def _with_heartbeat(self, delivery: InboundCommandDelivery, operation: Any) -> Any:
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=self._policy.in_progress_interval_seconds,
                    )
                    return
                except TimeoutError:
                    await delivery.in_progress()

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            return await operation
        finally:
            stop.set()
            await heartbeat_task

    async def _terminal_rejection(
        self,
        delivery: InboundCommandDelivery,
        intake: CommandIntakeResult,
        verified: VerifiedRunnerCommand,
        reason_code: str,
    ) -> RunnerCommandRuntimeResult:
        try:
            await self._durability.commit_verified_terminal_outcome(
                delivery_id=delivery.delivery_id,
                verified=verified,
                outcome="retry_exhausted",
                reason_code=reason_code,
            )
        except Exception as error:
            logger.exception(
                "durable runner command rejection failed",
                extra={
                    "delivery_id": delivery.delivery_id,
                    "deployment_instance_id": str(verified.command.deployment_instance_id),
                    "generation": verified.command.generation,
                },
            )
            await delivery.nak(delay=self._policy.backoff_for(delivery.delivered_count))
            return RunnerCommandRuntimeResult(
                status=RunnerCommandRuntimeStatus.RETRY_SCHEDULED,
                intake=intake,
                reason_code=self._reason_code("durable_runtime_rejection_failed", error),
            )
        await delivery.term()
        return RunnerCommandRuntimeResult(
            status=RunnerCommandRuntimeStatus.TERMINAL_REJECTED,
            intake=intake,
            reason_code=reason_code,
        )

    async def _retry_or_exhaust(
        self,
        delivery: InboundCommandDelivery,
        intake: CommandIntakeResult,
        verified: VerifiedRunnerCommand,
        reason_code: str,
    ) -> RunnerCommandRuntimeResult:
        if delivery.delivered_count >= self._policy.max_deliver:
            return await self._terminal_rejection(
                delivery,
                intake,
                verified,
                f"retry_exhausted:{reason_code}",
            )
        await delivery.nak(delay=self._policy.backoff_for(delivery.delivered_count))
        return RunnerCommandRuntimeResult(
            status=RunnerCommandRuntimeStatus.RETRY_SCHEDULED,
            intake=intake,
            reason_code=reason_code,
        )

    @staticmethod
    def _reason_code(prefix: str, error: BaseException) -> str:
        code = getattr(error, "code", None)
        value = getattr(code, "value", None)
        suffix = str(value or type(error).__name__).lower()
        return f"{prefix}:{suffix}"


__all__ = [
    "RunnerCommandRuntimeCoordinator",
    "RunnerCommandRuntimeResult",
    "RunnerCommandRuntimeStatus",
    "RunnerCredentialResolutionError",
    "RunnerCredentialResolutionRejected",
    "RunnerCredentialResolutionUnavailable",
    "RunnerCredentialResolverV1",
]
