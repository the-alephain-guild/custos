"""Local permit binding and atomic account risk and venue evidence transactions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    Clamped,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    InvalidOperation,
    Overflow,
    Subnormal,
    Underflow,
    localcontext,
)
from fractions import Fraction
from typing import Protocol, cast
from uuid import UUID

from custos.core.execution_events import (
    ExecutionEventStore,
    ExecutionRunIdentity,
)
from custos.core.execution_reservation_store import (
    ExecutionReservationSnapshot,
)
from custos.core.execution_risk_events import RiskMutationRecord, _token
from custos.core.local_runtime_state import RuntimeLease
from custos.core.runtime_authority import (
    RuntimeAuthorityError,
    RuntimeRiskPending,
    RuntimeStorageUnavailable,
)
from custos.local.authorization import VerifiedLocalRunAuthorization, _utc_instant
from custos.local.loss_state import epoch_ns, loss_admission, loss_row

_MATH = Context(
    prec=100,
    rounding=ROUND_CEILING,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[
        Clamped,
        DivisionByZero,
        FloatOperation,
        InvalidOperation,
        Overflow,
        Subnormal,
        Underflow,
    ],
)


class RiskBlocker(Protocol):
    def freeze(self) -> None: ...
    def is_frozen(self) -> bool: ...


class _Refused(Exception):
    def __init__(
        self, code: str, *, retryable: bool = False, loss_budget_id: str | None = None
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.loss_budget_id = loss_budget_id


@dataclass(frozen=True)
class _Decision:
    snapshot: ExecutionReservationSnapshot | None
    error_code: str | None = None
    audit_sequence: int | None = None
    retryable: bool = False


def _number(value: object, *, positive: bool = False, derived: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise RuntimeAuthorityError("risk amounts require finite nonnegative Decimal values")
    if positive and value == 0:
        raise RuntimeAuthorityError("risk amount must be positive")
    if value != 0:
        digits = value.as_tuple()
        if value.adjusted() > (100 if derived else 17) or cast(int, digits.exponent) < (
            -120 if derived else -18
        ):
            raise RuntimeAuthorityError("risk amount exceeds the supported decimal range")
    return value


def _text(value: Decimal) -> str:
    value = _number(value, derived=True)
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _stored_reservation(value: Decimal) -> Decimal:
    if cast(int, value.as_tuple().exponent) < -120:
        value = value.quantize(Decimal("1e-120"), rounding=ROUND_CEILING)
    return _number(value, derived=True)


def _document(value: Mapping[str, str | None]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _audit_identifier(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        _token(value, "quarantined_identifier")
    except RuntimeAuthorityError:
        return None
    return value


def _snapshot_document(snapshot: ExecutionReservationSnapshot) -> dict[str, str | None]:
    return {
        "run_id": str(snapshot.runtime_id),
        "client_order_id": snapshot.client_order_id,
        "policy_id": str(snapshot.policy_id),
        "reserved_notional": _text(snapshot.reserved_notional),
        "filled_exposure": _text(snapshot.filled_exposure),
        "filled_quantity": _text(snapshot.filled_quantity),
        "state": snapshot.state,
    }


def _snapshot_document_read(document: Mapping[str, str | None]) -> ExecutionReservationSnapshot:
    return ExecutionReservationSnapshot(
        UUID(str(document["run_id"])),
        str(document["client_order_id"]),
        UUID(str(document["policy_id"])),
        _number(Decimal(str(document["reserved_notional"])), derived=True),
        _number(Decimal(str(document["filled_exposure"])), derived=True),
        _number(Decimal(str(document["filled_quantity"])), derived=True),
        str(document["state"]),
    )


class LocalReservationLedger:
    def __init__(
        self,
        *,
        store: ExecutionEventStore,
        lease: RuntimeLease,
        authorization: VerifiedLocalRunAuthorization,
        risk_blocker: RiskBlocker,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._events = store
        self._lease = lease
        self._authorization = authorization
        self._blocker = risk_blocker
        self._clock = clock
        if (
            not isinstance(authorization, VerifiedLocalRunAuthorization)
            or not isinstance(lease, RuntimeLease)
            or not isinstance(store, ExecutionEventStore)
        ):
            self._blocker.freeze()
            raise RuntimeAuthorityError("local risk requires concrete verified runtime material")
        self._run = authorization.manifest.run_id
        self._account = authorization.permit.account_scope
        self._policy = authorization.permit.permit_id
        self._limits = authorization.manifest.limits
        self._identity = ExecutionRunIdentity(
            self._run,
            self._account,
            authorization.permit.authorization_epoch,
            authorization.manifest.configuration_revision,
        )
        try:
            if (
                not isinstance(authorization, VerifiedLocalRunAuthorization)
                or authorization.checkpoint.run_id != self._run
                or authorization.permit.control_registry_id != lease.control_registry_id
                or authorization.checkpoint.control_registry_id != lease.control_registry_id
                or authorization.checkpoint.account_scope != lease.fence.account_scope
                or authorization.checkpoint.authorization_epoch != lease.fence.authorization_epoch
                or authorization.permit_digest != authorization.checkpoint.permit_digest
                or authorization.permit_digest != lease.permit_digest
                or lease.fence.control_source != "local"
            ):
                raise RuntimeAuthorityError("local risk authorization differs from actual lease")
            with self._events._runtime_transaction(
                lease=lease, identity=self._identity
            ) as connection:
                metadata = connection.execute(
                    "SELECT schema_version,account_scope,native_account_id,client_id,native_journal_id FROM local_risk_meta"
                ).fetchall()
                if len(metadata) != 1 or metadata[0][0] != 5:
                    raise RuntimeStorageUnavailable("local risk schema is unavailable")
                connection.execute("SELECT * FROM local_venue_fill LIMIT 0")
                connection.execute("SELECT * FROM local_order_reservation LIMIT 0")
                connection.execute("SELECT * FROM local_reduction_claim LIMIT 0")
                connection.execute("SELECT * FROM local_native_dispatch LIMIT 0")
                if (metadata[0][2] is None) != (metadata[0][3] is None):
                    raise RuntimeStorageUnavailable(
                        "local reduction routing metadata is incomplete"
                    )
                if metadata[0][4] is not None and metadata[0][2] is None:
                    raise RuntimeStorageUnavailable("local Native journal has no routing binding")
                if (
                    metadata[0][2] is None
                    and connection.execute("SELECT 1 FROM local_reduction_claim LIMIT 1").fetchone()
                ):
                    raise RuntimeStorageUnavailable("local reduction routing history is unbound")
                native_history = connection.execute(
                    "SELECT 1 FROM local_native_dispatch LIMIT 1"
                ).fetchone()
                if native_history and (metadata[0][2] is None or metadata[0][4] is None):
                    raise RuntimeStorageUnavailable("local Native dispatch history is unbound")
                if metadata[0][4] is not None and metadata[0][4] != str(
                    lease.journal_store.store_id
                ):
                    raise RuntimeStorageUnavailable(
                        "local Native journal differs from registration"
                    )
                if metadata[0][1] is None:
                    history = connection.execute(
                        "SELECT 1 FROM local_account_risk UNION ALL SELECT 1 FROM local_order_reservation UNION ALL SELECT 1 FROM local_venue_fill UNION ALL SELECT 1 FROM local_reduction_claim UNION ALL SELECT 1 FROM local_native_dispatch UNION ALL SELECT 1 FROM execution_event WHERE kind='risk_mutation' LIMIT 1"
                    ).fetchone()
                    identities = connection.execute("SELECT identity FROM execution_run").fetchall()
                    if history is not None or any(
                        json.loads(row[0]).get("account_scope") != self._account
                        for row in identities
                    ):
                        raise RuntimeStorageUnavailable(
                            "unbound risk metadata has existing account history"
                        )
                    connection.execute(
                        "UPDATE local_risk_meta SET account_scope=? WHERE account_scope IS NULL",
                        (self._account,),
                    )
                    connection.execute(
                        "INSERT INTO local_account_risk(account_scope) VALUES (?)", (self._account,)
                    )
                elif metadata[0][1] != self._account:
                    raise RuntimeStorageUnavailable("risk store belongs to another account")
                if connection.execute("SELECT COUNT(*) FROM local_account_risk").fetchone()[0] != 1:
                    raise RuntimeStorageUnavailable(
                        "registered account risk row is missing or conflicting"
                    )
                if self.latch_reason(connection):
                    self._blocker.freeze()
                loss_row(connection, self._account)
                inherited = connection.execute(
                    "SELECT reserved_notional,filled_exposure FROM local_order_reservation WHERE account_scope=? AND run_id<>?",
                    (self._account, str(self._run)),
                ).fetchall()
                if any(Decimal(row[0]) != 0 or Decimal(row[1]) != 0 for row in inherited):
                    self._fault(
                        connection,
                        "new-owner",
                        "recovery_required",
                        {},
                        "inherited_risk_requires_reconciliation",
                    )
                pending = connection.execute(
                    "SELECT 1 FROM local_reduction_claim WHERE account_scope=? AND state IN ('prepared','dispatching','unknown') AND (run_id<>? OR process_generation<>?) LIMIT 1",
                    (self._account, str(self._run), lease.fence.process_generation),
                ).fetchone()
                if pending:
                    self._fault(
                        connection,
                        "inherited-close",
                        "recovery_required",
                        {},
                        "inherited_close_requires_reconciliation",
                    )
                if connection.execute(
                    "SELECT 1 FROM local_native_dispatch WHERE account_scope=? AND state IN ('prepared','dispatched') AND (run_id<>? OR process_generation<>?) LIMIT 1",
                    (self._account, str(self._run), lease.fence.process_generation),
                ).fetchone():
                    self._fault(
                        connection,
                        "inherited-native",
                        "recovery_required",
                        {},
                        "inherited_native_dispatch_requires_reconciliation",
                    )
        except Exception:
            self._blocker.freeze()
            raise

    def check_runtime(self, runtime_id: UUID) -> None:
        if runtime_id != self._run:
            self._blocker.freeze()
            raise RuntimeAuthorityError("risk request belongs to another local run")

    def latch_reason(self, connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT reason_code FROM local_account_risk WHERE account_scope=?", (self._account,)
        ).fetchone()
        if row is None:
            raise RuntimeStorageUnavailable("account risk state is missing")
        reason = row[0]
        if reason is not None:
            _token(reason, "risk_latch_reason")
            return str(reason)
        return None

    def _latch(self, connection: sqlite3.Connection, event_id: str, code: str) -> None:
        connection.execute(
            "UPDATE local_account_risk SET reason_code=COALESCE(reason_code,?),source_event_id=COALESCE(source_event_id,?) WHERE account_scope=?",
            (code, event_id, self._account),
        )
        self._blocker.freeze()

    def _fault(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        operation: str,
        request: Mapping[str, str | None],
        code: str,
    ) -> _Decision:
        fault_request = {**request, "source_event_id": event_id}
        record = RiskMutationRecord(operation, fault_request, {"error_code": code})
        fault_id = "risk-fault:" + hashlib.sha256(record.payload().encode()).hexdigest()
        sequence = self._events._append_risk(
            connection, lease=self._lease, identity=self._identity, event_id=fault_id, risk=record
        )
        self._latch(connection, fault_id, code)
        return _Decision(None, code, sequence)

    def _reject(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        request: Mapping[str, str | None],
        refusal: _Refused,
    ) -> _Decision:
        if refusal.loss_budget_id is not None:
            request = {**request, "budget_id": refusal.loss_budget_id}
        if not refusal.retryable:
            operation = "loss_fault" if refusal.loss_budget_id is not None else "risk_refused"
            return self._fault(connection, event_id, operation, request, refusal.code)
        record = RiskMutationRecord(
            "risk_refused", {**request, "source_event_id": event_id}, {"error_code": refusal.code}
        )
        identity = "risk-pending:" + hashlib.sha256(record.payload().encode()).hexdigest()
        sequence = self._events._append_risk(
            connection, lease=self._lease, identity=self._identity, event_id=identity, risk=record
        )
        return _Decision(None, refusal.code, sequence, retryable=True)

    def _gate(self, connection: sqlite3.Connection) -> _Refused | None:
        if self.latch_reason(connection) or self._blocker.is_frozen():
            return _Refused("local_risk_frozen")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            return _Refused("local_risk_clock_unreliable")
        permit = self._authorization.permit
        if not (_utc_instant(permit.not_before_utc) <= now < _utc_instant(permit.expires_at_utc)):
            return _Refused("local_permit_not_current")
        if refusal := loss_admission(
            connection,
            self._account,
            now=epoch_ns(now),
            maximum_account_age_ns=self._authorization.manifest.data_policy.max_account_age_seconds
            * 10**9,
            maximum_quote_age_ns=self._authorization.manifest.data_policy.max_quote_age_seconds
            * 10**9,
            maximum_loss=Fraction(Decimal(self._authorization.manifest.limits.max_loss_usd)),
            required_markets=self._authorization.manifest.instrument_ids,
        ):
            return _Refused(
                refusal.code, retryable=refusal.retryable, loss_budget_id=refusal.budget_id
            )
        return None

    def row(
        self, connection: sqlite3.Connection, order: str, *, current: bool = True
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM local_order_reservation WHERE account_scope=? AND client_order_id=?",
            (self._account, order),
        ).fetchone()
        if row is None:
            raise _Refused("local_order_reservation_missing")
        if current and row["run_id"] != str(self._run):
            raise _Refused("order_belongs_to_another_run")
        return cast(sqlite3.Row, row)

    @staticmethod
    def snapshot(row: sqlite3.Row) -> ExecutionReservationSnapshot:
        return _snapshot_document_read(
            {
                key: row[key]
                for key in (
                    "run_id",
                    "client_order_id",
                    "policy_id",
                    "reserved_notional",
                    "filled_exposure",
                    "filled_quantity",
                    "state",
                )
            }
        )

    def totals(self, connection: sqlite3.Connection) -> tuple[Decimal, Decimal]:
        rows = connection.execute(
            "SELECT reserved_notional,filled_exposure FROM local_order_reservation WHERE account_scope=?",
            (self._account,),
        ).fetchall()
        reserved = sum((_number(Decimal(row[0]), derived=True) for row in rows), Decimal(0))
        filled = sum((_number(Decimal(row[1]), derived=True) for row in rows), Decimal(0))
        return reserved, filled

    def _origin_run(
        self, connection: sqlite3.Connection, request: Mapping[str, str | None]
    ) -> str | None:
        rows = connection.execute(
            "SELECT DISTINCT run_id FROM local_order_reservation WHERE account_scope=? AND (client_order_id=? OR position_id=?)",
            (self._account, request.get("client_order_id"), request.get("position_id")),
        ).fetchall()
        return str(rows[0][0]) if len(rows) == 1 else None

    def _retain_fact(
        self,
        connection: sqlite3.Connection,
        identity: str,
        operation: str,
        request: Mapping[str, str | None],
        outcome: Mapping[str, str | None],
        sequence: int | None,
    ) -> None:
        if sequence is None:
            raise RuntimeStorageUnavailable("venue evidence has no audit sequence")
        connection.execute(
            "INSERT INTO local_venue_fill VALUES (?,?,?,?,?,?,?)",
            (
                self._account,
                identity,
                self._origin_run(connection, request),
                operation,
                _document(request),
                _document(outcome),
                sequence,
            ),
        )

    def retain_unapplied(
        self,
        *,
        event_id: str,
        operation: str,
        context: str,
        reducing: bool,
        raw: Mapping[str, object],
    ) -> None:
        self._blocker.freeze()
        observation = {
            key: {"type": type(value).__name__, "value": str(value)}
            for key, value in {
                **raw,
                "source_event_id": event_id,
                "source_context": context,
            }.items()
        }
        digest = hashlib.sha256(
            json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        request: dict[str, str | None] = {
            "input_digest": digest,
            "position_id": _audit_identifier(raw.get("position_id")),
            "client_order_id": _audit_identifier(context) if not reducing else None,
        }
        try:
            identity = self.fill_identity(event_id, reducing=reducing, context=None)
        except RuntimeAuthorityError:
            identity = None
        if identity:
            request["fill_identity"] = identity
        with self.transaction() as connection:
            decision = self._fault(
                connection,
                "unapplied:" + digest,
                "risk_refused",
                request,
                "local_venue_fact_unusable",
            )
            if (
                identity
                and connection.execute(
                    "SELECT 1 FROM local_venue_fill WHERE account_scope=? AND fill_identity=?",
                    (self._account, identity),
                ).fetchone()
                is None
            ):
                self._retain_fact(
                    connection,
                    identity,
                    operation,
                    request,
                    {"error_code": decision.error_code},
                    decision.audit_sequence,
                )

    def apply(
        self,
        operation: str,
        event_id: str,
        request: Mapping[str, str | None],
        change: Callable[[sqlite3.Connection], _Decision],
        *,
        increases_risk: bool = False,
        fill_identity: str | None = None,
        unbound_only: bool = False,
    ) -> ExecutionReservationSnapshot:
        _token(event_id, "event_id")
        decision = _Decision(None, "local_risk_operation_incomplete")
        try:
            with (
                localcontext(_MATH),
                self._events._runtime_transaction(
                    lease=self._lease, identity=self._identity
                ) as connection,
            ):
                reason = None
                if unbound_only:
                    metadata = connection.execute(
                        "SELECT native_account_id,client_id,native_journal_id FROM local_risk_meta"
                    ).fetchall()
                    if len(metadata) != 1 or (metadata[0][0] is None) != (metadata[0][1] is None):
                        raise RuntimeStorageUnavailable("local Native routing metadata is invalid")
                    if metadata[0][2] is not None and metadata[0][0] is None:
                        raise RuntimeStorageUnavailable(
                            "local Native journal has no routing binding"
                        )
                    if metadata[0][0] is not None:
                        reason = _Refused("local_native_mutation_requires_evidence")
                    elif connection.execute(
                        "SELECT 1 FROM local_reduction_claim UNION ALL SELECT 1 FROM local_native_dispatch LIMIT 1"
                    ).fetchone():
                        raise RuntimeStorageUnavailable("local Native routing history is unbound")
                if reason is None and increases_risk:
                    reason = self._gate(connection)
                fact = (
                    connection.execute(
                        "SELECT * FROM local_venue_fill WHERE account_scope=? AND fill_identity=?",
                        (self._account, fill_identity),
                    ).fetchone()
                    if fill_identity
                    else None
                )
                prior = self._events._find_risk(connection, run_id=self._run, event_id=event_id)
                if reason:
                    decision = self._reject(connection, event_id, request, reason)
                elif fact is not None:
                    if fact["original_run_id"] != str(self._run):
                        decision = self._fault(
                            connection,
                            event_id,
                            "cross_run_fact",
                            request,
                            "venue_fact_origin_unresolved"
                            if fact["original_run_id"] is None
                            else "venue_fact_belongs_to_another_run",
                        )
                    elif fact["action"] != operation or fact["request_json"] != _document(request):
                        decision = self._fault(
                            connection,
                            event_id,
                            "fact_conflict",
                            request,
                            "venue_fact_identity_conflict",
                        )
                    else:
                        outcome = json.loads(fact["outcome_json"])
                        original = connection.execute(
                            "SELECT event_id,payload FROM execution_event WHERE sequence=?",
                            (fact["audit_sequence"],),
                        ).fetchone()
                        if original is None:
                            raise RuntimeStorageUnavailable("venue fact audit record is missing")
                        record = RiskMutationRecord(**json.loads(original["payload"]))
                        audit_request = {
                            key: value
                            for key, value in record.request.items()
                            if key != "source_event_id"
                        }
                        if audit_request != request or dict(record.outcome) != outcome:
                            raise RuntimeStorageUnavailable("venue fact and audit disagree")
                        self._events._append_risk(
                            connection,
                            lease=self._lease,
                            identity=self._identity,
                            event_id=original[0],
                            risk=record,
                        )
                        decision = _Decision(
                            _snapshot_document_read(outcome) if "run_id" in outcome else None,
                            outcome.get("error_code"),
                        )
                elif prior is not None:
                    if fill_identity:
                        raise RuntimeStorageUnavailable(
                            "account venue identity is missing from an audited fill"
                        )
                    if prior.operation != operation or dict(prior.request) != request:
                        decision = self._fault(
                            connection,
                            event_id,
                            "fact_conflict",
                            request,
                            "risk_event_identity_conflict",
                        )
                    else:
                        self._events._append_risk(
                            connection,
                            lease=self._lease,
                            identity=self._identity,
                            event_id=event_id,
                            risk=prior,
                        )
                        decision = _Decision(
                            _snapshot_document_read(prior.outcome), prior.outcome.get("error_code")
                        )
                else:
                    connection.execute("SAVEPOINT local_risk_change")
                    try:
                        decision = change(connection)
                    except (_Refused, RuntimeAuthorityError, DecimalException, ValueError) as error:
                        connection.execute("ROLLBACK TO local_risk_change")
                        connection.execute("RELEASE local_risk_change")
                        if not isinstance(error, _Refused) and fill_identity is None:
                            raise
                        code = (
                            error.code
                            if isinstance(error, _Refused)
                            else "local_fact_application_unreliable"
                        )
                        decision = self._fault(connection, event_id, "risk_refused", request, code)
                        if fill_identity:
                            self._retain_fact(
                                connection,
                                fill_identity,
                                operation,
                                request,
                                {"error_code": code},
                                decision.audit_sequence,
                            )
                    else:
                        connection.execute("RELEASE local_risk_change")
                        if decision.snapshot is None:
                            raise RuntimeStorageUnavailable(
                                "risk mutation has no immutable outcome"
                            )
                        outcome = _snapshot_document(decision.snapshot)
                        if decision.error_code:
                            outcome["error_code"] = decision.error_code
                        record = RiskMutationRecord(operation, request, outcome)
                        sequence = self._events._append_risk(
                            connection,
                            lease=self._lease,
                            identity=self._identity,
                            event_id=event_id,
                            risk=record,
                        )
                        if fill_identity:
                            self._retain_fact(
                                connection, fill_identity, operation, request, outcome, sequence
                            )
                        if decision.error_code:
                            self._latch(connection, event_id, decision.error_code)
        except Exception:
            self._blocker.freeze()
            raise
        if decision.error_code:
            if decision.retryable:
                raise RuntimeRiskPending(decision.error_code)
            raise RuntimeAuthorityError(decision.error_code)
        if decision.snapshot is None:
            raise RuntimeStorageUnavailable("risk operation completed without an outcome")
        return decision.snapshot

    def fill_identity(self, event_id: str, *, reducing: bool, context: str | None) -> str:
        _token(event_id, "event_id")
        action = "fill_reduce" if reducing else "fill"
        prefix = f"runner-order-reservation:{self._run}:{action}:fill-source-v1:venue:"
        if not event_id.startswith(prefix):
            self._blocker.freeze()
            raise RuntimeAuthorityError("local fills require stable venue identity")
        match = re.fullmatch(r"([0-9a-f]{64}):(.+)", event_id[len(prefix) :])
        if match is None or (context is not None and match[2] != context):
            self._blocker.freeze()
            raise RuntimeAuthorityError("venue fill identity has invalid context")
        return match[1]

    @property
    def identity(self) -> ExecutionRunIdentity:
        return self._identity

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._events._runtime_transaction(
                lease=self._lease, identity=self._identity
            ) as connection:
                yield connection
        except Exception:
            self._blocker.freeze()
            raise
