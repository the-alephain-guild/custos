"""Require fresh independent venue state before releasing a target flat fence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.external_target_dispatcher import ExternalTargetHost
from custos.engines.nautilus.external_target_flat_observation import VenueFlatObservation
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox


class VenueFlatReader(Protocol):
    async def observe_flat(self, *, connector: str, pair: str) -> VenueFlatObservation: ...


class ExternalTargetFlatVerifier:
    """The reader is a trusted venue adapter, never source-message input."""

    def __init__(
        self, inbox: ExternalTargetInbox, host: ExternalTargetHost, reader: VenueFlatReader
    ) -> None:
        self.inbox = inbox
        self.host = host
        self.reader = reader

    async def confirm_if_flat(
        self, delivery_id: str, *, authority: EngineLifecycleAuthority, at: datetime
    ) -> bool:
        scope = self.host.external_source_runtime_scope(str(authority.deployment_instance_id))
        if scope.authority != authority:
            raise RuntimeError("external target authority differs from active deployment")
        request = await asyncio.to_thread(
            self.inbox.pending_flat_request, delivery_id, authority=authority
        )
        if request is None:
            return False
        if request.connector != scope.connector or request.pair not in scope.allowed_pairs:
            raise RuntimeError("flat request differs from active venue scope")
        observed = await self.reader.observe_flat(connector=request.connector, pair=request.pair)
        if observed.observed_at.tzinfo is None:
            return False
        observed_at = observed.observed_at.astimezone(UTC)
        if (
            observed.source != "venue_api"
            or observed.connector != request.connector
            or observed.pair != request.pair
            or not observed.positions_complete
            or not observed.orders_complete
            or type(observed.open_positions) is not int
            or type(observed.open_orders) is not int
            or observed.open_positions != 0
            or observed.open_orders != 0
            or observed_at < request.handoff_at
            or not observed_at <= at <= observed_at + timedelta(seconds=30)
        ):
            return False
        current = self.host.external_source_runtime_scope(str(authority.deployment_instance_id))
        if current != scope:
            raise RuntimeError("active venue scope changed during flat observation")
        return await asyncio.to_thread(
            self.inbox.confirm_flat, delivery_id, authority=authority, at=at
        )
