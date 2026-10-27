"""Handoff one durable target delivery on its active host event loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.external_target_inbox import (
    ExternalTargetInbox,
    ExternalTargetInboxError,
)
from custos.engines.nautilus.host import ExternalSourceRuntimeScope


class ExternalTargetHost(Protocol):
    def external_source_runtime_scope(
        self, deployment_instance_id: str
    ) -> ExternalSourceRuntimeScope: ...

    def queue_external_source_signal(
        self, authority: EngineLifecycleAuthority, signal: object
    ) -> bool: ...

    async def request_external_source_flatten(
        self, authority: EngineLifecycleAuthority, *, risk_action: str | None = None
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TargetDispatchReceipt:
    delivery_id: str
    status: str


class ExternalTargetDispatcher:
    """The inbox claim precedes the host call; no handoff claims a venue result."""

    def __init__(self, inbox: ExternalTargetInbox, host: ExternalTargetHost) -> None:
        self.inbox = inbox
        self.host = host

    def _active_scope(self, authority: EngineLifecycleAuthority) -> ExternalSourceRuntimeScope:
        scope = self.host.external_source_runtime_scope(str(authority.deployment_instance_id))
        if scope.authority != authority:
            raise RuntimeError("external target authority differs from active deployment")
        return scope

    async def recover_on_startup(self, authority: EngineLifecycleAuthority, *, at: datetime) -> int:
        """Quarantine an interrupted handoff before any new delivery is claimed."""
        self._active_scope(authority)
        return await asyncio.to_thread(
            self.inbox.recover_uncertain, authority.deployment_instance_id, at=at
        )

    async def process_once(
        self, authority: EngineLifecycleAuthority, *, at: datetime
    ) -> TargetDispatchReceipt | None:
        self._active_scope(authority)
        pending = await asyncio.to_thread(self.inbox.pending, authority, at=at)
        if not pending:
            return None
        delivery = pending[0]
        claimed = await asyncio.to_thread(
            self.inbox.begin_delivery, delivery.delivery_id, authority=authority, at=at
        )
        if not claimed:
            return None
        try:
            if delivery.target == "FLAT":
                await self.host.request_external_source_flatten(
                    authority, risk_action=delivery.risk_action
                )
                status = "flat_requested"
            else:
                signal = delivery.to_signal()
                if (
                    signal is None
                    or self.host.queue_external_source_signal(authority, signal) is not True
                ):
                    raise RuntimeError("active strategy refused the external target signal")
                status = "queued"
            marked = await asyncio.to_thread(
                self.inbox.mark_handoff, delivery.delivery_id, authority=authority, at=at
            )
            if not marked:
                raise ExternalTargetInboxError("target handoff could not be persisted")
        except (Exception, asyncio.CancelledError):
            await asyncio.shield(
                asyncio.to_thread(
                    self.inbox.recover_uncertain, authority.deployment_instance_id, at=at
                )
            )
            raise
        return TargetDispatchReceipt(delivery.delivery_id, status)
