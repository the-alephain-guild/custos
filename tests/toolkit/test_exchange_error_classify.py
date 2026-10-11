"""Grading a venue's rejection reason into the class that decides how to back off.

A pure function over the reason string, with no engine dependency, so these run in
the base profile.
"""

from __future__ import annotations

import pytest
from custos_toolkit.risk.exchange_errors import classify_rejection_reason, is_reduce_only_refusal


class TestClassifyServer:
    """Rate limits, gateways and timeouts grade as server — the long back-off."""

    def test_timeout_minus_1007(self):
        reason = "{'code': -1007, 'msg': 'Timeout waiting for response from backend server.'}"
        assert classify_rejection_reason(reason) == "server"

    def test_too_many_requests_minus_1003(self):
        assert classify_rejection_reason("{'code': -1003, 'msg': 'Too many requests.'}") == "server"

    def test_too_many_orders_minus_1015(self):
        assert (
            classify_rejection_reason("{'code': -1015, 'msg': 'Too many new orders.'}") == "server"
        )

    def test_html_502_bad_gateway(self):
        reason = '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head><title>502 Bad Gateway</title></head></html>'
        assert classify_rejection_reason(reason) == "server"

    def test_503_504(self):
        assert classify_rejection_reason("503 Service Unavailable") == "server"
        assert classify_rejection_reason("504 Gateway Timeout") == "server"


class TestClassifyLogic:
    """A rejection the order itself caused grades as logic — short back-off, sweep orphans."""

    def test_reduce_only_minus_2022(self):
        reason = "{'code': -2022, 'msg': 'ReduceOnly Order is rejected.'}"
        assert classify_rejection_reason(reason) == "logic"

    def test_insufficient_balance_minus_2019(self):
        assert (
            classify_rejection_reason("{'code': -2019, 'msg': 'Margin is insufficient.'}")
            == "logic"
        )

    def test_unknown_reason_defaults_logic(self):
        assert classify_rejection_reason("some unrecognized rejection") == "logic"


class TestClassifyEdge:
    def test_none_defaults_logic(self):
        assert classify_rejection_reason(None) == "logic"

    def test_empty_defaults_logic(self):
        assert classify_rejection_reason("") == "logic"

    def test_case_insensitive(self):
        assert classify_rejection_reason("BAD GATEWAY") == "server"
        assert classify_rejection_reason("Timeout") == "server"


# The SoDEX adapter's own error texts (NT fork crates/adapters/sodex/src/http/client.rs:
# "transport failed: {0}" and "venue returned HTTP {status}: {body}"); the 429 body is
# the one the 2026-10-10 spot testnet run logged 152 times.
_SODEX_SERVER_ERRORS = (
    'venue returned HTTP 429: {"code":-1,"error":"per-account order rate limit exceeded"}',
    "venue returned HTTP 500: internal server error",
    "transport failed: error sending request: connection refused",
    "venue returned HTTP 429: rate limit exceeded",
)


@pytest.mark.parametrize("reason", _SODEX_SERVER_ERRORS)
def test_sodex_rate_limit_and_transport_errors_are_server_errors(reason) -> None:
    """A throttled or unreachable venue backs off long and cancels nothing.

    Read as logic, a rate limit would make the strategy cancel every open order on an
    account that is already throttled, back off two seconds, and count it towards the
    halt.
    """
    assert classify_rejection_reason(reason) == "server"
    assert is_reduce_only_refusal(reason) is False


@pytest.mark.parametrize(
    "reason",
    ["code=-2022, msg=ReduceOnly Order is rejected.", "code=-2019, msg=Margin is insufficient."],
)
def test_binance_logic_rejections_stay_logic(reason) -> None:
    assert classify_rejection_reason(reason) == "logic"
