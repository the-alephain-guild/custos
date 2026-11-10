"""Translate a platform instance into the neutral execution reservation port."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.platform_process_fence import PlatformProcessLease
from custos.core.runner_fact import RunnerStateAuthorityError


class RunnerOpeningGateClosed(RuntimeError):
    """An instance has no committed opening-release permission."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PlatformOpeningGate:
    """Enforce the signed lane's durable opening boundary, without an open API."""

    def __init__(self, store: Any, authority: EngineLifecycleAuthority) -> None:
        self._store = store
        self.authority = authority
        self._process_lease: PlatformProcessLease | None = None

    def bind_process_lease(self, lease: PlatformProcessLease) -> None:
        if self._process_lease is not None or lease.authority != self.authority:
            raise RunnerStateAuthorityError("opening gate differs from the platform process lease")
        self._process_lease = lease

    def assert_released(self) -> None:
        try:
            state = self._store.load_opening_gate_state_sync(self.authority)
        except Exception as error:
            raise RunnerOpeningGateClosed("opening_gate_unavailable") from error
        # No signed release protocol has been delivered in this foundation. Even
        # corrupt or future state strings cannot manufacture permission to trade.
        reason = (
            "awaiting_opening_release" if state == "awaiting_release" else "opening_gate_revoked"
        )
        raise RunnerOpeningGateClosed(reason)

    @contextmanager
    def control_action(self):
        with self._store.opening_control_action(self.authority) as connection:
            if self._process_lease is None:
                yield
            else:
                with self._process_lease.dispatch_control(connection):
                    yield

    def defer_refusal(self, callback: Callable[[], None]) -> bool:
        return self._store.defer_opening_control_callback(callback)

    async def current_observation_desired(self):
        current = await self._store.load_durable_desired_command(
            self.authority.deployment_instance_id
        )
        if (
            current.command.lifecycle_state not in {"running", "paused"}
            or EngineLifecycleAuthority.from_verified_command(current) != self.authority
        ):
            raise RunnerStateAuthorityError("risk observation authority is no longer current")
        return current

    async def current_running_desired(self):
        current = await self.current_observation_desired()
        if current.command.lifecycle_state != "running":
            raise RunnerStateAuthorityError("opening observation authority is no longer running")
        await asyncio.to_thread(self._store.load_opening_gate_state_sync, self.authority)
        return current


@dataclass(frozen=True)
class ExecutionReservationSnapshot:
    runtime_id: UUID
    client_order_id: str
    policy_id: UUID
    reserved_notional: Decimal
    filled_exposure: Decimal
    filled_quantity: Decimal
    state: str


@dataclass(frozen=True)
class ExecutionExposureSnapshot:
    policy_id: UUID
    open_exposure: Decimal
    reserved_notional: Decimal
    total_exposure: Decimal
    max_total_notional: Decimal
    within_policy: bool
    source_digest: str | None


class PlatformReservationPort(Protocol):
    def reserve_order_notional_sync(self, **kwargs: Any) -> Any: ...
    def load_order_reservation_sync(
        self, deployment_instance_id: UUID, client_order_id: str
    ) -> Any: ...
    def has_order_reservation_sync(
        self, deployment_instance_id: UUID, client_order_id: str
    ) -> bool: ...
    def replace_order_reservation_sync(self, **kwargs: Any) -> Any: ...
    def release_order_reservation_sync(self, **kwargs: Any) -> Any: ...
    def record_order_fill_sync(self, **kwargs: Any) -> Any: ...
    def record_position_reduction_sync(self, **kwargs: Any) -> Any: ...
    def record_position_reduction_fifo_sync(self, **kwargs: Any) -> Any: ...
    async def load_runner_exposure(self, policy_id: UUID) -> Any: ...


class PlatformReservationStore:
    def __init__(self, store: PlatformReservationPort, *, deployment_instance_id: UUID) -> None:
        if not isinstance(deployment_instance_id, UUID) or deployment_instance_id.int == 0:
            raise RunnerStateAuthorityError(
                "platform reservation binding requires an instance UUID"
            )
        self._store = store
        self._instance_id = deployment_instance_id

    def _instance(self, runtime_id: UUID) -> UUID:
        if runtime_id != self._instance_id:
            raise RunnerStateAuthorityError(
                "reservation runtime differs from bound platform instance"
            )
        return self._instance_id

    def _snapshot(self, snapshot: Any) -> ExecutionReservationSnapshot:
        if snapshot.deployment_instance_id != self._instance_id:
            raise RunnerStateAuthorityError(
                "reservation result differs from bound platform instance"
            )
        return ExecutionReservationSnapshot(
            self._instance_id,
            snapshot.client_order_id,
            snapshot.policy_id,
            snapshot.reserved_notional,
            snapshot.filled_exposure,
            snapshot.filled_quantity,
            snapshot.state,
        )

    def reserve_order_notional_sync(
        self, *, runtime_id: UUID, **fields: Any
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.reserve_order_notional_sync(
                deployment_instance_id=self._instance(runtime_id), **fields
            )
        )

    def load_order_reservation_sync(
        self, runtime_id: UUID, client_order_id: str
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.load_order_reservation_sync(self._instance(runtime_id), client_order_id)
        )

    def has_order_reservation_sync(self, runtime_id: UUID, client_order_id: str) -> bool:
        return self._store.has_order_reservation_sync(self._instance(runtime_id), client_order_id)

    def replace_order_reservation_sync(
        self, *, runtime_id: UUID, **fields: Any
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.replace_order_reservation_sync(
                deployment_instance_id=self._instance(runtime_id), **fields
            )
        )

    def release_order_reservation_sync(
        self, *, runtime_id: UUID, **fields: Any
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.release_order_reservation_sync(
                deployment_instance_id=self._instance(runtime_id), **fields
            )
        )

    def record_order_fill_sync(
        self, *, runtime_id: UUID, **fields: Any
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.record_order_fill_sync(
                deployment_instance_id=self._instance(runtime_id), **fields
            )
        )

    def record_position_reduction_sync(
        self, *, runtime_id: UUID, **fields: Any
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.record_position_reduction_sync(
                deployment_instance_id=self._instance(runtime_id), **fields
            )
        )

    def record_position_reduction_fifo_sync(
        self, *, runtime_id: UUID, **fields: Any
    ) -> ExecutionReservationSnapshot:
        return self._snapshot(
            self._store.record_position_reduction_fifo_sync(
                deployment_instance_id=self._instance(runtime_id), **fields
            )
        )

    async def load_runner_exposure(self, policy_id: UUID) -> ExecutionExposureSnapshot:
        snapshot = await self._store.load_runner_exposure(policy_id)
        return ExecutionExposureSnapshot(
            snapshot.policy_id,
            snapshot.open_exposure,
            snapshot.reserved_notional,
            snapshot.total_exposure,
            snapshot.max_total_notional,
            snapshot.within_policy,
            snapshot.source_digest,
        )
