"""Durable Native call markers and evidence settlement in the existing risk database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID

from custos.core.execution_risk_events import RiskMutationRecord, _token
from custos.core.local_runtime_state import BoundRuntimeStore
from custos.core.order_nonexecution import (
    NativeDispatchBinding,
    NativeDispatchSnapshot,
    NativeNoFillSettlement,
    _revision,
)
from custos.core.runtime_authority import (
    RuntimeAuthorityError,
    RuntimeRiskPending,
    RuntimeStorageUnavailable,
)
from custos.local.reduction_claims import LocalReductionClaims
from custos.local.reservation_ledger import LocalReservationLedger, _Refused


class LocalNativeDispatch:
    def __init__(
        self,
        *,
        ledger: LocalReservationLedger,
        reductions: LocalReductionClaims,
        journal: BoundRuntimeStore,
    ) -> None:
        self._ledger = ledger
        self._reductions = reductions
        if journal != ledger._lease.journal_store:
            raise RuntimeStorageUnavailable("Native dispatch journal differs from the lease")
        self._journal = journal
        with ledger.transaction() as connection:
            metadata = connection.execute(
                "SELECT native_account_id,client_id,native_journal_id FROM local_risk_meta"
            ).fetchall()
            if len(metadata) != 1 or metadata[0][0] is None or metadata[0][1] is None:
                raise RuntimeStorageUnavailable("Native dispatch routing is not durably bound")
            if metadata[0][2] is None:
                if connection.execute("SELECT 1 FROM local_native_dispatch LIMIT 1").fetchone():
                    raise RuntimeStorageUnavailable("Native dispatch history lost its journal")
                connection.execute(
                    "UPDATE local_risk_meta SET native_journal_id=?", (str(journal.store_id),)
                )
            elif metadata[0][2] != str(journal.store_id):
                raise RuntimeStorageUnavailable("Native dispatch journal binding changed")
            self._account_id, self._client_id = metadata[0][:2]

    def _row(self, connection: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM local_native_dispatch WHERE account_scope=? AND client_order_id=?",
                (self._ledger.identity.account_scope, identifier),
            ).fetchone(),
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> NativeDispatchSnapshot:
        return NativeDispatchSnapshot(
            NativeDispatchBinding.from_document(row["binding_json"]),
            UUID(row["run_id"]),
            row["authorization_epoch"],
            row["process_generation"],
            row["permit_digest"],
            UUID(row["journal_id"]),
            row["missing_revision"],
            row["state"],
        )

    def _current(self, connection: sqlite3.Connection, identifier: str) -> NativeDispatchSnapshot:
        row = self._row(connection, identifier)
        if row is None:
            raise _Refused("local_native_dispatch_binding_missing")
        value = self._snapshot(row)
        ledger = self._ledger
        metadata = connection.execute(
            "SELECT native_account_id,client_id,native_journal_id FROM local_risk_meta"
        ).fetchall()
        if (
            len(metadata) != 1
            or tuple(metadata[0])
            != (self._account_id, self._client_id, str(self._journal.store_id))
            or value.runtime_id != ledger.identity.run_id
            or value.authorization_epoch != ledger.identity.authorization_epoch
            or value.permit_digest != ledger._authorization.permit_digest
            or value.journal_id != self._journal.store_id
            or value.binding.native_account_id != self._account_id
            or value.binding.client_id != self._client_id
            or value.binding.client_order_id != identifier
        ):
            raise _Refused("local_native_dispatch_owner_conflict")
        _revision(value.missing_revision)
        return value

    def _mutate(
        self,
        operation: str,
        identifier: str,
        request: Mapping[str, str | None],
        change: Callable[[sqlite3.Connection], NativeDispatchSnapshot | bool],
    ) -> NativeDispatchSnapshot | bool:
        _token(identifier, "client_order_id")
        request = {**request, "client_order_id": identifier}
        result: NativeDispatchSnapshot | bool = False
        fault = None
        with self._ledger.transaction() as connection:
            connection.execute("SAVEPOINT native_risk_change")
            try:
                result = change(connection)
            except _Refused as error:
                connection.execute("ROLLBACK TO native_risk_change")
                connection.execute("RELEASE native_risk_change")
                fault = error
                self._ledger._reject(connection, operation + ":" + identifier, request, error)
            else:
                connection.execute("RELEASE native_risk_change")
                current = self._current(connection, identifier)
                record = RiskMutationRecord(operation, request, {"state": current.state})
                event_id = "local-native:" + hashlib.sha256(record.payload().encode()).hexdigest()
                self._ledger._events._append_risk(
                    connection,
                    lease=self._ledger._lease,
                    identity=self._ledger.identity,
                    event_id=event_id,
                    risk=record,
                )
        if fault:
            if fault.retryable:
                raise RuntimeRiskPending(fault.code)
            raise RuntimeAuthorityError(fault.code)
        return result

    def load(self, identifier: str) -> NativeDispatchSnapshot | None:
        _token(identifier, "client_order_id")
        with self._ledger.transaction() as connection:
            row = self._row(connection, identifier)
            return self._snapshot(row) if row is not None else None

    def prepare(
        self, *, binding: NativeDispatchBinding, missing_revision: int
    ) -> NativeDispatchSnapshot:
        if not isinstance(binding, NativeDispatchBinding):
            raise RuntimeAuthorityError("Native dispatch requires immutable original semantics")
        _revision(missing_revision)
        identifier = binding.client_order_id
        request = {
            "input_digest": hashlib.sha256(binding.document().encode()).hexdigest(),
            "missing_revision": str(missing_revision),
            "journal_id": str(self._journal.store_id),
        }

        def change(connection: sqlite3.Connection) -> NativeDispatchSnapshot:
            if self._row(connection, identifier) is not None:
                current = self._current(connection, identifier)
                if current.binding != binding or current.missing_revision != missing_revision:
                    raise _Refused("local_native_dispatch_identity_reused")
                return current
            if (
                binding.native_account_id != self._account_id
                or binding.client_id != self._client_id
            ):
                raise _Refused("local_native_dispatch_route_conflict")
            if missing_revision < self._ledger._lease.observed_journal_sequence:
                raise _Refused("local_native_missing_observation_is_stale")
            if binding.reduce_only:
                claim = self._reductions._current(connection, identifier)
                if (
                    claim.state != "prepared"
                    or claim.process_generation != self._ledger._lease.fence.process_generation
                    or claim.target.position_id != "PT-" + cast(str, binding.target)
                    or claim.target.instrument_id != binding.instrument_id
                    or claim.target.side != binding.side
                    or claim.target.quantity != binding.quantity
                ):
                    raise _Refused("local_native_close_binding_conflict")
            else:
                row = self._ledger.row(connection, identifier)
                if (
                    row["policy_id"] != str(self._ledger._policy)
                    or Decimal(row["reserved_notional"]) != binding.quantity
                    or Decimal(row["filled_quantity"]) != 0
                ):
                    raise _Refused("local_native_opening_binding_conflict")
            connection.execute(
                "INSERT INTO local_native_dispatch VALUES (?,?,?,?,?,?,?,?,?,'prepared',NULL)",
                (
                    self._ledger.identity.account_scope,
                    identifier,
                    str(self._ledger.identity.run_id),
                    self._ledger.identity.authorization_epoch,
                    self._ledger._lease.fence.process_generation,
                    self._ledger._authorization.permit_digest,
                    str(self._journal.store_id),
                    missing_revision,
                    binding.document(),
                ),
            )
            return self._current(connection, identifier)

        return cast(
            NativeDispatchSnapshot, self._mutate("native_prepare", identifier, request, change)
        )

    def begin_dispatch(self, identifier: str) -> bool:
        def change(connection: sqlite3.Connection) -> bool:
            current = self._current(connection, identifier)
            if (
                current.state != "prepared"
                or current.process_generation != self._ledger._lease.fence.process_generation
            ):
                return False
            if current.binding.reduce_only:
                self._reductions._exit_current()
                claim = self._reductions._current(connection, identifier)
                if (
                    claim.state != "prepared"
                    or claim.process_generation != current.process_generation
                ):
                    raise _Refused("local_native_close_marker_conflict")
                self._reductions._set_state(connection, identifier, "dispatching")
            else:
                if reason := self._ledger._gate(connection):
                    raise reason
            connection.execute(
                "UPDATE local_native_dispatch SET state='dispatched' WHERE account_scope=? AND client_order_id=?",
                (self._ledger.identity.account_scope, identifier),
            )
            return True

        return cast(bool, self._mutate("native_dispatch", identifier, {}, change))

    def _require_no_fill(
        self, connection: sqlite3.Connection, current: NativeDispatchSnapshot
    ) -> None:
        identifier = current.binding.client_order_id
        for row in connection.execute(
            "SELECT action,request_json,outcome_json FROM local_venue_fill WHERE account_scope=?",
            (self._ledger.identity.account_scope,),
        ):
            request = json.loads(row[1])
            if identifier in (
                request.get("client_order_id"),
                request.get("closing_client_order_id"),
            ):
                raise _Refused("local_native_proof_conflicts_with_venue_fact")
            if (
                current.binding.reduce_only
                and row[0] == "reduce"
                and "error_code" in json.loads(row[2])
                and (
                    request.get("position_id") is None
                    or request.get("position_id") == "PT-" + cast(str, current.binding.target)
                )
            ):
                raise _Refused("local_native_proof_conflicts_with_pending_close_fact")
        if current.binding.reduce_only:
            claim = self._reductions._current(connection, identifier)
            if claim.state == "filled":
                raise _Refused("local_native_proof_conflicts_with_fill")
        else:
            row = self._ledger.row(connection, identifier)
            if Decimal(row["filled_quantity"]) != 0 or Decimal(row["filled_exposure"]) != 0:
                raise _Refused("local_native_proof_conflicts_with_fill")

    def _release(
        self, connection: sqlite3.Connection, current: NativeDispatchSnapshot, *, proven: bool
    ) -> None:
        identifier = current.binding.client_order_id
        if current.binding.reduce_only:
            self._reductions._set_state(
                connection, identifier, "proven_no_fill" if proven else "not_sent"
            )
        else:
            connection.execute(
                "UPDATE local_order_reservation SET reserved_notional='0',state='released' WHERE account_scope=? AND client_order_id=?",
                (self._ledger.identity.account_scope, identifier),
            )

    def withdraw_unsent(self, identifier: str) -> NativeDispatchSnapshot:
        def change(connection: sqlite3.Connection) -> NativeDispatchSnapshot:
            current = self._current(connection, identifier)
            if current.process_generation != self._ledger._lease.fence.process_generation:
                raise _Refused("local_native_unsent_belongs_to_another_process")
            self._require_no_fill(connection, current)
            if current.state == "not_sent":
                if (
                    current.binding.reduce_only
                    and self._reductions._current(connection, identifier).state != "not_sent"
                ):
                    raise _Refused("local_native_withdrawn_claim_state_conflict")
                return current
            if current.state != "prepared":
                raise _Refused("local_native_dispatch_may_have_been_sent")
            if current.binding.reduce_only:
                claim = self._reductions._current(connection, identifier)
                if (
                    claim.state != "prepared"
                    or claim.process_generation != current.process_generation
                ):
                    raise _Refused("local_native_unsent_claim_may_have_been_sent")
            self._release(connection, current, proven=False)
            connection.execute(
                "UPDATE local_native_dispatch SET state='not_sent' WHERE account_scope=? AND client_order_id=?",
                (self._ledger.identity.account_scope, identifier),
            )
            return self._current(connection, identifier)

        return cast(NativeDispatchSnapshot, self._mutate("native_not_sent", identifier, {}, change))

    def settle_no_fill(self, evidence: NativeNoFillSettlement) -> NativeDispatchSnapshot:
        if not isinstance(evidence, NativeNoFillSettlement):
            raise RuntimeAuthorityError("Native settlement requires verified no-fill evidence")
        identifier = evidence.binding.client_order_id
        request = {
            "proof_digest": evidence.proof_digest,
            "intent_id": evidence.intent_id,
            "command_keccak256": evidence.command_keccak256,
            "sealed_at_revision": str(evidence.sealed_at_revision),
            "proof_source": evidence.source,
        }

        def change(connection: sqlite3.Connection) -> NativeDispatchSnapshot:
            current = self._current(connection, identifier)
            if (
                current.binding != evidence.binding
                or current.journal_id != evidence.journal_id
                or evidence.sealed_at_revision <= current.missing_revision
                or evidence.journal_revision < self._ledger._lease.observed_journal_sequence
                or current.state not in {"prepared", "dispatched", "proven_no_fill"}
            ):
                raise _Refused("local_native_nonexecution_proof_conflict")
            self._require_no_fill(connection, current)
            document = evidence.identity_document()
            row = self._row(connection, identifier)
            assert row is not None
            if current.state == "proven_no_fill":
                if row["proof_json"] != document:
                    raise _Refused("local_native_nonexecution_proof_changed")
                if (
                    current.binding.reduce_only
                    and self._reductions._current(connection, identifier).state != "proven_no_fill"
                ):
                    raise _Refused("local_native_nonexecution_claim_state_conflict")
                return current
            if row["proof_json"] is not None:
                raise _Refused("local_native_nonexecution_state_conflict")
            self._release(connection, current, proven=True)
            connection.execute(
                "UPDATE local_native_dispatch SET state='proven_no_fill',proof_json=? WHERE account_scope=? AND client_order_id=?",
                (document, self._ledger.identity.account_scope, identifier),
            )
            return self._current(connection, identifier)

        return cast(
            NativeDispatchSnapshot, self._mutate("native_no_fill", identifier, request, change)
        )
