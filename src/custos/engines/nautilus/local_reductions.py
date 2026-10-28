"""Bind the local risk ledger to one Native cache and Papertrade execution route."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import UUID

from nautilus_trader.model import ClientId, InstrumentId, OrderFilled

from custos.core.local_runtime_state import RuntimeLease
from custos.core.order_nonexecution import NativeDispatchBinding, NativeNoFillSettlement
from custos.core.runtime_authority import RuntimeAuthorityError
from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics
from custos.engines.nautilus.runtime_storage import PapertradeRuntimeStorageProbe
from custos.local.reservations import LocalReservationStore

if TYPE_CHECKING:
    from custos.engines.nautilus.local_risk_supervisor import PapertradeRiskSupervisor


class NativeParamsFactory(Protocol):
    def submission_params_keccak256(self, params: dict[str, Any] | None = None) -> str: ...


class LocalPapertradeReductions:
    def __init__(
        self,
        *,
        store: LocalReservationStore,
        account_id: str,
        client_id: str,
        storage_probe: PapertradeRuntimeStorageProbe,
        native_factory: NativeParamsFactory,
    ) -> None:
        if not isinstance(store, LocalReservationStore):
            raise RuntimeAuthorityError(
                "local reductions require the concrete registered risk store"
            )
        if not isinstance(storage_probe, PapertradeRuntimeStorageProbe) or (
            storage_probe.bindings.state != store.runtime_lease.state_store
            or storage_probe.bindings.journal != store.runtime_lease.journal_store
        ):
            raise RuntimeAuthorityError(
                "local reductions require the actual registered Native stores"
            )
        self._store = store
        with self.runtime_lease.control_action():
            storage_probe.require_route(account_id=account_id, client_id=client_id)
            self._params_digest = native_factory.submission_params_keccak256
            digest = self._params_digest(None)
            if type(digest) is not str or re.fullmatch(r"0x[0-9a-f]{64}", digest) is None:
                raise RuntimeAuthorityError(
                    "Native factory does not provide a valid parameter bridge"
                )
        self._claims = store.bind_reductions(account_id=account_id, client_id=client_id)
        self._storage = storage_probe
        self._dispatch = store.bind_native_dispatch(journal=storage_probe.bindings.journal)
        self._account_id = account_id
        self._client_id = client_id
        self._semantics: NautilusCachedOrderSemantics | None = None
        self._risk_supervisor: PapertradeRiskSupervisor | None = None

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
        self.runtime_lease.assert_control_current()
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

    def require_observation_source(
        self,
        *,
        storage_probe: PapertradeRuntimeStorageProbe,
        semantics: NautilusCachedOrderSemantics,
    ) -> tuple[str, str]:
        with self.runtime_lease.control_action():
            if storage_probe is not self._storage or semantics is not self._bound_semantics():
                raise RuntimeAuthorityError(
                    "risk observation requires the same original cache and held probe"
                )
            storage_probe.require_route(account_id=self._account_id, client_id=self._client_id)
            return self._account_id, self._client_id

    def bind_risk_supervisor(self, supervisor: PapertradeRiskSupervisor) -> None:
        from custos.engines.nautilus.local_risk_supervisor import PapertradeRiskSupervisor

        with self.runtime_lease.control_action():
            if (
                type(supervisor) is not PapertradeRiskSupervisor
                or supervisor.reservation_store is not self._store
                or (self._risk_supervisor is not None and supervisor is not self._risk_supervisor)
            ):
                raise RuntimeAuthorityError("local risk supervisor cannot be replaced")
            supervisor.require_port(self)
            self._risk_supervisor = supervisor

    def prepare_submit(self, intent: Any) -> bool:
        self.runtime_lease.assert_control_current()
        semantics = self._bound_semantics()
        if intent.client_id is not None and (
            not isinstance(intent.client_id, ClientId) or str(intent.client_id) != self._client_id
        ):
            raise RuntimeAuthorityError("local submit selects another Native execution client")
        intent.bind_client_id(ClientId(self._client_id))
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
        if self._risk_supervisor is not None:
            self._risk_supervisor.require_increase_ready()
        return False

    def record_prepared(self, intent: Any) -> None:
        identifier = str(intent.order.client_order_id)
        try:
            with self.runtime_lease.control_action():
                claim = self._claims.load(identifier)
                side = str(intent.order.side)
                if side not in {"BUY", "SELL"}:
                    raise RuntimeAuthorityError("Native dispatch order direction is invalid")
                binding = NativeDispatchBinding(
                    client_order_id=identifier,
                    native_account_id=self._account_id,
                    client_id=self._client_id,
                    instrument_id=str(intent.order.instrument_id),
                    side=cast(Literal["BUY", "SELL"], side),
                    quantity=intent.order.quantity.as_decimal(),
                    quote_quantity=intent.order.is_quote_quantity,
                    reduce_only=intent.order.is_reduce_only,
                    target=claim.target.position_id.removeprefix("PT-") if claim else None,
                    submission_params_keccak256=self._params_digest(intent.params),
                )
                previous = self._dispatch.load(identifier)
                if previous is None:
                    observed = self._storage.order_evidence(identifier)
                    if (
                        observed.account_id != self._account_id
                        or observed.client_id != self._client_id
                    ):
                        raise RuntimeAuthorityError(
                            "Native Missing observation names another execution route"
                        )
                    if observed.kind != "missing":
                        raise RuntimeAuthorityError(
                            "Native client order ID already exists in the original journal"
                        )
                    missing_revision = observed.journal.sequence
                else:
                    missing_revision = previous.missing_revision
                self._dispatch.prepare(binding=binding, missing_revision=missing_revision)
        except Exception:
            self._store.record_dispatch_unknown(
                identifier, reason="native_dispatch_binding_unverified"
            )
            raise

    def begin_dispatch(self, client_order_id: str) -> bool:
        if self._risk_supervisor is not None:
            marker = self._dispatch.load(client_order_id)
            if marker is None:
                raise RuntimeAuthorityError("risk refresh requires the original prepared marker")
            if not marker.binding.reduce_only:
                self._risk_supervisor.require_increase_ready()
        return self._dispatch.begin_dispatch(client_order_id)

    def mark_not_sent(self, client_order_id: str) -> None:
        self._dispatch.withdraw_unsent(client_order_id)

    def reconcile_no_fill(self, client_order_id: str) -> bool:
        try:
            with self.runtime_lease.control_action():
                observed = self._storage.order_evidence(client_order_id)
                if observed.kind != "proven_no_fill":
                    return False
                binding, proof = observed.binding, observed.proof
                if (
                    binding is None
                    or proof is None
                    or binding.action
                    != ("closePositions" if binding.reduce_only else "openPosition")
                ):
                    raise RuntimeAuthorityError(
                        "Native no-fill observation has no consistent intent"
                    )
                original = NativeDispatchBinding(
                    client_order_id=client_order_id,
                    native_account_id=observed.account_id,
                    client_id=observed.client_id,
                    instrument_id=binding.instrument_id,
                    side=binding.side,
                    quantity=binding.quantity,
                    quote_quantity=binding.quote_quantity,
                    reduce_only=binding.reduce_only,
                    target=binding.target,
                    submission_params_keccak256=binding.submission_params_keccak256,
                )
                digest = hashlib.sha256(
                    json.dumps(
                        {"schema_version": 1, **asdict(proof)},
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                self._dispatch.settle_no_fill(
                    NativeNoFillSettlement(
                        binding=original,
                        journal_id=observed.journal.store_id,
                        journal_revision=observed.journal.sequence,
                        sealed_at_revision=proof.sealed_at_revision,
                        intent_id=binding.intent_id,
                        command_keccak256=binding.command_keccak256,
                        proof_digest=digest,
                        source=proof.source,
                    )
                )
                return True
        except Exception:
            self._store.record_dispatch_unknown(
                client_order_id, reason="native_nonexecution_evidence_unverified"
            )
            raise

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
        self.runtime_lease.assert_control_current()
        client_order_id = str(event.client_order_id)
        event_name = type(event).__name__
        if event_name in {"OrderRejected", "OrderDenied", "OrderCanceled", "OrderExpired"}:
            if not self.reconcile_no_fill(client_order_id):
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
