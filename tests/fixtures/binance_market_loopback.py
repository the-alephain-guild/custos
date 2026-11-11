"""Local market-data plumbing; this fixture never supplies account or execution facts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import struct
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlsplit


def _local_endpoint(value: str, scheme: str, paths: tuple[str, ...]):
    parsed = urlsplit(value)
    if (
        parsed.scheme != scheme
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or parsed.port == 0
        or parsed.path not in paths
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("market fixture endpoints must be anonymous literal loopback URLs")


def loopback_data_config(spec: dict, http_url: str, websocket_url: str):
    if spec.get("trading_mode") != "sandbox":
        raise ValueError("market fixture requires an explicit sandbox scope")
    _local_endpoint(http_url, "http", ("", "/"))
    _local_endpoint(websocket_url, "ws", ("/ws",))
    from nautilus_trader.adapters.binance import BinanceDataClientConfig

    from custos.engines.nautilus.venue_binance import build_data_client_config_for_mode

    original = build_data_client_config_for_mode(spec, {}, "sandbox")
    return BinanceDataClientConfig(
        product_type=original.product_type,
        environment=original.environment,
        base_url_http=http_url,
        base_url_ws=websocket_url,
        api_key=None,
        api_secret=None,
        spot_market_data_mode=original.spot_market_data_mode,
        instrument_provider=original.instrument_provider,
    )


class BinanceMarketLoopback:
    """Serve only public instrument metadata; never emulate an account or execution API."""

    def __init__(self, exchange_info: bytes):
        if not isinstance(exchange_info, bytes) or not 0 < len(exchange_info) <= 1024 * 1024:
            raise ValueError("the catalog fixture must be a bounded byte payload")
        self.exchange_info = exchange_info
        self.requests: list[dict[str, str]] = []
        self.violations: list[str] = []
        self.websocket_methods: list[str] = []
        self._subscriptions: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closing = False

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._connected, "127.0.0.1", 0, limit=16 * 1024)
        self.address = self._server.sockets[0].getsockname()[:2]
        self.http_url = f"http://127.0.0.1:{self.address[1]}"
        self.websocket_url = f"ws://127.0.0.1:{self.address[1]}/ws"
        return self

    async def __aexit__(self, *_error):
        self._closing = True
        self._server.close()
        await self._server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _connected(self, reader, writer):
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reply(self, writer, status, body, content_type="application/json"):
        reason = "OK" if status == 200 else "Forbidden"
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n"
            f"Content-Type: {content_type}\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(head + body)
        await writer.drain()

    async def _ws_send(self, writer, opcode, payload):
        if len(payload) < 126:
            head = bytes([0x80 | opcode, len(payload)])
        else:
            head = bytes([0x80 | opcode, 126]) + struct.pack("!H", len(payload))
        writer.write(head + payload)
        await writer.drain()

    async def _websocket(self, reader, writer, headers):
        key = headers.get("sec-websocket-key", "")
        if (
            headers.get("sec-websocket-version") != "13"
            or "upgrade"
            not in {value.strip().lower() for value in headers.get("connection", "").split(",")}
            or len(base64.b64decode(key, validate=True)) != 16
        ):
            raise ValueError("invalid public websocket handshake")
        accept = base64.b64encode(
            hashlib.sha1(
                key.encode("ascii") + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
                usedforsecurity=False,
            ).digest()
        ).decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        while True:
            header = await reader.readexactly(2)
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if not header[0] & 0x80 or header[0] & 0x70 or not header[1] & 0x80:
                raise ValueError("unsupported websocket frame")
            if length in (126, 127):
                size = 2 if length == 126 else 8
                raw_size = await asyncio.wait_for(reader.readexactly(size), timeout=3)
                length = int.from_bytes(raw_size, "big")
            if length > 16 * 1024 or (opcode >= 8 and length > 125):
                raise ValueError("websocket frame exceeds fixture budget")
            mask = await asyncio.wait_for(reader.readexactly(4), timeout=3)
            masked = await asyncio.wait_for(reader.readexactly(length), timeout=3)
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(masked))
            if opcode == 8:
                await self._ws_send(writer, 8, payload)
                return
            if opcode == 9:
                await self._ws_send(writer, 10, payload)
                continue
            if opcode == 10:
                continue
            if opcode != 1:
                raise ValueError("only public text requests are supported")
            request = json.loads(payload)
            if not isinstance(request, dict):
                raise ValueError("websocket request is not an object")
            method = request.get("method")
            parameters = request.get("params", [])
            if (
                not isinstance(method, str)
                or method not in {"SUBSCRIBE", "UNSUBSCRIBE", "LIST_SUBSCRIPTIONS"}
                or set(request) - {"method", "params", "id"}
                or type(request.get("id")) is not int
                or not 0 <= request["id"] < 2**64
                or not isinstance(parameters, list)
                or len(parameters) > 32
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(
                        r"btcusdt@(bookTicker|trade|aggTrade|depth(?:5|10|20)?(?:@100ms)?|kline_[0-9]+[mhdw])",
                        value,
                    )
                    is None
                    for value in parameters
                )
            ):
                self.violations.append("forbidden_websocket_method")
                await self._ws_send(writer, 8, struct.pack("!H", 1008))
                return
            self.websocket_methods.append(method)
            if method == "SUBSCRIBE":
                self._subscriptions.update(parameters)
            elif method == "UNSUBSCRIBE":
                self._subscriptions.difference_update(parameters)
            result = sorted(self._subscriptions) if method == "LIST_SUBSCRIPTIONS" else None
            await self._ws_send(
                writer, 1, json.dumps({"result": result, "id": request["id"]}).encode()
            )

    async def _serve(self, reader, writer):
        self._writers.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
            lines = raw.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            headers = {
                key.lower(): value.strip()
                for key, value in (line.split(":", 1) for line in lines[1:] if line)
            }
            parsed = urlsplit(target)
            self.requests.append({"method": method, "path": parsed.path})
            authenticated = bool(
                {"authorization", "proxy-authorization", "x-mbx-apikey", "x-api-key", "cookie"}
                & headers.keys()
            ) or bool(
                {"signature", "apikey", "api_key", "listenkey"}
                & {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
            )
            if authenticated:
                self.violations.append("authenticated_request")
                await self._reply(writer, 403, b"{}")
            elif (
                method != "GET"
                or version != "HTTP/1.1"
                or parsed.scheme
                or parsed.netloc
                or headers.get("host") not in {"127.0.0.1", f"127.0.0.1:{self.address[1]}"}
                or headers.get("content-length", "0") != "0"
                or "transfer-encoding" in headers
            ):
                self.violations.append("forbidden_request")
                await self._reply(writer, 403, b"{}")
            elif (
                parsed.path in {"/ws", "/stream"}
                and headers.get("upgrade", "").lower() == "websocket"
            ):
                await self._websocket(reader, writer, headers)
            elif parsed.path == "/api/v3/exchangeInfo":
                await self._reply(writer, 200, self.exchange_info, "application/sbe")
            elif parsed.path == "/api/v3/ping":
                await self._reply(writer, 200, b"{}")
            elif parsed.path == "/api/v3/time":
                body = json.dumps({"serverTime": time.time_ns() // 1_000_000}).encode()
                await self._reply(writer, 200, body)
            else:
                self.violations.append("forbidden_endpoint")
                await self._reply(writer, 403, b"{}")
        except asyncio.IncompleteReadError as error:
            if error.partial and not self._closing:
                self.violations.append("partial_request")
        except (ValueError, TimeoutError, ConnectionError, asyncio.LimitOverrunError):
            if not self._closing:
                self.violations.append("invalid_request")
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self._writers.discard(writer)
