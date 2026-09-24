"""An operator can send venue traffic through a forward proxy.

Where the venue cannot be reached directly -- a poisoned resolver, a filtered
network, an allow-listed egress address -- the runner sends every venue request
through one proxy the operator names. The proxy address may carry credentials,
so it is read from the environment, never echoed and only logged redacted. A
venue that cannot route through the proxy refuses to deploy rather than
connecting directly behind the operator's back.
"""

from __future__ import annotations

import pytest

from custos.core.venue_proxy import (
    VENUE_PROXY_ENV,
    VenueProxy,
    VenueProxyError,
    venue_proxy_from_environment,
)

SECRET = "hunter2-proxy-secret"


def test_an_http_proxy_is_accepted_and_redacted() -> None:
    proxy = VenueProxy.parse(f"http://alice:{SECRET}@proxy.example:8080")

    assert proxy.url == f"http://alice:{SECRET}@proxy.example:8080"
    assert proxy.redacted == "http://proxy.example:8080"
    assert SECRET not in repr(proxy)
    assert SECRET not in str(proxy)


def test_an_https_proxy_without_a_port_is_accepted() -> None:
    assert VenueProxy.parse("https://proxy.example").redacted == "https://proxy.example"


@pytest.mark.parametrize(
    "value",
    [
        f"socks5://alice:{SECRET}@proxy.example:1080",
        f"ftp://alice:{SECRET}@proxy.example",
        f"http://alice:{SECRET}@:8080",
        f"alice:{SECRET}@proxy.example:8080",
        "http://proxy.example:not-a-port",
    ],
)
def test_an_unusable_proxy_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(VenueProxyError) as raised:
        VenueProxy.parse(value)

    assert SECRET not in str(raised.value)
    assert value not in str(raised.value)


def test_the_environment_names_the_proxy() -> None:
    proxy = venue_proxy_from_environment({VENUE_PROXY_ENV: "http://proxy.example:3128"})

    assert proxy is not None
    assert proxy.redacted == "http://proxy.example:3128"


@pytest.mark.parametrize("environ", [{}, {VENUE_PROXY_ENV: ""}, {VENUE_PROXY_ENV: "   "}])
def test_no_proxy_is_configured_by_default(environ: dict[str, str]) -> None:
    assert venue_proxy_from_environment(environ) is None


def test_a_general_https_proxy_variable_is_not_a_venue_proxy() -> None:
    # Venue traffic follows only the variable meant for it. A proxy set for the
    # whole machine may not be one the operator wants its exchange keys to cross.
    assert venue_proxy_from_environment({"HTTPS_PROXY": "http://proxy.example:3128"}) is None
