"""Withdrawal details remain raw, account-bound observations."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from custos.engines.nautilus.ledger_http import decode_exact_venue_response
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

START = datetime(2026, 11, 19, tzinfo=UTC)
END = START + timedelta(seconds=1)
START_MS = (START - datetime(1970, 1, 1, tzinfo=UTC)).days * 86400000


def withdrawal(identity="9007199254740993", **changes):
    return {
        "wdId": identity,
        "ts": str(START_MS + 500),
        "amt": "1.000000000000000001",
        "fee": "0.0001",
        "feeCcy": "BTC",
        "ccy": "BTC",
        "state": "2",
        "toAddrType": "1",
        "to": "raw-recipient-address",
        "chain": "BTC-Bitcoin",
        "memo": "",
        "nonTradableAsset": False,
        "addrEx": {"comment": "raw-tag", "future": [None, True]},
    } | changes


class WithdrawalSource(OkxVenueLedgerSource):
    def __init__(self, records, *, identities=None, clock=START_MS + 2000):
        super().__init__(
            {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
            {
                "api_key": "fixture-key",
                "api_secret": "fixture-secret",
                "api_passphrase": "fixture-pass",
            },
        )
        self.records = records
        self.identities = iter(identities or [("42", "7"), ("42", "7")])
        self.clock = clock
        self.calls = []

    def _get_funding_exact(self, path, params, *, private=True):
        self.calls.append((path, dict(params), private))
        if path.endswith("/config"):
            uid, main = next(self.identities)
            data = [{"uid": uid, "mainUid": main}]
        elif path.endswith("/time"):
            data = [{"ts": str(self.clock)}]
            self.clock += 1
        else:
            assert path == "/api/v5/asset/withdrawal-history"
            assert set(params) == {"after", "before", "limit"} and params["limit"] == 100
            data = [
                row
                for row in self.records
                if int(params["before"]) < int(row["ts"]) < int(params["after"])
            ]
            data = sorted(data, key=lambda row: int(row["ts"]), reverse=True)[:100]
        return decode_exact_venue_response(json.dumps({"code": "0", "data": data}).encode())


async def test_raw_withdrawal_retains_nested_fields_without_wallet_or_cash_flow_claims():
    raw = withdrawal()
    source = WithdrawalSource([raw])
    result = await source.collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert (result.uid, result.main_uid) == ("42", "7")
    assert result.source_wallet_type is None
    assert result.cash_flows_complete is False
    assert result.retention_qualification == "unknown"
    assert result.timestamp_semantics == "withdrawal-request-submitted-at-not-completion"
    assert json.loads(result.raw_details[0]) == raw
    pages = [params for path, params, _ in source.calls if path.endswith("/withdrawal-history")]
    assert pages == [
        {"after": START_MS + 1000, "before": START_MS - 1, "limit": 100},
        {"after": START_MS + 501, "before": START_MS + 499, "limit": 100},
        {"after": START_MS + 500, "before": START_MS - 1, "limit": 100},
    ]
    assert result.pages[-1].row_count == 0


async def test_withdrawal_content_digest_is_reconstructible_from_public_fields():
    import hashlib

    result = await WithdrawalSource([withdrawal()]).collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    content = {
        "venue": "OKX",
        "source_profile": result.source_profile,
        "endpoint": "/api/v5/asset/withdrawal-history",
        "uid": result.uid,
        "main_uid": result.main_uid,
        "trading_mode": result.trading_mode,
        "api_origin": result.api_origin,
        "coverage_from": result.coverage_from.isoformat(),
        "closed_at": result.closed_at.isoformat(),
        "source_wallet_type": result.source_wallet_type,
        "retention_qualification": result.retention_qualification,
        "timestamp_semantics": result.timestamp_semantics,
        "details": [json.loads(raw) for raw in result.raw_details],
    }
    expected = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert result.content_digest == expected


async def test_partial_oldest_millisecond_is_reloaded_before_the_cursor_advances():
    newer = [withdrawal(str(n), ts=str(START_MS + 700)) for n in range(1, 100)]
    boundary = [withdrawal("100"), withdrawal("101")]
    source = WithdrawalSource(newer + boundary)
    result = await source.collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert len(result.raw_details) == 101
    assert {json.loads(row)["wdId"] for row in result.raw_details} == {
        str(n) for n in range(1, 102)
    }
    assert [page.row_count for page in result.pages] == [100, 2, 0]


async def test_ids_do_not_select_the_time_cursor_and_output_ignores_response_order():
    records = [withdrawal("90", ts=str(START_MS)), withdrawal("1", ts=str(START_MS + 999))]
    a = await WithdrawalSource(records).collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    b = await WithdrawalSource(
        list(reversed(records)), clock=START_MS + 5000
    ).collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")
    assert a.content_digest == b.content_digest
    assert [json.loads(row)["wdId"] for row in a.raw_details] == ["1", "90"]
    assert [(page.after, page.before) for page in a.pages] == [
        (START_MS + 1000, START_MS - 1),
        (START_MS + 1, START_MS - 1),
        (START_MS, START_MS - 1),
    ]

    class ResponseOrderSource(WithdrawalSource):
        def __init__(self, records):
            super().__init__(records)
            self.returned_ids = []

        def _get_funding_exact(self, path, params, *, private=True):
            response = super()._get_funding_exact(path, params, private=private)
            if path.endswith("/withdrawal-history"):
                self.returned_ids.append(tuple(row["wdId"] for row in response.data))
            return response

    same_millisecond = [withdrawal("90"), withdrawal("1")]
    forward = ResponseOrderSource(same_millisecond)
    reverse = ResponseOrderSource(list(reversed(same_millisecond)))
    first = await forward.collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    second = await reverse.collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert forward.returned_ids == [("90", "1"), ("90", "1"), ()]
    assert reverse.returned_ids == [("1", "90"), ("1", "90"), ()]
    assert [json.loads(row)["wdId"] for row in first.raw_details] == ["1", "90"]
    assert first.raw_details == second.raw_details
    assert first.content_digest == second.content_digest


async def test_two_observations_of_one_withdrawal_keep_independent_state_digests():
    pending = withdrawal(state="new-unrecognized-state", to="masked-recipient", toAddrType="future")
    del pending["fee"]
    del pending["feeCcy"]
    del pending["chain"]
    before = await WithdrawalSource([pending]).collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    after = await WithdrawalSource([withdrawal(state="2")]).collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert json.loads(before.raw_details[0]) == pending
    assert "fee" not in json.loads(before.raw_details[0])
    assert before.content_digest != after.content_digest
    assert before.source_wallet_type is None and after.cash_flows_complete is False


async def test_optional_nulls_empty_fields_and_non_uid_recipient_are_not_invented():
    raw = withdrawal(
        fee=None, feeCcy="", state="", to="masked@example.invalid", toAddrType="1", memo=None
    )
    raw.pop("amt")
    result = await WithdrawalSource([raw]).collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert json.loads(result.raw_details[0]) == raw
    assert "counterparty_uid" not in json.loads(result.raw_details[0])


async def test_unknown_json_numbers_retain_precision_type_and_nested_shape():
    from decimal import Decimal

    class ExactNumericSource(WithdrawalSource):
        def _get_funding_exact(self, path, params, *, private=True):
            response = super()._get_funding_exact(path, params, private=private)
            payload = json.dumps({"code": "0", "data": response.data}).encode()
            payload = payload.replace(
                b'"EXACT_NUMBER"', b"0.123456789012345678901234567890123456789"
            )
            payload = payload.replace(b'"HUGE_NUMBER"', b"1e400")
            return decode_exact_venue_response(payload)

    row = withdrawal(
        future={"a": ["EXACT_NUMBER", 9007199254740993, True, None, {"b": "HUGE_NUMBER"}]}
    )
    result = await ExactNumericSource([row]).collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    values = json.loads(result.raw_details[0], parse_float=Decimal)["future"]["a"]
    assert values == [
        Decimal("0.123456789012345678901234567890123456789"),
        9007199254740993,
        True,
        None,
        {"b": Decimal("1e400")},
    ]
    assert isinstance(values[0], Decimal) and type(values[1]) is int and type(values[2]) is bool


async def test_empty_window_still_checks_identity_time_and_actual_empty_history():
    source = WithdrawalSource([])
    result = await source.collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert result.raw_details == () and len(source.calls) == 5
    assert len(result.pages) == 1 and result.pages[0].row_count == 0


async def test_real_authenticated_withdrawal_gets_keep_exact_query_and_serial_rate(monkeypatch):
    import base64
    import hashlib
    import hmac
    import io

    from custos.engines.nautilus import ledger_http

    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(ledger_http, "time", clock)
    source = OkxVenueLedgerSource(
        {
            "connector": "okx",
            "trading_mode": "testnet",
            "pairs": ["BTC-USDT"],
            "nautilus_config": {"venue": {"region": "eea"}},
        },
        {
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        },
    )
    config = [{"uid": "42", "mainUid": "7"}]
    times = [{"ts": str(START_MS + 2000)}]
    data = iter([config, times, [withdrawal()], [withdrawal()], [], config, times])
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 15
            headers = {key.lower(): value for key, value in request.header_items()}
            target = request.full_url.removeprefix(source._http.base_url)
            assert request.get_method() == "GET"
            assert headers["x-simulated-trading"] == "1"
            if not target.startswith("/api/v5/public/time"):
                stamp = headers["ok-access-timestamp"]
                expected = base64.b64encode(
                    hmac.new(
                        b"fixture-secret", (stamp + "GET" + target).encode(), hashlib.sha256
                    ).digest()
                ).decode()
                assert headers["ok-access-sign"] == expected
            requests.append((clock.now, request.full_url))
            return io.BytesIO(json.dumps({"code": "0", "data": next(data)}).encode())

    source._funding_http._opener = Opener()
    result = await source.collect_withdrawal_details(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert len(requests) == 7
    assert all(b[0] - a[0] >= 1 for a, b in zip(requests, requests[1:], strict=False))
    assert result.trading_mode == "testnet" and result.api_origin == "https://eea.okx.com"
    assert "/withdrawal-history?after=" in requests[2][1]
    assert source._funding_http.before_request is None


@pytest.mark.parametrize("first_kind", ["funding", "withdrawal"])
async def test_funding_and_withdrawal_share_the_lock_without_clobbering_the_active_hook(first_kind):
    import asyncio
    import threading

    entered, release, queued = threading.Event(), threading.Event(), threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.guard = threading.Lock()
            self.attempts = 0

        def acquire(self, *, timeout):
            with self.guard:
                self.attempts += 1
                if self.attempts == 2:
                    queued.set()
            return self.lock.acquire(timeout=timeout)

        def release(self):
            self.lock.release()

    class MixedSource(WithdrawalSource):
        def _get_funding_exact(self, path, params, *, private=True):
            callback = self._funding_http.before_request
            assert callback is not None
            if not entered.is_set():
                entered.set()
                assert release.wait(2), "release the test-owned HTTP operation"
                assert self._funding_http.before_request is callback
            callback()
            if path.endswith("/bills-history"):
                self.calls.append((path, dict(params), private))
                return decode_exact_venue_response(b'{"code":"0","data":[]}')
            return super()._get_funding_exact(path, params, private=private)

    source = MixedSource([], identities=[("42", "7")] * 4)
    source._funding_window_lock = ObservedLock()
    first_method = (
        source.collect_funding_bills
        if first_kind == "funding"
        else source.collect_withdrawal_details
    )
    second_method = (
        source.collect_withdrawal_details
        if first_kind == "funding"
        else source.collect_funding_bills
    )
    first = asyncio.create_task(first_method(START, END, expected_uid="42", expected_main_uid="7"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(
            second_method(START, END, expected_uid="42", expected_main_uid="7")
        )
        assert await asyncio.to_thread(queued.wait, 2)
        assert source.calls == []
    finally:
        release.set()
        a = await asyncio.wait_for(first, 2)
        if second is not None:
            b = await asyncio.wait_for(second, 2)
    assert a.cash_flows_complete is False and b.cash_flows_complete is False
    assert len(source.calls) == 10 and source._funding_http.before_request is None
