"""Bounded internal cash observations and audited account-loss storage reads."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, cast
from uuid import UUID

from custos.core.execution_risk_events import RiskMutationRecord, _token
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable

COLLATERAL_SCALE = 10**18


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def uint(value: object, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimeAuthorityError("cash observation integer is outside its bound")
    return value


def raw(value: object, *, cash: bool = False) -> int:
    return uint(value, maximum=2 ** (257 if cash else 256) - 1)


def epoch_ns(value: datetime) -> int:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise RuntimeAuthorityError("cash budget requires an aware UTC clock")
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return uint((delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000)


def fraction_text(value: Fraction) -> tuple[str, str]:
    if (
        type(value) is not Fraction
        or abs(value.numerator) >= 10**512
        or value.denominator >= 10**512
    ):
        raise RuntimeAuthorityError("cash loss fraction exceeds its storage bound")
    return str(value.numerator), str(value.denominator)


@dataclass(frozen=True)
class CashRiskObservation:
    journal_id: UUID
    checkpoint_sequence: int
    source_digest: str
    source_timestamp_ns: int
    cash_equity_raw: int
    cashflow_counters: tuple[int, int, int]
    position_ids: tuple[str, ...]
    unresolved_intent_ids: tuple[str, ...]
    queued_raw: int
    debt_raw: int
    cohort_ready: bool
    adverse_pnl: tuple[Fraction, ...]
    mark_timestamps_ns: tuple[int, ...]
    market_timestamps_ns: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if type(self.journal_id) is not UUID or self.journal_id.version != 4:
            raise RuntimeAuthorityError("cash observation requires the original journal UUID4")
        uint(self.checkpoint_sequence, minimum=1)
        uint(self.source_timestamp_ns)
        if (
            type(self.source_digest) is not str
            or re.fullmatch(r"0x[0-9a-f]{64}", self.source_digest) is None
        ):
            raise RuntimeAuthorityError("cash observation source digest is invalid")
        raw(self.cash_equity_raw, cash=True)
        raw(self.queued_raw)
        raw(self.debt_raw)
        if type(self.cashflow_counters) is not tuple or len(self.cashflow_counters) != 3:
            raise RuntimeAuthorityError("cash observation requires the original counter tuple")
        for value in self.cashflow_counters:
            raw(value)
        for values, maximum in ((self.position_ids, 64), (self.unresolved_intent_ids, 256)):
            if (
                type(values) is not tuple
                or len(values) > maximum
                or len(set(values)) != len(values)
            ):
                raise RuntimeAuthorityError(
                    "cash observation identities must be bounded and unique"
                )
            for identifier in values:
                _token(identifier, "cash_observation_identity")
        if (
            type(self.cohort_ready) is not bool
            or type(self.adverse_pnl) is not tuple
            or type(self.mark_timestamps_ns) is not tuple
        ):
            raise RuntimeAuthorityError("cash observation valuation must be immutable")
        if self.cohort_ready:
            if len(self.position_ids) != len(self.adverse_pnl) or len(self.position_ids) != len(
                self.mark_timestamps_ns
            ):
                raise RuntimeAuthorityError("cash valuation does not cover every position")
        elif self.adverse_pnl or self.mark_timestamps_ns:
            raise RuntimeAuthorityError(
                "pending cash observation cannot claim a complete valuation"
            )
        for pnl in self.adverse_pnl:
            fraction_text(pnl)
            if pnl > 0:
                raise RuntimeAuthorityError("unrealized gains cannot credit the cash loss budget")
        for stamp in self.mark_timestamps_ns:
            uint(stamp)

        if type(self.market_timestamps_ns) is not tuple or len(self.market_timestamps_ns) > 64:
            raise RuntimeAuthorityError("market observations must be a bounded immutable tuple")
        markets = set()
        for row in self.market_timestamps_ns:
            if type(row) is not tuple or len(row) != 2:
                raise RuntimeAuthorityError(
                    "market observations require immutable identity/time pairs"
                )
            identifier, stamp = row
            if (
                type(identifier) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\.PAPERTRADE", identifier)
                is None
                or identifier in markets
            ):
                raise RuntimeAuthorityError("market observation identity is invalid or duplicated")
            markets.add(identifier)
            uint(stamp)

    def source(self) -> dict[str, Any]:
        body = asdict(self)
        for key in (
            "cohort_ready",
            "adverse_pnl",
            "mark_timestamps_ns",
            "unresolved_intent_ids",
            "market_timestamps_ns",
        ):
            del body[key]
        body["journal_id"] = str(self.journal_id)
        body["cash_equity_raw"] = str(self.cash_equity_raw)
        body["queued_raw"] = str(self.queued_raw)
        body["debt_raw"] = str(self.debt_raw)
        body["cashflow_counters"] = [str(value) for value in self.cashflow_counters]
        return body

    def body(self) -> dict[str, Any]:
        return {
            **self.source(),
            "unresolved_intent_ids": self.unresolved_intent_ids,
            "cohort_ready": self.cohort_ready,
            "adverse_pnl": [fraction_text(pnl) for pnl in self.adverse_pnl],
            "mark_timestamps_ns": self.mark_timestamps_ns,
            "market_timestamps_ns": self.market_timestamps_ns,
        }


@dataclass(frozen=True)
class LossBudgetSnapshot:
    baseline_id: UUID
    baseline_raw: int | None
    state: str
    loss: Fraction
    pending_since_ns: int | None
    ready_until_ns: int | None
    reason_code: str | None


@dataclass(frozen=True)
class CashAdmissionRefusal:
    code: str
    retryable: bool
    budget_id: str


def audited_body(
    connection: sqlite3.Connection, sequence: object, operation: str, body: str, budget_id: str
) -> dict[str, Any]:
    uint(sequence, minimum=1)
    event = connection.execute(
        "SELECT * FROM execution_event WHERE sequence=?", (sequence,)
    ).fetchone()
    if event is None or event["kind"] != "risk_mutation":
        raise RuntimeStorageUnavailable("cash budget audit record is missing")
    payload = json.loads(event["payload"])
    record = RiskMutationRecord(**payload)
    if (
        record.operation != operation
        or record.request.get("budget_id") != budget_id
        or record.request.get("input_digest") != digest(body)
        or record.payload() != event["payload"]
    ):
        raise RuntimeStorageUnavailable("cash budget differs from its original audit")
    value = json.loads(body)
    if type(value) is not dict or canonical(value) != body or value.get("budget_id") != budget_id:
        raise RuntimeStorageUnavailable("cash budget payload is not canonical")
    return value


def loss_row(connection: sqlite3.Connection, account_scope: str) -> sqlite3.Row | None:
    metadata = connection.execute(
        "SELECT schema_version,account_scope,loss_budget_required,loss_budget_id FROM local_risk_meta"
    ).fetchall()
    if (
        len(metadata) != 1
        or metadata[0][0] != 5
        or metadata[0][1] != account_scope
        or metadata[0][2] not in (0, 1)
    ):
        raise RuntimeStorageUnavailable("cash budget metadata is unavailable")
    rows = connection.execute("SELECT * FROM local_account_loss").fetchall()
    history = connection.execute(
        "SELECT 1 FROM execution_event WHERE event_id LIKE 'local-loss:%' LIMIT 1"
    ).fetchone()
    required, identity = metadata[0][2], metadata[0][3]
    if not required:
        if identity is not None or rows or history:
            raise RuntimeStorageUnavailable("cash budget history cannot become unbound")
        return None
    if type(identity) is not str or str(UUID(identity)) != identity or UUID(identity).version != 4:
        raise RuntimeStorageUnavailable("cash budget UUID is invalid")
    if (
        len(rows) != 1
        or rows[0]["account_scope"] != account_scope
        or rows[0]["budget_id"] != identity
    ):
        raise RuntimeStorageUnavailable(
            "enabled cash budget is missing or belongs to another owner"
        )
    row = rows[0]
    durable_fault = connection.execute(
        "SELECT event_id,payload FROM execution_event WHERE kind='risk_mutation' AND json_extract(payload,'$.request.budget_id')=? AND json_extract(payload,'$.operation')='loss_fault' ORDER BY sequence DESC LIMIT 1",
        (identity,),
    ).fetchone()
    if durable_fault is not None:
        latch = connection.execute(
            "SELECT reason_code,source_event_id FROM local_account_risk WHERE account_scope=?",
            (account_scope,),
        ).fetchone()
        reason = json.loads(durable_fault[1])["outcome"]["error_code"]
        if latch is None or latch[0] != reason or latch[1] != durable_fault[0]:
            raise RuntimeStorageUnavailable(
                "cash budget fault differs from its persistent account latch"
            )
    enabled = connection.execute(
        "SELECT payload,kind FROM execution_event WHERE sequence=?",
        (row["enabled_audit_sequence"],),
    ).fetchone()
    expected = RiskMutationRecord(
        "loss_enable",
        {"account_scope": account_scope, "budget_id": identity},
        {"state": "uninitialized"},
    )
    if (
        enabled is None
        or enabled["kind"] != "risk_mutation"
        or enabled["payload"] != expected.payload()
    ):
        raise RuntimeStorageUnavailable("cash budget enable history differs")
    for name, operation in (("baseline", "loss_baseline"), ("observation", "loss_observe")):
        body, sequence = row[name + "_json"], row[name + "_audit_sequence"]
        first, latest = connection.execute(
            "SELECT MIN(sequence),MAX(sequence) FROM execution_event WHERE kind='risk_mutation' AND json_extract(payload,'$.request.budget_id')=? AND json_extract(payload,'$.operation')=?",
            (identity, operation),
        ).fetchone()
        if sequence != latest or (name == "baseline" and first != latest):
            raise RuntimeStorageUnavailable("cash budget does not reference its current audit head")
        if (body is None) != (sequence is None):
            raise RuntimeStorageUnavailable("cash budget payload and audit association differ")
        if body is not None:
            value = audited_body(connection, sequence, operation, body, identity)
            if name == "observation" and value["state"] == "latched":
                latch = connection.execute(
                    "SELECT reason_code,source_event_id FROM local_account_risk WHERE account_scope=?",
                    (account_scope,),
                ).fetchone()
                event_id = connection.execute(
                    "SELECT event_id FROM execution_event WHERE sequence=?", (sequence,)
                ).fetchone()[0]
                if (
                    latch is None
                    or not value["reason_code"]
                    or latch[0] != value["reason_code"]
                    or latch[1] != event_id
                ):
                    raise RuntimeStorageUnavailable(
                        "latched cash budget differs from its persistent account latch"
                    )
        if name == "observation" and sequence is not None:
            previous = connection.execute(
                "SELECT MAX(sequence) FROM execution_event WHERE kind='risk_mutation' AND json_extract(payload,'$.request.budget_id')=? AND json_extract(payload,'$.operation')='loss_observe' AND sequence<?",
                (identity, sequence),
            ).fetchone()[0]
            payload = connection.execute(
                "SELECT payload FROM execution_event WHERE sequence=?", (sequence,)
            ).fetchone()[0]
            if json.loads(payload)["request"].get("previous_audit_sequence") != str(previous or 0):
                raise RuntimeStorageUnavailable(
                    "cash budget transition does not extend its prior head"
                )
    if (row["baseline_json"] is None) != (row["observation_json"] is None):
        raise RuntimeStorageUnavailable("cash baseline and observation association differ")
    return cast(sqlite3.Row, row)


def loss_admission(
    connection: sqlite3.Connection,
    account_scope: str,
    *,
    now: int,
    maximum_account_age_ns: int,
    maximum_quote_age_ns: int,
    maximum_loss: Fraction,
    required_markets: tuple[str, ...],
) -> CashAdmissionRefusal | None:
    row = loss_row(connection, account_scope)
    if row is None:
        return None
    if row["baseline_json"] is None:
        return CashAdmissionRefusal("local_loss_baseline_missing", True, row["budget_id"])
    state = json.loads(row["observation_json"])
    if state["schema_version"] != 2 or state["state"] not in {"ready", "pending", "latched"}:
        raise RuntimeStorageUnavailable("cash budget observation state is invalid")
    if now < uint(state["observed_at_ns"]):
        return CashAdmissionRefusal("local_loss_clock_regressed", False, row["budget_id"])
    persisted_loss = Fraction(int(state["loss_numerator"]), int(state["loss_denominator"]))
    if persisted_loss >= maximum_loss:
        return CashAdmissionRefusal("local_loss_limit_reached", False, row["budget_id"])
    source_time = uint(state["observation"]["source_timestamp_ns"])
    if now < source_time or now >= source_time + maximum_account_age_ns:
        return CashAdmissionRefusal("local_loss_source_stale", False, row["budget_id"])
    if state["pending_since_ns"] is not None:
        pending = uint(state["pending_since_ns"])
        if now < pending or now >= pending + maximum_account_age_ns:
            return CashAdmissionRefusal("local_loss_pending_timeout", False, row["budget_id"])
    market_quotes = dict(state["observation"]["market_timestamps_ns"])
    if set(market_quotes) != set(required_markets):
        return CashAdmissionRefusal("local_loss_market_coverage_pending", True, row["budget_id"])
    if any(
        now < uint(stamp) or now >= stamp + maximum_quote_age_ns for stamp in market_quotes.values()
    ):
        return CashAdmissionRefusal("local_loss_market_unreliable", False, row["budget_id"])
    if state["pending_since_ns"] is not None:
        return CashAdmissionRefusal("local_loss_observation_pending", True, row["budget_id"])
    if state["state"] != "ready" or now >= uint(state["ready_until_ns"]):
        return CashAdmissionRefusal("local_loss_observation_stale", False, row["budget_id"])
    if any(
        now < uint(stamp) or now >= stamp + maximum_quote_age_ns
        for stamp in state["observation"]["mark_timestamps_ns"]
    ):
        return CashAdmissionRefusal("local_loss_mark_unreliable", False, row["budget_id"])
    return None
