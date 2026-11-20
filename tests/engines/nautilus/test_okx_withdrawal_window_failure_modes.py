"""Incomplete, ambiguous or unsafe withdrawal history never becomes a successful window."""

from datetime import timedelta

import pytest

from custos.engines.nautilus import okx_bill_window, okx_withdrawal_window
from custos.engines.nautilus.ledger_http import (
    ExactVenueResponse,
    VenueLedgerError,
    decode_exact_venue_response,
)
from tests.engines.nautilus.test_okx_withdrawal_window import (
    END,
    START,
    START_MS,
    WithdrawalSource,
    withdrawal,
)


class ScriptedSource(WithdrawalSource):
    def __init__(self, history):
        super().__init__([])
        self.history = history

    def _get_funding_exact(self, path, params, *, private=True):
        if not path.endswith("/withdrawal-history"):
            return super()._get_funding_exact(path, params, private=private)
        self.calls.append((path, dict(params), private))
        return ExactVenueResponse(self.history(params), "a" * 64)


@pytest.mark.parametrize("identity", [("43", "7"), ("42", "8"), ("", "7"), (42, "7")])
async def test_actual_uid_and_main_uid_must_match_the_expected_account(identity):
    with pytest.raises(VenueLedgerError):
        await WithdrawalSource([], identities=[identity]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize("identity", ["", "0", "01", 42, None, "\uff11\uff12", "1" * 65])
async def test_invalid_expected_uid_is_refused_before_network(identity):
    source = WithdrawalSource([])
    with pytest.raises(VenueLedgerError):
        await source.collect_withdrawal_details(
            START, END, expected_uid=identity, expected_main_uid="7"
        )
    assert source.calls == []


async def test_uid_change_after_actual_empty_page_is_not_a_successful_window():
    with pytest.raises(VenueLedgerError, match="account changed"):
        await WithdrawalSource(
            [], identities=[("42", "7"), ("42", "8")]
        ).collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")


@pytest.mark.parametrize(
    "changes",
    [
        {"wdId": ""},
        {"wdId": "0"},
        {"wdId": 12},
        {"wdId": "01"},
        {"wdId": "1" * 65},
        {"ts": ""},
        {"ts": "0"},
        {"ts": 123},
        {"ts": "1.1"},
        {"ts": "9" * 64},
    ],
)
async def test_identity_and_timestamp_are_required_exact_parseable_strings(changes):
    with pytest.raises(VenueLedgerError):
        await ScriptedSource(lambda _: [withdrawal(**changes)]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize("field", ["amt", "fee"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "bad", True, {}, []])
async def test_provided_nonempty_amounts_must_be_exact_finite_numbers(field, value):
    with pytest.raises(VenueLedgerError):
        await ScriptedSource(lambda _: [withdrawal(**{field: value})]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize("field", ["amt", "fee", "future"])
async def test_binary_float_cannot_bypass_exact_decoder_boundary(field):
    with pytest.raises(VenueLedgerError):
        await ScriptedSource(lambda _: [withdrawal(**{field: 1.25})]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize("page", [None, {}, ["bad"], [withdrawal()] * 101])
async def test_malformed_or_oversized_history_pages_are_refused(page):
    with pytest.raises(VenueLedgerError):
        await ScriptedSource(lambda _: page).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


async def test_full_single_millisecond_is_ambiguous_even_if_exactly_one_hundred_exists():
    rows = [withdrawal(str(n)) for n in range(1, 101)]
    with pytest.raises(VenueLedgerError, match="timestamp group is saturated"):
        await WithdrawalSource(rows).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


async def test_partial_homepage_boundary_with_one_visible_row_can_still_be_saturated():
    newer = [withdrawal(str(n), ts=str(START_MS + 700)) for n in range(1, 100)]
    boundary = [withdrawal(str(n)) for n in range(100, 200)]
    with pytest.raises(VenueLedgerError, match="timestamp group is saturated"):
        await WithdrawalSource(newer + boundary).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize("group", [[], [withdrawal("2")]])
async def test_boundary_recheck_must_include_every_observed_boundary_id(group):
    with pytest.raises(VenueLedgerError, match="boundary group omitted"):
        await ScriptedSource(
            lambda p: [withdrawal()] if p["after"] == START_MS + 1000 else group
        ).collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")


async def test_boundary_recheck_must_only_return_the_target_millisecond():
    with pytest.raises(VenueLedgerError, match="timestamp interval"):
        await ScriptedSource(
            lambda p: (
                [withdrawal()]
                if p["after"] == START_MS + 1000
                else [withdrawal("2", ts=str(START_MS + 501))]
            )
        ).collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")


async def test_one_observation_cannot_silently_overwrite_a_withdrawal_state():
    with pytest.raises(VenueLedgerError, match="changed within one collection"):
        await ScriptedSource(
            lambda p: (
                [withdrawal()] if p["after"] == START_MS + 1000 else [withdrawal(state="pending")]
            )
        ).collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")


async def test_a_repeated_timestamp_cursor_page_is_refused_rather_than_looped():
    with pytest.raises(VenueLedgerError, match="timestamp interval"):
        await ScriptedSource(lambda _: [withdrawal()]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


async def test_every_overlap_row_counts_against_the_scan_budget(monkeypatch):
    raw = withdrawal()
    size = len(okx_withdrawal_window._exact_json(raw).encode("ascii"))
    monkeypatch.setattr(okx_withdrawal_window, "MAX_WITHDRAWAL_BYTES", size * 2 - 1)
    with pytest.raises(VenueLedgerError, match="scan content size"):
        await WithdrawalSource([raw]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


async def test_boundary_requests_also_count_against_the_page_budget(monkeypatch):
    monkeypatch.setattr(okx_withdrawal_window, "MAX_WITHDRAWAL_PAGES", 2)
    with pytest.raises(VenueLedgerError, match="bounded pagination"):
        await WithdrawalSource([withdrawal()]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


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
async def test_invalid_half_open_millisecond_bounds_make_no_request(start, end):
    source = WithdrawalSource([])
    with pytest.raises(VenueLedgerError):
        await source.collect_withdrawal_details(
            start, end, expected_uid="42", expected_main_uid="7"
        )
    assert source.calls == []


async def test_future_window_is_refused():
    with pytest.raises(VenueLedgerError, match="exceeds server time"):
        await WithdrawalSource([], clock=START_MS).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"code":"429","data":[]}',
        b'{"code":"1","data":[]}',
        b'{"code":"0","data":[],"data":[]}',
        b'{"code":"0","data":[NaN]}',
        b"bad-json",
        b'{"code":"0","data":[{"fixture-key":"value"}]}',
        b'{"code":"0","data":[{"nested":["fixture-secret"]}]}',
    ],
)
async def test_real_exact_transport_refuses_errors_and_credential_echo_in_keys_or_values(payload):
    source = WithdrawalSource([])
    source._get_funding_exact = lambda path, params, *, private=True: (
        source._checked_exact_response(decode_exact_venue_response(payload))
    )
    with pytest.raises(VenueLedgerError):
        await source.collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )


@pytest.mark.parametrize("acquired", [True, False])
async def test_public_budget_includes_queue_wait_and_lock_timeout(monkeypatch, acquired):
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
            return acquired

        def release(self):
            self.released = True

    monkeypatch.setattr(okx_bill_window, "time", clock)
    source = WithdrawalSource([])
    source._funding_window_lock = lock = ExpiringLock()
    with pytest.raises(VenueLedgerError, match="withdrawal window exceeded its time budget"):
        await source.collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )
    assert source.calls == [] and lock.released == acquired


async def test_inflight_http_return_after_deadline_cannot_start_another_request(monkeypatch):
    class Clock:
        now = 0

        def monotonic(self):
            return self.now

    clock = Clock()

    class SlowSource(WithdrawalSource):
        def _get_funding_exact(self, path, params, *, private=True):
            response = super()._get_funding_exact(path, params, private=private)
            clock.now = okx_bill_window.MAX_FUNDING_WINDOW_SECONDS + 1
            return response

    monkeypatch.setattr(okx_bill_window, "time", clock)
    source = SlowSource([])
    with pytest.raises(VenueLedgerError, match="withdrawal window exceeded its time budget"):
        await source.collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )
    assert len(source.calls) == 1 and source._funding_http.before_request is None


async def test_local_final_digest_work_also_counts_against_success_acceptance(monkeypatch):
    import hashlib

    class Clock:
        now = 0

        def monotonic(self):
            return self.now

    clock = Clock()

    class Hashing:
        def sha256(self, content):
            value = hashlib.sha256(content)
            clock.now = okx_bill_window.MAX_FUNDING_WINDOW_SECONDS + 1
            return value

    monkeypatch.setattr(okx_bill_window, "time", clock)
    monkeypatch.setattr(okx_withdrawal_window, "hashlib", Hashing())
    source = WithdrawalSource([])
    with pytest.raises(VenueLedgerError, match="withdrawal window exceeded its time budget"):
        await source.collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )
    assert len(source.calls) == 5


async def test_cancel_in_rate_wait_prevents_open_and_retires_the_shared_hook(monkeypatch):
    import asyncio
    import threading

    from custos.engines.nautilus import ledger_http

    waiting, release = threading.Event(), threading.Event()
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
            raise OSError("cancelled transport must not open")

    monkeypatch.setattr(ledger_http, "time", Clock())
    source = WithdrawalSource([])
    source._funding_http._last_request = 10.0
    source._funding_http._opener = Opener()
    source._get_funding_exact = lambda path, params, *, private=True: (
        source._checked_exact_response(
            source._funding_http.get_exact(
                path, params, source._headers(path, params, private=private)
            )
        )
    )
    task = asyncio.create_task(
        source.collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        acquired = await asyncio.to_thread(source._funding_window_lock.acquire, timeout=2)
        assert acquired, "cancelled worker must actually finish"
        source._funding_window_lock.release()
    assert opened == [] and source._funding_http.before_request is None


async def test_cancelled_queued_withdrawal_never_reads_or_replaces_the_funding_hook():
    import asyncio
    import threading

    entered, release, queued, retired = (threading.Event() for _ in range(4))

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.guard = threading.Lock()
            self.attempts = self.releases = 0

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

    class MixedSource(WithdrawalSource):
        def _get_funding_exact(self, path, params, *, private=True):
            callback = self._funding_http.before_request
            if not entered.is_set():
                entered.set()
                assert release.wait(2), "release the test-owned HTTP operation"
                assert self._funding_http.before_request is callback
            callback()
            if path.endswith("/bills-history"):
                self.calls.append((path, dict(params), private))
                return decode_exact_venue_response(b'{"code":"0","data":[]}')
            return super()._get_funding_exact(path, params, private=private)

    source = MixedSource([])
    source._funding_window_lock = ObservedLock()
    first = asyncio.create_task(
        source.collect_funding_bills(START, END, expected_uid="42", expected_main_uid="7")
    )
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(
            source.collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")
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
    assert len(source.calls) == 5 and source._funding_http.before_request is None


async def test_cancelling_inflight_withdrawal_keeps_scope_until_actual_return():
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()

    class BlockedSource(WithdrawalSource):
        def _get_funding_exact(self, path, params, *, private=True):
            entered.set()
            assert release.wait(2), "release the test-owned inflight operation"
            return super()._get_funding_exact(path, params, private=private)

    source = BlockedSource([])
    task = asyncio.create_task(
        source.collect_withdrawal_details(START, END, expected_uid="42", expected_main_uid="7")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert source._funding_window_lock.locked()
        assert source._funding_http.before_request is not None
        assert source.calls == []
    finally:
        release.set()
        acquired = await asyncio.to_thread(source._funding_window_lock.acquire, timeout=2)
        assert acquired, "the owned worker must actually finish"
        source._funding_window_lock.release()
    assert len(source.calls) == 1 and source._funding_http.before_request is None


async def test_server_clock_reversal_after_empty_history_is_refused():
    class BackwardsSource(WithdrawalSource):
        def _get_funding_exact(self, path, params, *, private=True):
            response = super()._get_funding_exact(path, params, private=private)
            if path.endswith("/time"):
                self.clock = START_MS + 1000
            return response

    with pytest.raises(VenueLedgerError, match="server time moved backwards"):
        await BackwardsSource([]).collect_withdrawal_details(
            START, END, expected_uid="42", expected_main_uid="7"
        )
