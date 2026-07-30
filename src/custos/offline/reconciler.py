"""Reconcile offline desired state and report what was actually observed.

The engine host reads canonical runtime keys the offline spec does not carry, so
one translation happens here and only here: identity is derived deterministically
from the spec id, and the digest from the spec's own content. Deriving rather
than inventing means a restarted runner recognises the instance it was already
running.

Nothing here is authoritative. The status this publishes is what the operator's
own harness waits on, not a business fact, and it is never promotion evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID, uuid5

import uuid6

from custos.contracts import LifecycleState
from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.core.log import get_logger
from custos.offline.mode_guard import refuse_live
from custos.offline.safety import OfflineExposureGuard, resolve_breaker_config
from custos.offline.spec import (
    OfflineDeploymentMessage,
    OfflineDeploymentSpec,
    now_rfc3339_nanos,
    offline_subject,
)
from custos.offline.state import AppliedRecord

_log = get_logger("custos.offline.reconciler")

_IDENTITY_NAMESPACE: Final = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
_TERMINAL_STATES: Final = frozenset({LifecycleState.STOPPED, LifecycleState.ARCHIVED})
_POLL_SECS: Final = 0.5
_RETRY_SECS: Final = 5.0

PublishStatus = Callable[[str, bytes], Awaitable[None]]


class Settlement(StrEnum):
    """How a delivery was disposed of.

    The safety rule draws this line: invalid commands are terminally rejected and
    audited, while transient engine or delivery failures stay retryable. A message
    that cannot be parsed will not parse on redelivery either, so asking for it
    again turns one bad message into an endless one.
    """

    APPLIED = "applied"
    REJECTED = "rejected"
    RETRYABLE = "retryable"


class OfflineEngine(Protocol):
    """The slice of the engine host contract this lane needs."""

    async def deploy(self, spec: dict, credential: dict, artifact: Any) -> str: ...

    async def reconfigure(self, spec: dict) -> None: ...

    async def stop(self, deployment_instance_id: str) -> None: ...

    def attached(self, deployment_instance_id: str) -> bool:
        """Whether this engine is holding that instance right now.

        Attachment does not outlive the process that made it, so this is the only
        thing that can tell a running deployment apart from one a previous process
        left a record of.
        """

    def supports_trading_mode(self, mode: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class OfflineRuntimeIdentity:
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str


def runtime_identity(spec: OfflineDeploymentSpec) -> OfflineRuntimeIdentity:
    """Derive the runtime keys from the spec, so they survive a restart."""

    return OfflineRuntimeIdentity(
        deployment_instance_id=uuid5(_IDENTITY_NAMESPACE, f"instance:{spec.spec_id}"),
        deployment_spec_id=uuid5(_IDENTITY_NAMESPACE, f"spec:{spec.spec_id}"),
        deployment_spec_digest=_content_digest(spec),
    )


def runtime_spec(spec: OfflineDeploymentSpec, identity: OfflineRuntimeIdentity) -> dict[str, Any]:
    """Present the offline spec in the shape the engine host reads."""

    document = spec.model_dump(mode="json")
    document.update(
        {
            "deployment_instance_id": str(identity.deployment_instance_id),
            "deployment_spec_id": str(identity.deployment_spec_id),
            "deployment_spec_digest": identity.deployment_spec_digest,
        }
    )
    return document


class AppliedStore(Protocol):
    """Where applied generations survive a restart."""

    def load(self) -> dict[str, AppliedRecord]: ...

    def save(self, spec_id: str, record: AppliedRecord) -> None: ...


@dataclass
class _Applied:
    """What is remembered about a spec id, and how far each half of it travels.

    ``generation`` is meant to survive a restart; recording it is the reason the
    store exists. ``container_id`` is what the engine handed back when it took the
    deployment, so it describes an attachment — and an attachment does not outlive
    the process that made it. It is kept as provenance and never decides what to
    ask the engine to do; that question goes to ``OfflineEngine.attached``.
    """

    generation: int = 0
    container_id: str = ""


class OfflineReconciler:
    """Apply each generation once and report the generation actually reached."""

    def __init__(
        self,
        *,
        tenant_id: str,
        runner_label: str,
        strategy_id: str,
        engine: OfflineEngine,
        publish: PublishStatus,
        artifact_for: Callable[[OfflineDeploymentSpec], Any],
        credential_for: Callable[[OfflineDeploymentSpec], dict],
        applied_store: AppliedStore | None = None,
        guard: OfflineExposureGuard | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._runner_label = runner_label
        self._strategy_id = strategy_id
        self._engine = engine
        self._publish = publish
        self._artifact_for = artifact_for
        self._credential_for = credential_for
        self._store = applied_store
        self._guard = guard
        self._applied: dict[str, _Applied] = {
            spec_id: _Applied(generation=record.generation, container_id=record.container_id)
            for spec_id, record in (applied_store.load() if applied_store else {}).items()
        }

    async def run(self, subscription: Any, stop: asyncio.Event) -> None:
        """Consume desired state until asked to stop, outliving bad messages."""

        while not stop.is_set():
            try:
                message = await subscription.next_msg(timeout=_POLL_SECS)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a broken read must not end the loop
                _log.warning("offline_desired_state_read_failed", error=str(exc))
                await asyncio.sleep(_POLL_SECS)
                continue
            await self._settle(message, await self.handle(message.data))

    async def handle(self, data: bytes) -> Settlement:
        """Apply the desired state in ``data`` and say how it was disposed of."""

        try:
            message = OfflineDeploymentMessage.parse(data, expected_tenant_id=self._tenant_id)
        except Exception as exc:  # noqa: BLE001 - unreadable is terminal, not fatal to the loop
            _log.error("offline_desired_state_rejected", error=str(exc))
            return Settlement.REJECTED
        return await self.apply(message.spec)

    async def _settle(self, message: Any, settlement: Settlement) -> None:
        """Acknowledge what is finished; ask again only for what could succeed later."""

        if settlement is Settlement.RETRYABLE:
            negative = getattr(message, "nak", None)
            if negative is not None:
                await negative(delay=_RETRY_SECS)
                return
            _log.warning("offline_delivery_not_settled", settlement=settlement.value)
            return
        acknowledge = getattr(message, "ack", None)
        if acknowledge is not None:
            await acknowledge()
            return
        _log.warning("offline_delivery_not_settled", settlement=settlement.value)

    async def apply(self, spec: OfflineDeploymentSpec) -> Settlement:
        refuse_live(spec.trading_mode.value, source="deployment spec")
        if self._guard is not None and not self._guard.allows_new_generations():
            # Terminal: redelivering this generation will not unfreeze the breaker,
            # and admitting it would hand back the exposure the guard just flattened.
            _log.error(
                "offline_generation_refused_after_trip",
                spec_id=spec.spec_id,
                generation=spec.generation,
            )
            await self._report(spec, healthy=False)
            return Settlement.REJECTED

        try:
            limits = resolve_breaker_config(spec)
        except ValueError as exc:
            # Terminal: the same spec will not read any better on redelivery, and
            # starting it under ceilings nobody could read is the worse answer.
            _log.error(
                "offline_exposure_limits_rejected",
                spec_id=spec.spec_id,
                generation=spec.generation,
                error=str(exc),
            )
            await self._report(spec, healthy=False)
            return Settlement.REJECTED

        applied = self._applied.setdefault(spec.spec_id, _Applied())
        identity = runtime_identity(spec)

        if spec.generation < applied.generation:
            _log.warning(
                "offline_desired_state_stale",
                spec_id=spec.spec_id,
                generation=spec.generation,
                applied_generation=applied.generation,
            )
            return Settlement.APPLIED
        if spec.generation == applied.generation:
            if self._engine_is_where_the_spec_asks(spec, identity):
                await self._report(spec, healthy=True)
                return Settlement.APPLIED
            # The record says this generation was applied, and it was — by a
            # process that is gone. Reporting healthy here is worse than failing:
            # the consumer's wait-status passes over an engine running nothing.
            _log.info(
                "offline_applied_generation_reapplied",
                spec_id=spec.spec_id,
                generation=spec.generation,
            )

        if not self._engine.supports_trading_mode(spec.trading_mode.value):
            # Terminal: this engine will not grow the capability on redelivery.
            _log.error(
                "offline_engine_cannot_run_mode",
                spec_id=spec.spec_id,
                mode=spec.trading_mode.value,
            )
            await self._report(spec, healthy=False)
            return Settlement.REJECTED

        try:
            applied.container_id = await self._engage(spec, applied, identity)
        except Exception as exc:  # noqa: BLE001 - a failed apply is reported, then retried
            _log.error(
                "offline_reconcile_failed",
                spec_id=spec.spec_id,
                generation=spec.generation,
                error=str(exc),
            )
            await self._report(spec, healthy=False)
            return Settlement.RETRYABLE

        applied.generation = spec.generation
        self._remember(spec.spec_id, applied)
        self._update_guard(spec, identity, limits)
        await self._report(spec, healthy=True)
        return Settlement.APPLIED

    def _engine_is_where_the_spec_asks(
        self,
        spec: OfflineDeploymentSpec,
        identity: OfflineRuntimeIdentity,
    ) -> bool:
        """Whether an already-applied generation needs nothing done to it.

        A running spec asks for an attached engine; a stopped one asks for the
        absence of it. Either way the question goes to the engine, because the
        applied record survives a restart and the attachment it describes does not.
        """

        wants_an_engine = spec.lifecycle_state not in _TERMINAL_STATES
        attached = self._engine.attached(str(identity.deployment_instance_id))
        return bool(attached) == wants_an_engine

    def _update_guard(
        self,
        spec: OfflineDeploymentSpec,
        identity: OfflineRuntimeIdentity,
        limits: FallbackBreakerConfig,
    ) -> None:
        """Tell the guard what changed, once the engine is in the asked-for state.

        There is no await between the engine reaching that state and this call, so
        the tick cannot observe a running deployment nobody is watching.
        """

        if self._guard is None:
            return
        if spec.lifecycle_state in _TERMINAL_STATES:
            self._guard.release(spec.spec_id)
            return
        self._guard.watch(spec.spec_id, str(identity.deployment_instance_id), limits)

    def _remember(self, spec_id: str, applied: _Applied) -> None:
        if self._store is None:
            return
        try:
            self._store.save(
                spec_id,
                AppliedRecord(generation=applied.generation, container_id=applied.container_id),
            )
        except Exception as exc:  # noqa: BLE001 - forgetting is worse than not recording
            _log.warning("offline_applied_state_not_recorded", spec_id=spec_id, error=str(exc))

    async def _engage(
        self,
        spec: OfflineDeploymentSpec,
        applied: _Applied,
        identity: OfflineRuntimeIdentity,
    ) -> str:
        document = runtime_spec(spec, identity)
        deployment_instance_id = str(identity.deployment_instance_id)
        if spec.lifecycle_state in _TERMINAL_STATES:
            await self._engine.stop(deployment_instance_id)
            return ""
        if not self._engine.attached(deployment_instance_id):
            # Only an engine already holding this instance can be reconfigured.
            # Asking the engine rather than reading the recorded container id is
            # what keeps a restart, and a node that died under us, on the deploy
            # path instead of a structural reconfigure the host would refuse.
            return await self._engine.deploy(
                document,
                self._credential_for(spec),
                self._artifact_for(spec),
            )
        await self._engine.reconfigure(document)
        return applied.container_id

    async def _report(self, spec: OfflineDeploymentSpec, *, healthy: bool) -> None:
        """Publish observed state, and keep running if the channel is gone.

        Losing the status channel says nothing about whether the strategy should
        still be trading, so a failure here is logged and left at that.
        """

        payload = {
            "observed_generation": spec.generation,
            "phase": spec.lifecycle_state.value,
            "health": "healthy" if healthy else "unhealthy",
        }
        envelope = {
            "envelope_version": 1,
            "event_id": str(uuid6.uuid7()),
            "tenant_id": self._tenant_id,
            "occurred_at": now_rfc3339_nanos(),
            "payload_schema_version": 1,
            "payload": payload,
        }
        subject = offline_subject(
            self._tenant_id, "deployment_status", self._runner_label, spec.spec_id
        )
        try:
            await self._publish(subject, json.dumps(envelope, separators=(",", ":")).encode())
        except Exception as exc:  # noqa: BLE001 - reporting is not a condition of trading
            _log.warning(
                "offline_status_publish_failed",
                subject=subject,
                generation=spec.generation,
                error=str(exc),
            )


def _content_digest(spec: OfflineDeploymentSpec) -> str:
    canonical = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
