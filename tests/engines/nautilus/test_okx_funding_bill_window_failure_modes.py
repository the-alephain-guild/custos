"""A funding read never turns partial, malformed or cross-account history into success."""

import hashlib
import json
from datetime import timedelta

import pytest

from custos.engines.nautilus import okx_bill_window
from custos.engines.nautilus.ledger_http import VenueLedgerError, decode_exact_venue_response
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource
from tests.engines.nautilus.test_okx_funding_bill_window import (
    END,
    START,
    START_MS,
    FundingSource,
    funding_bill,
)


@pytest.mark.parametrize("identity", [("43", "7"), ("42", "8"), ("", "7"), (42, "7")])
async def test_funding_rejects_actual_account_mismatch(identity):
    source = FundingSource({None: []}, identities=[identity])
    with pytest.raises(VenueLedgerError):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert len(source.calls) == 1


@pytest.mark.parametrize("uid", ["", "0", "042", True, 42, "42\n", "٤٢"])
async def test_funding_rejects_invalid_expected_account_before_network(uid):
    source = FundingSource({None: []})
    with pytest.raises(VenueLedgerError):
        await source.collect_funding_bills(START, END, expected_uid=uid, expected_main_uid="7")
    assert not source.calls


async def test_funding_rejects_account_change_after_empty_terminal_page():
    source = FundingSource({None: []}, identities=[("42", "7"), ("43", "7")])
    with pytest.raises(VenueLedgerError, match="account changed"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_funding_refuses_a_future_window():
    with pytest.raises(VenueLedgerError, match="server time"):
        await FundingSource({None: []}, clock=START_MS).collect_funding_bills(
            START, END, expected_uid="42", expected_main_uid="7"
        )


async def test_funding_refuses_server_clock_reversal_after_exhaustion():
    class BackwardSource(FundingSource):
        def _get_funding_exact(self, path, params, *, private=True):
            value = super()._get_funding_exact(path, params, private=private)
            if path.endswith("/time"):
                self.clock -= 2
            return value

    with pytest.raises(VenueLedgerError, match="backwards"):
        await BackwardSource({None: []}).collect_funding_bills(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"billId": "0"},
        {"billId": "01"},
        {"billId": 9007199254740993},
        {"ts": "1.5"},
        {"ts": "0"},
        {"ccy": ""},
        {"type": ""},
        {"type": "no"},
        {"balChg": "NaN"},
        {"balChg": 1.5},
        {"bal": "Infinity"},
        {"bal": None},
        {"extra": {}},
        {"notes": True},
    ],
)
async def test_funding_rejects_malformed_raw_fields(changes):
    source = FundingSource({None: [funding_bill(**changes)]})
    with pytest.raises(VenueLedgerError):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")


@pytest.mark.parametrize(
    "start,end",
    [
        (START.replace(tzinfo=None), END),
        (START, END.replace(tzinfo=None)),
        (START + timedelta(microseconds=1), END),
        (START, END + timedelta(microseconds=1)),
        (END, START),
        (START, START),
        (okx_bill_window.FUNDING_HISTORY_START - timedelta(milliseconds=1), END),
    ],
)
async def test_funding_rejects_invalid_time_boundaries_before_network(start, end):
    source = FundingSource({None: []})
    with pytest.raises(VenueLedgerError):
        await source.collect_funding_bills(start, end, expected_uid="42", expected_main_uid="7")
    assert not source.calls


async def test_funding_rejects_a_changed_duplicate_even_outside_requested_window():
    source = FundingSource(
        {
            None: [funding_bill("5", ts=str(START_MS - 1))],
            "5": [funding_bill("5", ts=str(START_MS - 1), bal="201"), funding_bill("4")],
        }
    )
    with pytest.raises(VenueLedgerError, match="changed within"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")


@pytest.mark.parametrize("next_page", [[funding_bill("6")], [funding_bill("5")]])
async def test_funding_rejects_cursor_escape_or_nonprogress_on_a_short_page(next_page):
    source = FundingSource({None: [funding_bill("5")], "5": next_page})
    with pytest.raises(VenueLedgerError, match="cursor"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_funding_page_budget_cannot_turn_a_short_page_into_exhaustion(monkeypatch):
    monkeypatch.setattr(okx_bill_window, "MAX_BILL_PAGES", 1)
    source = FundingSource({None: [funding_bill()], "9007199254740993": []})
    with pytest.raises(VenueLedgerError, match="bounded pagination"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_funding_scan_budget_counts_discarded_and_duplicate_rows(monkeypatch):
    row = funding_bill(ts=str(START_MS - 1))
    raw = json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    monkeypatch.setattr(okx_bill_window, "MAX_BILL_WINDOW_BYTES", 2 * len(raw.encode()) - 1)
    source = FundingSource({None: [row, row]})
    with pytest.raises(VenueLedgerError, match="scan content size"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_funding_deadline_checks_after_each_blocking_read(monkeypatch):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(okx_bill_window, "time", clock)
    monkeypatch.setattr(okx_bill_window, "MAX_FUNDING_WINDOW_SECONDS", 1)

    class SlowSource(FundingSource):
        def _get_funding_exact(self, *args, **kwargs):
            value = super()._get_funding_exact(*args, **kwargs)
            clock.now += 2
            return value

    source = SlowSource({None: []})
    with pytest.raises(VenueLedgerError, match="time budget"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert len(source.calls) == 1


async def test_funding_result_budget_is_checked_after_local_digest_work(monkeypatch):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

    clock = Clock()

    class Hashing:
        def sha256(self, payload):
            value = hashlib.sha256(payload)
            clock.now = okx_bill_window.MAX_FUNDING_WINDOW_SECONDS + 1
            return value

    monkeypatch.setattr(okx_bill_window, "time", clock)
    monkeypatch.setattr(okx_bill_window, "hashlib", Hashing())
    with pytest.raises(VenueLedgerError, match="time budget"):
        await FundingSource({None: []}).collect_funding_bills(
            START, END, expected_uid="42", expected_main_uid="7"
        )


async def test_funding_queue_wait_consumes_the_public_call_budget(monkeypatch):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

    clock = Clock()

    class ExpiringLock:
        released = False

        def acquire(self, *, timeout):
            assert timeout == okx_bill_window.MAX_FUNDING_WINDOW_SECONDS
            clock.now = timeout + 1
            return True

        def release(self):
            self.released = True

    monkeypatch.setattr(okx_bill_window, "time", clock)
    source = FundingSource({None: []})
    source._funding_window_lock = lock = ExpiringLock()
    with pytest.raises(VenueLedgerError, match="time budget"):
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert lock.released and source.calls == []


@pytest.mark.parametrize("page", [None, {}, ["wrong"], [funding_bill()] * 101])
async def test_funding_rejects_malformed_or_oversized_pages(page):
    with pytest.raises(VenueLedgerError):
        await FundingSource({None: page}).collect_funding_bills(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"code":"50011","data":[]}',
        b'{"code":"0","data":[],"data":[]}',
        b'{"code":"0","data":[NaN]}',
        b"not-json",
        b'{"code":"0","data":[{"uid":"42","mainUid":"7","fixture-key":"value"}]}',
        b'{"code":"0","data":[{"uid":"42","mainUid":"7","label":"fixture-secret"}]}',
    ],
)
async def test_real_funding_transport_refuses_api_errors_invalid_json_and_secret_echo(payload):
    source = OkxVenueLedgerSource(
        {"connector": "okx", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
        {
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        },
    )
    source._funding_http.get_exact = lambda *_args: decode_exact_venue_response(payload)
    with pytest.raises(VenueLedgerError) as error:
        await source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert all(value not in str(error.value) for value in source._credential.values())
