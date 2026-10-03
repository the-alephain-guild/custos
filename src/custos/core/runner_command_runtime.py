"""Sole V1 command-to-verified-artifact-to-engine coordination path."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
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
from custos.core.engine_protocol import EngineNodeCapacity, EngineReadyReceipt
from custos.core.log import get_logger
from custos.core.runner_command_intake import (
    CommandDeliveryPolicy,
    CommandIntakeCoordinator,
    CommandIntakeDurability,
    CommandIntakeResult,
    CommandIntakeStatus,
    InboundCommandDelivery,
    VerifiedRunnerCommand,
)
from custos.core.runner_fact import RunnerFactContractError, RunnerStateSupersededError

logger = logging.getLogger(__name__)
_slog = get_logger("custos.runner_command_runtime")


class RunnerCredentialResolutionError(RuntimeError):
    """Base error for resolving a signed credential-scope reference."""


class RunnerCredentialResolutionUnavailable(RunnerCredentialResolutionError):
    """The local credential capability is temporarily unavailable."""


class RunnerCredentialResolutionRejected(RunnerCredentialResolutionError):
    """The signed scope does not resolve to an authorized local credential."""


class RunnerEngineCapacityError(RuntimeError):
    """The runner's one engine node is held by another deployment instance."""

    reason_code: str

    def __init__(self, holder_instance_id: str) -> None:
        super().__init__(
            f"{self.reason_code}: deployment instance {holder_instance_id!r} holds the "
            "runner's engine node"
        )
        self.holder_instance_id = holder_instance_id


class RunnerEngineOccupied(RunnerEngineCapacityError):
    """Another instance holds the node; this command cannot start here."""

    reason_code = "runner_engine_occupied"


class RunnerEngineReleasing(RunnerEngineCapacityError):
    """The instance holding the node is being stopped; the node will be free."""

    reason_code = "runner_engine_releasing"


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
    # Acknowledged and durably recorded, but neither applied nor refused: the
    # runner cannot sign for the instance until it restarts with a capability
    # that binds it, and then recovers and reports it.
    DEFERRED_AWAITING_BINDING = "deferred_awaiting_binding"


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
        capability_binding: Callable[[VerifiedRunnerCommand], str | None],
        node_capacity: EngineNodeCapacity | None = None,
    ) -> None:
        self._intake = intake
        self._durability = durability
        self._release_resolver = release_resolver
        self._artifact_runtime = artifact_runtime
        self._development_artifact_runtime = development_artifact_runtime
        self._entry_point_loader = entry_point_loader
        self._credential_resolver = credential_resolver
        self._engine_lifecycle = engine_lifecycle
        try:
            supervise_once = engine_lifecycle.supervise_once
        except AttributeError as exc:
            raise TypeError("engine lifecycle must provide supervise_once") from exc
        if not callable(supervise_once):
            raise TypeError("engine lifecycle supervise_once must be callable")
        self._policy = delivery_policy
        # Why the capability cannot yet sign for a command's instance, or None.
        self._capability_binding = capability_binding
        # The engine's one-node limit, or None for an engine without one.
        self._node_capacity = node_capacity
        self._engine_supervisions: dict[object, asyncio.Task[None]] = {}
        self._engine_supervision_failures: asyncio.Queue[BaseException] = asyncio.Queue()
        # One lifecycle operation per deployment at a time, and the background
        # recovery a restarted runner started for it, if one is still running.
        self._lifecycle_locks: dict[object, asyncio.Lock] = {}
        self._recoveries: dict[object, asyncio.Task[None]] = {}

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
        instance = verified.command.deployment_instance_id
        # A newer signed command supersedes a recovery still waiting on the venue;
        # a stop must not queue behind every remaining recovery attempt.
        await self._preempt_recovery(instance)
        async with self._lifecycle_lock(instance):
            return await self._apply_verified(delivery, intake, verified)

    async def _apply_verified(
        self,
        delivery: InboundCommandDelivery,
        intake: Any,
        verified: VerifiedRunnerCommand,
    ) -> RunnerCommandRuntimeResult:
        activated: ActivatedStrategyArtifact | ActivatedDevelopmentStrategyArtifact | None
        ready: EngineReadyReceipt | None
        binding_gap = self._capability_binding(verified)
        if binding_gap is not None:
            return await self._defer_awaiting_binding(delivery, intake, verified, binding_gap)
        try:
            if verified.command.lifecycle_state == "running":
                _prepared, activated, ready = await self._with_heartbeat(
                    delivery,
                    self._resolve_activate_apply(delivery.delivery_id, verified),
                )
            else:
                activated = None
                ready = None
                await self._cancel_engine_supervision(verified.command.deployment_instance_id)
                await self._with_heartbeat(
                    delivery,
                    self._engine_lifecycle.apply_non_running(
                        delivery_id=delivery.delivery_id,
                        verified=verified,
                    ),
                )
        except Exception as error:  # noqa: BLE001 - classified below, never swallowed
            failure = _classify_failure(error)
            if failure.kind is _FailureKind.REFUSED:
                return await self._terminal_rejection(
                    delivery, intake, verified, failure.reason_code
                )
            if failure.kind is _FailureKind.QUARANTINED:
                await delivery.term()
                return RunnerCommandRuntimeResult(
                    status=RunnerCommandRuntimeStatus.TERMINAL_QUARANTINED,
                    intake=intake,
                    reason_code=failure.reason_code,
                )
            if failure.kind is _FailureKind.UNEXPECTED:
                logger.exception(
                    "runner command apply failed",
                    extra={
                        "delivery_id": delivery.delivery_id,
                        "deployment_instance_id": str(verified.command.deployment_instance_id),
                        "generation": verified.command.generation,
                    },
                )
            return await self._retry_or_exhaust(delivery, intake, verified, failure.reason_code)

        await delivery.ack()
        return RunnerCommandRuntimeResult(
            status=RunnerCommandRuntimeStatus.APPLIED_ACKED,
            intake=intake,
            activation_id=activated.activation_id if activated is not None else None,
            ready_receipt=ready,
        )

    def schedule_recovery(
        self,
        verified: VerifiedRunnerCommand,
        *,
        after: asyncio.Task[None] | None = None,
    ) -> asyncio.Task[None]:
        """Recover one durable command in the background.

        Recovery waits for the engine, which waits for the venue. Running it here
        instead of inline lets a restarted runner become ready, report and take
        commands while a venue is unreachable, and keeps one deployment that
        cannot recover from stopping the others. With ``after`` it starts once
        that recovery has finished, however it finished.
        """

        instance = verified.command.deployment_instance_id
        existing = self._recoveries.get(instance)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(
            self._recover_in_background(verified, after=after),
            name=f"runner-recovery-{instance}",
        )
        self._recoveries[instance] = task
        task.add_done_callback(lambda done: self._forget_recovery(instance, done))
        return task

    def schedule_recoveries(self, verified_commands: Sequence[VerifiedRunnerCommand]) -> None:
        """Recover durable commands, running ones in turn on a one-node engine.

        An engine that runs one node per process can recover only one instance;
        recovering running commands concurrently makes the outcome a race. They
        are recovered in the order given (the daemon puts applied instances
        before recorded ones), so the first that starts holds the node and each
        later one is refused as occupied and reported. Without that limit they
        recover concurrently, as before. A command that stops, pauses or
        archives an instance takes no node, so it neither waits for a running
        recovery nor holds one back.
        """

        previous: asyncio.Task[None] | None = None
        for verified in verified_commands:
            if verified.command.lifecycle_state != "running":
                self.schedule_recovery(verified)
                continue
            after = previous if self._node_capacity is not None else None
            previous = self.schedule_recovery(verified, after=after)

    def recovery_in_progress(self, instance: object) -> bool:
        task = self._recoveries.get(instance)
        return task is not None and not task.done()

    async def recoveries_settled(self) -> None:
        await asyncio.gather(*tuple(self._recoveries.values()), return_exceptions=True)

    async def close_recoveries(self) -> None:
        tasks = tuple(self._recoveries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _forget_recovery(self, instance: object, task: asyncio.Task[None]) -> None:
        if self._recoveries.get(instance) is task:
            del self._recoveries[instance]

    def _lifecycle_lock(self, instance: object) -> asyncio.Lock:
        lock = self._lifecycle_locks.get(instance)
        if lock is None:
            lock = self._lifecycle_locks[instance] = asyncio.Lock()
        return lock

    async def _preempt_recovery(self, instance: object) -> None:
        task = self._recoveries.get(instance)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _recover_in_background(
        self,
        verified: VerifiedRunnerCommand,
        *,
        after: asyncio.Task[None] | None = None,
    ) -> None:
        if after is not None:
            # ``wait`` rather than ``gather``: cancelling this recovery must not
            # cancel the one it is waiting for.
            await asyncio.wait({after})
        instance = verified.command.deployment_instance_id
        attempt = 1
        while True:
            async with self._lifecycle_lock(instance):
                try:
                    await self.recover(verified)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one deployment must not end the runner
                    retry_in = self._recovery_retry_delay(verified, exc, attempt)
                    if retry_in is None:
                        await self._report_recovery_failure(verified, exc)
                        return
            # The back-off is waited outside the lock: a newer command for the
            # instance cancels this recovery and must not queue behind it.
            await asyncio.sleep(retry_in)
            attempt += 1
        _slog.info(
            "durable_command_recovered",
            deployment_instance_id=str(instance),
            generation=verified.command.generation,
            lifecycle_state=str(verified.command.lifecycle_state),
            attempt=attempt,
        )

    def _recovery_retry_delay(
        self,
        verified: VerifiedRunnerCommand,
        error: Exception,
        attempt: int,
    ) -> float | None:
        """Seconds until a failed recovery is tried again, or None when it is not.

        A recovery has no delivery that could be redelivered, so a failure that
        may pass is retried in place on the schedule a delivered command gets:
        as many attempts as deliveries, with the same back-off. A refusal, a
        quarantine, a superseded or an unsignable outcome is final at once.
        """

        if isinstance(error, RunnerStateSupersededError | RunnerFactContractError):
            return None
        failure = _classify_failure(error)
        if failure.kind not in _MAY_PASS:
            return None
        if attempt >= self._policy.max_deliver:
            return None
        retry_in = self._policy.backoff_for(attempt)
        if failure.kind is _FailureKind.UNEXPECTED:
            logger.exception(
                "runner command recovery failed",
                extra={
                    "deployment_instance_id": str(verified.command.deployment_instance_id),
                    "generation": verified.command.generation,
                },
            )
        _slog.warning(
            "durable_command_recovery_retry_scheduled",
            deployment_instance_id=str(verified.command.deployment_instance_id),
            generation=verified.command.generation,
            lifecycle_state=str(verified.command.lifecycle_state),
            attempt=attempt,
            max_attempts=self._policy.max_deliver,
            retry_in_seconds=retry_in,
            reason_code=failure.reason_code,
            **_failure_detail(error, failure),
        )
        return retry_in

    async def _report_recovery_failure(
        self,
        verified: VerifiedRunnerCommand,
        error: Exception,
    ) -> None:
        """Say how a recovery ended, with the same classification as a command.

        A refusal is committed as a final outcome and signed as a lifecycle
        RunnerFact, as it would have been for the command itself; an engine
        quarantine was already committed and signed by the lifecycle supervisor.
        A start that was still failing for a reason that may pass after its last
        attempt is refused as exhausted, as a delivered command is. A stop, pause
        or archive in that state is logged and kept for the next restart: the
        instance was not started by this process, and dropping the command would
        lose the outcome it still owes.
        """

        instance = str(verified.command.deployment_instance_id)
        lifecycle_state = str(verified.command.lifecycle_state)
        if isinstance(error, RunnerStateSupersededError):
            # A newer command for the instance arrived after this one was listed
            # for recovery; the newer one owns the instance and this one has no
            # outcome to report. Nothing was done to the engine.
            _slog.warning(
                "durable_command_recovery_superseded",
                deployment_instance_id=instance,
                generation=verified.command.generation,
                lifecycle_state=lifecycle_state,
            )
            return
        if isinstance(error, RunnerFactContractError):
            # The outcome cannot be signed for the instance. It stays recorded and
            # is recovered again by the next restart.
            _slog.error(
                "durable_command_recovery_unsigned",
                deployment_instance_id=instance,
                generation=verified.command.generation,
                lifecycle_state=lifecycle_state,
                binding_gap=str(error),
            )
            return
        failure = _classify_failure(error)
        if failure.kind is _FailureKind.REFUSED:
            reason_code = failure.reason_code
        elif failure.kind in _MAY_PASS and lifecycle_state == "running":
            reason_code = f"retry_exhausted:{failure.reason_code}"
        else:
            _slog.warning(
                "durable_command_recovery_failed",
                deployment_instance_id=instance,
                generation=verified.command.generation,
                lifecycle_state=lifecycle_state,
                reason_code=str(error) if failure.kind is _FailureKind.QUARANTINED else None,
                **_failure_detail(error, failure),
            )
            return
        try:
            await self._engine_lifecycle.commit_refusal(
                delivery_id=_recovery_delivery_id(verified),
                verified=verified,
                reason_code=reason_code,
            )
        except Exception as commit_error:  # noqa: BLE001 - reported, runner continues
            _slog.error(
                "durable_command_recovery_refusal_unrecorded",
                deployment_instance_id=instance,
                generation=verified.command.generation,
                reason_code=reason_code,
                error_type=type(commit_error).__name__,
            )
            return
        _slog.warning(
            "durable_command_recovery_refused",
            deployment_instance_id=instance,
            generation=verified.command.generation,
            reason_code=reason_code,
            **_failure_detail(error, failure),
        )

    async def recover(self, verified: VerifiedRunnerCommand) -> EngineReadyReceipt | None:
        """Restore one durable command without an inbound ACK boundary.

        A running command restores its engine and returns the ready receipt. A
        command that stops, pauses or archives the instance is applied as it
        would have been on delivery and returns None; one kept while its
        instance was unbound was never started by this process, so the stop is
        a no-op and the outcome is committed and signed. Supervision is left
        alone: if a newer generation owns the instance, the lease refuses this
        command before the engine is touched.
        """

        if verified.command.lifecycle_state != "running":
            await self._engine_lifecycle.apply_non_running(
                delivery_id=_recovery_delivery_id(verified),
                verified=verified,
            )
            return None
        _prepared, _activated, ready = await self._resolve_activate_apply(
            _recovery_delivery_id(verified),
            verified,
            initial_reconciliation_backfill=False,
        )
        return ready

    async def _resolve_activate_apply(
        self,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        *,
        initial_reconciliation_backfill: bool = True,
    ) -> tuple[
        PreparedStrategyArtifact | PreparedDevelopmentStrategyArtifact,
        ActivatedStrategyArtifact | ActivatedDevelopmentStrategyArtifact,
        EngineReadyReceipt,
    ]:
        self._require_node_capacity(verified)
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
        # The signed command timestamp is the earliest authoritative boundary
        # for a fresh runtime's first independent venue-ledger snapshot. A
        # startup recovery must not replay the full deployment history into a
        # later reconciliation period; an absent boundary stays fail-closed.
        runtime_spec["reconciliation_coverage_started_at"] = (
            verified.command.issued_at if initial_reconciliation_backfill else None
        )
        await self._cancel_engine_supervision(verified.command.deployment_instance_id)
        ready = await self._engine_lifecycle.apply(
            delivery_id=delivery_id,
            verified=verified,
            runtime_spec=runtime_spec,
            credential=credential,
            artifact=activated,
            artifact_policy_id=artifact_policy_id,
        )
        self._start_engine_supervision(
            delivery_id=delivery_id,
            verified=verified,
            runtime_spec=runtime_spec,
            credential=credential,
            artifact=activated,
            artifact_policy_id=artifact_policy_id,
        )
        return prepared, activated, ready

    def _require_node_capacity(self, verified: VerifiedRunnerCommand) -> None:
        """Refuse before anything is resolved, activated or imported.

        A runner whose engine runs one node at a time cannot take a second
        instance while the first is held: the refusal is final for the command.
        A holder that is already being stopped frees the node shortly, so that
        command is retried. A new generation of the holding instance itself is
        the ordinary replacement path and proceeds.
        """

        capacity = self._node_capacity
        if capacity is None:
            return
        holder = capacity.node_holder()
        instance = str(verified.command.deployment_instance_id)
        if holder is None or holder.deployment_instance_id == instance:
            return
        _slog.warning(
            "runner_command_engine_node_held",
            deployment_instance_id=instance,
            generation=verified.command.generation,
            holder_deployment_instance_id=holder.deployment_instance_id,
            holder_releasing=holder.releasing,
        )
        if holder.releasing:
            raise RunnerEngineReleasing(holder.deployment_instance_id)
        raise RunnerEngineOccupied(holder.deployment_instance_id)

    def _start_engine_supervision(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        runtime_spec: dict[str, Any],
        credential: dict[str, Any],
        artifact: ActivatedStrategyArtifact | ActivatedDevelopmentStrategyArtifact,
        artifact_policy_id: str | None,
    ) -> None:
        instance_id = verified.command.deployment_instance_id
        task = asyncio.create_task(
            self._supervise_running_engine(
                delivery_id=delivery_id,
                verified=verified,
                runtime_spec=runtime_spec,
                credential=credential,
                artifact=artifact,
                artifact_policy_id=artifact_policy_id,
            ),
            name=(f"runner-engine-supervision:{instance_id}:{verified.command.generation}"),
        )
        self._engine_supervisions[instance_id] = task
        task.add_done_callback(
            lambda completed, instance=instance_id: self._on_engine_supervision_done(
                instance,
                completed,
            )
        )

    async def _supervise_running_engine(self, **context: Any) -> None:
        while True:
            recovered = await self._engine_lifecycle.supervise_once(**context)
            if recovered is None:
                return

    async def _cancel_engine_supervision(self, instance_id: object) -> None:
        task = self._engine_supervisions.pop(instance_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _on_engine_supervision_done(
        self,
        instance_id: object,
        task: asyncio.Task[None],
    ) -> None:
        if self._engine_supervisions.get(instance_id) is task:
            self._engine_supervisions.pop(instance_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        if isinstance(error, EngineLifecycleQuarantined):
            logger.warning("runner engine supervision reached durable quarantine")
            return
        self._engine_supervision_failures.put_nowait(error)

    async def run_engine_supervision(self, stop: asyncio.Event) -> None:
        failure = asyncio.create_task(self._engine_supervision_failures.get())
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {failure, stopped},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failure in done:
                raise failure.result()
        finally:
            failure.cancel()
            stopped.cancel()
            await asyncio.gather(failure, stopped, return_exceptions=True)
            tasks = tuple(self._engine_supervisions.values())
            self._engine_supervisions.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

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
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            done, pending = await asyncio.wait(
                {heartbeat_task},
                timeout=self._policy.in_progress_interval_seconds,
            )
            if heartbeat_task in done:
                try:
                    heartbeat_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as error:  # noqa: BLE001 - auxiliary lease failure is observed
                    logger.warning(
                        "runner command heartbeat ended after the main operation: %s",
                        type(error).__name__,
                    )
            elif pending:
                logger.warning("runner command heartbeat did not stop within its interval")
                heartbeat_task.add_done_callback(self._consume_heartbeat_result)

    @staticmethod
    def _consume_heartbeat_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return

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
        except RunnerFactContractError as error:
            # The refusal cannot be signed for this instance. Redelivering it
            # cannot change that before a restart and would hold every later
            # command behind it, so it waits durably like an unbound command.
            return await self._defer_awaiting_binding(
                delivery,
                intake,
                verified,
                str(error),
                refused_reason_code=reason_code,
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
                reason_code=_reason_code("durable_runtime_rejection_failed", error),
            )
        await delivery.term()
        return RunnerCommandRuntimeResult(
            status=RunnerCommandRuntimeStatus.TERMINAL_REJECTED,
            intake=intake,
            reason_code=reason_code,
        )

    async def _defer_awaiting_binding(
        self,
        delivery: InboundCommandDelivery,
        intake: CommandIntakeResult,
        verified: VerifiedRunnerCommand,
        binding_gap: str,
        *,
        refused_reason_code: str | None = None,
    ) -> RunnerCommandRuntimeResult:
        """Acknowledge a command this runner cannot yet sign for, and keep it.

        Intake has already recorded the command durably as the desired state,
        and nothing has been activated or started for it: an instance the
        capability does not bind was never started by this process. After a
        restart with a capability that binds it, startup recovery applies or
        refuses it and signs the outcome.
        """

        _slog.warning(
            "runner_command_awaiting_capability_binding",
            delivery_id=delivery.delivery_id,
            deployment_instance_id=str(verified.command.deployment_instance_id),
            generation=verified.command.generation,
            lifecycle_state=str(verified.command.lifecycle_state),
            binding_gap=binding_gap,
            refused_reason_code=refused_reason_code,
        )
        await delivery.ack()
        return RunnerCommandRuntimeResult(
            status=RunnerCommandRuntimeStatus.DEFERRED_AWAITING_BINDING,
            intake=intake,
            reason_code="awaiting_capability_binding",
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


class _FailureKind(StrEnum):
    # Final for this command: committed and signed as retry_exhausted.
    REFUSED = "refused"
    # Already committed and signed by the lifecycle supervisor.
    QUARANTINED = "quarantined"
    # A dependency that may become available: retried.
    RETRYABLE = "retryable"
    # Not anticipated: retried within the delivery budget, and logged loudly.
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class _Failure:
    kind: _FailureKind
    reason_code: str


def _reason_code(prefix: str, error: BaseException) -> str:
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    suffix = str(value or type(error).__name__).lower()
    return f"{prefix}:{suffix}"


def _classify_failure(error: Exception) -> _Failure:
    """The one classification of a failed start, for a command and a recovery alike.

    Only a defect of the artifact or a decision about the command is final; an
    environment that may change (a dependency, a node being released) is retried.
    """

    if isinstance(
        error,
        StrategyReleaseResolutionRejected
        | ArtifactVerificationError
        | DevelopmentSourceVerificationError,
    ):
        return _Failure(_FailureKind.REFUSED, _reason_code("artifact_authority_rejected", error))
    if isinstance(error, RunnerCredentialResolutionRejected | ArtifactRuntimeActivationError):
        return _Failure(_FailureKind.REFUSED, _reason_code("runtime_authority_rejected", error))
    if isinstance(error, RunnerEngineOccupied):
        return _Failure(_FailureKind.REFUSED, f"runtime_capacity_rejected:{error.reason_code}")
    if isinstance(error, RunnerEngineReleasing):
        return _Failure(_FailureKind.RETRYABLE, f"runtime_capacity_unavailable:{error.reason_code}")
    if isinstance(error, EngineLifecycleQuarantined):
        return _Failure(
            _FailureKind.QUARANTINED, _reason_code("engine_lifecycle_quarantined", error)
        )
    if isinstance(
        error,
        StrategyReleaseResolutionUnavailable
        | DevelopmentArtifactRuntimeBlocked
        | RunnerCredentialResolutionUnavailable
        | ArtifactRuntimeBlocked
        | EngineLifecycleBlocked,
    ):
        return _Failure(
            _FailureKind.RETRYABLE, _reason_code("runtime_dependency_unavailable", error)
        )
    return _Failure(_FailureKind.UNEXPECTED, _reason_code("runtime_apply_failed", error))


# Failures retried while attempts remain: a dependency that may become
# available, and anything not anticipated.
_MAY_PASS = frozenset({_FailureKind.RETRYABLE, _FailureKind.UNEXPECTED})


def _failure_detail(error: BaseException, failure: _Failure) -> dict[str, Any]:
    """What a log line needs to tell which dependency failed and how.

    The message is logged only for the dependency errors this runner raises
    itself, whose messages are fixed text with no credential or payload in
    them; the chain of causes is logged by type only, since a third-party
    message may carry a URL, a header or a response body.
    """

    causes: list[str] = []
    cause = error.__cause__ or error.__context__
    while cause is not None and len(causes) < 5:
        causes.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    detail: dict[str, Any] = {"error_type": type(error).__name__, "error_cause_types": causes}
    if failure.kind is _FailureKind.RETRYABLE:
        detail["error_message"] = str(error)[:200]
    return detail


def _recovery_delivery_id(verified: VerifiedRunnerCommand) -> str:
    return (
        f"startup-recovery:{verified.command.deployment_instance_id}:{verified.command.generation}"
    )


__all__ = [
    "RunnerEngineCapacityError",
    "RunnerEngineOccupied",
    "RunnerEngineReleasing",
    "RunnerCommandRuntimeCoordinator",
    "RunnerCommandRuntimeResult",
    "RunnerCommandRuntimeStatus",
    "RunnerCredentialResolutionError",
    "RunnerCredentialResolutionRejected",
    "RunnerCredentialResolutionUnavailable",
    "RunnerCredentialResolverV1",
]
