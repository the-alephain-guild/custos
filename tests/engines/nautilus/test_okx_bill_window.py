"""Trading bills keep their original account, values and coverage independently of observation time."""

import base64
import hashlib
import hmac
import io
import json
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta

from custos.engines.nautilus.ledger_http import decode_exact_venue_response
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

START = datetime(2026, 11, 19, tzinfo=UTC)
END = START + timedelta(seconds=1)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
START_MS = (START - EPOCH).days * 86400000


def bill(identity="9007199254740993", **changes):
    return {
        "billId": identity,
        "type": "1",
        "subType": "12",
        "ccy": "USDT",
        "ts": str(START_MS),
        "balChg": "100.000000000000000001",
        "fee": "",
        "from": "6",
        "to": "18",
    } | changes


class Source(OkxVenueLedgerSource):
    def __init__(self, pages, *, clock=START_MS + 2000, identities=None):
        super().__init__(
            {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
            {
                "api_key": "fixture-key",
                "api_secret": "fixture-secret",
                "api_passphrase": "fixture-pass",
            },
        )
        self.pages = pages
        self.clock = clock
        self.identities = iter(identities or [("42", "7"), ("42", "7")])
        self.calls = []

    def _get_exact(self, path, params, *, private=True):
        self.calls.append((path, params, private))
        if path.endswith("/config"):
            uid, main = next(self.identities)
            data = [{"uid": uid, "mainUid": main, "label": "not exported"}]
        elif path.endswith("/time"):
            data = [{"ts": str(self.clock)}]
            self.clock += 1
        else:
            assert path == "/api/v5/account/bills-archive"
            data = self.pages[params.get("after")]
        return decode_exact_venue_response(json.dumps({"code": "0", "data": data}).encode())


async def test_collects_original_trading_bills_without_symbol_type_or_currency_filter():
    original = bill()
    source = Source({None: [original]})
    result = await source.collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert (result.uid, result.main_uid, result.wallet_type, result.trading_mode) == (
        "42",
        "7",
        "trading",
        "testnet",
    )
    assert [json.loads(value) for value in result.raw_bills] == [original]
    assert result.coverage_from == START and result.closed_at == END
    assert not result.cash_flows_complete
    assert source.calls[2] == (
        "/api/v5/account/bills-archive",
        {"begin": START_MS, "end": START_MS + 999, "limit": 100},
        True,
    )
    assert result.pages[0].row_count == 1 and result.pages[0].after is None
    expected = json.dumps({"code": "0", "data": [original]}).encode()
    assert result.pages[0].payload_digest == hashlib.sha256(expected).hexdigest()
    assert "not exported" not in repr(result)


async def test_full_page_requires_a_terminal_page_and_keeps_same_millisecond_rows():
    page = [bill(str(9007199254741193 - index)) for index in range(100)]
    source = Source({None: page, page[-1]["billId"]: []})
    result = await source.collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert len(result.raw_bills) == 100
    assert [(item.after, item.row_count) for item in result.pages] == [
        (None, 100),
        (page[-1]["billId"], 0),
    ]
    assert len({json.loads(raw)["billId"] for raw in result.raw_bills}) == 100


async def test_content_digest_is_independent_of_clock_and_overlapping_page_grouping():
    first_page = [bill(str(identity)) for identity in range(201, 101, -1)]
    left = Source({None: first_page, "102": [bill("101")]})
    right_page = [bill("201")] + first_page[:-1]
    right = Source(
        {None: right_page, "103": [bill("103"), bill("102"), bill("101")]}, clock=START_MS + 9000
    )
    one = await left.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")
    two = await right.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert len(one.raw_bills) == len(two.raw_bills) == 101
    assert one.content_digest == two.content_digest
    assert one.pages != two.pages and one.observed_at != two.observed_at
    late = Source({None: [bill("102"), bill("101")]})
    initial = Source({None: [bill("101")]})
    before = await initial.collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    after = await late.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert before.content_digest != after.content_digest


async def test_empty_account_still_verifies_identity_and_terminal_page():
    source = Source({None: []})
    result = await source.collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert result.raw_bills == () and len(result.pages) == 1
    assert result.pages[0].row_count == 0 and len(source.calls) == 5
    assert not result.cash_flows_complete


async def test_unknown_currency_and_bill_type_are_preserved_without_classification():
    original = bill(type="99999", subType="88888", ccy="NEWASSET", fee="-0.0000000000000000001")
    source = Source({None: [original]})
    result = await source.collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )
    assert json.loads(result.raw_bills[0]) == original
    assert not result.cash_flows_complete


async def test_window_end_is_exclusive_and_offset_boundaries_keep_exact_milliseconds():
    from datetime import timezone

    offset = timezone(timedelta(hours=8))
    first = START + timedelta(milliseconds=1)
    last = START + timedelta(milliseconds=999)
    source = Source({None: [bill(ts=str(START_MS + 1))]})
    result = await source.collect_trading_bills(
        first.astimezone(offset), last.astimezone(offset), expected_uid="42", expected_main_uid="7"
    )
    assert result.coverage_from == first and result.closed_at == last
    assert source.calls[2][1] == {"begin": START_MS + 1, "end": START_MS + 998, "limit": 100}


async def test_network_reads_run_outside_the_event_loop_thread():
    loop_thread = threading.get_ident()

    class WorkerSource(Source):
        def _get_exact(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            return super()._get_exact(*args, **kwargs)

    await WorkerSource({None: []}).collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="7"
    )


async def test_fractional_local_offset_keeps_a_representable_utc_interval():
    from datetime import timezone

    offset = timezone(timedelta(microseconds=1))
    result = await Source({None: [bill()]}).collect_trading_bills(
        START.astimezone(offset), END.astimezone(offset), expected_uid="42", expected_main_uid="7"
    )
    assert result.coverage_from == START and result.closed_at == END


class Opener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 15
        return io.BytesIO(self.payload)


def authenticated_source(payload):
    source = OkxVenueLedgerSource(
        {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
        {
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        },
    )
    opener = Opener(payload)
    source._http._opener = opener
    source._http._minimum_interval = 0
    return source, opener


def test_authenticated_exact_get_signs_the_actual_path_and_preserves_payload_digest():
    payload = b'{"code":"0","data":[]}'
    source, opener = authenticated_source(payload)
    params = {"begin": START_MS, "end": START_MS + 999, "limit": 100, "after": "9007199254740993"}
    response = source._get_exact("/api/v5/account/bills-archive", params)
    (request,) = opener.requests
    target = "/api/v5/account/bills-archive?" + urllib.parse.urlencode(params)
    timestamp = request.get_header("Ok-access-timestamp")
    signature = base64.b64encode(
        hmac.new(b"fixture-secret", (timestamp + "GET" + target).encode(), hashlib.sha256).digest()
    ).decode()
    assert request.full_url == "https://openapi.okx.com" + target
    assert request.method == "GET" and request.data is None
    assert request.get_header("Ok-access-sign") == signature
    assert request.get_header("Ok-access-key") == "fixture-key"
    assert request.get_header("Ok-access-passphrase") == "fixture-pass"
    assert request.get_header("X-simulated-trading") == "1"
    assert request.get_header("User-agent").startswith("custos/")
    assert response.payload_digest == hashlib.sha256(payload).hexdigest()
    assert response.data == []
