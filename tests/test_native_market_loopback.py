from __future__ import annotations

import asyncio
import json

import pytest

from tests.fixtures.binance_market_loopback import loopback_data_config


def test_market_fixture_replaces_only_the_public_data_endpoints():
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.venue_binance import build_data_client_config_for_mode

    spec = {"connector": "binance", "trading_mode": "sandbox", "pairs": ["BTC-USDT"]}
    original = build_data_client_config_for_mode(spec, {}, "sandbox")
    actual = loopback_data_config(spec, "http://127.0.0.1:23456", "ws://127.0.0.1:23456/ws")

    assert actual.base_url_http == "http://127.0.0.1:23456"
    assert actual.base_url_ws == "ws://127.0.0.1:23456/ws"
    for field in ("product_type", "environment", "spot_market_data_mode", "us"):
        assert getattr(actual, field) == getattr(original, field)
    assert actual.instrument_provider.load_ids == original.instrument_provider.load_ids
    assert actual.instrument_provider.load_all == original.instrument_provider.load_all
    assert actual.has_proxy_url is False


@pytest.mark.parametrize(
    "mode,http_url,websocket_url",
    [
        ("live", "http://127.0.0.1:23456", "ws://127.0.0.1:23456/ws"),
        (None, "http://127.0.0.1:23456", "ws://127.0.0.1:23456/ws"),
        ("sandbox", "https://api.binance.com", "ws://127.0.0.1:23456/ws"),
        ("sandbox", "http://localhost:23456", "ws://127.0.0.1:23456/ws"),
        ("sandbox", "http://127.0.0.1:0", "ws://127.0.0.1:23456/ws"),
        ("sandbox", "http://user:fixture@127.0.0.1:23456", "ws://127.0.0.1:23456/ws"),
        ("sandbox", "http://127.0.0.1:23456", "wss://stream.binance.com/ws"),
        ("sandbox", "http://127.0.0.1:23456", "ws://127.0.0.1:23456/ws?secret=fixture"),
    ],
)
def test_market_fixture_refuses_external_or_authenticated_configuration(
    mode, http_url, websocket_url
):
    pytest.importorskip("nautilus_trader")
    with pytest.raises(ValueError):
        loopback_data_config(
            {"connector": "binance", "trading_mode": mode, "pairs": ["BTC-USDT"]},
            http_url,
            websocket_url,
        )


async def test_market_fixture_serves_its_read_only_catalog_over_loopback():
    from tests.fixtures.binance_market_loopback import BinanceMarketLoopback

    async with BinanceMarketLoopback(b"catalog-fixture") as fixture:
        reader, writer = await asyncio.open_connection(*fixture.address)
        writer.write(b"GET /api/v3/exchangeInfo HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()

        header, body = response.split(b"\r\n\r\n", 1)
        assert header.startswith(b"HTTP/1.1 200 ")
        assert body == b"catalog-fixture"
        assert fixture.violations == []
        assert fixture.requests == [{"method": "GET", "path": "/api/v3/exchangeInfo"}]


@pytest.mark.parametrize(
    "wire_request,reason",
    [
        (
            b"GET /api/v3/exchangeInfo HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"X-MBX-APIKEY: fixture-secret\r\n\r\n",
            "authenticated_request",
        ),
        (
            b"GET /api/v3/exchangeInfo?signature= HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "authenticated_request",
        ),
        (
            b"POST /api/v3/order HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "forbidden_request",
        ),
        (
            b"GET /api/v3/account HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "forbidden_endpoint",
        ),
        (
            b"GET /api/v3/exchangeInfo HTTP/1.1\r\nHost: api.binance.com\r\n\r\n",
            "forbidden_request",
        ),
    ],
)
async def test_market_fixture_refuses_financial_and_authenticated_requests(wire_request, reason):
    from tests.fixtures.binance_market_loopback import BinanceMarketLoopback

    async with BinanceMarketLoopback(b"catalog-fixture") as fixture:
        reader, writer = await asyncio.open_connection(*fixture.address)
        writer.write(wire_request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()

        assert response.startswith(b"HTTP/1.1 403 ")
        assert fixture.violations == [reason]
        assert "fixture-secret" not in str(fixture.requests)
        assert "fixture-secret" not in str(fixture.violations)


async def _public_websocket(fixture, path):
    reader, writer = await asyncio.open_connection(*fixture.address)
    writer.write(
        b"GET " + path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
    assert head.startswith(b"HTTP/1.1 101 ")
    assert b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head
    return reader, writer


def _client_frame(payload):
    mask = b"mask"
    assert len(payload) < 126
    return (
        bytes([0x81, 0x80 | len(payload)])
        + mask
        + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    )


@pytest.mark.parametrize("path", [b"/ws", b"/stream"])
async def test_market_fixture_upgrades_public_websocket_and_acknowledges_subscription(path):
    from tests.fixtures.binance_market_loopback import BinanceMarketLoopback

    async with BinanceMarketLoopback(b"catalog-fixture") as fixture:
        reader, writer = await _public_websocket(fixture, path)
        try:
            payload = json.dumps(
                {"method": "SUBSCRIBE", "params": ["btcusdt@bookTicker"], "id": 7}
            ).encode()
            writer.write(_client_frame(payload))
            await writer.drain()
            frame = await asyncio.wait_for(reader.readexactly(2), timeout=2)
            assert frame[0] == 0x81
            body = await asyncio.wait_for(reader.readexactly(frame[1]), timeout=2)
            assert json.loads(body) == {"result": None, "id": 7}
            assert fixture.websocket_methods == ["SUBSCRIBE"]
            assert fixture.violations == []
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    "message",
    [
        {"method": "order.place", "params": [], "id": 7},
        {
            "method": "SUBSCRIBE",
            "params": ["btcusdt@bookTicker"],
            "id": 7,
            "apiKey": "fixture-secret",
        },
        {"method": "SUBSCRIBE", "params": ["fixture-secret"], "id": 7},
    ],
)
async def test_market_websocket_cannot_emulate_account_or_order_commands(message):
    from tests.fixtures.binance_market_loopback import BinanceMarketLoopback

    async with BinanceMarketLoopback(b"catalog-fixture") as fixture:
        reader, writer = await _public_websocket(fixture, b"/stream")
        try:
            writer.write(_client_frame(json.dumps(message).encode()))
            await writer.drain()
            head = await asyncio.wait_for(reader.readexactly(2), timeout=2)
            assert head == b"\x88\x02"
            body = await asyncio.wait_for(reader.readexactly(2), timeout=2)
            assert int.from_bytes(body, "big") == 1008
            assert fixture.violations == ["forbidden_websocket_method"]
            assert fixture.websocket_methods == []
            assert "fixture-secret" not in str(fixture.requests)
        finally:
            writer.close()
            await writer.wait_closed()
