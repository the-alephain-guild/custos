"""Runner-local order notional reservation at the execution boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from custos.core.fallback_breaker import FallbackBreaker


class RunnerReservationStore(Protocol):
    def reserve_order_notional(self, **kwargs: Any) -> Any: ...

    def load_order_reservation(
        self,
        deployment_instance_id: UUID,
        client_order_id: str,
    ) -> Any: ...

    def replace_order_reservation(self, **kwargs: Any) -> Any: ...

    def release_order_reservation(self, **kwargs: Any) -> Any: ...

    def record_order_fill(self, **kwargs: Any) -> Any: ...

    def record_position_reduction(self, **kwargs: Any) -> Any: ...


class OrderSemantics(Protocol):
    def order_notional(self, order: Any) -> Decimal: ...

    def modified_order_notional(self, command: Any) -> Decimal: ...

    def fill_notional(self, event: Any) -> Decimal: ...

    def order_is_risk_reducing(self, order: Any) -> bool: ...

    def event_is_risk_reducing(self, event: Any) -> bool: ...


@dataclass(frozen=True)
class _Reservation:
    client_order_id: str


@dataclass(frozen=True)
class _Modification:
    client_order_id: str
    prior_reserved_notional: Decimal


class RunnerReservationBoundary:
    """Serialize order intent and execution facts through RunnerFact SQLite."""

    def __init__(
        self,
        *,
        store: RunnerReservationStore,
        deployment_instance_id: UUID,
        policy_id: UUID,
        fallback_breaker: FallbackBreaker,
        semantics: OrderSemantics | None = None,
    ) -> None:
        self._store = store
        self._deployment_instance_id = deployment_instance_id
        self._policy_id = policy_id
        self._fallback_breaker = fallback_breaker
        self._semantics = semantics
        self._pending_modifications: dict[str, _Modification] = {}

    def bind_runtime(self, *, semantics: OrderSemantics) -> None:
        if self._semantics is None:
            self._semantics = semantics

    def bootstrap(self, message_bus: Any) -> None:
        if message_bus is None:
            raise RuntimeError("execution MessageBus unavailable for runner safety bridge")
        message_bus.subscribe("events.order.*", self.on_order_event)

    def before_submit_order(self, command: Any) -> tuple[_Reservation, ...]:
        return self._reserve_orders((command.order,), command_id=command.command_id)

    def before_submit_order_list(self, command: Any) -> tuple[_Reservation, ...]:
        return self._reserve_orders(
            tuple(command.order_list.orders),
            command_id=command.command_id,
        )

    def before_modify_order(self, command: Any) -> _Modification:
        self._require_risk_increasing_allowed()
        semantics = self._require_semantics()
        client_order_id = str(command.client_order_id)
        prior = self._store.load_order_reservation(
            self._deployment_instance_id,
            client_order_id,
        )
        modification = _Modification(
            client_order_id=client_order_id,
            prior_reserved_notional=Decimal(prior.reserved_notional),
        )
        self._store.replace_order_reservation(
            event_id=self._event_id("modify", command.command_id, client_order_id),
            deployment_instance_id=self._deployment_instance_id,
            client_order_id=client_order_id,
            new_reserved_notional=semantics.modified_order_notional(command),
        )
        self._pending_modifications[client_order_id] = modification
        return modification

    def rollback_submit(
        self,
        reservations: tuple[_Reservation, ...],
        *,
        command_id: Any,
    ) -> None:
        for reservation in reservations:
            self._store.release_order_reservation(
                event_id=self._event_id(
                    "submit_dispatch_failed",
                    command_id,
                    reservation.client_order_id,
                ),
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=reservation.client_order_id,
                reason="rejected",
            )

    def rollback_modify(self, modification: _Modification, *, event_id: Any) -> None:
        self._store.replace_order_reservation(
            event_id=self._event_id(
                "modify_rejected",
                event_id,
                modification.client_order_id,
            ),
            deployment_instance_id=self._deployment_instance_id,
            client_order_id=modification.client_order_id,
            new_reserved_notional=modification.prior_reserved_notional,
        )
        self._pending_modifications.pop(modification.client_order_id, None)

    def on_order_event(self, event: Any) -> None:
        event_name = type(event).__name__
        data = self._event_data(event)
        client_order_id = str(
            data.get("client_order_id") or getattr(event, "client_order_id", "")
        ).strip()
        if not client_order_id:
            raise RuntimeError(f"{event_name} has no client_order_id")
        stable_event_id = (
            data.get("event_id") or data.get("trade_id") or getattr(event, "event_id", None)
        )
        if stable_event_id is None:
            raise RuntimeError(f"{event_name} has no stable event identity")

        if event_name == "OrderFilled":
            semantics = self._require_semantics()
            notional = semantics.fill_notional(event)
            if semantics.event_is_risk_reducing(event):
                self._store.record_position_reduction(
                    event_id=self._event_id("fill_reduce", stable_event_id, client_order_id),
                    deployment_instance_id=self._deployment_instance_id,
                    client_order_id=client_order_id,
                    reduction_notional=notional,
                )
            else:
                self._store.record_order_fill(
                    event_id=self._event_id("fill", stable_event_id, client_order_id),
                    deployment_instance_id=self._deployment_instance_id,
                    client_order_id=client_order_id,
                    fill_notional=notional,
                )
            return

        if event_name in {"OrderRejected", "OrderDenied"}:
            self._store.release_order_reservation(
                event_id=self._event_id("rejected", stable_event_id, client_order_id),
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=client_order_id,
                reason="rejected",
            )
            return

        if event_name in {"OrderCanceled", "OrderExpired"}:
            self._store.release_order_reservation(
                event_id=self._event_id("canceled", stable_event_id, client_order_id),
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=client_order_id,
                reason="canceled",
            )
            return

        if event_name == "OrderModifyRejected":
            modification = self._pending_modifications.get(client_order_id)
            if modification is not None:
                self.rollback_modify(modification, event_id=stable_event_id)
            return

        if event_name == "OrderUpdated":
            self._pending_modifications.pop(client_order_id, None)

    def _reserve_orders(
        self,
        orders: tuple[Any, ...],
        *,
        command_id: Any,
    ) -> tuple[_Reservation, ...]:
        semantics = self._require_semantics()
        reservations: list[_Reservation] = []
        try:
            for order in orders:
                if semantics.order_is_risk_reducing(order):
                    continue
                self._require_risk_increasing_allowed()
                client_order_id = str(order.client_order_id)
                self._store.reserve_order_notional(
                    event_id=self._event_id("submit", command_id, client_order_id),
                    deployment_instance_id=self._deployment_instance_id,
                    client_order_id=client_order_id,
                    policy_id=self._policy_id,
                    requested_notional=semantics.order_notional(order),
                )
                reservations.append(_Reservation(client_order_id=client_order_id))
        except Exception:
            self.rollback_submit(tuple(reservations), command_id=command_id)
            raise
        return tuple(reservations)

    def _require_risk_increasing_allowed(self) -> None:
        if not self._fallback_breaker.allows_new_orders():
            raise RuntimeError("runner fallback breaker is frozen")

    def _require_semantics(self) -> OrderSemantics:
        if self._semantics is None:
            raise RuntimeError("runner safety boundary has no execution semantics")
        return self._semantics

    def _event_id(self, action: str, source_id: Any, client_order_id: str) -> str:
        return (
            f"runner-order-reservation:{self._deployment_instance_id}:"
            f"{action}:{source_id}:{client_order_id}"
        )

    @staticmethod
    def _event_data(event: Any) -> dict[str, Any]:
        converter = getattr(type(event), "to_dict", None)
        if callable(converter):
            result = converter(event)
            if isinstance(result, dict):
                return result
        return {}
