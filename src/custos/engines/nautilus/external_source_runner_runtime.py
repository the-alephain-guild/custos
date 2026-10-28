"""Optional source subscriber lifecycle beside, never inside, the signed lane."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from custos.core.external_source_transport import ExternalSourceTransportError
from custos.engines.nautilus.external_source_authorization import (
    ExternalSourceAuthorizationError,
    load_source_subscription,
)
from custos.engines.nautilus.external_source_nats import ExternalSourceNatsProfile
from custos.engines.nautilus.external_source_runner_session import ExternalSourceRunnerSession
from custos.engines.nautilus.external_target_dispatcher import ExternalTargetHost
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox


@dataclass(frozen=True, slots=True)
class SourceRunnerOptions:
    instance_id: UUID
    authorization_path: Path
    credentials_path: Path
    inbox_path: Path
    nats_url: str
    nats_ca: Path
    nats_server_name: str


class ExternalSourceRunnerRuntime:
    """Suspend source intake on faults while the signed host keeps its positions."""

    def __init__(
        self,
        *,
        options: SourceRunnerOptions,
        tenant_id: str,
        runner_id: UUID,
        host: ExternalTargetHost,
        report: Callable[[str, str | None], None],
        clock: Callable[[], datetime] | None = None,
        retry_seconds: float = 1.0,
    ) -> None:
        if not 0 < retry_seconds <= 30:
            raise ValueError("source subscription retry interval is invalid")
        self.options = options
        self.tenant_id = tenant_id
        self.runner_id = runner_id
        self.host = host
        self.report = report
        self.clock = clock or (lambda: datetime.now(UTC))
        self.retry_seconds = retry_seconds
        self._last_status: tuple[str, str | None] | None = None

    def _status(self, state: str, reason: str | None = None) -> None:
        current = (state, reason)
        if current != self._last_status:
            self.report(state, reason)
            self._last_status = current

    async def _retry_or_stop(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.retry_seconds)
        except TimeoutError:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                scope = self.host.external_source_runtime_scope(str(self.options.instance_id))
            except RuntimeError:
                self._status("waiting", "deployment_inactive")
                await self._retry_or_stop(stop)
                continue

            connection = None
            session_task: asyncio.Task | None = None
            lost_task: asyncio.Task | None = None
            try:
                grant = load_source_subscription(
                    self.options.authorization_path,
                    credentials_path=self.options.credentials_path,
                    expected_tenant_id=self.tenant_id,
                    expected_runner_id=self.runner_id,
                    active_scope=scope,
                )
                lost = asyncio.Event()

                async def disconnected(signal: asyncio.Event = lost) -> None:
                    signal.set()

                profile = ExternalSourceNatsProfile(
                    grant=grant,
                    url=self.options.nats_url,
                    ca_path=self.options.nats_ca,
                    server_name=self.options.nats_server_name,
                )
                connection = await profile.connect(disconnected_cb=disconnected)
                session = ExternalSourceRunnerSession(
                    authorization_path=self.options.authorization_path,
                    credentials_path=self.options.credentials_path,
                    tenant_id=self.tenant_id,
                    runner_id=self.runner_id,
                    authority=scope.authority,
                    host=self.host,
                    inbox=ExternalTargetInbox(self.options.inbox_path),
                    broker=connection.jetstream(),
                )
                await session.bind(at=self.clock())
                self._status("active")
                session_task = asyncio.create_task(session.run(stop, clock=self.clock))
                lost_task = asyncio.create_task(lost.wait())
                done, _ = await asyncio.wait(
                    {session_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if lost_task in done and not stop.is_set():
                    raise ExternalSourceTransportError("source broker connection was lost")
                await session_task
                if not stop.is_set():
                    raise RuntimeError("source subscription ended unexpectedly")
            except ExternalSourceAuthorizationError:
                self._status("fault", "authorization_invalid")
            except ExternalSourceTransportError:
                self._status("fault", "broker_binding_invalid")
            except Exception:  # noqa: BLE001 — report without leaking credential or payload text
                self._status("fault", "source_unavailable")
            finally:
                if session_task is not None and not session_task.done():
                    session_task.cancel()
                    await asyncio.gather(session_task, return_exceptions=True)
                if lost_task is not None:
                    lost_task.cancel()
                    await asyncio.gather(lost_task, return_exceptions=True)
                if connection is not None:
                    try:
                        await asyncio.wait_for(connection.close(), timeout=2)
                    except Exception:  # noqa: BLE001 — source fault must not stop signed host
                        self._status("fault", "source_unavailable")
            if not stop.is_set():
                await self._retry_or_stop(stop)
