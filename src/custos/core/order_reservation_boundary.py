"""Runner-local order notional reservation at the execution boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from custos.core.fallback_breaker import FallbackBreaker
from custos.core.log import get_logger

_log = get_logger("custos.order_reservation_boundary")


class RunnerReservationStore(Protocol):
    def reserve_order_notional_sync(self, **kwargs: Any) -> Any: ...

    def load_order_reservation_sync(
        self,
        deployment_instance_id: UUID,
        client_order_id: str,
    ) -> Any: ...

    def has_order_reservation_sync(
        self,
        deployment_instance_id: UUID,
        client_order_id: str,
    ) -> bool: ...

    def replace_order_reservation_sync(self, **kwargs: Any) -> Any: ...

    def release_order_reservation_sync(self, **kwargs: Any) -> Any: ...

    def record_order_fill_sync(self, **kwargs: Any) -> Any: ...

    def record_position_reduction_sync(self, **kwargs: Any) -> Any: ...

    def record_position_reduction_fifo_sync(self, **kwargs: Any) -> Any: ...

    async def load_runner_exposure(self, policy_id: UUID) -> Any: ...


class OrderSemantics(Protocol):
    def order_notional(self, order: Any) -> Decimal: ...

    def modified_order_notional(self, command: Any) -> Decimal: ...

    def fill_notional(self, event: Any) -> Decimal: ...

    def fill_quantity(self, event: Any) -> Decimal: ...

    def fill_leaves_quantity(self, event: Any) -> Decimal | None: ...

    def order_instrument_id(self, order: Any) -> str: ...

    def order_quantity(self, order: Any) -> Decimal: ...

    def order_is_risk_reducing(
        self, order: Any, already_reducing: Decimal = Decimal(0)
    ) -> bool: ...

    def event_is_risk_reducing(self, event: Any) -> bool: ...

    def event_exposure_source_order_id(self, event: Any) -> str | None: ...

    def event_position_id(self, event: Any) -> str | None: ...

    def event_instrument_id(self, event: Any) -> str | None: ...

    def event_side(self, event: Any) -> str | None: ...


class RunnerRiskIncreaseFrozenError(RuntimeError):
    """The local fallback breaker intentionally refused new exposure."""


def runner_command_id(command: Any) -> Any:
    """Read the public Nautilus command identity across supported test doubles.

    Nautilus documents the constructor argument as ``command_id`` but exposes the
    resulting identity as ``id``.  Keeping that ABI translation at this boundary
    prevents mocks with an invented ``command_id`` attribute from masking a live-only
    failure.
    """

    identity = getattr(command, "id", None)
    if identity is None:
        identity = getattr(command, "command_id", None)
    if identity is None:
        raise RuntimeError("runner execution command has no public identity")
    return identity


@dataclass(frozen=True)
class _Reservation:
    client_order_id: str


@dataclass(frozen=True)
class _Modification:
    client_order_id: str
    prior_reserved_notional: Decimal | None


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
        # Reduce-only orders deliberately bypass notional reservation so a frozen or
        # temporarily unavailable policy store can never block risk reduction. Keep
        # their local ownership separately, otherwise account-wide venue events from a
        # sibling deployment are indistinguishable from this instance's protection.
        self._risk_reducing_order_ids: set[str] = set()
        # client_order_id -> (instrument_id, quantity still unsettled). A plain
        # close is only "reducing" against the room left after the closes already
        # accepted, so that room has to be tracked until they settle.
        self._unsettled_reductions: dict[str, tuple[str, Decimal]] = {}

    def bind_runtime(self, *, semantics: OrderSemantics) -> None:
        if self._semantics is None:
            self._semantics = semantics

    @property
    def fallback_breaker(self) -> FallbackBreaker:
        return self._fallback_breaker

    def bootstrap(self, forwarder: Any) -> None:
        """Register with the host's event forwarder.

        Nautilus 2.0 has no python surface on the internal message bus, so the
        wildcard subscription this used to hold is now a sink the host runs from the
        strategy's typed order callback.
        """
        if forwarder is None:
            raise RuntimeError("execution event forwarder unavailable for runner safety bridge")
        forwarder.add_order_sink("order_reservations", self.on_order_event)

    async def exposure_snapshot(self) -> Any:
        """Return the durable policy-scoped reservation/exposure aggregate."""

        return await self._store.load_runner_exposure(self._policy_id)

    def _unsettled_reduction_for(self, instrument_id: str) -> Decimal:
        """How much of this instrument's position accepted closes already claim."""
        return sum(
            (
                quantity
                for claimed_instrument, quantity in self._unsettled_reductions.values()
                if claimed_instrument == instrument_id
            ),
            Decimal(0),
        )

    def _settle_reduction(self, client_order_id: str, filled_quantity: Decimal) -> None:
        """Give back the room a fill has now actually used."""
        claim = self._unsettled_reductions.get(client_order_id)
        if claim is None:
            return
        instrument_id, outstanding = claim
        remaining = outstanding - filled_quantity
        if remaining > 0:
            self._unsettled_reductions[client_order_id] = (instrument_id, remaining)
        else:
            self._unsettled_reductions.pop(client_order_id, None)

    def before_submit_order(self, command: Any) -> tuple[_Reservation, ...]:
        return self._reserve_orders((command.order,), command_id=runner_command_id(command))

    def before_submit_order_list(self, command: Any) -> tuple[_Reservation, ...]:
        return self._reserve_orders(
            tuple(command.order_list.orders),
            command_id=runner_command_id(command),
        )

    def before_modify_order(self, command: Any) -> _Modification:
        semantics = self._require_semantics()
        client_order_id = str(command.client_order_id)
        # The risk semantics of the order being modified comes from the canonical
        # cache, not from the caller: 2.0 identifies it by id and hands over no
        # object, and the engine's view of what is resting is the one that counts.
        order = self.cached_order(command.client_order_id)
        if order is not None and semantics.order_is_risk_reducing(order):
            return _Modification(
                client_order_id=client_order_id,
                prior_reserved_notional=None,
            )
        self._require_risk_increasing_allowed()
        prior = self._store.load_order_reservation_sync(
            self._deployment_instance_id,
            client_order_id,
        )
        modification = _Modification(
            client_order_id=client_order_id,
            prior_reserved_notional=Decimal(prior.reserved_notional),
        )
        self._store.replace_order_reservation_sync(
            event_id=self._event_id("modify", runner_command_id(command), client_order_id),
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
            self._store.release_order_reservation_sync(
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
        if modification.prior_reserved_notional is None:
            return
        self._store.replace_order_reservation_sync(
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
            risk_reducing = (
                client_order_id in self._risk_reducing_order_ids
                or semantics.event_is_risk_reducing(event)
            )
            source_order_id = (
                semantics.event_exposure_source_order_id(event) if risk_reducing else None
            )
            position_id = semantics.event_position_id(event)
            if risk_reducing:
                if client_order_id not in self._risk_reducing_order_ids and (
                    source_order_id is None or not self._has_reservation(source_order_id)
                ):
                    return
            elif not self._has_reservation(client_order_id):
                return
            notional = semantics.fill_notional(event)
            quantity = semantics.fill_quantity(event)
            if risk_reducing:
                self._settle_reduction(client_order_id, quantity)
            try:
                if risk_reducing:
                    if position_id is not None:
                        self._store.record_position_reduction_fifo_sync(
                            event_id=self._event_id("fill_reduce", stable_event_id, position_id),
                            deployment_instance_id=self._deployment_instance_id,
                            position_id=position_id,
                            reduction_notional=notional,
                            reduction_quantity=quantity,
                        )
                    else:
                        if source_order_id is None or not self._has_reservation(source_order_id):
                            raise RuntimeError(
                                "risk-reducing fill has no durable opening-order exposure"
                            )
                        self._store.record_position_reduction_sync(
                            event_id=self._event_id(
                                "fill_reduce", stable_event_id, source_order_id
                            ),
                            deployment_instance_id=self._deployment_instance_id,
                            client_order_id=source_order_id,
                            reduction_notional=notional,
                            reduction_quantity=quantity,
                        )
                else:
                    self._store.record_order_fill_sync(
                        event_id=self._event_id("fill", stable_event_id, client_order_id),
                        deployment_instance_id=self._deployment_instance_id,
                        client_order_id=client_order_id,
                        fill_notional=notional,
                        fill_quantity=quantity,
                        position_id=position_id,
                        instrument_id=semantics.event_instrument_id(event),
                        side=semantics.event_side(event),
                        leaves_quantity=semantics.fill_leaves_quantity(event),
                    )
            except Exception as exc:
                # An exchange fill is already authoritative and cannot be rejected after
                # the fact. Never let an accounting mismatch tear down the execution
                # loop (which would also prevent protective reduce-only orders). Freeze
                # new risk and let the live portfolio rebuild reconcile conservatively.
                self._fallback_breaker.fail_closed("runner_order_fill_accounting_failed")
                _log.error(
                    "runner_order_fill_accounting_failed",
                    deployment_instance_id=str(self._deployment_instance_id),
                    event_kind=event_name,
                    client_order_id=client_order_id,
                    error_type=type(exc).__name__,
                )
            return

        if event_name in {"OrderRejected", "OrderDenied"}:
            if client_order_id in self._risk_reducing_order_ids:
                self._risk_reducing_order_ids.discard(client_order_id)
                self._unsettled_reductions.pop(client_order_id, None)
                return
            if not self._has_reservation(client_order_id):
                return
            self._store.release_order_reservation_sync(
                event_id=self._event_id("rejected", stable_event_id, client_order_id),
                deployment_instance_id=self._deployment_instance_id,
                client_order_id=client_order_id,
                reason="rejected",
            )
            return

        if event_name in {"OrderCanceled", "OrderExpired"}:
            if client_order_id in self._risk_reducing_order_ids:
                self._risk_reducing_order_ids.discard(client_order_id)
                self._unsettled_reductions.pop(client_order_id, None)
                return
            if not self._has_reservation(client_order_id):
                return
            self._store.release_order_reservation_sync(
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
                instrument_id = semantics.order_instrument_id(order)
                if semantics.order_is_risk_reducing(
                    order, self._unsettled_reduction_for(instrument_id)
                ):
                    client_order_id = str(order.client_order_id)
                    self._risk_reducing_order_ids.add(client_order_id)
                    self._unsettled_reductions[client_order_id] = (
                        instrument_id,
                        semantics.order_quantity(order),
                    )
                    continue
                self._require_risk_increasing_allowed()
                client_order_id = str(order.client_order_id)
                self._store.reserve_order_notional_sync(
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
            raise RunnerRiskIncreaseFrozenError("runner fallback breaker is frozen")

    def _has_reservation(self, client_order_id: str) -> bool:
        return self._store.has_order_reservation_sync(
            self._deployment_instance_id,
            client_order_id,
        )

    @property
    def policy_id(self) -> UUID:
        """The signed policy this boundary currently reserves under."""
        return self._policy_id

    def adopt_policy(self, policy_id: UUID, breaker_config: Any) -> None:
        """Switch to a renewed signed policy without interrupting the deployment.

        Both halves move together. Reserving requires the boundary's policy to be the
        durable head, so a boundary left on the previous revision refuses every new
        order -- while a boundary that took the new id but kept the old ceilings would
        enforce limits nobody signed.

        What is not touched is the guarding: the breaker's freeze and its high-water
        mark survive, because a renewal raises or lowers ceilings and is not a way to
        clear a trip. The risk scope is tenant + mode + runner rather than the policy
        id, so exposure and latches recorded under the previous revision stay in view.
        """
        self._policy_id = policy_id
        self._fallback_breaker.apply_config(breaker_config)

    def cached_order(self, client_order_id: Any) -> Any:
        """What the canonical cache holds for this id, or ``None``.

        The gate needs it to name the instrument and side of a refused modification:
        2.0 identifies an order to modify by id, so there is no object to read those
        from, and the engine's view is the one that matters anyway.
        """
        reader = getattr(self._require_semantics(), "cached_order", None)
        return None if reader is None else reader(client_order_id)

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
