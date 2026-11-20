"""Account-bound, read-only trading bills; raw observations are not cash-flow classification."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from custos.engines.nautilus.ledger_http import ExactVenueResponse, VenueLedgerError, decimal, rows

MAX_BILL_PAGES = 1000
MAX_BILL_WINDOW_BYTES = 16 * 1024 * 1024
SUPPORTED_BILL_AGE = timedelta(days=89)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FUNDING_HISTORY_START = datetime(2021, 2, 1, tzinfo=UTC)
MAX_FUNDING_WINDOW_SECONDS = 300
FUNDING_PROFILE: Final = "funding-history-bill-id-default-custody-v1"
FUNDING_CUSTODY_SCOPE: Final = "venue-default-not-all-custody-providers"


class FundingWindowBudget:
    """Acceptance deadline; an in-flight blocking read is not forcibly terminated."""

    def __init__(self, *, label: str = "funding"):
        self.deadline = time.monotonic() + MAX_FUNDING_WINDOW_SECONDS
        self.cancelled = threading.Event()
        self._label = label

    def remaining(self) -> float:
        if self.cancelled.is_set():
            raise VenueLedgerError(f"OKX {self._label} collection was cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise VenueLedgerError(f"OKX {self._label} window exceeded its time budget")
        return remaining

    def check(self) -> None:
        self.remaining()


@dataclass(frozen=True, slots=True)
class OkxBillPage:
    after: str | None
    row_count: int
    payload_digest: str


@dataclass(frozen=True, slots=True)
class OkxTradingBillWindow:
    uid: str
    main_uid: str
    trading_mode: str
    api_origin: str
    coverage_from: datetime
    closed_at: datetime
    collection_started_at: datetime
    observed_at: datetime
    raw_bills: tuple[str, ...]
    pages: tuple[OkxBillPage, ...]
    account_payload_digests: tuple[str, str]
    content_digest: str

    @property
    def wallet_type(self) -> Literal["trading"]:
        return "trading"

    @property
    def cash_flows_complete(self) -> Literal[False]:
        return False


@dataclass(frozen=True, slots=True)
class OkxFundingBillWindow:
    uid: str
    main_uid: str
    trading_mode: str
    api_origin: str
    coverage_from: datetime
    closed_at: datetime
    collection_started_at: datetime
    observed_at: datetime
    raw_bills: tuple[str, ...]
    pages: tuple[OkxBillPage, ...]
    account_payload_digests: tuple[str, str]
    content_digest: str

    @property
    def wallet_type(self) -> Literal["funding"]:
        return "funding"

    @property
    def source_profile(self) -> Literal["funding-history-bill-id-default-custody-v1"]:
        return FUNDING_PROFILE

    @property
    def custody_scope(self) -> Literal["venue-default-not-all-custody-providers"]:
        return FUNDING_CUSTODY_SCOPE

    @property
    def cash_flows_complete(self) -> Literal[False]:
        return False


def _identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or len(value) > 64
        or value.startswith("0")
    ):
        raise VenueLedgerError("OKX identity is not a positive decimal string")
    return value


def _time(value: object) -> datetime:
    identity = _identity(value)
    try:
        return _EPOCH + timedelta(milliseconds=int(identity))
    except OverflowError as error:
        raise VenueLedgerError("OKX timestamp is out of range") from error


def _bounds(start: datetime, end: datetime) -> tuple[datetime, datetime, int, int]:
    if any(
        not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
        for value in (start, end)
    ):
        raise VenueLedgerError("OKX bills require timezone-aware millisecond boundaries")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start.microsecond % 1000 or end.microsecond % 1000:
        raise VenueLedgerError("OKX bills require timezone-aware millisecond boundaries")
    if start >= end or start < _EPOCH:
        raise VenueLedgerError("OKX bills require an ordered positive interval")

    def milliseconds(value: datetime) -> int:
        delta = value - _EPOCH
        return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000

    return start, end, milliseconds(start), milliseconds(end) - 1


def _account(response: ExactVenueResponse) -> tuple[str, str]:
    data = rows(response.data)
    if len(data) != 1:
        raise VenueLedgerError("OKX bill account identity is ambiguous")
    return _identity(data[0].get("uid")), _identity(data[0].get("mainUid"))


def _clock(response: ExactVenueResponse) -> datetime:
    data = rows(response.data)
    if len(data) != 1:
        raise VenueLedgerError("OKX bill server time is ambiguous")
    return _time(data[0].get("ts"))


def _bill(row: dict, start: datetime, end: datetime) -> tuple[str, str]:
    # The venue defines bills as flat string fields. Preserve unknown strings,
    # including future bill types, without interpreting them as money movement.
    if any(not isinstance(value, str) for value in row.values()):
        raise VenueLedgerError("OKX bill fields must retain their original strings")
    identity = _identity(row.get("billId"))
    if not start <= _time(row.get("ts")) < end:
        raise VenueLedgerError("OKX bill escaped its requested interval")
    for name in ("type", "subType"):
        value = row.get(name)
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            raise VenueLedgerError("OKX bill type is missing or malformed")
    if not row.get("ccy"):
        raise VenueLedgerError("OKX bill currency is missing")
    decimal(row.get("balChg"), "bill balance change")
    fee = row.get("fee")
    if fee is None:
        raise VenueLedgerError("OKX bill fee field is missing")
    if fee != "":
        decimal(fee, "bill fee")
    return identity, json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def collect_trading_bill_window(
    get: Callable[..., ExactVenueResponse],
    start: datetime,
    end: datetime,
    *,
    expected_uid: str,
    expected_main_uid: str,
    trading_mode: str,
    api_origin: str,
) -> OkxTradingBillWindow:
    start, end, begin, finish = _bounds(start, end)
    expected = _identity(expected_uid), _identity(expected_main_uid)
    account_before = get("/api/v5/account/config", {})
    actual = _account(account_before)
    if actual != expected:
        raise VenueLedgerError("OKX bill account differs from the expected identity")
    clock_before = _clock(get("/api/v5/public/time", {}, private=False))
    if clock_before < end or clock_before - start > SUPPORTED_BILL_AGE:
        raise VenueLedgerError("OKX bill interval exceeds supported history or server time")
    result: dict[str, str] = {}
    pages = []
    cursor = None
    seen_cursors = set()
    size = 0
    for _ in range(MAX_BILL_PAGES):
        params = {"begin": begin, "end": finish, "limit": 100}
        if cursor is not None:
            params["after"] = cursor
        response = get("/api/v5/account/bills-archive", params)
        page = rows(response.data)
        if len(page) > 100:
            raise VenueLedgerError("OKX bill page exceeded the requested limit")
        page_ids = []
        for row in page:
            identity, raw = _bill(row, start, end)
            if cursor is not None and int(identity) > int(cursor):
                raise VenueLedgerError("OKX bill page escaped its earlier cursor")
            if identity in result:
                if result[identity] != raw:
                    raise VenueLedgerError("OKX bill changed within one collection")
            else:
                size += len(raw.encode("ascii"))
                if size > MAX_BILL_WINDOW_BYTES:
                    raise VenueLedgerError("OKX bill window exceeded the content size limit")
                result[identity] = raw
            page_ids.append(identity)
        pages.append(OkxBillPage(cursor, len(page), response.payload_digest))
        if cursor is not None and page_ids and min(map(int, page_ids)) >= int(cursor):
            raise VenueLedgerError("OKX bill cursor did not advance")
        if len(page) < 100:
            break
        next_cursor = min(page_ids, key=int)
        if next_cursor in seen_cursors or (cursor is not None and int(next_cursor) >= int(cursor)):
            raise VenueLedgerError("OKX bill cursor did not advance")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise VenueLedgerError("OKX bill window exceeded bounded pagination")

    account_after = get("/api/v5/account/config", {})
    if _account(account_after) != actual:
        raise VenueLedgerError("OKX bill account changed during collection")
    clock_after = _clock(get("/api/v5/public/time", {}, private=False))
    if clock_after < clock_before:
        raise VenueLedgerError("OKX bill server time moved backwards")
    if clock_after - start > SUPPORTED_BILL_AGE:
        raise VenueLedgerError("OKX bill collection exceeded supported history")
    ordered = tuple(result[key] for key in sorted(result, key=int))
    content = {
        "venue": "OKX",
        "wallet_type": "trading",
        "trading_mode": trading_mode,
        "api_origin": api_origin,
        "uid": actual[0],
        "main_uid": actual[1],
        "coverage_from": start.isoformat(),
        "closed_at": end.isoformat(),
        "bills": [json.loads(raw) for raw in ordered],
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OkxTradingBillWindow(
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
        digest,
    )


def _funding_bill(row: dict) -> tuple[str, datetime, str]:
    if any(not isinstance(value, str) for value in row.values()):
        raise VenueLedgerError("OKX funding bill fields must retain their original strings")
    identity = _identity(row.get("billId"))
    observed = _time(row.get("ts"))
    kind = row.get("type")
    if not isinstance(kind, str) or not kind.isascii() or not kind.isdigit():
        raise VenueLedgerError("OKX funding bill type is missing or malformed")
    if not row.get("ccy"):
        raise VenueLedgerError("OKX funding bill currency is missing")
    decimal(row.get("balChg"), "funding bill balance change")
    decimal(row.get("bal"), "funding bill balance")
    return (
        identity,
        observed,
        json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":")),
    )


def collect_funding_bill_window(
    get: Callable[..., ExactVenueResponse],
    start: datetime,
    end: datetime,
    *,
    expected_uid: str,
    expected_main_uid: str,
    trading_mode: str,
    api_origin: str,
    budget: FundingWindowBudget,
) -> OkxFundingBillWindow:
    start, end, _, _ = _bounds(start, end)
    if start < FUNDING_HISTORY_START:
        raise VenueLedgerError("OKX funding interval predates supported history")
    expected = _identity(expected_uid), _identity(expected_main_uid)

    def fetch(path, params, *, private=True):
        budget.check()
        response = get(path, params, private=private)
        budget.check()
        return response

    account_before = fetch("/api/v5/account/config", {})
    actual = _account(account_before)
    if actual != expected:
        raise VenueLedgerError("OKX funding account differs from the expected identity")
    clock_before = _clock(fetch("/api/v5/public/time", {}, private=False))
    if clock_before < end:
        raise VenueLedgerError("OKX funding interval exceeds server time")
    seen: dict[str, str] = {}
    selected: dict[str, str] = {}
    pages = []
    cursor = None
    size = 0
    for _ in range(MAX_BILL_PAGES):
        params = {"pagingType": "2", "limit": 100}
        if cursor is not None:
            params["after"] = cursor
        response = fetch("/api/v5/asset/bills-history", params)
        page = rows(response.data)
        if len(page) > 100:
            raise VenueLedgerError("OKX funding bill page exceeded the requested limit")
        pages.append(OkxBillPage(cursor, len(page), response.payload_digest))
        if not page:
            break
        identities = []
        for row in page:
            budget.check()
            identity, observed, raw = _funding_bill(row)
            if cursor is not None and int(identity) > int(cursor):
                raise VenueLedgerError("OKX funding page escaped its earlier cursor")
            size += len(raw.encode("ascii"))
            if size > MAX_BILL_WINDOW_BYTES:
                raise VenueLedgerError("OKX funding window exceeded the scan content size limit")
            if identity in seen and seen[identity] != raw:
                raise VenueLedgerError("OKX funding bill changed within one collection")
            seen[identity] = raw
            if start <= observed < end:
                selected[identity] = raw
            identities.append(identity)
        next_cursor = min(identities, key=int)
        if cursor is not None and int(next_cursor) >= int(cursor):
            raise VenueLedgerError("OKX funding cursor did not advance")
        cursor = next_cursor
    else:
        raise VenueLedgerError("OKX funding window exceeded bounded pagination")
    account_after = fetch("/api/v5/account/config", {})
    if _account(account_after) != actual:
        raise VenueLedgerError("OKX funding account changed during collection")
    clock_after = _clock(fetch("/api/v5/public/time", {}, private=False))
    if clock_after < clock_before:
        raise VenueLedgerError("OKX funding server time moved backwards")
    ordered = tuple(selected[key] for key in sorted(selected, key=int))
    content = {
        "venue": "OKX",
        "wallet_type": "funding",
        "source_profile": FUNDING_PROFILE,
        "endpoint": "/api/v5/asset/bills-history",
        "pagingType": "2",
        "custody_scope": FUNDING_CUSTODY_SCOPE,
        "trading_mode": trading_mode,
        "api_origin": api_origin,
        "uid": actual[0],
        "main_uid": actual[1],
        "coverage_from": start.isoformat(),
        "closed_at": end.isoformat(),
        "bills": [json.loads(raw) for raw in ordered],
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = OkxFundingBillWindow(
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
        digest,
    )
    budget.check()
    return result
