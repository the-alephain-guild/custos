"""Exact raw withdrawal details; neither wallet assignment nor cash-flow classification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from custos.engines.nautilus.ledger_http import ExactVenueResponse, VenueLedgerError, decimal, rows
from custos.engines.nautilus.okx_bill_window import (
    FundingWindowBudget,
    _account,
    _bounds,
    _clock,
    _identity,
    _time,
)

MAX_WITHDRAWAL_PAGES = 1000
MAX_WITHDRAWAL_BYTES = 16 * 1024 * 1024
WITHDRAWAL_PROFILE = "withdrawal-history-overlapping-timestamp-v1"


@dataclass(frozen=True, slots=True)
class OkxWithdrawalPage:
    after: int
    before: int
    row_count: int
    payload_digest: str


@dataclass(frozen=True, slots=True)
class OkxWithdrawalWindow:
    uid: str
    main_uid: str
    trading_mode: str
    api_origin: str
    coverage_from: datetime
    closed_at: datetime
    collection_started_at: datetime
    observed_at: datetime
    raw_details: tuple[str, ...]
    pages: tuple[OkxWithdrawalPage, ...]
    account_payload_digests: tuple[str, str]
    content_digest: str

    @property
    def source_profile(self) -> str:
        return WITHDRAWAL_PROFILE

    @property
    def source_wallet_type(self) -> None:
        return None

    @property
    def retention_qualification(self) -> Literal["unknown"]:
        return "unknown"

    @property
    def timestamp_semantics(self) -> Literal["withdrawal-request-submitted-at-not-completion"]:
        return "withdrawal-request-submitted-at-not-completion"

    @property
    def cash_flows_complete(self) -> Literal[False]:
        return False


def _exact_json(value) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise VenueLedgerError("OKX withdrawal contains a nonfinite JSON number")
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_exact_json(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            "{"
            + ",".join(_exact_json(key) + ":" + _exact_json(value[key]) for key in sorted(value))
            + "}"
        )
    raise VenueLedgerError("OKX withdrawal contains an unsupported exact JSON value")


def collect_withdrawal_window(
    get: Callable[..., ExactVenueResponse],
    start: datetime,
    end: datetime,
    *,
    expected_uid: str,
    expected_main_uid: str,
    trading_mode: str,
    api_origin: str,
    budget: FundingWindowBudget,
) -> OkxWithdrawalWindow:
    start, end, first_ms, last_ms = _bounds(start, end)
    expected = _identity(expected_uid), _identity(expected_main_uid)

    def fetch(path, params, *, private=True):
        budget.check()
        response = get(path, params, private=private)
        budget.check()
        return response

    account_before = fetch("/api/v5/account/config", {})
    actual = _account(account_before)
    if actual != expected:
        raise VenueLedgerError("OKX withdrawal account differs from the expected identity")
    clock_before = _clock(fetch("/api/v5/public/time", {}, private=False))
    if clock_before < end:
        raise VenueLedgerError("OKX withdrawal interval exceeds server time")
    seen: dict[str, str] = {}
    pages = []
    size = 0

    def page(after, before):
        nonlocal size
        if len(pages) >= MAX_WITHDRAWAL_PAGES:
            raise VenueLedgerError("OKX withdrawal window exceeded bounded pagination")
        response = fetch(
            "/api/v5/asset/withdrawal-history", {"after": after, "before": before, "limit": 100}
        )
        values = rows(response.data)
        if len(values) > 100:
            raise VenueLedgerError("OKX withdrawal page exceeded the requested limit")
        pages.append(OkxWithdrawalPage(after, before, len(values), response.payload_digest))
        observed = {}
        for row in values:
            budget.check()
            identity = _identity(row.get("wdId"))
            at = _time(row.get("ts"))
            stamp = int(row["ts"])
            if not start <= at < end or not before < stamp < after:
                raise VenueLedgerError("OKX withdrawal escaped its requested timestamp interval")
            for field in ("amt", "fee"):
                value = row.get(field)
                if value is not None and value != "":
                    decimal(value, "withdrawal " + field)
            raw = _exact_json(row)
            size += len(raw.encode("ascii"))
            if size > MAX_WITHDRAWAL_BYTES:
                raise VenueLedgerError("OKX withdrawal window exceeded the scan content size limit")
            if identity in seen and seen[identity] != raw:
                raise VenueLedgerError("OKX withdrawal changed within one collection")
            seen[identity] = raw
            observed[identity] = stamp
        return observed, len(values)

    cursor = last_ms + 1
    while True:
        current, _ = page(cursor, first_ms - 1)
        if not current:
            break
        boundary = min(current.values())
        group, count = page(boundary + 1, boundary - 1)
        if count == 100:
            raise VenueLedgerError("OKX withdrawal timestamp group is saturated")
        if not {key for key, at in current.items() if at == boundary}.issubset(group):
            raise VenueLedgerError("OKX withdrawal boundary group omitted an observed identity")
        cursor = boundary
    account_after = fetch("/api/v5/account/config", {})
    if _account(account_after) != actual:
        raise VenueLedgerError("OKX withdrawal account changed during collection")
    clock_after = _clock(fetch("/api/v5/public/time", {}, private=False))
    if clock_after < clock_before:
        raise VenueLedgerError("OKX withdrawal server time moved backwards")
    ordered = tuple(seen[key] for key in sorted(seen, key=int))
    prefix = {
        "venue": "OKX",
        "source_profile": WITHDRAWAL_PROFILE,
        "endpoint": "/api/v5/asset/withdrawal-history",
        "uid": actual[0],
        "main_uid": actual[1],
        "trading_mode": trading_mode,
        "api_origin": api_origin,
        "coverage_from": start.isoformat(),
        "closed_at": end.isoformat(),
        "source_wallet_type": None,
        "retention_qualification": "unknown",
        "timestamp_semantics": "withdrawal-request-submitted-at-not-completion",
    }
    content = _exact_json(
        {**prefix, "details": [json.loads(raw, parse_float=Decimal) for raw in ordered]}
    )
    result = OkxWithdrawalWindow(
        *actual,
        trading_mode,
        api_origin,
        start,
        end,
        clock_before,
        clock_after,
        ordered,
        tuple(pages),
        (account_before.payload_digest, account_after.payload_digest),
        hashlib.sha256(content.encode("ascii")).hexdigest(),
    )
    budget.check()
    return result
