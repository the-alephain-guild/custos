"""Account-scoped reservation accounting inside the registered execution database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import (
    Decimal,
    localcontext,
)
from uuid import UUID

from custos.core.execution_events import (
    ExecutionEventStore,
)
from custos.core.execution_reservation_store import (
    ExecutionExposureSnapshot,
    ExecutionReservationSnapshot,
)
from custos.core.execution_risk_events import _token
from custos.core.local_runtime_state import RuntimeLease
from custos.core.reservation_math import (
    PositionCostLot,
    allocate_fifo_cost,
    notional_limit_breach,
    released_position_cost,
    remaining_order_reservation,
)
from custos.core.runtime_authority import RuntimeAuthorityError
from custos.local.authorization import VerifiedLocalRunAuthorization
from custos.local.reduction_claims import LocalReductionClaims
from custos.local.reservation_ledger import (
    _MATH,
    LocalReservationLedger,
    RiskBlocker,
    _Decision,
    _number,
    _Refused,
    _stored_reservation,
    _text,
)


class LocalReservationStore:
    def __init__(
        self,
        *,
        store: ExecutionEventStore,
        lease: RuntimeLease,
        authorization: VerifiedLocalRunAuthorization,
        risk_blocker: RiskBlocker,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ledger = LocalReservationLedger(
            store=store,
            lease=lease,
            authorization=authorization,
            risk_blocker=risk_blocker,
            clock=clock,
        )
        self._authorization = authorization
        self._blocker = risk_blocker
        self._run = authorization.manifest.run_id
        self._account = authorization.permit.account_scope
        self._policy = authorization.permit.permit_id
        self._limits = authorization.manifest.limits
        self._reductions: LocalReductionClaims | None = None

    def bind_reductions(self, *, account_id: str, client_id: str) -> LocalReductionClaims:
        candidate = LocalReductionClaims(
            ledger=self._ledger, account_id=account_id, client_id=client_id
        )
        if self._reductions is None:
            self._reductions = candidate
        return self._reductions

    @property
    def runtime_id(self) -> UUID:
        return self._run

    @property
    def policy_id(self) -> UUID:
        return self._policy

    @property
    def runtime_lease(self) -> RuntimeLease:
        return self._ledger._lease

    def require_opening_instrument(self, instrument_id: str) -> None:
        _token(instrument_id, "instrument_id")
        if instrument_id not in self._authorization.manifest.instrument_ids:
            raise RuntimeAuthorityError("local opening instrument is outside the signed manifest")

    def record_dispatch_unknown(self, client_order_id: str, *, reason: str) -> None:
        _token(client_order_id, "client_order_id")
        _token(reason, "reason")
        with self._ledger.transaction() as connection:
            self._ledger._fault(
                connection,
                "dispatch-unknown:" + client_order_id,
                "risk_refused",
                {"client_order_id": client_order_id, "reason": reason},
                "local_order_outcome_unknown",
            )

    def retain_unapplied_fill(
        self, *, event_id: str, context: str, reducing: bool, raw: Mapping[str, object]
    ) -> None:
        self._ledger.retain_unapplied(
            event_id=event_id,
            operation="reduce" if reducing else "fill",
            context=context,
            reducing=reducing,
            raw=raw,
        )

    @property
    def reductions(self) -> LocalReductionClaims:
        if self._reductions is None:
            self._blocker.freeze()
            raise RuntimeAuthorityError(
                "local reductions require the actual Native routing binding"
            )
        return self._reductions

    def reserve_order_notional_sync(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        client_order_id: str,
        policy_id: UUID,
        requested_notional: Decimal,
    ) -> ExecutionReservationSnapshot:
        self._ledger.check_runtime(runtime_id)
        _token(client_order_id, "client_order_id")
        amount = _number(requested_notional, positive=True)
        if policy_id != self._policy:
            raise RuntimeAuthorityError("reservation policy differs from local permit")
        request = {
            "client_order_id": client_order_id,
            "policy_id": str(policy_id),
            "requested_notional": _text(amount),
        }

        def change(connection: sqlite3.Connection) -> _Decision:
            reserved, filled = self._ledger.totals(connection)
            if notional_limit_breach(
                amount,
                reserved + filled + amount,
                Decimal(self._limits.max_order_notional_usd),
                Decimal(self._limits.max_total_notional_usd),
            ):
                raise _Refused("local_reservation_cap_exceeded")
            if connection.execute(
                "SELECT 1 FROM local_order_reservation WHERE account_scope=? AND client_order_id=?",
                (self._account, client_order_id),
            ).fetchone():
                raise _Refused("local_client_order_identity_reused")
            connection.execute(
                "INSERT INTO local_order_reservation VALUES (?,?,?,?,?,'0','0','reserved',NULL,NULL,NULL)",
                (self._account, client_order_id, str(self._run), str(self._policy), _text(amount)),
            )
            return _Decision(self._ledger.snapshot(self._ledger.row(connection, client_order_id)))

        return self._ledger.apply("reserve", event_id, request, change, increases_risk=True)

    def load_order_reservation_sync(
        self, runtime_id: UUID, client_order_id: str
    ) -> ExecutionReservationSnapshot:
        self._ledger.check_runtime(runtime_id)
        with (
            localcontext(_MATH),
            self._ledger.transaction() as connection,
        ):
            try:
                return self._ledger.snapshot(self._ledger.row(connection, client_order_id))
            except _Refused as error:
                raise RuntimeAuthorityError(error.code) from None

    def has_order_reservation_sync(self, runtime_id: UUID, client_order_id: str) -> bool:
        self._ledger.check_runtime(runtime_id)
        with self._ledger.transaction() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM local_order_reservation WHERE account_scope=? AND client_order_id=?",
                    (self._account, client_order_id),
                ).fetchone()
                is not None
            )

    def replace_order_reservation_sync(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        client_order_id: str,
        new_reserved_notional: Decimal,
    ) -> ExecutionReservationSnapshot:
        self._ledger.check_runtime(runtime_id)
        amount = _number(new_reserved_notional)
        request = {"client_order_id": client_order_id, "new_reserved_notional": _text(amount)}

        def change(connection: sqlite3.Connection) -> _Decision:
            row = self._ledger.row(connection, client_order_id)
            reserved, filled = self._ledger.totals(connection)
            own_cost = Decimal(row["filled_exposure"])
            if notional_limit_breach(
                own_cost + amount,
                reserved + filled - Decimal(row["reserved_notional"]) + amount,
                Decimal(self._limits.max_order_notional_usd),
                Decimal(self._limits.max_total_notional_usd),
            ):
                raise _Refused("local_replacement_cap_exceeded")
            state = (
                "partially_filled"
                if own_cost and amount
                else "filled"
                if own_cost
                else "reserved"
                if amount
                else "released"
            )
            connection.execute(
                "UPDATE local_order_reservation SET reserved_notional=?,state=? WHERE account_scope=? AND client_order_id=?",
                (_text(amount), state, self._account, client_order_id),
            )
            return _Decision(self._ledger.snapshot(self._ledger.row(connection, client_order_id)))

        return self._ledger.apply("replace", event_id, request, change, increases_risk=True)

    def release_order_reservation_sync(
        self, *, event_id: str, runtime_id: UUID, client_order_id: str, reason: str
    ) -> ExecutionReservationSnapshot:
        self._ledger.check_runtime(runtime_id)
        if reason not in {"rejected", "canceled", "expired"}:
            self._blocker.freeze()
            raise RuntimeAuthorityError("only a confirmed terminal result releases reservation")
        request = {"client_order_id": client_order_id, "reason": reason}

        def change(connection: sqlite3.Connection) -> _Decision:
            row = self._ledger.row(connection, client_order_id)
            state = "filled" if Decimal(row["filled_quantity"]) > 0 else "released"
            connection.execute(
                "UPDATE local_order_reservation SET reserved_notional='0',state=? WHERE account_scope=? AND client_order_id=?",
                (state, self._account, client_order_id),
            )
            return _Decision(self._ledger.snapshot(self._ledger.row(connection, client_order_id)))

        return self._ledger.apply("release", event_id, request, change)

    def record_order_fill_sync(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        client_order_id: str,
        fill_notional: Decimal,
        fill_quantity: Decimal,
        position_id: str | None = None,
        instrument_id: str | None = None,
        side: str | None = None,
        leaves_quantity: Decimal | None = None,
    ) -> ExecutionReservationSnapshot:
        self._ledger.check_runtime(runtime_id)
        try:
            identity = self._ledger.fill_identity(event_id, reducing=False, context=client_order_id)
            for name, value in (
                ("position_id", position_id),
                ("instrument_id", instrument_id),
                ("side", side),
            ):
                if value is not None:
                    _token(value, name)
            amount = _number(fill_notional, positive=True)
            quantity = _number(fill_quantity, positive=True)
            leaves = None if leaves_quantity is None else _number(leaves_quantity)
        except RuntimeAuthorityError:
            self._ledger.retain_unapplied(
                event_id=event_id,
                operation="fill",
                context=client_order_id,
                reducing=False,
                raw={
                    "client_order_id": client_order_id,
                    "fill_notional": fill_notional,
                    "fill_quantity": fill_quantity,
                    "leaves_quantity": leaves_quantity,
                    "position_id": position_id,
                    "instrument_id": instrument_id,
                    "side": side,
                },
            )
            raise RuntimeAuthorityError("local_venue_fact_unusable") from None
        request = {
            "client_order_id": client_order_id,
            "fill_identity": identity,
            "fill_notional": _text(amount),
            "fill_quantity": _text(quantity),
            "leaves_quantity": None if leaves is None else _text(leaves),
            "position_id": position_id,
            "instrument_id": instrument_id,
            "side": side,
        }

        def change(connection: sqlite3.Connection) -> _Decision:
            row = self._ledger.row(connection, client_order_id)
            if row["position_id"] is not None and (
                row["position_id"] != position_id
                or row["instrument_id"] != instrument_id
                or row["side"] != side
            ):
                raise _Refused("local_position_identity_conflict")
            remaining = _stored_reservation(
                remaining_order_reservation(
                    Decimal(row["reserved_notional"]), amount, quantity, leaves
                )
            )
            cost = Decimal(row["filled_exposure"]) + amount
            filled_quantity = Decimal(row["filled_quantity"]) + quantity
            state = "partially_filled" if remaining else "filled"
            connection.execute(
                "UPDATE local_order_reservation SET reserved_notional=?,filled_exposure=?,filled_quantity=?,state=?,position_id=?,instrument_id=?,side=? WHERE account_scope=? AND client_order_id=?",
                (
                    _text(remaining),
                    _text(cost),
                    _text(filled_quantity),
                    state,
                    position_id,
                    instrument_id,
                    side,
                    self._account,
                    client_order_id,
                ),
            )
            reserved, filled = self._ledger.totals(connection)
            breach = notional_limit_breach(
                cost + remaining,
                reserved + filled,
                Decimal(self._limits.max_order_notional_usd),
                Decimal(self._limits.max_total_notional_usd),
            )
            code = "local_post_trade_cap_exceeded" if breach else None
            if (
                instrument_id not in self._authorization.manifest.instrument_ids
                or side not in {"BUY", "SELL"}
                or not position_id
            ):
                code = code or "local_fill_scope_unreliable"
            return _Decision(
                self._ledger.snapshot(self._ledger.row(connection, client_order_id)), code
            )

        return self._ledger.apply("fill", event_id, request, change, fill_identity=identity)

    def record_position_reduction_sync(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        client_order_id: str,
        reduction_notional: Decimal,
        reduction_quantity: Decimal,
    ) -> ExecutionReservationSnapshot:
        return self._reduce(
            event_id=event_id,
            runtime_id=runtime_id,
            context=client_order_id,
            position_id=None,
            notional=reduction_notional,
            quantity=reduction_quantity,
        )

    def record_position_reduction_fifo_sync(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        position_id: str,
        reduction_notional: Decimal,
        reduction_quantity: Decimal,
    ) -> ExecutionReservationSnapshot:
        return self._reduce(
            event_id=event_id,
            runtime_id=runtime_id,
            context=position_id,
            position_id=position_id,
            notional=reduction_notional,
            quantity=reduction_quantity,
        )

    def _reduce(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        context: str,
        position_id: str | None,
        notional: Decimal,
        quantity: Decimal,
        closing_client_order_id: str | None = None,
        instrument_id: str | None = None,
        side: str | None = None,
    ) -> ExecutionReservationSnapshot:
        self._ledger.check_runtime(runtime_id)
        try:
            identity = self._ledger.fill_identity(event_id, reducing=True, context=context)
            amount = _number(notional, positive=True)
            closing = _number(quantity, positive=True)
            for name, value in (
                ("closing_client_order_id", closing_client_order_id),
                ("instrument_id", instrument_id),
                ("side", side),
            ):
                if value is not None:
                    _token(value, name)
        except RuntimeAuthorityError:
            self._ledger.retain_unapplied(
                event_id=event_id,
                operation="reduce",
                context=context,
                reducing=True,
                raw={
                    "reduction_notional": notional,
                    "reduction_quantity": quantity,
                    "position_id": position_id,
                    "closing_client_order_id": closing_client_order_id,
                },
            )
            raise RuntimeAuthorityError("local_venue_fact_unusable") from None
        request = {
            "fill_identity": identity,
            "position_id": position_id,
            "client_order_id": None if position_id else context,
            "reduction_notional": _text(amount),
            "reduction_quantity": _text(closing),
        }
        if closing_client_order_id is not None:
            request.update(
                closing_client_order_id=closing_client_order_id,
                instrument_id=instrument_id,
                side=side,
            )

        def change(connection: sqlite3.Connection) -> _Decision:
            bound_route = connection.execute(
                "SELECT native_account_id FROM local_risk_meta"
            ).fetchone()[0]
            if bound_route is not None and closing_client_order_id is None:
                raise _Refused("local_close_requires_persistent_claim")
            if closing_client_order_id is not None:
                if position_id is None or instrument_id is None or side is None:
                    raise _Refused("local_close_fill_identity_incomplete")
                self.reductions.validate_fill(
                    connection,
                    closing_client_order_id=closing_client_order_id,
                    position_id=position_id,
                    quantity=closing,
                    instrument_id=instrument_id,
                    side=side,
                )
            rows = (
                connection.execute(
                    "SELECT * FROM local_order_reservation WHERE account_scope=? AND run_id=? AND position_id=? ORDER BY client_order_id",
                    (self._account, str(self._run), position_id),
                ).fetchall()
                if position_id
                else [self._ledger.row(connection, context)]
            )
            rows = [row for row in rows if Decimal(row["filled_quantity"]) > 0]
            available = sum((Decimal(row["filled_quantity"]) for row in rows), Decimal(0))
            if available != closing or not rows:
                raise _Refused("local_full_position_reduction_required")
            lots = tuple(
                PositionCostLot(
                    row["client_order_id"],
                    Decimal(row["filled_quantity"]),
                    Decimal(row["filled_exposure"]),
                )
                for row in rows
            )
            allocations = allocate_fifo_cost(lots, closing)
            for row, allocation in zip(rows, allocations, strict=True):
                released = released_position_cost(
                    Decimal(row["filled_exposure"]),
                    Decimal(row["filled_quantity"]),
                    allocation.quantity,
                )
                remaining_cost = Decimal(row["filled_exposure"]) - released
                remaining_quantity = Decimal(row["filled_quantity"]) - allocation.quantity
                state = (
                    "partially_filled"
                    if Decimal(row["reserved_notional"])
                    else "filled"
                    if remaining_quantity
                    else "closed"
                )
                connection.execute(
                    "UPDATE local_order_reservation SET filled_exposure=?,filled_quantity=?,state=? WHERE account_scope=? AND client_order_id=?",
                    (
                        _text(remaining_cost),
                        _text(remaining_quantity),
                        state,
                        self._account,
                        row["client_order_id"],
                    ),
                )
            if closing_client_order_id is not None:
                self.reductions.settle_fill(connection, closing_client_order_id)
            return _Decision(
                self._ledger.snapshot(self._ledger.row(connection, rows[-1]["client_order_id"]))
            )

        return self._ledger.apply("reduce", event_id, request, change, fill_identity=identity)

    def record_claimed_reduction_sync(
        self,
        *,
        event_id: str,
        runtime_id: UUID,
        closing_client_order_id: str,
        position_id: str,
        instrument_id: str,
        side: str,
        reduction_notional: Decimal,
        reduction_quantity: Decimal,
    ) -> ExecutionReservationSnapshot:
        return self._reduce(
            event_id=event_id,
            runtime_id=runtime_id,
            context=closing_client_order_id,
            position_id=position_id,
            notional=reduction_notional,
            quantity=reduction_quantity,
            closing_client_order_id=closing_client_order_id,
            instrument_id=instrument_id,
            side=side,
        )

    async def load_runner_exposure(self, policy_id: UUID) -> ExecutionExposureSnapshot:
        if policy_id != self._policy:
            raise RuntimeAuthorityError("exposure policy differs from local permit")
        with (
            localcontext(_MATH),
            self._ledger.transaction() as connection,
        ):
            reserved, filled = self._ledger.totals(connection)
            maximum = Decimal(self._limits.max_total_notional_usd)
            return ExecutionExposureSnapshot(
                self._policy,
                filled,
                reserved,
                filled + reserved,
                maximum,
                filled + reserved <= maximum and not self._ledger.latch_reason(connection),
                None,
            )
