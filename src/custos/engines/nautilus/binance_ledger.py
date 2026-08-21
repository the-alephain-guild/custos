"""Independent Binance REST ledger source for reconciliation evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from custos.core.runner_fact import SUPPORTED_CURRENCIES
from custos.core.runner_fact_producer import VenueLedgerEvidence

_SPOT_LIVE = "https://api.binance.com"
_SPOT_TESTNET = "https://testnet.binance.vision"
_FUTURES_LIVE = "https://fapi.binance.com"
_FUTURES_TESTNET = "https://demo-fapi.binance.com"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_PAGE_SIZE = 1000


class BinanceVenueLedgerError(RuntimeError):
    pass


class BinanceVenueLedgerSource:
    """Queries account/trade ledgers separately from Nautilus cache state."""

    __slots__ = (
        "_api_key",
        "_credential_secret",
        "_key_type",
        "_base_url",
        "_futures",
        "_pairs",
        "_symbols",
        "_currencies",
        "_settlement_currencies",
    )

    def __init__(self, *, spec: Mapping[str, Any], credential: Mapping[str, Any]) -> None:
        mode = str(spec.get("trading_mode") or "").lower()
        if mode not in {"testnet", "live"}:
            raise BinanceVenueLedgerError(
                "sandbox has no independent venue ledger and cannot claim completeness"
            )
        connector = str(spec.get("connector") or "")
        if connector not in {"binance", "binance_perpetual"}:
            raise BinanceVenueLedgerError(f"unsupported Binance ledger connector {connector!r}")
        self._futures = connector == "binance_perpetual"
        self._base_url = (
            (_FUTURES_TESTNET if self._futures else _SPOT_TESTNET)
            if mode == "testnet"
            else (_FUTURES_LIVE if self._futures else _SPOT_LIVE)
        )
        self._api_key = self._required(credential.get("api_key"), "api_key")
        self._credential_secret = self._required(credential.get("api_secret"), "api_secret")
        self._key_type = str(credential.get("key_type") or "HMAC").upper()
        if self._key_type not in {"HMAC", "RSA", "ED25519"}:
            raise BinanceVenueLedgerError(f"unsupported Binance key type {self._key_type!r}")
        pairs = spec.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            raise BinanceVenueLedgerError("Binance ledger requires non-empty deployment pairs")
        self._pairs = tuple(self._parse_pair(str(pair)) for pair in pairs)
        self._symbols = tuple(base + quote for base, quote in self._pairs)
        self._currencies = frozenset(currency for pair in self._pairs for currency in pair)
        self._settlement_currencies = frozenset(quote for _base, quote in self._pairs)
        unsupported = self._currencies - SUPPORTED_CURRENCIES
        if unsupported:
            raise BinanceVenueLedgerError(
                f"deployment currencies are outside RunnerFact v1: {sorted(unsupported)}"
            )

    async def collect(self, coverage_from: datetime, closed_at: datetime) -> VenueLedgerEvidence:
        return await asyncio.to_thread(self._collect, coverage_from, closed_at)

    def _collect(self, coverage_from: datetime, closed_at: datetime) -> VenueLedgerEvidence:
        coverage_from = coverage_from.astimezone(UTC)
        closed_at = closed_at.astimezone(UTC)
        collection_started_ms = self._server_time_ms()
        observed_through = datetime.fromtimestamp(collection_started_ms / 1000, UTC)
        if observed_through < closed_at:
            raise BinanceVenueLedgerError("venue clock has not reached the requested close")
        account = self._signed_get("/fapi/v3/account" if self._futures else "/api/v3/account", {})
        futures_positions: list[dict[str, Any]] | None = None
        if self._futures:
            futures_positions = []
            for symbol in self._symbols:
                futures_positions.extend(
                    self._expect_list(self._signed_get("/fapi/v3/positionRisk", {"symbol": symbol}))
                )
        trades: list[dict[str, Any]] = []
        for symbol in self._symbols:
            trades.extend(self._trade_history(symbol, coverage_from, closed_at))
        incomes = self._income_history(coverage_from, closed_at) if self._futures else []
        balances, positions = self._account_rows(
            account,
            futures_positions=futures_positions,
        )
        fills, fees = self._trade_rows(trades)
        fees.extend(self._income_fee_rows(incomes))
        observed_ms = self._server_time_ms()
        observed_through = datetime.fromtimestamp(observed_ms / 1000, UTC)
        venue_wallet_balances = self._wallet_balances(account)
        valuation_positions = self._valuation_positions(futures_positions or ())
        source_state = {
            "collection_started_ms": collection_started_ms,
            "observed_ms": observed_ms,
            "symbols": list(self._symbols),
            "trade_ids": sorted((str(row.get("symbol")), str(row.get("id"))) for row in trades),
            "income_ids": sorted(str(row.get("tranId")) for row in incomes),
        }
        watermark = hashlib.sha256(self._canonical(source_state)).hexdigest()
        return VenueLedgerEvidence(
            venue="BINANCE",
            source="venue_api",
            watermark=watermark,
            coverage_from=coverage_from,
            observed_through=observed_through,
            completeness={
                "balances_complete": True,
                "positions_complete": True,
                "fills_complete": True,
                "fees_complete": True,
            },
            balances=balances,
            positions=positions,
            fills=fills,
            fees=fees,
            valuation_collection_started_at=datetime.fromtimestamp(
                collection_started_ms / 1000, UTC
            ),
            venue_wallet_balances=venue_wallet_balances,
            valuation_positions=valuation_positions,
        )

    def _server_time_ms(self) -> int:
        payload = self._public_get("/fapi/v1/time" if self._futures else "/api/v3/time")
        try:
            return int(payload["serverTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BinanceVenueLedgerError("Binance time response is malformed") from exc

    def _trade_history(
        self, symbol: str, coverage_from: datetime, closed_at: datetime
    ) -> list[dict[str, Any]]:
        path = "/fapi/v1/userTrades" if self._futures else "/api/v3/myTrades"
        start_ms = int(coverage_from.timestamp() * 1000)
        end_ms = self._closed_interval_end_ms(closed_at)
        result: list[dict[str, Any]] = []
        cursor = start_ms
        while cursor <= end_ms:
            window_end = min(end_ms, cursor + int(timedelta(hours=23).total_seconds() * 1000))
            page = self._expect_list(
                self._signed_get(
                    path,
                    {
                        "symbol": symbol,
                        "startTime": cursor,
                        "endTime": window_end,
                        "limit": _PAGE_SIZE,
                    },
                )
            )
            result.extend(page)
            while len(page) == _PAGE_SIZE:
                last_id = max(int(row["id"]) for row in page)
                page = self._expect_list(
                    self._signed_get(
                        path,
                        {"symbol": symbol, "fromId": last_id + 1, "limit": _PAGE_SIZE},
                    )
                )
                in_window = [row for row in page if int(row.get("time", 0)) <= window_end]
                result.extend(in_window)
                if not in_window or any(int(row.get("time", 0)) > window_end for row in page):
                    break
            cursor = window_end + 1
        unique: dict[str, dict[str, Any]] = {}
        for row in result:
            identity = str(row.get("id") or "")
            if not identity:
                raise BinanceVenueLedgerError("Binance trade row has no id")
            unique[identity] = row
        return list(unique.values())

    def _income_history(self, coverage_from: datetime, closed_at: datetime) -> list[dict[str, Any]]:
        start_ms = int(coverage_from.timestamp() * 1000)
        end_ms = self._closed_interval_end_ms(closed_at)
        result: list[dict[str, Any]] = []
        page_number = 1
        while True:
            page = self._expect_list(
                self._signed_get(
                    "/fapi/v1/income",
                    {
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "page": page_number,
                        "limit": _PAGE_SIZE,
                    },
                )
            )
            result.extend(page)
            if len(page) < _PAGE_SIZE:
                return result
            page_number += 1
            if page_number > 4096:
                raise BinanceVenueLedgerError("Binance income pagination exceeded 4096 pages")

    def _account_rows(
        self,
        account: Any,
        *,
        futures_positions: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(account, dict):
            raise BinanceVenueLedgerError("Binance account response must be an object")
        balances: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        if self._futures:
            for row in self._expect_list(account.get("assets")):
                asset = str(row.get("asset") or "").upper()
                # USD-M perpetual account assets are collateral balances, not
                # spot inventory. Reconcile only the deployment's settlement
                # currencies; treating the pair's base asset as cash creates a
                # false balance discrepancy against the settlement ledger.
                if asset not in self._settlement_currencies:
                    continue
                balances.append(
                    {
                        "asset": asset,
                        "currency": asset,
                        "total": self._decimal(row.get("marginBalance"), "marginBalance"),
                        "available": self._decimal(row.get("availableBalance"), "availableBalance"),
                    }
                )
            account_positions = self._expect_list(account.get("positions"))
            if futures_positions is None:
                raise BinanceVenueLedgerError(
                    "Binance perpetual position-risk snapshot is unavailable"
                )
            account_active = self._active_position_quantities(account_positions)
            risk_active = self._active_position_quantities(futures_positions)
            if account_active != risk_active:
                raise BinanceVenueLedgerError("Binance account and position-risk quantity differs")
            for row in futures_positions:
                symbol = str(row.get("symbol") or "")
                if symbol not in self._symbols:
                    continue
                quantity = Decimal(self._decimal(row.get("positionAmt"), "positionAmt"))
                if quantity == 0:
                    continue
                quote = self._quote_currency(symbol)
                side = "buy" if quantity > 0 else "sell"
                positions.append(
                    {
                        "venue_position_id": f"{symbol}:{row.get('positionSide') or 'BOTH'}",
                        "instrument": self._canonical_instrument(symbol),
                        "side": side,
                        "quantity": self._render(abs(quantity)),
                        "avg_entry_price": self._decimal(row.get("entryPrice"), "entryPrice"),
                        "currency": quote,
                    }
                )
        else:
            for row in self._expect_list(account.get("balances")):
                asset = str(row.get("asset") or "").upper()
                if asset not in self._currencies:
                    continue
                free = Decimal(self._decimal(row.get("free"), "free"))
                locked = Decimal(self._decimal(row.get("locked"), "locked"))
                balances.append(
                    {
                        "asset": asset,
                        "currency": asset,
                        "total": self._render(free + locked),
                        "available": self._render(free),
                    }
                )
        return balances, positions

    def _active_position_quantities(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> dict[tuple[str, str], Decimal]:
        active: dict[tuple[str, str], Decimal] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol not in self._symbols:
                continue
            side = str(row.get("positionSide") or "BOTH")
            key = (symbol, side)
            quantity = Decimal(self._decimal(row.get("positionAmt"), "positionAmt"))
            if quantity == 0:
                continue
            if key in active:
                raise BinanceVenueLedgerError(
                    "Binance position snapshot repeats symbol and position side"
                )
            active[key] = quantity
        return active

    def _wallet_balances(self, account: Any) -> dict[str, str]:
        if not self._futures:
            return {}
        if not isinstance(account, dict):
            raise BinanceVenueLedgerError("Binance account response must be an object")
        result: dict[str, str] = {}
        for row in self._expect_list(account.get("assets")):
            asset = str(row.get("asset") or "").upper()
            if asset not in self._settlement_currencies:
                continue
            if asset in result:
                raise BinanceVenueLedgerError("Binance account repeats settlement wallet")
            result[asset] = self._decimal(row.get("walletBalance"), "walletBalance")
        return result

    def _valuation_positions(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol not in self._symbols:
                continue
            quantity = Decimal(self._decimal(row.get("positionAmt"), "positionAmt"))
            if quantity == 0:
                continue
            result.append(
                {
                    "instrument": self._canonical_instrument(symbol),
                    "currency": self._quote_currency(symbol),
                    "quantity": self._render(quantity),
                    "avg_entry_price": self._decimal(row.get("entryPrice"), "entryPrice"),
                    "mark_price": self._decimal(row.get("markPrice"), "markPrice"),
                }
            )
        result.sort(key=lambda row: row["instrument"])
        if len({row["instrument"] for row in result}) != len(result):
            raise BinanceVenueLedgerError("Binance valuation repeats instrument")
        return result

    def _trade_rows(
        self, trades: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fills: list[dict[str, Any]] = []
        fees: list[dict[str, Any]] = []
        for row in trades:
            symbol = self._required(row.get("symbol"), "trade.symbol")
            quote = self._quote_currency(symbol)
            commission_currency = str(row.get("commissionAsset") or quote).upper()
            if commission_currency != quote or quote not in SUPPORTED_CURRENCIES:
                raise BinanceVenueLedgerError(
                    f"trade {row.get('id')} fee currency {commission_currency} cannot be represented "
                    f"by RunnerFact v1 settlement currency {quote}"
                )
            trade_id = self._required(row.get("id"), "trade.id")
            order_id = self._required(row.get("orderId"), "trade.orderId")
            occurred_at = self._timestamp_ms(row.get("time"))
            fee = self._decimal(row.get("commission", "0"), "commission")
            side_value = str(row.get("side") or "").lower()
            if side_value not in {"buy", "sell"}:
                side_value = "buy" if bool(row.get("isBuyer")) else "sell"
            fills.append(
                {
                    "venue_trade_id": trade_id,
                    "venue_order_id": order_id,
                    "instrument": self._canonical_instrument(symbol),
                    "side": side_value,
                    "quantity": self._decimal(row.get("qty"), "qty"),
                    "price": self._decimal(row.get("price"), "price"),
                    "fee": fee,
                    "currency": quote,
                    "occurred_at": occurred_at,
                }
            )
            fees.append(
                {
                    "fee_id": f"trade:{symbol}:{trade_id}:commission",
                    "kind": "commission",
                    "currency": quote,
                    "amount": fee,
                    "occurred_at": occurred_at,
                }
            )
        return fills, fees

    def _income_fee_rows(self, incomes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in incomes:
            kind = str(row.get("incomeType") or "").upper()
            if kind not in {"REALIZED_PNL", "FUNDING_FEE", "INSURANCE_CLEAR"}:
                continue
            symbol = str(row.get("symbol") or "")
            if symbol and symbol not in self._symbols:
                continue
            currency = str(row.get("asset") or "").upper()
            if currency not in SUPPORTED_CURRENCIES:
                raise BinanceVenueLedgerError(f"unsupported Binance income currency {currency}")
            income = Decimal(self._decimal(row.get("income"), "income"))
            if kind == "REALIZED_PNL":
                economic_kind = "realized_pnl_credit" if income >= 0 else "realized_pnl_debit"
            elif kind == "FUNDING_FEE":
                economic_kind = "funding_credit" if income >= 0 else "funding_cost"
            else:
                economic_kind = "insurance_credit" if income >= 0 else "insurance_cost"
            rows.append(
                {
                    "fee_id": f"income:{row.get('tranId')}:{kind}",
                    # RunnerFact v1 keeps this legacy collection named `fees`,
                    # while `kind` carries the accounting direction. Amounts
                    # stay non-negative so the pinned wire schema remains
                    # unchanged and consumers never infer a sign from `abs()`.
                    "kind": economic_kind,
                    "currency": currency,
                    "amount": self._render(abs(income)),
                    "occurred_at": self._timestamp_ms(row.get("time")),
                }
            )
        return rows

    def _public_get(self, path: str) -> Any:
        return self._request_json(f"{self._base_url}{path}", headers={})

    def _signed_get(self, path: str, parameters: Mapping[str, Any]) -> Any:
        params = dict(parameters)
        params["recvWindow"] = 5000
        params["timestamp"] = int(time.time() * 1000)
        encoded = urllib.parse.urlencode(params)
        signature = self._sign(encoded.encode("ascii"))
        url = f"{self._base_url}{path}?{encoded}&signature={urllib.parse.quote(signature, safe='')}"
        return self._request_json(url, headers={"X-MBX-APIKEY": self._api_key})

    def _sign(self, payload: bytes) -> str:
        if self._key_type == "HMAC":
            return hmac.new(
                self._credential_secret.encode("utf-8"), payload, hashlib.sha256
            ).hexdigest()
        try:
            private_key = serialization.load_pem_private_key(
                self._credential_secret.encode("utf-8"), password=None
            )
            if self._key_type == "ED25519":
                signature = private_key.sign(payload)
            else:
                signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        except (TypeError, ValueError) as exc:
            raise BinanceVenueLedgerError("Binance private signing key is invalid") from exc
        return base64.b64encode(signature).decode("ascii")

    @staticmethod
    def _request_json(url: str, *, headers: Mapping[str, str]) -> Any:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "custos-runner/0.3", **headers},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            body = exc.read(512).decode("utf-8", errors="replace")
            raise BinanceVenueLedgerError(f"Binance HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise BinanceVenueLedgerError(f"Binance venue API unavailable: {exc.reason}") from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise BinanceVenueLedgerError("Binance response exceeds 16 MiB")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BinanceVenueLedgerError("Binance response is not JSON") from exc

    def _quote_currency(self, symbol: str) -> str:
        for (_base, quote), configured in zip(self._pairs, self._symbols, strict=True):
            if configured == symbol:
                return quote
        raise BinanceVenueLedgerError(f"unexpected Binance symbol {symbol!r}")

    def _canonical_instrument(self, symbol: str) -> str:
        # Reconciliation compares independently sourced venue rows with the
        # canonical Nautilus instrument identity emitted by the engine. Raw
        # Binance symbols are venue locators, not cross-source identities.
        self._quote_currency(symbol)
        suffix = "-PERP.BINANCE" if self._futures else ".BINANCE"
        return f"{symbol}{suffix}"

    @staticmethod
    def _closed_interval_end_ms(closed_at: datetime) -> int:
        # Binance startTime/endTime are inclusive. Reconciliation periods are
        # half-open [started_at, closed_at), so subtract one millisecond to
        # prevent a boundary fill or income row from appearing in two periods.
        return int(closed_at.timestamp() * 1000) - 1

    @staticmethod
    def _parse_pair(value: str) -> tuple[str, str]:
        parts = value.upper().replace("/", "-").split("-")
        if len(parts) != 2 or not all(parts):
            raise BinanceVenueLedgerError(f"pair {value!r} must be BASE-QUOTE")
        return parts[0], parts[1]

    @staticmethod
    def _required(value: Any, field: str) -> str:
        rendered = str(value or "").strip()
        if not rendered:
            raise BinanceVenueLedgerError(f"{field} is required")
        return rendered

    @staticmethod
    def _expect_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise BinanceVenueLedgerError("Binance response must be a list of objects")
        return value

    @staticmethod
    def _decimal(value: Any, field: str) -> str:
        if isinstance(value, float):
            raise BinanceVenueLedgerError(f"Binance {field} arrived as binary float")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise BinanceVenueLedgerError(f"Binance {field} is not decimal") from exc
        if not parsed.is_finite():
            raise BinanceVenueLedgerError(f"Binance {field} is not finite")
        return BinanceVenueLedgerSource._render(parsed)

    @staticmethod
    def _render(value: Decimal) -> str:
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"

    @staticmethod
    def _timestamp_ms(value: Any) -> str:
        try:
            milliseconds = int(value)
        except (TypeError, ValueError) as exc:
            raise BinanceVenueLedgerError("Binance timestamp is invalid") from exc
        seconds, millis = divmod(milliseconds, 1000)
        base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
        return f"{base}.{millis:03d}Z" if millis else f"{base}Z"

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
