"""Persistent per-position closing intent, without granting Native dispatch itself."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import cast
from uuid import UUID

from custos.core.execution_risk_events import RiskMutationRecord, _token
from custos.core.reduction_claims import (
    ReductionClaimSnapshot,
    ReductionClaimState,
    ReductionTarget,
)
from custos.core.runtime_authority import RuntimeAuthorityError
from custos.local.authorization import _utc_instant
from custos.local.reservation_ledger import _MATH, LocalReservationLedger, _number, _Refused, _text


class LocalReductionClaims:
    def __init__(self, *, ledger: LocalReservationLedger, account_id: str, client_id: str) -> None:
        _token(account_id, "native_account_id")
        _token(client_id, "client_id")
        self._ledger = ledger
        self._account_id = account_id
        self._client_id = client_id
        with ledger.transaction() as connection:
            binding = connection.execute(
                "SELECT native_account_id,client_id FROM local_risk_meta"
            ).fetchone()
            if binding[0] is None and binding[1] is None:
                connection.execute(
                    "UPDATE local_risk_meta SET native_account_id=?,client_id=?",
                    (account_id, client_id),
                )
            elif tuple(binding) != (account_id, client_id):
                raise RuntimeAuthorityError(
                    "local Native reduction routing differs from its durable binding"
                )

    def _row(self, connection: sqlite3.Connection, close: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM local_reduction_claim WHERE account_scope=? AND closing_client_order_id=?",
                (self._ledger.identity.account_scope, close),
            ).fetchone(),
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> ReductionClaimSnapshot:
        return ReductionClaimSnapshot(
            runtime_id=UUID(row["run_id"]),
            authorization_epoch=row["authorization_epoch"],
            process_generation=row["process_generation"],
            permit_digest=row["permit_digest"],
            closing_client_order_id=row["closing_client_order_id"],
            target=ReductionTarget(
                account_id=row["native_account_id"],
                client_id=row["client_id"],
                position_id=row["position_id"],
                instrument_id=row["instrument_id"],
                side=row["side"],
                quantity=_number(Decimal(row["quantity"]), positive=True),
                params_digest=row["params_digest"],
            ),
            state=cast(ReductionClaimState, row["state"]),
        )

    def _current(self, connection: sqlite3.Connection, close: str) -> ReductionClaimSnapshot:
        row = self._row(connection, close)
        if row is None:
            raise _Refused("local_closing_claim_missing")
        claim = self._snapshot(row)
        ledger = self._ledger
        if (
            claim.runtime_id != ledger.identity.run_id
            or claim.authorization_epoch != ledger.identity.authorization_epoch
            or claim.permit_digest != ledger._authorization.permit_digest
            or claim.target.account_id != self._account_id
            or claim.target.client_id != self._client_id
        ):
            raise _Refused("local_closing_claim_owner_conflict")
        return claim

    def _exit_current(self) -> None:
        now = self._ledger._clock()
        permit = self._ledger._authorization.permit
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or not _utc_instant(permit.not_before_utc) <= now < _utc_instant(permit.exit_until_utc)
        ):
            raise _Refused("local_exit_permit_not_current")

    def _mutate(
        self,
        operation: str,
        close: str,
        request: Mapping[str, str | None],
        change: Callable[[sqlite3.Connection], ReductionClaimSnapshot | bool],
    ) -> ReductionClaimSnapshot | bool:
        _token(close, "closing_client_order_id")
        request = {**request, "closing_client_order_id": close}
        fault = None
        result: ReductionClaimSnapshot | bool = False
        with localcontext(_MATH), self._ledger.transaction() as connection:
            connection.execute("SAVEPOINT local_close_change")
            try:
                result = change(connection)
            except _Refused as error:
                connection.execute("ROLLBACK TO local_close_change")
                connection.execute("RELEASE local_close_change")
                fault = error.code
                self._ledger._fault(
                    connection, operation + ":" + close, "risk_refused", request, fault
                )
            else:
                connection.execute("RELEASE local_close_change")
                claim = self._current(connection, close)
                record = RiskMutationRecord(operation, request, {"claim_state": claim.state})
                event_id = "local-close:" + hashlib.sha256(record.payload().encode()).hexdigest()
                self._ledger._events._append_risk(
                    connection,
                    lease=self._ledger._lease,
                    identity=self._ledger.identity,
                    event_id=event_id,
                    risk=record,
                )
        if fault:
            raise RuntimeAuthorityError(fault)
        return result

    def load(self, closing_client_order_id: str) -> ReductionClaimSnapshot | None:
        _token(closing_client_order_id, "closing_client_order_id")
        with self._ledger.transaction() as connection:
            row = self._row(connection, closing_client_order_id)
            return self._snapshot(row) if row is not None else None

    def pending(self) -> tuple[ReductionClaimSnapshot, ...]:
        with self._ledger.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM local_reduction_claim WHERE account_scope=? AND state IN ('prepared','dispatching','unknown') ORDER BY closing_client_order_id",
                (self._ledger.identity.account_scope,),
            ).fetchall()
            return tuple(self._snapshot(row) for row in rows)

    def prepare(
        self, *, closing_client_order_id: str, target: ReductionTarget
    ) -> ReductionClaimSnapshot:
        if not isinstance(target, ReductionTarget):
            raise RuntimeAuthorityError("local close requires an immutable reduction target")
        quantity = _number(target.quantity, positive=True)
        request = {
            "position_id": target.position_id,
            "instrument_id": target.instrument_id,
            "side": target.side,
            "quantity": _text(quantity),
            "native_account_id": target.account_id,
            "client_id": target.client_id,
            "params_digest": target.params_digest,
            "permit_digest": self._ledger._authorization.permit_digest,
            "authorization_epoch": str(self._ledger.identity.authorization_epoch),
        }

        def change(connection: sqlite3.Connection) -> ReductionClaimSnapshot:
            self._exit_current()
            if target.account_id != self._account_id or target.client_id != self._client_id:
                raise _Refused("local_close_native_routing_conflict")
            prior = self._row(connection, closing_client_order_id)
            if prior is not None:
                claim = self._current(connection, closing_client_order_id)
                if claim.target != target:
                    raise _Refused("local_close_identity_conflict")
                return claim
            rows = connection.execute(
                "SELECT * FROM local_order_reservation WHERE account_scope=? AND position_id=?",
                (self._ledger.identity.account_scope, target.position_id),
            ).fetchall()
            available = sum((Decimal(row["filled_quantity"]) for row in rows), Decimal(0))
            if (
                not rows
                or available != quantity
                or any(
                    row["run_id"] != str(self._ledger.identity.run_id)
                    or row["instrument_id"] != target.instrument_id
                    or row["side"] != ("BUY" if target.side == "SELL" else "SELL")
                    or Decimal(row["reserved_notional"]) != 0
                    for row in rows
                )
            ):
                raise _Refused("local_close_original_position_mismatch")
            if connection.execute(
                "SELECT 1 FROM local_reduction_claim WHERE account_scope=? AND position_id=? AND state IN ('prepared','dispatching','unknown')",
                (self._ledger.identity.account_scope, target.position_id),
            ).fetchone():
                raise _Refused("local_position_close_already_pending")
            connection.execute(
                "INSERT INTO local_reduction_claim VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared')",
                (
                    self._ledger.identity.account_scope,
                    closing_client_order_id,
                    str(self._ledger.identity.run_id),
                    self._ledger.identity.authorization_epoch,
                    self._ledger._lease.fence.process_generation,
                    self._ledger._authorization.permit_digest,
                    target.account_id,
                    target.client_id,
                    target.position_id,
                    target.instrument_id,
                    target.side,
                    _text(quantity),
                    target.params_digest,
                ),
            )
            return self._current(connection, closing_client_order_id)

        return cast(
            ReductionClaimSnapshot,
            self._mutate("close_prepare", closing_client_order_id, request, change),
        )

    def begin_dispatch(self, closing_client_order_id: str) -> bool:
        def change(connection: sqlite3.Connection) -> bool:
            self._require_legacy_transition(connection, closing_client_order_id)
            self._exit_current()
            claim = self._current(connection, closing_client_order_id)
            if claim.state != "prepared" or (
                claim.process_generation != self._ledger._lease.fence.process_generation
            ):
                return False
            self._set_state(connection, closing_client_order_id, "dispatching")
            return True

        return cast(bool, self._mutate("close_dispatch", closing_client_order_id, {}, change))

    def mark_unknown(self, closing_client_order_id: str) -> ReductionClaimSnapshot:
        def change(connection: sqlite3.Connection) -> ReductionClaimSnapshot:
            claim = self._current(connection, closing_client_order_id)
            if claim.state in {"dispatching", "unknown"}:
                self._set_state(connection, closing_client_order_id, "unknown")
                self._ledger._latch(
                    connection,
                    "close-unknown:" + closing_client_order_id,
                    "local_close_outcome_unknown",
                )
            elif claim.state == "prepared":
                raise _Refused("local_close_dispatch_not_started")
            return self._current(connection, closing_client_order_id)

        return cast(
            ReductionClaimSnapshot,
            self._mutate("close_unknown", closing_client_order_id, {}, change),
        )

    def mark_not_sent(self, closing_client_order_id: str) -> ReductionClaimSnapshot:
        def change(connection: sqlite3.Connection) -> ReductionClaimSnapshot:
            self._require_legacy_transition(connection, closing_client_order_id)
            claim = self._current(connection, closing_client_order_id)
            if claim.state == "not_sent":
                return claim
            if claim.state != "prepared" or (
                claim.process_generation != self._ledger._lease.fence.process_generation
            ):
                raise _Refused("local_close_may_have_been_sent")
            self._set_state(connection, closing_client_order_id, "not_sent")
            return self._current(connection, closing_client_order_id)

        return cast(
            ReductionClaimSnapshot,
            self._mutate("close_not_sent", closing_client_order_id, {}, change),
        )

    def _require_legacy_transition(self, connection: sqlite3.Connection, close: str) -> None:
        native = connection.execute("SELECT native_journal_id FROM local_risk_meta").fetchone()
        if (
            native is None
            or native[0] is not None
            or connection.execute(
                "SELECT 1 FROM local_native_dispatch WHERE account_scope=? AND client_order_id=?",
                (self._ledger.identity.account_scope, close),
            ).fetchone()
        ):
            raise _Refused("local_native_close_requires_joint_transition")

    def _set_state(
        self, connection: sqlite3.Connection, close: str, state: ReductionClaimState
    ) -> None:
        connection.execute(
            "UPDATE local_reduction_claim SET state=? WHERE account_scope=? AND closing_client_order_id=?",
            (state, self._ledger.identity.account_scope, close),
        )

    def validate_fill(
        self,
        connection: sqlite3.Connection,
        *,
        closing_client_order_id: str,
        position_id: str,
        quantity: Decimal,
        instrument_id: str,
        side: str,
    ) -> None:
        claim = self._current(connection, closing_client_order_id)
        if (
            claim.state not in {"dispatching", "unknown"}
            or claim.target.position_id != position_id
            or claim.target.instrument_id != instrument_id
            or claim.target.side != side
            or claim.target.quantity != quantity
        ):
            raise _Refused("local_close_fill_conflict")

    def settle_fill(self, connection: sqlite3.Connection, closing_client_order_id: str) -> None:
        self._set_state(connection, closing_client_order_id, "filled")
