"""A partial, changed or misbound trading bill window is never a successful observation."""

import json
from datetime import timedelta, timezone

import pytest

from custos.engines.nautilus import okx_bill_window
from custos.engines.nautilus.ledger_http import VenueLedgerError
from tests.engines.nautilus.test_okx_bill_window import (
    END,
    START,
    START_MS,
    Source,
    authenticated_source,
    bill,
)


@pytest.mark.parametrize("identity", [("43", "7"), ("42", "8"), ("", "7"), (42, "7")])
async def test_wrong_or_missing_actual_account_is_refused(identity):
    source = Source({None: []}, identities=[identity])
    with pytest.raises(VenueLedgerError):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")
    assert len(source.calls) == 1


@pytest.mark.parametrize("identity", [("43", "7"), ("42", "8")])
async def test_account_change_after_the_last_page_is_refused(identity):
    source = Source({None: [bill()]}, identities=[("42", "7"), identity])
    with pytest.raises(VenueLedgerError, match="account changed"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


@pytest.mark.parametrize("uid", ["", "0", "042", 42, True, "42\n", "٤٢"])
async def test_expected_identity_must_be_exact_before_any_request(uid):
    source = Source({None: []})
    with pytest.raises(VenueLedgerError):
        await source.collect_trading_bills(START, END, expected_uid=uid, expected_main_uid="7")
    assert source.calls == []


@pytest.mark.parametrize(
    "start,end",
    [
        (START.replace(tzinfo=None), END),
        (START, END.replace(tzinfo=None)),
        (START + timedelta(microseconds=1), END),
        (START, END + timedelta(microseconds=1)),
        (END, START),
        (START, START),
    ],
)
async def test_unrepresentable_or_unordered_window_is_refused_before_network(start, end):
    source = Source({None: []})
    with pytest.raises(VenueLedgerError):
        await source.collect_trading_bills(start, end, expected_uid="42", expected_main_uid="7")
    assert source.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"billId": ""},
        {"billId": 9007199254740993},
        {"billId": "0"},
        {"ts": str(START_MS - 1)},
        {"ts": str(START_MS + 1000)},
        {"ts": "1.5"},
        {"balChg": "NaN"},
        {"balChg": 1.5},
        {"balChg": None},
        {"fee": "Infinity"},
        {"fee": None},
        {"ccy": ""},
        {"type": ""},
        {"subType": "unknown"},
        {"other": {}},
    ],
)
async def test_malformed_or_out_of_interval_bill_cannot_be_silently_dropped(changes):
    source = Source({None: [bill(**changes)]})
    with pytest.raises(VenueLedgerError):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_duplicate_identity_with_changed_content_is_refused():
    page = [bill(str(value)) for value in range(300, 200, -1)]
    source = Source({None: page, "201": [bill("201", balChg="101")]})
    with pytest.raises(VenueLedgerError, match="changed within"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_cursor_loop_is_refused():
    page = [bill("2")] * 100
    source = Source({None: page, "2": page})
    with pytest.raises(VenueLedgerError, match="cursor did not advance"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_short_nonempty_page_without_cursor_progress_is_not_exhaustion():
    page = [bill(str(value)) for value in range(300, 200, -1)]
    source = Source({None: page, "201": [bill("201")]})
    with pytest.raises(VenueLedgerError, match="cursor did not advance"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_history_age_is_rechecked_after_the_last_page():
    source = Source({None: []}, clock=START_MS + 89 * 86400000)
    with pytest.raises(VenueLedgerError, match="supported history"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_page_escaping_earlier_cursor_is_refused():
    page = [bill(str(value)) for value in range(300, 200, -1)]
    source = Source({None: page, "201": [bill("301")]})
    with pytest.raises(VenueLedgerError, match="escaped its earlier cursor"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_page_count_is_bounded_and_never_returns_a_prefix(monkeypatch):
    monkeypatch.setattr(okx_bill_window, "MAX_BILL_PAGES", 1)
    source = Source({None: [bill(str(value)) for value in range(100, 0, -1)]})
    with pytest.raises(VenueLedgerError, match="bounded pagination"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_total_content_size_is_bounded(monkeypatch):
    monkeypatch.setattr(okx_bill_window, "MAX_BILL_WINDOW_BYTES", 1)
    source = Source({None: [bill()]})
    with pytest.raises(VenueLedgerError, match="content size"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_oversized_page_is_refused():
    source = Source({None: [bill()] * 101})
    with pytest.raises(VenueLedgerError, match="requested limit"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


@pytest.mark.parametrize("clock", [START_MS + 999, START_MS + 90 * 86400000])
async def test_not_yet_closed_or_outside_supported_history_is_refused(clock):
    source = Source({None: []}, clock=clock)
    with pytest.raises(VenueLedgerError, match="supported history or server time"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")


async def test_backward_venue_clock_is_refused():
    class BackwardSource(Source):
        def _get_exact(self, path, params, *, private=True):
            result = super()._get_exact(path, params, private=private)
            if path.endswith("/time"):
                self.clock -= 2
            return result

    with pytest.raises(VenueLedgerError, match="moved backwards"):
        await BackwardSource({None: []}).collect_trading_bills(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"code":"50011","data":[]}',
        b'{"code":"50105","data":[]}',
        b'{"code":"0","data":[{"billId":"1","billId":"2"}]}',
        b'{"code":"0","data":[{"fee":NaN}]}',
        b'{"code":"0","data":[{"notes":"fixture-secret"}]}',
    ],
)
def test_authenticated_exact_get_refuses_errors_duplicate_keys_and_credential_echo(payload):
    source, _ = authenticated_source(payload)
    with pytest.raises(VenueLedgerError):
        source._get_exact("/api/v5/account/bills-archive", {})


def test_credential_echo_in_a_response_key_is_refused():
    payload = json.dumps({"code": "0", "data": [bill(**{"fixture-secret": "ordinary"})]}).encode()
    source, _ = authenticated_source(payload)
    with pytest.raises(VenueLedgerError, match="credential material"):
        source._get_exact("/api/v5/account/bills-archive", {})


async def test_fractional_utc_offset_cannot_hide_submillisecond_boundaries():
    offset = timezone(timedelta(microseconds=1))
    source = Source({None: []})
    with pytest.raises(VenueLedgerError, match="millisecond boundaries"):
        await source.collect_trading_bills(
            START.replace(tzinfo=offset),
            END.replace(tzinfo=offset),
            expected_uid="42",
            expected_main_uid="7",
        )
    assert source.calls == []


async def test_failed_page_after_success_does_not_return_partial_history():
    class BrokenSource(Source):
        def _get_exact(self, path, params, *, private=True):
            if params.get("after") is not None:
                raise VenueLedgerError("venue ledger request failed")
            return super()._get_exact(path, params, private=private)

    source = BrokenSource({None: [bill(str(value)) for value in range(100, 0, -1)]})
    with pytest.raises(VenueLedgerError, match="request failed"):
        await source.collect_trading_bills(START, END, expected_uid="42", expected_main_uid="7")
