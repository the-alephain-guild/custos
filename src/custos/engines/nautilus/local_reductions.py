"""Bind the local risk ledger to one Native cache and Papertrade execution route."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from nautilus_trader.model import ClientId, InstrumentId, OrderFilled

from custos.core.local_runtime_state import RuntimeLease
from custos.core.runtime_authority import RuntimeAuthorityError
from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics
from custos.local.reservations import LocalReservationStore


class LocalPapertradeReductions:
    def __init__(self, *, store: LocalReservationStore, account_id: str, client_id: str) -> None:
        if not isinstance(store, LocalReservationStore):
            raise RuntimeAuthorityError(
                "local reductions require the concrete registered risk store"
            )
        self._store = store
        self._claims = store.bind_reductions(account_id=account_id, client_id=client_id)
        self._account_id = account_id
        self._client_id = client_id
        self._semantics: NautilusCachedOrderSemantics | None = None

    @property
    def reservation_store(self) -> LocalReservationStore:
        return self._store

    @property
    def runtime_lease(self) -> RuntimeLease:
        return self._store.runtime_lease

    @property
    def runtime_id(self) -> UUID:
        return self._store.runtime_id

    @property
    def policy_id(self) -> UUID:
        return self._store.policy_id

    def bind_runtime(self, semantics: Any) -> None:
        if not isinstance(semantics, NautilusCachedOrderSemantics) or (
            self._semantics is not None
            and self._semantics.canonical_cache is not semantics.canonical_cache
        ):
            raise RuntimeAuthorityError("local reductions require the same canonical Native cache")
        self._semantics = semantics

    def _bound_semantics(self) -> NautilusCachedOrderSemantics:
        if self._semantics is None:
            raise RuntimeAuthorityError("local reductions have no bound Native cache")
        return self._semantics

    def prepare_submit(self, intent: Any) -> bool:
        semantics = self._bound_semantics()
        if intent.client_id is not None and (
            not isinstance(intent.client_id, ClientId) or str(intent.client_id) != self._client_id
        ):
            raise RuntimeAuthorityError("local submit selects another Native execution client")
        if intent.order.is_reduce_only is True:
            target = semantics.papertrade_reduction_target(
                intent, account_id=self._account_id, client_id=self._client_id
            )
            self._claims.prepare(
                closing_client_order_id=str(intent.order.client_order_id), target=target
            )
            return True
        if (
            intent.order.is_reduce_only is not False
            or intent.order.is_quote_quantity is not True
            or intent.position_id is not None
            or (intent.params is not None and "papertrade_position_id" in intent.params)
        ):
            raise RuntimeAuthorityError(
                "local opening requires quote quantity without a close target"
            )
        if not isinstance(intent.order.instrument_id, InstrumentId) or not str(
            intent.order.instrument_id
        ).endswith(".PAPERTRADE"):
            raise RuntimeAuthorityError("local opening requires a Native Papertrade instrument")
        self._store.require_opening_instrument(str(intent.order.instrument_id))
        return False

    def begin_dispatch(self, client_order_id: str) -> bool:
        return self._claims.begin_dispatch(client_order_id)

    def mark_not_sent(self, client_order_id: str) -> None:
        self._claims.mark_not_sent(client_order_id)

    def dispatch_failed(self, client_order_id: str) -> None:
        if self._claims.load(client_order_id) is not None:
            self._claims.mark_unknown(client_order_id)
        else:
            self._store.record_dispatch_unknown(
                client_order_id, reason="native_outcome_unconfirmed"
            )

    def judged_risk_reducing(self, client_order_id: str) -> bool | None:
        if self._claims.load(client_order_id) is not None:
            return True
        if self._store.has_order_reservation_sync(self.runtime_id, client_order_id):
            return False
        return None

    def on_order_event(self, event: Any, *, stable_event_id: str) -> None:
        client_order_id = str(event.client_order_id)
        event_name = type(event).__name__
        if event_name in {"OrderRejected", "OrderDenied", "OrderCanceled", "OrderExpired"}:
            # The generic manager can synthesize terminal events after a timeout.
            # Only adapter-held per-intent evidence can release this risk later.
            self.dispatch_failed(client_order_id)
            return
        if event_name != "OrderFilled":
            return
        if not isinstance(event, OrderFilled):
            self._store.record_dispatch_unknown(
                client_order_id, reason="native_fill_type_unverified"
            )
            return
        semantics = self._bound_semantics()
        reducing = self._claims.load(client_order_id) is not None
        event_id = (
            f"runner-order-reservation:{self.runtime_id}:"
            f"{'fill_reduce' if reducing else 'fill'}:{stable_event_id}:{client_order_id}"
        )
        if str(event.account_id) != self._account_id:
            self._store.retain_unapplied_fill(
                event_id=event_id,
                context=client_order_id,
                reducing=reducing,
                raw={
                    "native_account_id": str(event.account_id),
                    "position_id": str(event.position_id),
                },
            )
            return
        try:
            amount = semantics.fill_notional(event)
            quantity = semantics.fill_quantity(event)
            leaves = None if reducing else semantics.fill_leaves_quantity(event)
        except Exception:
            self._store.retain_unapplied_fill(
                event_id=event_id,
                context=client_order_id,
                reducing=reducing,
                raw=event.to_dict(),
            )
            return
        if reducing:
            self._store.record_claimed_reduction_sync(
                event_id=event_id,
                runtime_id=self.runtime_id,
                closing_client_order_id=client_order_id,
                position_id=str(event.position_id) if event.position_id is not None else "",
                instrument_id=str(event.instrument_id),
                side=str(event.order_side),
                reduction_notional=amount,
                reduction_quantity=quantity,
            )
        else:
            self._store.record_order_fill_sync(
                event_id=event_id,
                runtime_id=self.runtime_id,
                client_order_id=client_order_id,
                fill_notional=amount,
                fill_quantity=quantity,
                position_id=semantics.event_position_id(event),
                instrument_id=semantics.event_instrument_id(event),
                side=semantics.event_side(event),
                leaves_quantity=leaves,
            )
