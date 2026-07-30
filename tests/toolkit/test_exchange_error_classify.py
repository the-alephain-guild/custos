"""Grading a venue's rejection reason into the class that decides how to back off.

A pure function over the reason string, with no engine dependency, so these run in
the base profile.
"""

from __future__ import annotations

from custos_toolkit.risk.exchange_errors import classify_rejection_reason


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
