"""Persist one account cash baseline and exact loss decisions with their audit."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any, Literal
from uuid import UUID, uuid4

from custos.core.execution_risk_events import RiskMutationRecord
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from custos.local.authorization import _utc_instant
from custos.local.loss_state import (
    COLLATERAL_SCALE,
    CashRiskObservation,
    LossBudgetSnapshot,
    canonical,
    digest,
    epoch_ns,
    fraction_text,
    loss_row,
)
from custos.local.reservation_ledger import LocalReservationLedger


class LocalLossBudget:
    def __init__(self, ledger: LocalReservationLedger) -> None:
        if not isinstance(ledger, LocalReservationLedger):
            raise RuntimeAuthorityError("cash budget requires the actual registered risk ledger")
        self._ledger = ledger
        self._scope = ledger.identity.account_scope
        with ledger.transaction() as connection:
            row = loss_row(connection, self._scope)
            if row is None:
                history = connection.execute(
                    "SELECT 1 FROM local_order_reservation UNION ALL SELECT 1 FROM local_native_dispatch UNION ALL SELECT 1 FROM local_venue_fill UNION ALL SELECT 1 FROM local_reduction_claim LIMIT 1"
                ).fetchone()
                events = connection.execute(
                    "SELECT payload FROM execution_event WHERE kind='risk_mutation'"
                ).fetchall()
                if history or any(
                    json.loads(event[0])["operation"] not in {"risk_refused"} for event in events
                ):
                    raise RuntimeStorageUnavailable(
                        "cash baseline requires a pre-trading account history"
                    )
                identity = str(uuid4())
                record = RiskMutationRecord(
                    "loss_enable",
                    {"account_scope": self._scope, "budget_id": identity},
                    {"state": "uninitialized"},
                )
                sequence = self._audit(connection, "local-loss:enable:" + identity, record)
                connection.execute(
                    "UPDATE local_risk_meta SET loss_budget_required=1,loss_budget_id=?",
                    (identity,),
                )
                connection.execute(
                    "INSERT INTO local_account_loss(account_scope,budget_id,enabled_audit_sequence) VALUES (?,?,?)",
                    (self._scope, identity, sequence),
                )
                row = loss_row(connection, self._scope)
            if row is None:
                raise RuntimeStorageUnavailable("cash budget was not bound")
            self._budget_id = row["budget_id"]

    def _audit(
        self, connection: sqlite3.Connection, event_id: str, record: RiskMutationRecord
    ) -> int:
        return self._ledger._events._append_risk(
            connection,
            lease=self._ledger._lease,
            identity=self._ledger.identity,
            event_id=event_id,
            risk=record,
        )

    def _row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = loss_row(connection, self._scope)
        if row is None or row["budget_id"] != self._budget_id:
            raise RuntimeStorageUnavailable("cash budget identity changed")
        return row

    def _snapshot(self, connection: sqlite3.Connection) -> LossBudgetSnapshot:
        row = self._row(connection)
        reason = self._ledger.latch_reason(connection)
        if row["baseline_json"] is None:
            return LossBudgetSnapshot(
                UUID(self._budget_id),
                None,
                "latched" if reason else "uninitialized",
                Fraction(0),
                None,
                None,
                reason,
            )
        baseline = json.loads(row["baseline_json"])
        state = json.loads(row["observation_json"])
        return LossBudgetSnapshot(
            UUID(self._budget_id),
            int(baseline["baseline_raw"]),
            "latched" if reason else state["state"],
            Fraction(int(state["loss_numerator"]), int(state["loss_denominator"])),
            state["pending_since_ns"],
            state["ready_until_ns"],
            reason,
        )

    def snapshot(self) -> LossBudgetSnapshot:
        with self._ledger.transaction() as connection:
            return self._snapshot(connection)

    def freeze_source(
        self, *, reason: Literal["account", "marks", "clock", "storage", "cohort"]
    ) -> LossBudgetSnapshot:
        if type(reason) is not str or reason not in {
            "account",
            "marks",
            "clock",
            "storage",
            "cohort",
        }:
            raise RuntimeAuthorityError("cash source failure requires a closed reason")
        with self._ledger.transaction() as connection:
            self._row(connection)
            if self._ledger.latch_reason(connection):
                return self._snapshot(connection)
            self._ledger._fault(
                connection,
                "local-loss:source:" + self._budget_id + ":" + reason,
                "loss_fault",
                {"budget_id": self._budget_id},
                "local_loss_" + reason + "_unavailable",
            )
            return self._snapshot(connection)

    def _source_reason(
        self,
        observation: CashRiskObservation,
        now: int,
        baseline: dict[str, Any] | None,
        previous: dict[str, Any] | None,
    ) -> str | None:
        if observation.journal_id != self._ledger._lease.journal_store.store_id:
            return "local_loss_journal_mismatch"
        maximum_age = (
            self._ledger._authorization.manifest.data_policy.max_account_age_seconds * 10**9
        )
        if observation.source_timestamp_ns > now:
            return "local_loss_source_future"
        if now >= observation.source_timestamp_ns + maximum_age:
            return "local_loss_source_stale"
        if observation.queued_raw or observation.debt_raw:
            return "local_loss_unsupported_funding"
        if (
            baseline is not None
            and [str(value) for value in observation.cashflow_counters]
            != baseline["cashflow_counters"]
        ):
            return "local_loss_cashflow_changed"
        if previous is not None:
            if now < previous["observed_at_ns"]:
                return "local_loss_clock_regressed"
            old = previous["observation"]
            if (
                observation.checkpoint_sequence < old["checkpoint_sequence"]
                or observation.source_timestamp_ns < old["source_timestamp_ns"]
            ):
                return "local_loss_source_regressed"
            source = {
                key: value
                for key, value in old.items()
                if key
                not in {
                    "cohort_ready",
                    "adverse_pnl",
                    "mark_timestamps_ns",
                    "unresolved_intent_ids",
                }
            }
            current = observation.source()
            if observation.checkpoint_sequence == old["checkpoint_sequence"] and canonical(
                source
            ) != canonical(current):
                return "local_loss_checkpoint_conflict"
            if observation.source_digest == old["source_digest"]:
                source.pop("checkpoint_sequence")
                current.pop("checkpoint_sequence")
                if canonical(source) != canonical(current):
                    return "local_loss_source_conflict"
        return None

    def initialize(self, observation: CashRiskObservation) -> LossBudgetSnapshot:
        if type(observation) is not CashRiskObservation:
            raise RuntimeAuthorityError("cash baseline requires an immutable internal observation")
        with self._ledger.transaction() as connection:
            row = self._row(connection)
            if row["baseline_json"] is not None:
                baseline = json.loads(row["baseline_json"])
                if baseline["observation_digest"] != digest(canonical(observation.body())):
                    raise RuntimeAuthorityError("the original cash baseline cannot be replaced")
                return self._snapshot(connection)
            if self._ledger.latch_reason(connection):
                return self._snapshot(connection)
            clock = self._ledger._clock()
            now = epoch_ns(clock)
            permit = self._ledger._authorization.permit
            if (
                not _utc_instant(permit.not_before_utc)
                <= clock
                < _utc_instant(permit.expires_at_utc)
            ):
                raise RuntimeAuthorityError("cash baseline requires a current operator permit")
            reason = self._source_reason(observation, now, None, None)
            history = connection.execute(
                "SELECT 1 FROM local_order_reservation UNION ALL SELECT 1 FROM local_native_dispatch UNION ALL SELECT 1 FROM local_venue_fill UNION ALL SELECT 1 FROM local_reduction_claim LIMIT 1"
            ).fetchone()
            if (
                history
                or observation.position_ids
                or observation.unresolved_intent_ids
                or not observation.cohort_ready
                or observation.cash_equity_raw == 0
            ):
                reason = reason or "local_loss_baseline_not_pretrade_flat"
            if reason:
                self._ledger._fault(
                    connection,
                    "local-loss:baseline:" + self._budget_id,
                    "loss_fault",
                    {"budget_id": self._budget_id},
                    reason,
                )
                return self._snapshot(connection)
            body = canonical(
                {
                    "schema_version": 1,
                    "budget_id": self._budget_id,
                    "account_scope": self._scope,
                    "journal_id": str(observation.journal_id),
                    "baseline_raw": str(observation.cash_equity_raw),
                    "cashflow_counters": [str(value) for value in observation.cashflow_counters],
                    "observation_digest": digest(canonical(observation.body())),
                    "initialized_at_ns": now,
                }
            )
            record = RiskMutationRecord(
                "loss_baseline",
                {"budget_id": self._budget_id, "input_digest": digest(body)},
                {"state": "ready"},
            )
            sequence = self._audit(connection, "local-loss:baseline:" + self._budget_id, record)
            connection.execute(
                "UPDATE local_account_loss SET baseline_json=?,baseline_audit_sequence=? WHERE account_scope=?",
                (body, sequence, self._scope),
            )
            self._write(
                connection,
                observation,
                now=now,
                state="ready",
                loss=Fraction(0),
                pending=None,
                until=observation.source_timestamp_ns
                + self._ledger._authorization.manifest.data_policy.max_account_age_seconds * 10**9,
                reason=None,
            )
            return self._snapshot(connection)

    def _write(
        self,
        connection: sqlite3.Connection,
        observation: CashRiskObservation,
        *,
        now: int,
        state: str,
        loss: Fraction,
        pending: int | None,
        until: int | None,
        reason: str | None,
        previous_watermarks: Mapping[str, int] | None = None,
    ) -> None:
        numerator, denominator = fraction_text(loss)
        watermarks = {
            identifier: stamp
            for identifier, stamp in (previous_watermarks or {}).items()
            if identifier in observation.position_ids
        }
        if observation.cohort_ready and reason is None:
            watermarks.update(
                zip(observation.position_ids, observation.mark_timestamps_ns, strict=True)
            )
        body = canonical(
            {
                "schema_version": 1,
                "budget_id": self._budget_id,
                "observation": observation.body(),
                "observed_at_ns": now,
                "state": state,
                "loss_numerator": numerator,
                "loss_denominator": denominator,
                "pending_since_ns": pending,
                "ready_until_ns": until,
                "reason_code": reason,
                "mark_watermarks_ns": watermarks,
            }
        )
        outcome = {"state": state}
        if reason is not None:
            outcome["error_code"] = reason
        previous = connection.execute(
            "SELECT observation_json,observation_audit_sequence FROM local_account_loss WHERE account_scope=?",
            (self._scope,),
        ).fetchone()
        if previous[0] == body:
            return
        head = str(previous[1] or 0)
        record = RiskMutationRecord(
            "loss_observe",
            {
                "budget_id": self._budget_id,
                "input_digest": digest(body),
                "previous_audit_sequence": head,
            },
            outcome,
        )
        event_id = "local-loss:observe:" + self._budget_id + ":" + head + ":" + digest(body)
        sequence = self._audit(connection, event_id, record)
        connection.execute(
            "UPDATE local_account_loss SET observation_json=?,observation_audit_sequence=? WHERE account_scope=?",
            (body, sequence, self._scope),
        )
        if reason is not None:
            self._ledger._latch(connection, event_id, reason)

    def observe(self, observation: CashRiskObservation) -> LossBudgetSnapshot:
        if type(observation) is not CashRiskObservation:
            raise RuntimeAuthorityError("cash budget requires an immutable internal observation")
        with self._ledger.transaction() as connection:
            row = self._row(connection)
            if row["baseline_json"] is None:
                raise RuntimeAuthorityError("cash budget has no original baseline")
            if self._ledger.latch_reason(connection):
                return self._snapshot(connection)
            baseline = json.loads(row["baseline_json"])
            previous = json.loads(row["observation_json"])
            now = epoch_ns(self._ledger._clock())
            reason = self._source_reason(observation, now, baseline, previous)
            cash_loss = Fraction(
                int(baseline["baseline_raw"]) - observation.cash_equity_raw, COLLATERAL_SCALE
            )
            loss = max(Fraction(0), cash_loss)
            limit = Fraction(Decimal(self._ledger._authorization.manifest.limits.max_loss_usd))
            pending = previous["pending_since_ns"]
            until = None
            if reason is None and cash_loss >= limit:
                reason = "local_loss_limit_reached"
            if (
                reason is None
                and pending is not None
                and now
                >= pending
                + self._ledger._authorization.manifest.data_policy.max_account_age_seconds * 10**9
            ):
                reason = "local_loss_pending_timeout"
            maximum_quote_age = (
                self._ledger._authorization.manifest.data_policy.max_quote_age_seconds * 10**9
            )
            watermarks = previous["mark_watermarks_ns"]
            if reason is None and observation.cohort_ready:
                if any(
                    stamp > now
                    or now >= stamp + maximum_quote_age
                    or stamp < watermarks.get(identifier, 0)
                    for identifier, stamp in zip(
                        observation.position_ids, observation.mark_timestamps_ns, strict=True
                    )
                ):
                    reason = "local_loss_mark_unreliable"
            if reason is None and (
                not observation.cohort_ready or observation.unresolved_intent_ids
            ):
                pending = now if pending is None else pending
                if (
                    now
                    >= pending
                    + self._ledger._authorization.manifest.data_policy.max_account_age_seconds
                    * 10**9
                ):
                    reason = "local_loss_pending_timeout"
                state = "pending"
            elif reason is None:
                loss = max(Fraction(0), cash_loss - sum(observation.adverse_pnl, Fraction(0)))
                if loss >= limit:
                    reason = "local_loss_limit_reached"
                if reason is None:
                    until = min(
                        (
                            observation.source_timestamp_ns
                            + self._ledger._authorization.manifest.data_policy.max_account_age_seconds
                            * 10**9,
                            *(
                                stamp + maximum_quote_age
                                for stamp in observation.mark_timestamps_ns
                            ),
                        )
                    )
                    pending = None
                state = "ready"
            else:
                state = "latched"
            if reason is not None:
                state = "latched"
                until = None
            self._write(
                connection,
                observation,
                now=now,
                state=state,
                loss=loss,
                pending=pending,
                until=until,
                reason=reason,
                previous_watermarks=watermarks,
            )
            return self._snapshot(connection)
