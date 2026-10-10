"""Every independent venue ledger read names the runner in its User-Agent.

Without one, urllib sends ``Python-urllib/<version>``, which venue gateways
refuse: the SoDEX gateway answers it with 403 even on its public endpoints, so
start-up reconciliation failed with "venue ledger request failed" on testnet and
would on live.
"""

from __future__ import annotations

import importlib.metadata
import io
import urllib.request

from custos.engines.nautilus.binance_ledger import BinanceVenueLedgerSource
from custos.engines.nautilus.ledger_http import ReadOnlyVenueHttp

EXPECTED = f"custos/{importlib.metadata.version('custos-runner')}"


class _RecordingOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.requests.append(request)
        return io.BytesIO(self.body)


def _venue_http() -> tuple[ReadOnlyVenueHttp, _RecordingOpener]:
    http = ReadOnlyVenueHttp("https://venue.example/api/v1/spot", minimum_interval=0)
    opener = _RecordingOpener(b'{"code": 0, "data": []}')
    http._opener = opener  # type: ignore[assignment]
    return http, opener


def test_a_ledger_read_names_the_runner_and_its_version() -> None:
    http, opener = _venue_http()

    http.get("/markets/symbols", {})

    (request,) = opener.requests
    assert request.get_header("User-agent") == EXPECTED


def test_caller_headers_keep_the_runner_user_agent() -> None:
    http, opener = _venue_http()

    http.get("/accounts/state", {"accountID": 1}, headers={"X-Extra": "kept"})

    (request,) = opener.requests
    assert request.get_header("User-agent") == EXPECTED
    assert request.get_header("X-extra") == "kept"


def test_a_caller_may_name_its_own_user_agent() -> None:
    http, opener = _venue_http()

    http.get("/markets/symbols", {}, headers={"User-Agent": "override/1"})

    (request,) = opener.requests
    assert request.get_header("User-agent") == "override/1"


def test_the_binance_ledger_sends_the_same_user_agent() -> None:
    ledger = BinanceVenueLedgerSource(
        spec={"connector": "binance", "trading_mode": "testnet", "pairs": ["BTC-USDT"]},
        credential={"api_key": "test-key", "api_secret": "test-secret"},
    )
    opener = _RecordingOpener(b"{}")
    ledger._opener = opener  # type: ignore[assignment]

    ledger._request_json("https://testnet.binance.vision/api/v3/time", headers={})

    (request,) = opener.requests
    assert request.get_header("User-agent") == EXPECTED
