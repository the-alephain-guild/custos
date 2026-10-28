"""Require fresh independent venue state before releasing a target flat fence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
        self,
        inbox: ExternalTargetInbox,
        host: ExternalTargetHost,
        reader: VenueFlatReader | None,
        *,
        clock: Callable[[], datetime] | None = None,
        revalidate: Callable[[], object] | None = None,
    ) -> None:
        self.inbox = inbox
        self.host = host
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(UTC))
        self.revalidate = revalidate

    async def confirm_if_flat(
        self, delivery_id: str, *, authority: EngineLifecycleAuthority
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
        if self.reader is None and authority.trading_mode != "sandbox":
            return False
        if await self.host.external_source_flat_local_clear(authority, request.pair) is not True:
            return False
        observed = None
        if self.reader is None:
            # The simulated engine has no independent venue API. Give already
            # queued callbacks a turn before reading its local state again.
            await asyncio.sleep(0)
        else:
            observed = await self.reader.observe_flat(
                connector=request.connector, pair=request.pair
            )
        at = self.clock()
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise RuntimeError("flat confirmation clock must be UTC")
        if at < request.handoff_at:
            return False
        if observed is not None:
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
        if await self.host.external_source_flat_local_clear(authority, request.pair) is not True:
            return False
        if self.revalidate is not None:
            self.revalidate()
        return await asyncio.to_thread(
            self.inbox.confirm_flat, delivery_id, authority=authority, at=at
        )
