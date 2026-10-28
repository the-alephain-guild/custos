"""Collect source-coherent cash inputs without granting host or order admission."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Literal

from custos.core.local_runtime_state import RuntimeLease
from custos.core.loss_math import adverse_collateral_pnl
from custos.core.order_nonexecution import NativeDispatchSnapshot
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from custos.engines.nautilus.account_observation import NativeAccountObservation
from custos.engines.nautilus.local_reductions import LocalPapertradeReductions
from custos.engines.nautilus.local_risk_cache import (
    CacheRiskFrame,
    CacheRiskMarket,
    CacheRiskUnavailable,
    cache_risk_frame,
)
from custos.engines.nautilus.order_evidence import NativeOrderEvidence
from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics
from custos.engines.nautilus.runtime_storage import PapertradeRuntimeStorageProbe
from custos.local.loss_state import CashRiskObservation, uint
from custos.local.risk_cohort import LocalRiskCohort

FaultReason = Literal["account", "marks", "cohort"]


@dataclass(frozen=True)
class PapertradeRiskSample:
    observation: CashRiskObservation
    fault: FaultReason | None


def _stamp(source: NativeAccountObservation) -> tuple[object, ...]:
    return (
        str(source.journal.store_id),
        source.checkpoint_revision,
        source.account_snapshot_keccak256,
        source.account_material_keccak256,
        str(source.source_epoch),
        source.source_timestamp * 10**9,
    )


def _same_account(first: NativeAccountObservation, second: NativeAccountObservation) -> bool:
    return (
        replace(
            second,
            journal=first.journal,
            pending_client_order_ids=first.pending_client_order_ids,
            account_source_healthy=first.account_source_healthy,
        )
        == first
    )


def _binding_matches(native: NativeOrderEvidence, local: NativeDispatchSnapshot) -> bool:
    value = native.binding
    binding = local.binding
    return (
        value is not None
        and native.journal.store_id == local.journal_id
        and native.journal.sequence > local.missing_revision
        and native.account_id == binding.native_account_id
        and native.client_id == binding.client_id
        and native.client_order_id == binding.client_order_id
        and value.instrument_id == binding.instrument_id
        and value.side == binding.side
        and value.quantity == binding.quantity
        and value.quote_quantity == binding.quote_quantity
        and value.reduce_only == binding.reduce_only
        and value.target == binding.target
        and value.submission_params_keccak256 == binding.submission_params_keccak256
    )


class PapertradeRiskSource:
    def __init__(
        self,
        *,
        port: LocalPapertradeReductions,
        storage_probe: PapertradeRuntimeStorageProbe,
        semantics: NautilusCachedOrderSemantics,
        minimum_quote_init_ns: int,
    ) -> None:
        if (
            not isinstance(port, LocalPapertradeReductions)
            or not isinstance(storage_probe, PapertradeRuntimeStorageProbe)
            or not isinstance(semantics, NautilusCachedOrderSemantics)
        ):
            raise RuntimeAuthorityError(
                "risk collection requires the concrete original Native bindings"
            )
        self._port, self._storage, self._semantics = port, storage_probe, semantics
        self._cache = semantics.canonical_cache
        self._minimum_quote_init_ns = uint(minimum_quote_init_ns, minimum=1)
        self._account_id, self._client_id = port.require_observation_source(
            storage_probe=storage_probe, semantics=semantics
        )
        self._authorization = port.reservation_store.authorization
        self._instruments = self._authorization.manifest.instrument_ids

    @property
    def runtime_lease(self) -> RuntimeLease:
        return self._port.runtime_lease

    def _cache_frame(self) -> CacheRiskFrame:
        return cache_risk_frame(
            self._cache, account_id=self._account_id, instrument_ids=self._instruments
        )

    def _costs(
        self, source: NativeAccountObservation, frame: CacheRiskFrame, cohort: LocalRiskCohort
    ) -> tuple[bool, set[str]]:
        ready = source.reconciliation_complete and frame.account_stamp == _stamp(source)
        pending: set[str] = set()
        positions = {row.position_id: row for row in source.positions}
        cached = {row.position_id: row for row in frame.positions}
        if set(positions) != set(cached):
            ready = False
        owners: dict[str, object] = {}
        for order in cohort.orders:
            reservation, marker = order.reservation, order.native_dispatch
            identifier = reservation.client_order_id
            evidence = self._storage.order_evidence(identifier)
            if marker is None:
                pending.add(identifier)
                continue
            if (
                marker.state == "prepared"
                and marker.process_generation == self.runtime_lease.fence.process_generation
                and evidence.kind == "missing"
                and reservation.filled_exposure == 0
            ):
                continue
            if (
                marker.state != "dispatched"
                or evidence.kind != "filled"
                or not _binding_matches(evidence, marker)
                or reservation.reserved_notional
            ):
                pending.add(identifier)
                continue
            if order.position_id is None or order.position_id in owners:
                ready = False
                continue
            owners[order.position_id] = order
            position, cache = positions.get(order.position_id), cached.get(order.position_id)
            if position is None or cache is None:
                ready = False
                continue
            market = next(
                (row for row in frame.markets if row.instrument_id == position.instrument_id), None
            )
            if market is None:
                ready = False
                continue
            scale = 10**market.size_precision
            base = Fraction(
                position.margin_raw * position.leverage * position.price_scale_raw,
                10**18 * position.entry_price_raw,
            )
            expected_quantity = Decimal(
                f"{base.numerator * scale // base.denominator}e-{market.size_precision}"
            )
            expected_notional = Fraction(position.margin_raw * position.leverage, 10**18)
            if (
                evidence.binding is None
                or position.opening_intent_id != evidence.binding.intent_id
                or position.instrument_id != order.instrument_id
                or cache.instrument_id != position.instrument_id
                or order.side != ("BUY" if position.is_long else "SELL")
                or cache.is_long != position.is_long
                or cache.quantity != reservation.filled_quantity
                or cache.quantity != expected_quantity
                or Fraction(reservation.filled_exposure) != expected_notional
                or identifier not in cache.opening_client_ids
                or position.price_scale_raw != 10**market.price_precision
                or position.leverage != self._authorization.manifest.limits.leverage
            ):
                ready = False
        if set(owners) != set(positions):
            ready = False
        for closing in cohort.closings:
            identifier, marker = closing.claim.closing_client_order_id, closing.native_dispatch
            evidence = self._storage.order_evidence(identifier)
            if (
                marker is not None
                and marker.state == "prepared"
                and marker.process_generation == self.runtime_lease.fence.process_generation
                and closing.claim.state == "prepared"
                and evidence.kind == "missing"
            ):
                continue
            pending.add(identifier)
        return ready and not pending, pending

    def sample(self, *, now_ns: int) -> PapertradeRiskSample:
        uint(now_ns)
        with self.runtime_lease.control_action():
            self._port.require_observation_source(
                storage_probe=self._storage, semantics=self._semantics
            )
            if self._semantics.canonical_cache is not self._cache:
                raise RuntimeStorageUnavailable("risk collection cache was replaced")
            first = self._storage.account_observation()
            fault: FaultReason | None = None
            try:
                frame = self._cache_frame()
                cohort = self._port.reservation_store.risk_cohort()
                ready, pending = self._costs(first, frame, cohort)
                stable_cache = self._cache_frame().economic_identity() == frame.economic_identity()
            except CacheRiskUnavailable as exc:
                frame = CacheRiskFrame(None, (), ())
                ready, pending, stable_cache = False, set(), False
                fault = exc.reason
            latest = self._storage.account_observation()
            if any(position.use_debt for position in latest.positions):
                raise RuntimeStorageUnavailable(
                    "local cash collection does not support debt-funded positions"
                )
            ready = ready and stable_cache and _same_account(first, latest)
            pending.update(latest.pending_client_order_ids)
            pending.update(latest.venue_operation_intent_ids)
            markets = {row.instrument_id: row for row in frame.markets}
            if set(markets) != set(self._instruments) or any(
                row.ts_init < self._minimum_quote_init_ns for row in frame.markets
            ):
                ready = False
            if (
                not latest.account_source_healthy
                or latest.session_key_address is None
                or latest.session_key_expiry_raw is None
                or latest.session_key_expiry_raw <= self._authorization.execution_window.exit_until
            ):
                ready = False
                fault = fault or "account"
            adverse, mark_times = [], []
            if ready and not pending:
                try:
                    with localcontext() as context:
                        context.prec = 128
                        for position in latest.positions:
                            market: CacheRiskMarket = markets[position.instrument_id]
                            mark = (
                                market.bid - market.tick
                                if position.is_long
                                else market.ask + market.tick
                            )
                            adverse.append(
                                adverse_collateral_pnl(
                                    margin=Decimal(f"{position.margin_raw}e-18"),
                                    leverage=position.leverage,
                                    entry_price=Decimal(
                                        f"{position.entry_price_raw}e-{market.price_precision}"
                                    ),
                                    mark_price=mark,
                                    side="BUY" if position.is_long else "SELL",
                                )
                            )
                            mark_times.append(market.ts_event)
                except (ValueError, ArithmeticError):
                    adverse, mark_times, ready, fault = [], [], False, "marks"
            if pending:
                ready = False
            observation = CashRiskObservation(
                latest.journal.store_id,
                latest.checkpoint_revision,
                latest.account_snapshot_keccak256,
                latest.source_timestamp * 10**9,
                latest.balance_raw + latest.locked_raw,
                latest.cashflow_counters,
                tuple(row.position_id for row in latest.positions),
                tuple(sorted(pending)),
                latest.queued_raw,
                latest.consumable_debt_raw,
                ready,
                tuple(adverse),
                tuple(mark_times),
                tuple((row.instrument_id, row.ts_event) for row in frame.markets),
            )
            return PapertradeRiskSample(observation, fault)
