"""Independent OKX REST evidence with pagination and contract-unit conversion."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from custos.core.runner_fact import SUPPORTED_CURRENCIES
from custos.core.runner_fact_producer import VenueLedgerEvidence
from custos.engines.nautilus.ledger_http import (
    ReadOnlyVenueHttp,
    VenueLedgerError,
    decimal,
    period_bounds,
    rows,
    timestamp_ms,
)
from custos.engines.nautilus.venue_config import require_credential, venue_options

_HOSTS = {
    "global": "https://openapi.okx.com",
    "eea": "https://eea.okx.com",
    "us": "https://us.okx.com",
}


class OkxVenueLedgerSource:
    def __init__(self, spec: dict, credential: dict) -> None:
        from custos.engines.nautilus.venue_okx import build_instrument_id_strings

        mode = spec.get("trading_mode")
        if mode not in {"testnet", "live"}:
            raise VenueLedgerError("OKX independent ledger requires testnet or live")
        options = venue_options(spec, {"region", "margin_mode"})
        region = options.get("region", "global")
        if region not in _HOSTS:
            raise VenueLedgerError("unsupported OKX ledger region")
        self._http = ReadOnlyVenueHttp(_HOSTS[region])
        self._credential = {
            key: require_credential(credential, key)
            for key in ("api_key", "api_secret", "api_passphrase")
        }
        self._leverage = spec.get("leverage", 1)
        self._margin = options.get("margin_mode", "cross")
        self._demo = mode == "testnet"
        self._perpetual = spec["connector"] == "okx_perpetual"
        self._kind = "SWAP" if self._perpetual else "SPOT"
        self._symbols = tuple(
            value.removesuffix(".OKX") for value in build_instrument_id_strings(spec)
        )

    def validate_account(self) -> None:
        config = rows(self._get("/api/v5/account/config", {}))
        if len(config) != 1 or config[0].get("posMode") != "net_mode":
            raise VenueLedgerError("OKX execution requires a verified net-mode account")
        if self._perpetual:
            for symbol in self._symbols:
                values = rows(
                    self._get(
                        "/api/v5/account/leverage-info", {"instId": symbol, "mgnMode": self._margin}
                    )
                )
                if not values or any(
                    decimal(row.get("lever"), "leverage") != self._leverage for row in values
                ):
                    raise VenueLedgerError(
                        "OKX account leverage differs from the signed deployment; configure it before starting"
                    )

    def _get(self, path: str, params: dict[str, object], *, private: bool = True):
        headers = {}
        if private:
            timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            query = urllib.parse.urlencode(params)
            target = path + ("?" + query if query else "")
            signature = base64.b64encode(
                hmac.new(
                    self._credential["api_secret"].encode(),
                    (timestamp + "GET" + target).encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            headers = {
                "OK-ACCESS-KEY": self._credential["api_key"],
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._credential["api_passphrase"],
            }
        if self._demo:
            headers["x-simulated-trading"] = "1"
        return self._http.get(path, params, headers)

    def _history(self, path: str, params: dict[str, object], start: int, end: int) -> list[dict]:
        result = {}
        cursor = None
        seen_cursors = set()
        for _ in range(1000):
            query = {**params, "begin": start, "end": end, "limit": 100}
            if cursor is not None:
                query["after"] = cursor
            page = rows(self._get(path, query))
            for row in page:
                if "instId" in params and row.get("instId") != params["instId"]:
                    raise VenueLedgerError("OKX history contains a different instrument")
                identity = str(row.get("billId", ""))
                if not identity:
                    raise VenueLedgerError("OKX history has no bill identity")
                if identity in result and result[identity] != row:
                    raise VenueLedgerError("OKX history changed within one collection")
                result[identity] = row
            if len(page) < 100:
                return [row for row in result.values() if start <= int(row["ts"]) <= end]
            next_cursor = str(page[-1].get("billId", ""))
            if next_cursor in seen_cursors:
                raise VenueLedgerError("OKX history cursor did not advance")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise VenueLedgerError("OKX history exceeded bounded pagination")

    def _closed_position_pnl(self, symbol: str, start: int, end: int) -> list[dict]:
        # This endpoint pages by update time, unlike fills/bills which page by bill ID.
        result = {}
        cursor = end + 1
        for _ in range(1000):
            page = rows(
                self._get(
                    "/api/v5/account/positions-history",
                    {
                        "instType": "SWAP",
                        "instId": symbol,
                        "after": cursor,
                        "limit": 100,
                    },
                )
            )
            for row in page:
                at = int(row["uTime"])
                if row.get("instId") != symbol or at >= cursor:
                    raise VenueLedgerError("OKX position history escaped its instrument/cursor")
                if at < start:
                    continue
                if row.get("type") == "1":
                    continue  # Native PositionClosed is emitted only on a complete close.
                if row.get("type") != "2" or decimal(row["liqPenalty"], "liquidation penalty"):
                    raise VenueLedgerError(
                        "OKX liquidation/ADL requires independent loss reconciliation"
                    )
                currency = str(row["ccy"])
                if currency not in SUPPORTED_CURRENCIES:
                    raise VenueLedgerError("OKX position settlement currency is unsupported")
                pnl = decimal(row["pnl"], "position PnL") + decimal(row["fee"], "position fees")
                identity = f"position:{symbol}:{row['posId']}:{row['cTime']}:{at}"
                value = {
                    "fee_id": identity,
                    "kind": "realized_pnl_credit" if pnl >= 0 else "realized_pnl_debit",
                    "amount": str(abs(pnl)),
                    "currency": currency,
                    "occurred_at": timestamp_ms(at).isoformat(),
                }
                if identity in result and result[identity] != value:
                    raise VenueLedgerError("OKX position history changed during collection")
                result[identity] = value
            if len(page) < 100 or min(int(row["uTime"]) for row in page) < start:
                return list(result.values())
            cursor = min(int(row["uTime"]) for row in page)
        raise VenueLedgerError("OKX position history exceeded bounded pagination")

    def _multiplier(self, symbol: str) -> Decimal:
        if not self._perpetual:
            return Decimal(1)
        data = rows(
            self._get(
                "/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol}, private=False
            )
        )
        if (
            len(data) != 1
            or data[0].get("instId") != symbol
            or data[0].get("ctType") != "linear"
            or data[0].get("ctValCcy") != symbol.split("-")[0]
        ):
            raise VenueLedgerError("OKX contract units are not a supported linear base quantity")
        value = decimal(data[0].get("ctVal"), "ctVal") * decimal(
            data[0].get("ctMult") or "1", "ctMult"
        )
        if value <= 0:
            raise VenueLedgerError("OKX contract multiplier is invalid")
        return value

    async def collect(self, coverage_from: datetime, closed_at: datetime) -> VenueLedgerEvidence:
        return await asyncio.to_thread(self._collect, coverage_from, closed_at)

    def _collect(self, start: datetime, end: datetime) -> VenueLedgerEvidence:
        begin, finish = period_bounds(start, end)
        if datetime.now(UTC) - start > timedelta(days=89):
            raise VenueLedgerError("OKX ledger interval exceeds supported history retention")
        observed = timestamp_ms(rows(self._get("/api/v5/public/time", {}, private=False))[0]["ts"])
        if observed < end:
            raise VenueLedgerError("OKX time has not reached the requested close")
        account = rows(self._get("/api/v5/account/balance", {}))
        if len(account) != 1:
            raise VenueLedgerError("OKX account snapshot is ambiguous")
        balances = []
        for row in rows(account[0].get("details")):
            currency = str(row.get("ccy", ""))
            total = decimal(row.get("cashBal"), "cashBal")
            if currency not in SUPPORTED_CURRENCIES:
                if total:
                    raise VenueLedgerError("OKX balance currency is unsupported")
                continue
            available = decimal(row.get("availBal") or row.get("availEq"), "available balance")
            balances.append(
                {
                    "asset": currency,
                    "currency": currency,
                    "total": str(total),
                    "available": str(available),
                }
            )
        positions, fills, fees = [], [], []
        for symbol in self._symbols:
            multiplier = self._multiplier(symbol)
            quote = symbol.split("-")[1]
            if self._perpetual:
                for row in rows(self._get("/api/v5/account/positions", {"instId": symbol})):
                    qty = decimal(row.get("pos"), "position")
                    if not qty:
                        continue
                    side = row.get("posSide")
                    if side not in {"net", "long", "short"}:
                        raise VenueLedgerError("OKX position side is unknown")
                    positions.append(
                        {
                            "venue_position_id": str(row["posId"]),
                            "instrument": symbol + ".OKX",
                            "side": "sell"
                            if side == "short" or (side == "net" and qty < 0)
                            else "buy",
                            "quantity": str(abs(qty) * multiplier),
                            "avg_entry_price": str(decimal(row.get("avgPx"), "avgPx")),
                            "currency": quote,
                        }
                    )
            if self._perpetual:
                fees.extend(self._closed_position_pnl(symbol, begin, finish))
            trades = self._history(
                "/api/v5/trade/fills-history",
                {"instType": self._kind, "instId": symbol},
                begin,
                finish,
            )
            for row in trades:
                fee_currency = str(row.get("feeCcy", ""))
                if fee_currency not in SUPPORTED_CURRENCIES:
                    raise VenueLedgerError("OKX fee currency is unsupported")
                fee = -decimal(row.get("fee"), "fee")
                trade_id, at = str(row["tradeId"]), timestamp_ms(row["ts"]).isoformat()
                fills.append(
                    {
                        "venue_trade_id": trade_id,
                        "venue_order_id": str(row["ordId"]),
                        "instrument": symbol + ".OKX",
                        "side": str(row["side"]),
                        "quantity": str(decimal(row["fillSz"], "fillSz") * multiplier),
                        "price": str(decimal(row["fillPx"], "fillPx")),
                        "fee": str(fee),
                        "currency": quote,
                        "fee_currency": fee_currency,
                        "occurred_at": at,
                    }
                )
                fees.append(
                    {
                        "fee_id": f"trade:{symbol}:{trade_id}:commission",
                        "kind": "commission",
                        "currency": fee_currency,
                        "amount": str(fee),
                        "occurred_at": at,
                    }
                )
        if self._perpetual:
            for row in self._history(
                "/api/v5/account/bills-archive", {"instType": "SWAP", "type": "8"}, begin, finish
            ):
                if row.get("instId") not in self._symbols:
                    continue
                currency = str(row.get("ccy", ""))
                if currency not in SUPPORTED_CURRENCIES:
                    raise VenueLedgerError("OKX funding currency is unsupported")
                fees.append(
                    {
                        "fee_id": "funding:" + str(row["billId"]),
                        "kind": "funding_cost"
                        if decimal(row["balChg"], "funding balance change") <= 0
                        else "funding_credit",
                        "currency": currency,
                        "amount": str(abs(decimal(row["balChg"], "funding balance change"))),
                        "occurred_at": timestamp_ms(row["ts"]).isoformat(),
                    }
                )
        watermark = hashlib.sha256(
            json.dumps([balances, positions, fills, fees], sort_keys=True).encode()
        ).hexdigest()
        return VenueLedgerEvidence(
            venue="OKX",
            source="venue_api",
            watermark=watermark,
            coverage_from=start,
            observed_through=observed,
            completeness={
                key: True
                for key in (
                    "balances_complete",
                    "positions_complete",
                    "fills_complete",
                    "fees_complete",
                )
            },
            balances=balances,
            positions=positions,
            fills=fills,
            fees=fees,
        )
