"""Funding observations preserve raw fields without claiming classified cash flow."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from custos.engines.nautilus import ledger_http
from custos.engines.nautilus.ledger_http import decode_exact_venue_response
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

START = datetime(2026, 11, 19, tzinfo=UTC)
END = START + timedelta(seconds=1)
START_MS = (START - datetime(1970, 1, 1, tzinfo=UTC)).days * 86400000


def funding_bill(identity="9007199254740993", **changes):
    return {
        "billId": identity,
        "ccy": "USDT",
        "clientId": "",
        "balChg": "100.000000000000000001",
        "bal": "200.000000000000000001",
        "type": "130",
        "ts": str(START_MS),
        "notes": "raw fixture",
    } | changes


class FundingSource(OkxVenueLedgerSource):
    def __init__(self, pages, *, identities=None, clock=START_MS + 2000):
        super().__init__(
            {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
            {
                "api_key": "fixture-key",
                "api_secret": "fixture-secret",
                "api_passphrase": "fixture-pass",
            },
        )
        self.pages = pages
        self.identities = iter(identities or [("42", "7"), ("42", "7")])
        self.clock = clock
        self.calls = []

    def _get_funding_exact(self, path, params, *, private=True):
        self.calls.append((path, params, private))
        if path.endswith("/config"):
            uid, main = next(self.identities)
            data = [{"uid": uid, "mainUid": main, "label": "not exported"}]
        elif path.endswith("/time"):
            data = [{"ts": str(self.clock)}]
            self.clock += 1
        else:
            assert path == "/api/v5/asset/bills-history"
            data = self.pages[params.get("after")]
        return decode_exact_venue_response(json.dumps({"code": "0", "data": data}).encode())


async def test_funding_window_keeps_original_strings_and_requires_real_empty_page():
    raw = funding_bill()
    source = FundingSource({None: [raw], raw["billId"]: []})
    result = await source.collect_funding_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert (result.uid, result.main_uid, result.wallet_type, result.trading_mode) == (
        "42",
        "7",
        "funding",
        "testnet",
    )
    assert [json.loads(value) for value in result.raw_bills] == [raw]
    assert result.coverage_from == START and result.closed_at == END
    assert not result.cash_flows_complete
    assert [page.row_count for page in result.pages] == [1, 0]
    assert source.calls[2:4] == [
        ("/api/v5/asset/bills-history", {"pagingType": "2", "limit": 100}, True),
        (
            "/api/v5/asset/bills-history",
            {"pagingType": "2", "limit": 100, "after": raw["billId"]},
            True,
        ),
    ]
    assert "not exported" not in repr(result)


async def test_funding_profile_does_not_claim_unknown_custody_provider_coverage():
    result = await FundingSource({None: []}).collect_funding_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert result.source_profile == "funding-history-bill-id-default-custody-v1"
    assert result.custody_scope == "venue-default-not-all-custody-providers"
    assert not result.cash_flows_complete


async def test_funding_public_fields_reconstruct_the_canonical_content_digest():
    result = await FundingSource(
        {None: [funding_bill()], "9007199254740993": []}
    ).collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    content = {
        "venue": "OKX",
        "wallet_type": result.wallet_type,
        "source_profile": result.source_profile,
        "endpoint": "/api/v5/asset/bills-history",
        "pagingType": "2",
        "custody_scope": result.custody_scope,
        "trading_mode": result.trading_mode,
        "api_origin": result.api_origin,
        "uid": result.uid,
        "main_uid": result.main_uid,
        "coverage_from": result.coverage_from.isoformat(),
        "closed_at": result.closed_at.isoformat(),
        "bills": [json.loads(row) for row in result.raw_bills],
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == result.content_digest


async def test_same_millisecond_many_ids_and_short_pages_continue_to_empty():
    page = [funding_bill(str(value)) for value in range(300, 200, -1)]
    source = FundingSource({None: page, "201": [page[-1], funding_bill("200")], "200": []})
    result = await source.collect_funding_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert len(result.raw_bills) == 101
    assert [p.row_count for p in result.pages] == [100, 2, 0]


async def test_bill_id_order_does_not_imply_time_order_and_bounds_are_half_open():
    source = FundingSource(
        {
            None: [
                funding_bill("50", ts=str(START_MS - 1)),
                funding_bill("49", ts=str(START_MS + 1000)),
            ],
            "49": [funding_bill("48"), funding_bill("47", ts=str(START_MS + 999))],
            "47": [],
        }
    )
    result = await source.collect_funding_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert [json.loads(v)["billId"] for v in result.raw_bills] == ["47", "48"]


async def test_funding_digest_ignores_page_grouping_and_clock_but_detects_late_rows():
    one = FundingSource({None: [funding_bill("4"), funding_bill("3")], "3": []})
    two = FundingSource(
        {None: [funding_bill("4")], "4": [funding_bill("3")], "3": []}, clock=START_MS + 5000
    )
    a = await one.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    b = await two.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert a.content_digest == b.content_digest and a.pages != b.pages
    late = await FundingSource(
        {None: [funding_bill("5"), funding_bill("4"), funding_bill("3")], "3": []}
    ).collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert late.content_digest != a.content_digest


async def test_same_source_serializes_workers_even_after_first_awaiter_is_cancelled():
    entered = threading.Event()
    release = threading.Event()
    attempted_second = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.counter_lock = threading.Lock()
            self.attempts = 0

        def acquire(self, *, timeout):
            with self.counter_lock:
                self.attempts += 1
                if self.attempts == 2:
                    attempted_second.set()
            return self.lock.acquire(timeout=timeout)

        def release(self):
            self.lock.release()

    class BlockedSource(FundingSource):
        def _get_funding_exact(self, path, params, *, private=True):
            if not entered.is_set():
                entered.set()
                assert release.wait(2), "release the test-owned worker"
            return super()._get_funding_exact(path, params, private=private)

    source = BlockedSource({None: []}, identities=[("42", "7")] * 4)
    source._funding_window_lock = ObservedLock()
    first = asyncio.create_task(
        source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    )
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(
            source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
        )
        assert await asyncio.to_thread(attempted_second.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert source.calls == [], "the cancelled worker still owns the serial read scope"
        release.set()
        result = await asyncio.wait_for(second, 2)
        assert result.raw_bills == () and len(source.calls) == 6
    finally:
        release.set()
        if second is not None and not second.done():
            await asyncio.wait_for(second, 2)


async def test_real_authenticated_funding_gets_enforce_the_per_source_second(monkeypatch):
    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now

        def sleep(self, delay):
            self.now += delay

    clock = Clock()
    monkeypatch.setattr(ledger_http, "time", clock)
    source = OkxVenueLedgerSource(
        {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
        {
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        },
    )
    requests = []
    values = iter(
        [
            [{"uid": "42", "mainUid": "7"}],
            [{"ts": str(START_MS + 2000)}],
            [funding_bill()],
            [],
            [{"uid": "42", "mainUid": "7"}],
            [{"ts": str(START_MS + 2001)}],
        ]
    )

    class Opener:
        def open(self, request, timeout):
            assert timeout == 15 and request.get_method() == "GET"
            requests.append((clock.now, request))
            if "/public/time" not in request.full_url:
                headers = {k.lower(): v for k, v in request.header_items()}
                stamp = headers["ok-access-timestamp"]
                target = request.full_url.removeprefix(source._http.base_url)
                expected = base64.b64encode(
                    hmac.new(
                        b"fixture-secret", (stamp + "GET" + target).encode(), hashlib.sha256
                    ).digest()
                ).decode()
                assert headers["ok-access-sign"] == expected
                assert headers["x-simulated-trading"] == "1"
            return io.BytesIO(json.dumps({"code": "0", "data": next(values)}).encode())

    source._funding_http._opener = Opener()
    result = await source.collect_funding_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert len(result.raw_bills) == 1 and len(requests) == 6
    assert all(b[0] - a[0] >= 1 for a, b in zip(requests, requests[1:], strict=False))
    assert source._http._minimum_interval == 0.45, "trading transport is unchanged"


async def test_funding_old_history_and_unknown_strings_are_preserved_without_trading_ttl():
    old = START - timedelta(days=100)
    stamp = (old - datetime(1970, 1, 1, tzinfo=UTC)).days * 86400000
    raw = funding_bill(type="99999", ccy="NEWASSET", ts=str(stamp), notes="", extra="future")
    del raw["clientId"]
    result = await FundingSource({None: [raw], raw["billId"]: []}).collect_funding_bills(
        old, old + timedelta(seconds=1), expected_uid="42", expected_main_uid="7"
    )
    assert json.loads(result.raw_bills[0]) == raw
    assert "fee" not in raw and "subType" not in raw


async def test_cancel_during_real_transport_rate_wait_does_not_open_a_request(monkeypatch):
    waiting = threading.Event()
    release = threading.Event()
    opened = []

    class Clock:
        def monotonic(self):
            return 10.0

        def sleep(self, delay):
            assert delay == 1.0
            waiting.set()
            assert release.wait(2), "release the test-owned rate wait"

    class Opener:
        def open(self, *_args, **_kwargs):
            opened.append(True)
            raise OSError("a cancelled request must not be opened")

    monkeypatch.setattr(ledger_http, "time", Clock())
    source = OkxVenueLedgerSource(
        {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
        {
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        },
    )
    source._funding_http._opener = Opener()
    source._funding_http._last_request = 10.0
    task = asyncio.create_task(
        source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        acquired = await asyncio.to_thread(source._funding_window_lock.acquire, timeout=2)
        assert acquired, "the cancelled worker must actually finish and retire its lock"
        source._funding_window_lock.release()
    assert opened == []


async def test_cancelled_queued_window_never_reads_after_the_active_window_finishes():
    entered = threading.Event()
    release = threading.Event()
    queued = threading.Event()
    retired = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.guard = threading.Lock()
            self.attempts = 0
            self.releases = 0

        def acquire(self, *, timeout):
            with self.guard:
                self.attempts += 1
                if self.attempts == 2:
                    queued.set()
            return self.lock.acquire(timeout=timeout)

        def release(self):
            with self.guard:
                self.releases += 1
                if self.releases == 2:
                    retired.set()
            self.lock.release()

    class BlockedSource(FundingSource):
        def _get_funding_exact(self, path, params, *, private=True):
            if not entered.is_set():
                entered.set()
                assert release.wait(2), "release the test-owned HTTP operation"
            return super()._get_funding_exact(path, params, private=private)

    source = BlockedSource({None: []})
    source._funding_window_lock = ObservedLock()
    first = asyncio.create_task(
        source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    )
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(
            source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
        )
        assert await asyncio.to_thread(queued.wait, 2)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert source.calls == []
    finally:
        release.set()
        await asyncio.wait_for(first, 2)
        if second is not None:
            assert await asyncio.to_thread(retired.wait, 2)
    assert len(source.calls) == 5
