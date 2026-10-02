"""Typed engine readiness, restart and terminal supervision over the RunnerFact store."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
from custos.core.engine_protocol import (
    ActivatedEngineArtifactV1,
    EngineDependencyUnavailable,
    EngineDeploymentRefused,
    EngineLifecycleAuthority,
    EngineReadyReceipt,
    EngineStopBoundary,
    EngineTerminalEvent,
    ExecutionEngineProtocol,
)
from custos.core.log import get_logger
from custos.core.runner_command_intake import VerifiedRunnerCommand
from custos.core.runner_fact import (
    CommandOutcomeCommitResult,
    EngineLifecycleDurableState,
    PendingStopReap,
)
from custos.core.runtime_log_fact import RuntimeLogFactError, RuntimeLogRedactor

log = get_logger("custos.engine-lifecycle")


class EngineLifecycleError(RuntimeError):
    """Base error for the corrected engine lifecycle adapter."""


class EngineLifecycleBlocked(EngineLifecycleError):
    """A required artifact or mode capability is not authorized."""


class EngineLifecycleQuarantined(EngineLifecycleError):
    """The durable restart budget is exhausted or a terminal event is final."""


@dataclass(frozen=True, slots=True)
class EngineLifecycleConfig:
    # Nautilus owns a 60 second connection deadline by default.  Custos must
    # observe the engine's typed ready/failed result rather than cancel its
    # startup first and misclassify an in-progress connection as a restart.
    readiness_timeout_secs: float = 90.0
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

    async def commit_stop_applied_and_enqueue_terminal(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        boundary: EngineStopBoundary,
        causes: frozenset[str],
    ) -> CommandOutcomeCommitResult: ...

    async def record_stop_pending_reap(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        stop_requested_at: datetime,
    ) -> None: ...

    async def list_pending_stop_reaps(self) -> tuple[PendingStopReap, ...]: ...


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
        # (instance, generation, fingerprint) -> a stop whose node already stopped
        # but whose commit failed. A redelivery commits it without stopping again.
        self._uncommitted_stops: dict[
            tuple[str, int, str], tuple[EngineStopBoundary, frozenset[str]]
        ] = {}
        # Background commits for stops whose node outlived its cancellation.
        self._reaps: set[asyncio.Task[None]] = set()
        # Instances whose node this process has started. A node lives and dies with
        # its process, so one that is absent at a stop and was never started here
        # ended when an earlier process did.
        self._started_here: set[str] = set()

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
        recovered_applied = (
            state.desired_status == "applied"
            and state.applied_generation == verified.command.generation
            and state.applied_command_fingerprint == verified.command_fingerprint
        )
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
            if (
                state.applied_generation is not None
                and state.applied_generation < verified.command.generation
            ):
                await self._store.record_in_progress_lease(
                    delivery_id=delivery_id,
                    verified=verified,
                    lease_until_ns=self._lease_deadline_ns(),
                )
                await self._engine.stop(str(authority.deployment_instance_id))
        if restart_count > self._config.restart_budget:
            await self._quarantine(
                delivery_id=delivery_id,
                verified=verified,
                reason_code="restart_budget_exhausted:process_recovery",
                artifact_activation_id=artifact_activation_id,
                artifact_policy_id=artifact_policy_id,
            )
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
        if lifecycle_state == "stopped":
            await self._apply_stop(delivery_id=delivery_id, verified=verified, authority=authority)
            return
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

    async def _apply_stop(
        self,
        *,
        delivery_id: str,
        verified: Any,
        authority: EngineLifecycleAuthority,
    ) -> None:
        """Apply a signed stop only once its node is known not to be running.

        Before the engine is stopped nothing durable is touched beyond the
        lifecycle read and the in-progress lease that every lifecycle operation
        already takes; the boundary capture, its persistence and the terminal fact
        all come after the local stop. A stop whose node outlives its cancellation
        commits nothing and returns, so the delivery is acknowledged rather than
        retried into exhaustion; its outcome is committed when the node is reaped,
        or after a restart by :meth:`recover_pending_stops`.
        """
        instance_id = str(authority.deployment_instance_id)
        key = (instance_id, verified.command.generation, verified.command_fingerprint)
        state = await self._store.load_engine_lifecycle_state(verified)
        if state.stop_reap_pending:
            log.info(
                "engine_stop_reap_pending_redelivery",
                deployment_instance_id=instance_id,
                generation=verified.command.generation,
            )
            return
        await self._store.record_in_progress_lease(
            delivery_id=delivery_id,
            verified=verified,
            lease_until_ns=self._lease_deadline_ns(),
        )
        remembered = self._uncommitted_stops.get(key)
        if remembered is not None:
            boundary, causes = remembered
        else:
            boundary = await self._stop_at_boundary(instance_id)
            causes = boundary.stop_causes()
            if not boundary.node_was_running and (
                state.in_progress_before
                or (state.last_applied_is_running and instance_id not in self._started_here)
            ):
                # Either an earlier attempt of this command took its lease and never
                # committed, or the instance was last applied running and this
                # process never started its node. With nothing running now, the node
                # ended with an earlier process, at a moment nobody observed.
                causes = frozenset({"process_exit_before_confirmation"})
        if not boundary.reaped:
            await self._store.record_stop_pending_reap(
                delivery_id=delivery_id,
                verified=verified,
                stop_requested_at=boundary.stop_requested_at,
            )
            log.error(
                "engine_stop_awaiting_reap",
                deployment_instance_id=instance_id,
                generation=verified.command.generation,
            )
            self._commit_when_reaped(delivery_id, verified, boundary)
            return
        try:
            await self._store.commit_stop_applied_and_enqueue_terminal(
                delivery_id=delivery_id,
                verified=verified,
                boundary=boundary,
                causes=causes,
            )
        except Exception:
            # The node is stopped and its capture is lost with this attempt; the
            # redelivery commits the stop without the valuation, never stops again.
            self._uncommitted_stops[key] = (
                replace(boundary, valuation=None, valuation_failure=None),
                causes | {"capture_persist_failed"},
            )
            raise
        self._uncommitted_stops.pop(key, None)

    async def _stop_at_boundary(self, instance_id: str) -> EngineStopBoundary:
        stop_at_boundary = getattr(self._engine, "stop_at_boundary", None)
        if callable(stop_at_boundary):
            return await stop_at_boundary(instance_id)
        # An engine that cannot report its stop boundary offers no valuation of it.
        requested_at = datetime.now(UTC)
        await self._engine.stop(instance_id)
        return EngineStopBoundary(
            stop_requested_at=requested_at,
            node_was_running=True,
            stopped_gracefully=True,
            run_task_failed=False,
            reaped=True,
            stop_effective_at=datetime.now(UTC),
            valuation=None,
            valuation_failure="valuation_unavailable",
        )

    def _commit_when_reaped(
        self, delivery_id: str, verified: Any, boundary: EngineStopBoundary
    ) -> None:
        reap = boundary.reap
        if reap is None:
            raise EngineLifecycleError("an unreaped stop has no reap to wait for")

        async def commit_after_reap() -> None:
            try:
                reaped_at = await reap
                await self._store.commit_stop_applied_and_enqueue_terminal(
                    delivery_id=delivery_id,
                    verified=verified,
                    boundary=replace(
                        boundary,
                        stopped_gracefully=False,
                        reaped=True,
                        reap=None,
                        stop_effective_at=reaped_at,
                        valuation=None,
                        valuation_failure=None,
                    ),
                    causes=frozenset({"stop_timeout"}),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the durable record recovers it on restart
                log.error(
                    "engine_stop_reap_commit_failed",
                    deployment_instance_id=str(verified.command.deployment_instance_id),
                    generation=verified.command.generation,
                    error_type=type(exc).__name__,
                )

        task = asyncio.create_task(
            commit_after_reap(),
            name=f"engine-stop-reap:{verified.command.deployment_instance_id}",
        )
        self._reaps.add(task)
        task.add_done_callback(self._reaps.discard)

    async def reaps_settled(self) -> None:
        """Wait for every stop that is committing after its node was reaped."""
        await asyncio.gather(*tuple(self._reaps), return_exceptions=True)

    async def recover_pending_stops(self) -> None:
        """Commit every stop whose node was still being reaped when the process ended.

        One node lives and dies with its runner process, so after a restart the
        node is known not to be running; when it stopped is not known. Nothing is
        stopped again and no shutdown policy runs.
        """
        for pending in await self._store.list_pending_stop_reaps():
            try:
                await self._store.commit_stop_applied_and_enqueue_terminal(
                    delivery_id=pending.delivery_id,
                    verified=pending.verified,
                    boundary=EngineStopBoundary(
                        stop_requested_at=pending.stop_requested_at,
                        node_was_running=False,
                        stopped_gracefully=False,
                        run_task_failed=False,
                        reaped=True,
                        stop_effective_at=None,
                    ),
                    causes=frozenset({"process_exit_before_confirmation"}),
                )
            except Exception as exc:  # noqa: BLE001 - one stop must not block the others
                log.error(
                    "engine_pending_stop_recovery_failed",
                    deployment_instance_id=str(pending.verified.command.deployment_instance_id),
                    generation=pending.verified.command.generation,
                    error_type=type(exc).__name__,
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
        state = await self._store.load_engine_lifecycle_state(verified)
        if not self._matches_ready_applied(state, verified):
            return None
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
            recovered_applied=True,
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
            self._started_here.add(str(authority.deployment_instance_id))
            try:
                handle = await self._engine.deploy(runtime_spec, credential, artifact)
                receipt = await self._await_ready(authority)
                self._require_ready_identity(receipt, authority)
                # Recording the engine is the last thing that can fail while it is
                # already running, so it belongs in the same cleanup scope. Outside it,
                # a failed commit left an engine nobody had a handle for: the retry's
                # deploy was refused as "already deployed", the stop below never ran
                # because that refusal returned no handle, and the record reached
                # quarantined while the engine kept trading.
                await self._commit_engine_ready(
                    delivery_id=delivery_id,
                    verified=verified,
                    handle=handle,
                    recovered_applied=recovered_applied,
                    artifact_activation_id=artifact_activation_id,
                    artifact_policy_id=artifact_policy_id,
                )
            except EngineDependencyUnavailable as exc:
                raise EngineLifecycleBlocked(str(exc)) from exc
            except EngineDeploymentRefused as exc:
                log.warning(
                    "engine_deployment_refused",
                    deployment_instance_id=str(authority.deployment_instance_id),
                    reason_code=exc.reason_code,
                    detail=str(exc),
                )
                if handle is not None:
                    await self._engine.stop(str(authority.deployment_instance_id))
                await self._quarantine(
                    delivery_id=delivery_id,
                    verified=verified,
                    reason_code=exc.reason_code,
                    artifact_activation_id=artifact_activation_id,
                    artifact_policy_id=artifact_policy_id,
                )
            except asyncio.CancelledError:
                # A newer command or a shutdown cancelled this start. Whatever it
                # deployed must not keep running without an owner; stopping by
                # instance also covers a deploy cancelled before it returned a
                # handle. A cancellation is not a failed attempt, so nothing is
                # recorded and the restart budget is untouched.
                await self._engine.stop(str(authority.deployment_instance_id))
                raise
            except Exception as exc:  # noqa: BLE001 - typed terminal mapping below
                try:
                    error = RuntimeLogRedactor(
                        (
                            credential.get("api_key", ""),
                            credential.get("api_secret", ""),
                        )
                    ).message(str(exc) or type(exc).__name__)
                except RuntimeLogFactError:
                    error = "<redacted: invalid runtime error message>"
                log.warning(
                    "engine_start_attempt_failed",
                    deployment_instance_id=str(authority.deployment_instance_id),
                    error_type=type(exc).__name__,
                    error=error,
                    restart_count=restart_count,
                )
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
            return receipt

    async def _commit_engine_ready(
        self,
        *,
        delivery_id: str,
        verified: Any,
        handle: str,
        recovered_applied: bool,
        artifact_activation_id: str,
        artifact_policy_id: str | None,
    ) -> None:
        """Record a started engine. Both exits are the same step, so both are guarded."""
        if recovered_applied:
            await self._store.commit_recovered_engine_ready(
                verified=verified,
                engine_handle=handle,
                observed_status="ready",
                artifact_activation_id=artifact_activation_id,
                artifact_policy_id=artifact_policy_id,
            )
            return
        await self._store.commit_applied_and_enqueue_lifecycle(
            delivery_id=delivery_id,
            verified=verified,
            engine_handle=handle,
            observed_status="ready",
            artifact_activation_id=artifact_activation_id,
            artifact_policy_id=artifact_policy_id,
        )

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
            # Every path here has stopped the engine or never started it, so the
            # instance is observed stopped whatever the command asked for.
            lifecycle_state="stopped",
            artifact_activation_id=artifact_activation_id,
            artifact_policy_id=artifact_policy_id,
        )
        raise EngineLifecycleQuarantined(reason_code)

    async def commit_refusal(
        self,
        *,
        delivery_id: str,
        verified: Any,
        reason_code: str,
    ) -> None:
        """Commit and sign a final refusal for a command the engine never started.

        Used by startup recovery, which has no delivery to terminate. Unlike a
        command's own refusal it may follow an earlier applied outcome of the same
        command, recorded before the runner restarted: that engine ended with the
        previous process, and this outcome says it was not started again.
        """

        await self._store.commit_verified_command_outcome_and_enqueue_fact(
            delivery_id=delivery_id,
            verified=verified,
            outcome="retry_exhausted",
            reason_code=reason_code,
            engine_handle=None,
            observed_status="quarantined",
            lifecycle_state="stopped",
            artifact_activation_id=None,
            artifact_policy_id=None,
        )

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
        if not connector or not self._engine.supports_venue(connector, mode):
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
                    "live execution lacks signed control-plane promotion evidence"
                )
        return authority

    @staticmethod
    def _matches_ready_applied(state: EngineLifecycleDurableState, verified: Any) -> bool:
        return (
            state.desired_status == "applied"
            and state.applied_generation == verified.command.generation
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
