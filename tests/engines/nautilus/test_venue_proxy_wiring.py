"""Every Binance request goes through the operator's proxy once one is named.

Market data, execution and the independent ledger all reach the venue, so all
three must take the same route: a ledger read that went direct would expose the
real egress address the proxy exists to hide, and fail where the venue is
unreachable directly.
"""

from __future__ import annotations

import urllib.request

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.venue_proxy import VenueProxy  # noqa: E402
from custos.engines.nautilus import venue_binance  # noqa: E402
from custos.engines.nautilus.binance_ledger import BinanceVenueLedgerSource  # noqa: E402

SECRET = "hunter2-proxy-secret"
PROXY = VenueProxy.parse(f"http://alice:{SECRET}@proxy.example:8080")


def _spec(connector: str = "binance", mode: str = "testnet") -> dict:
    return {
        "connector": connector,
        "trading_mode": mode,
        "pairs": ["BTC-USDT"],
        "leverage": 1 if connector == "binance" else 3,
        "promotion_id": "44444444-4444-4444-8444-444444444444",
        "promotion_evidence_digest": "a" * 64,
    }


def _credential() -> dict:
    return {
        "api_key": "test-key",
        "api_secret": "test-secret",
        "permission_scope": "trade_no_withdraw",
    }


def _captured(monkeypatch, config_name: str) -> dict:
    # The 2.0 config objects keep the proxy secret, so the only place to observe
    # what was forwarded is the constructor call.
    captured: dict = {}
    original = getattr(venue_binance, config_name)

    def _record(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(venue_binance, config_name, _record)
    return captured


@pytest.mark.parametrize("mode", ["sandbox", "testnet", "live"])
def test_market_data_goes_through_the_proxy(monkeypatch, mode: str) -> None:
    forwarded = _captured(monkeypatch, "BinanceDataClientConfig")

    venue_binance.build_data_client_config_for_mode(
        _spec(mode=mode), _credential(), mode, proxy=PROXY
    )

    assert forwarded["proxy_url"] == PROXY.url


@pytest.mark.parametrize(
    "builder",
    ["build_exec_client_config_testnet", "build_exec_client_config_live"],
)
@pytest.mark.parametrize("connector", ["binance", "binance_perpetual"])
def test_execution_goes_through_the_proxy(monkeypatch, builder: str, connector: str) -> None:
    forwarded = _captured(monkeypatch, "BinanceExecutionClientConfig")

    getattr(venue_binance, builder)(_spec(connector), _credential(), proxy=PROXY)

    assert forwarded["proxy_url"] == PROXY.url


def test_without_a_proxy_the_clients_connect_directly(monkeypatch) -> None:
    data = _captured(monkeypatch, "BinanceDataClientConfig")
    execution = _captured(monkeypatch, "BinanceExecutionClientConfig")

    venue_binance.build_data_client_config_for_mode(_spec(), _credential(), "testnet")
    venue_binance.build_exec_client_config_testnet(_spec(), _credential())

    assert data["proxy_url"] is None
    assert execution["proxy_url"] is None


def _proxies(ledger: BinanceVenueLedgerSource) -> dict[str, str]:
    # An empty ProxyHandler registers no protocol methods, so the opener keeps
    # no handler at all for it: no proxy handler means a direct connection.
    proxies: dict[str, str] = {}
    for handler in ledger._opener.handlers:
        if isinstance(handler, urllib.request.ProxyHandler):
            proxies.update(handler.proxies)
    return proxies


def test_the_ledger_reads_through_the_proxy() -> None:
    ledger = venue_binance.venue_ledger_source(_spec(), _credential(), proxy=PROXY)

    assert _proxies(ledger) == {"http": PROXY.url, "https": PROXY.url}


def test_the_ledger_ignores_a_machine_wide_proxy(monkeypatch) -> None:
    # Without the venue proxy the ledger connects directly, as the engine's own
    # clients do; urllib would otherwise pick up HTTPS_PROXY on its own.
    monkeypatch.setenv("HTTPS_PROXY", "http://elsewhere.example:3128")

    ledger = venue_binance.venue_ledger_source(_spec(), _credential())

    assert _proxies(ledger) == {}


def test_a_venue_without_proxy_support_refuses_to_deploy_behind_one() -> None:
    from custos.engines.nautilus.host import NtTradingNodeHost, _venue_module_for

    host = NtTradingNodeHost(venue_proxy=PROXY)

    with pytest.raises(RuntimeError, match="does not route through a proxy") as raised:
        host._venue_function(_venue_module_for("okx"), "build_exec_client_config_testnet")

    assert SECRET not in str(raised.value)
    assert "proxy.example:8080" in str(raised.value)


def test_a_venue_without_proxy_support_is_unaffected_when_none_is_named() -> None:
    from custos.engines.nautilus.host import NtTradingNodeHost, _venue_module_for

    module = _venue_module_for("okx")
    function = NtTradingNodeHost()._venue_function(module, "build_exec_client_config_testnet")

    assert function is module.build_exec_client_config_testnet
