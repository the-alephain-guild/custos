"""Read one bounded, immutable economic cohort from the registered transaction."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from custos.core.execution_reservation_store import ExecutionReservationSnapshot
from custos.core.execution_risk_events import RiskMutationRecord
from custos.core.order_nonexecution import NativeDispatchSnapshot
from custos.core.reduction_claims import ReductionClaimSnapshot
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from custos.local.native_dispatch import LocalNativeDispatch
from custos.local.reduction_claims import LocalReductionClaims
from custos.local.reservation_ledger import LocalReservationLedger, _Refused


@dataclass(frozen=True)
class LocalRiskOrder:
    reservation: ExecutionReservationSnapshot
    position_id: str | None
    instrument_id: str | None
    side: Literal["BUY", "SELL"] | None
    native_dispatch: NativeDispatchSnapshot | None


@dataclass(frozen=True)
class LocalClosingRisk:
    claim: ReductionClaimSnapshot
    native_dispatch: NativeDispatchSnapshot | None


@dataclass(frozen=True)
class LocalRiskCohort:
    runtime_id: UUID
    account_scope: str
    native_account_id: str
    client_id: str
    journal_id: UUID
    state_sequence: int
    orders: tuple[LocalRiskOrder, ...]
    closings: tuple[LocalClosingRisk, ...]


def _require_order_state(value: ExecutionReservationSnapshot) -> None:
    reserved, cost, quantity = value.reserved_notional, value.filled_exposure, value.filled_quantity
    valid = {
        "reserved": reserved > 0 and cost == quantity == 0,
        "partially_filled": reserved > 0 and cost > 0 and quantity > 0,
        "filled": reserved == 0 and cost > 0 and quantity > 0,
        "closed": reserved == cost == quantity == 0,
        "released": reserved == cost == quantity == 0,
    }
    if not valid.get(value.state, False):
        raise RuntimeStorageUnavailable("economic cohort state disagrees with its original amounts")


def _completed_fact(
    connection: sqlite3.Connection,
    *,
    ledger: LocalReservationLedger,
    native: NativeDispatchSnapshot,
    operation: Literal["fill", "reduce"],
    required: dict[str, str],
) -> bool:
    clauses = [
        "f.account_scope=?",
        "f.original_run_id=?",
        "f.action=?",
        "json_extract(f.outcome_json,'$.state')=?",
    ]
    parameters: list[str] = [
        ledger.identity.account_scope,
        str(native.runtime_id),
        operation,
        "filled" if operation == "fill" else "closed",
    ]
    for key, value in required.items():
        clauses.append("json_extract(f.request_json,?)=?")
        parameters.extend(("$." + key, value))
    rows = connection.execute(
        "SELECT f.*,e.kind,e.payload FROM local_venue_fill f JOIN execution_event e ON e.sequence=f.audit_sequence WHERE "
        + " AND ".join(clauses)
        + " LIMIT 257",
        parameters,
    ).fetchall()
    if len(rows) > 256:
        raise RuntimeStorageUnavailable("completed Native cohort exceeds its fact bound")
    for row in rows:
        request, outcome = json.loads(row["request_json"]), json.loads(row["outcome_json"])
        record = RiskMutationRecord(operation, request, outcome)
        if (
            row["kind"] != "risk_mutation"
            or record.payload() != row["payload"]
            or request.get("fill_identity") != row["fill_identity"]
        ):
            raise RuntimeStorageUnavailable("completed Native fact differs from its original audit")
        if "error_code" not in outcome:
            return True
    return False


def _require_dispatch_links(
    connection: sqlite3.Connection,
    ledger: LocalReservationLedger,
    dispatch: LocalNativeDispatch,
    reductions: LocalReductionClaims,
) -> None:
    markers = connection.execute(
        "SELECT * FROM local_native_dispatch WHERE account_scope=? AND state IN ('prepared','dispatched') ORDER BY client_order_id LIMIT 4097",
        (ledger.identity.account_scope,),
    ).fetchall()
    if len(markers) > 4096:
        raise RuntimeStorageUnavailable("Native cohort history exceeds its supported bound")
    for row in markers:
        native = dispatch._snapshot(row)
        binding = native.binding
        identifier = binding.client_order_id
        if (
            identifier != row["client_order_id"]
            or binding.native_account_id != dispatch._account_id
            or binding.client_id != dispatch._client_id
            or native.journal_id != ledger._lease.journal_store.store_id
        ):
            raise RuntimeStorageUnavailable("Native cohort marker has another original route")
        if binding.reduce_only:
            parent = reductions._row(connection, identifier)
            if parent is None:
                raise RuntimeStorageUnavailable("Native closing marker has no original claim")
            claim = reductions._snapshot(parent)
            if (
                claim.runtime_id != native.runtime_id
                or claim.authorization_epoch != native.authorization_epoch
                or claim.process_generation != native.process_generation
                or claim.permit_digest != native.permit_digest
                or claim.target.account_id != binding.native_account_id
                or claim.target.client_id != binding.client_id
                or claim.target.position_id != "PT-" + str(binding.target)
                or claim.target.instrument_id != binding.instrument_id
                or claim.target.side != binding.side
                or claim.target.quantity != binding.quantity
            ):
                raise RuntimeStorageUnavailable(
                    "Native closing marker differs from its original claim"
                )
            if claim.state == "filled" and native.state == "dispatched":
                complete = _completed_fact(
                    connection,
                    ledger=ledger,
                    native=native,
                    operation="reduce",
                    required={
                        "closing_client_order_id": identifier,
                        "position_id": claim.target.position_id,
                        "instrument_id": binding.instrument_id,
                        "side": binding.side,
                    },
                )
                if not complete:
                    raise RuntimeStorageUnavailable(
                        "completed closing marker has no original venue fact"
                    )
            elif (
                claim.state == "unknown"
                or (claim.state == "prepared" and native.state == "prepared")
                or (claim.state == "dispatching" and native.state == "dispatched")
            ):
                continue
            else:
                raise RuntimeStorageUnavailable(
                    "Native closing marker conflicts with its claim state"
                )
        else:
            parent = connection.execute(
                "SELECT * FROM local_order_reservation WHERE account_scope=? AND client_order_id=?",
                (ledger.identity.account_scope, identifier),
            ).fetchone()
            if parent is None or parent["run_id"] != str(native.runtime_id):
                raise RuntimeStorageUnavailable("Native opening marker has no original reservation")
            reservation = ledger.snapshot(parent)
            _require_order_state(reservation)
            if reservation.state == "closed" and native.state == "dispatched":
                if (
                    reservation.reserved_notional
                    or reservation.filled_exposure
                    or reservation.filled_quantity
                    or parent["position_id"] is None
                    or parent["instrument_id"] != binding.instrument_id
                    or parent["side"] != binding.side
                ):
                    raise RuntimeStorageUnavailable(
                        "completed opening marker has conflicting economics"
                    )
                opened = _completed_fact(
                    connection,
                    ledger=ledger,
                    native=native,
                    operation="fill",
                    required={
                        "client_order_id": identifier,
                        "position_id": parent["position_id"],
                        "instrument_id": binding.instrument_id,
                        "side": binding.side,
                    },
                )
                closed = _completed_fact(
                    connection,
                    ledger=ledger,
                    native=native,
                    operation="reduce",
                    required={
                        "position_id": parent["position_id"],
                        "instrument_id": binding.instrument_id,
                        "side": "SELL" if binding.side == "BUY" else "BUY",
                    },
                )
                if not opened or not closed:
                    raise RuntimeStorageUnavailable(
                        "completed opening marker has no original round-trip facts"
                    )
            elif reservation.state not in {"reserved", "partially_filled", "filled"} or (
                native.state == "prepared" and reservation.filled_quantity
            ):
                raise RuntimeStorageUnavailable(
                    "Native opening marker conflicts with its economic state"
                )


def read_risk_cohort(
    ledger: LocalReservationLedger,
    *,
    reductions: LocalReductionClaims,
    dispatch: LocalNativeDispatch,
) -> LocalRiskCohort:
    if reductions._ledger is not ledger or dispatch._ledger is not ledger:
        raise RuntimeAuthorityError("risk cohort requires the original economic bindings")
    with ledger.transaction() as connection:
        route = connection.execute(
            "SELECT native_account_id,client_id,native_journal_id FROM local_risk_meta"
        ).fetchall()
        expected = (
            dispatch._account_id,
            dispatch._client_id,
            str(ledger._lease.journal_store.store_id),
        )
        if len(route) != 1 or tuple(route[0]) != expected:
            raise RuntimeStorageUnavailable("risk cohort route differs from its original binding")
        _require_dispatch_links(connection, ledger, dispatch, reductions)
        rows = connection.execute(
            "SELECT * FROM local_order_reservation WHERE account_scope=? AND (state NOT IN ('closed','released') OR reserved_notional<>'0' OR filled_exposure<>'0' OR filled_quantity<>'0') ORDER BY client_order_id LIMIT 257",
            (ledger.identity.account_scope,),
        ).fetchall()
        closes = connection.execute(
            "SELECT closing_client_order_id FROM local_reduction_claim WHERE account_scope=? AND state IN ('prepared','dispatching','unknown') ORDER BY closing_client_order_id LIMIT 257",
            (ledger.identity.account_scope,),
        ).fetchall()
        if len(rows) > 256 or len(closes) > 256:
            raise RuntimeStorageUnavailable("active economic cohort exceeds its supported bound")

        def native(identifier: str) -> NativeDispatchSnapshot | None:
            if dispatch._row(connection, identifier) is None:
                return None
            try:
                return dispatch._current(connection, identifier)
            except _Refused as exc:
                raise RuntimeStorageUnavailable("risk cohort Native owner is invalid") from exc

        orders = []
        for row in rows:
            value = ledger.snapshot(row)
            _require_order_state(value)
            if value.runtime_id != ledger.identity.run_id:
                raise RuntimeStorageUnavailable("inherited economic cohort requires recovery")
            if value.state in {"closed", "released"}:
                if value.reserved_notional or value.filled_exposure or value.filled_quantity:
                    raise RuntimeStorageUnavailable("terminal order retains conflicting economics")
                continue
            if (value.filled_exposure > 0) != (value.filled_quantity > 0):
                raise RuntimeStorageUnavailable("position cost and quantity association differs")
            if value.filled_exposure and (
                type(row["position_id"]) is not str
                or type(row["instrument_id"]) is not str
                or row["side"] not in {"BUY", "SELL"}
            ):
                raise RuntimeStorageUnavailable("position cost identity is incomplete")
            orders.append(
                LocalRiskOrder(
                    value,
                    row["position_id"],
                    row["instrument_id"],
                    cast(Literal["BUY", "SELL"] | None, row["side"]),
                    native(row["client_order_id"]),
                )
            )
        closing = []
        for row in closes:
            identifier = row["closing_client_order_id"]
            try:
                claim = reductions._current(connection, identifier)
            except _Refused as exc:
                raise RuntimeStorageUnavailable("risk cohort closing owner is invalid") from exc
            closing.append(LocalClosingRisk(claim, native(identifier)))
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM execution_event"
        ).fetchone()[0]
        return LocalRiskCohort(
            ledger.identity.run_id,
            ledger.identity.account_scope,
            expected[0],
            expected[1],
            ledger._lease.journal_store.store_id,
            sequence,
            tuple(orders),
            tuple(closing),
        )
