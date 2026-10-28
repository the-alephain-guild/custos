"""Revalidate one operator grant before broker intake and host handoff."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.external_source_transport import bind_signal_consumer
from custos.engines.nautilus.external_source_authorization import (
    AuthorizedSourceSubscription,
    ExternalSourceAuthorizationError,
    load_source_subscription,
)
from custos.engines.nautilus.external_source_subscriber import ExternalSourceSubscriber
from custos.engines.nautilus.external_target_dispatcher import (
    ExternalTargetDispatcher,
    ExternalTargetHost,
    TargetDispatchReceipt,
)
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from custos.engines.nautilus.host import ExternalSourceRuntimeScope


class ExternalSourceRunnerSession:
    """One active signed deployment, one separate source durable and inbox."""

    def __init__(
        self,
        *,
        authorization_path: Path,
        credentials_path: Path,
        tenant_id: str,
        runner_id: UUID,
        authority: EngineLifecycleAuthority,
        host: ExternalTargetHost,
        inbox: ExternalTargetInbox,
        broker: Any,
    ) -> None:
        self.authorization_path = authorization_path
        self.credentials_path = credentials_path
        self.tenant_id = tenant_id
        self.runner_id = runner_id
        self.authority = authority
        self.host = host
        self.inbox = inbox
        self.broker = broker
        self.dispatcher = ExternalTargetDispatcher(inbox, host)
        self.bound: AuthorizedSourceSubscription | None = None
        self.bound_scope: ExternalSourceRuntimeScope | None = None
        self.subscription: Any = None
        self.subscriber: ExternalSourceSubscriber | None = None

    def _revalidated_scope(self) -> ExternalSourceRuntimeScope:
        scope = self.host.external_source_runtime_scope(str(self.authority.deployment_instance_id))
        if scope.authority != self.authority:
            raise ExternalSourceAuthorizationError("source session differs from active deployment")
        if self.bound_scope is not None and scope != self.bound_scope:
            raise ExternalSourceAuthorizationError("source session differs from active venue scope")
        current = load_source_subscription(
            self.authorization_path,
            credentials_path=self.credentials_path,
            expected_tenant_id=self.tenant_id,
            expected_runner_id=self.runner_id,
            active_scope=scope,
        )
        if self.bound is not None and (
            current.spec.durable_name != self.bound.spec.durable_name
            or current.spec.filter_subject != self.bound.spec.filter_subject
            or current.spec.delivery_subject != self.bound.spec.delivery_subject
            or current.binding != self.bound.binding
            or current.credentials_path != self.bound.credentials_path
            or current.credentials_sha256 != self.bound.credentials_sha256
        ):
            raise ExternalSourceAuthorizationError("source authorization changed after binding")
        return scope

    async def bind(self, *, at: datetime) -> None:
        if self.subscription is not None:
            raise RuntimeError("source session is already bound")
        scope = self._revalidated_scope()
        grant = load_source_subscription(
            self.authorization_path,
            credentials_path=self.credentials_path,
            expected_tenant_id=self.tenant_id,
            expected_runner_id=self.runner_id,
            active_scope=scope,
        )
        await self.dispatcher.recover_on_startup(self.authority, at=at)
        subscription = await bind_signal_consumer(self.broker, grant.spec)
        self.bound = grant
        self.bound_scope = scope
        self.subscription = subscription
        self.subscriber = ExternalSourceSubscriber(
            grant.spec,
            grant.binding,
            self.inbox,
            scope_provider=self._revalidated_scope,
        )

    async def handle_message(self, message: Any, *, at: datetime) -> TargetDispatchReceipt | None:
        if self.subscriber is None:
            raise RuntimeError("source session has not bound a broker consumer")
        self._revalidated_scope()
        receipt = await self.subscriber.handle_message(message, at=at)
        if receipt is None:
            return None
        self._revalidated_scope()
        return await self.dispatcher.process_once(self.authority, at=at)

    async def run(
        self,
        stop: asyncio.Event,
        *,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float = 0.5,
    ) -> None:
        """Drain durable targets and consume one exact broker subscription."""
        if not 0 < poll_seconds <= 5:
            raise ValueError("source subscription poll interval is invalid")
        now = clock or (lambda: datetime.now(UTC))
        try:
            if self.subscription is None:
                await self.bind(at=now())
            while not stop.is_set():
                self._revalidated_scope()
                await self.dispatcher.process_once(self.authority, at=now())
                try:
                    message = await self.subscription.next_msg(timeout=poll_seconds)
                except TimeoutError:
                    continue
                if stop.is_set():
                    break
                await self.handle_message(message, at=now())
        finally:
            if self.subscription is not None:
                await self.subscription.unsubscribe()
                self.subscription = None
