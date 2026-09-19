"""Independent SoDEX account reads with bounded time-window pagination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime

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
from custos.engines.nautilus.venue_config import venue_options


class SodexVenueLedgerSource:
    def __init__(self, spec: dict, credential: dict) -> None:
        del credential
        mode = spec.get("trading_mode")
        if mode not in {"testnet", "live"}:
            raise VenueLedgerError("SoDEX independent ledger requires testnet or live")
        options = venue_options(
            spec, {"wallet_address", "sodex_account_id", "margin_mode", "settlement_currency"}
        )
        wallet = options.get("wallet_address")
        account = options.get("sodex_account_id")
        if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
            raise VenueLedgerError("SoDEX wallet address is invalid")
        if type(account) is not int or not 0 < account < 2**64:
            raise VenueLedgerError("SoDEX account id must be a positive uint64")
        if spec["connector"] not in {"sodex", "sodex_perpetual"}:
            raise VenueLedgerError("SoDEX connector is invalid")
        self._perpetual = spec["connector"] == "sodex_perpetual"
        self._venue = "SODEX_PERPS" if self._perpetual else "SODEX_SPOT"
        network, market = (
            ("testnet" if mode == "testnet" else "mainnet"),
            ("perps" if self._perpetual else "spot"),
        )
        self._http = ReadOnlyVenueHttp(
            f"https://{network}-gw.sodex.dev/api/v1/{market}", minimum_interval=0.1
        )
        self._wallet, self._account = wallet, account
        self._symbols = tuple(str(value) for value in spec["pairs"])
        self._leverage = spec.get("leverage", 1)
        self._margin = options.get("margin_mode", "cross")
        self._settlement = str(options.get("settlement_currency", "")).upper()
        if self._settlement not in SUPPORTED_CURRENCIES:
            raise VenueLedgerError("SoDEX requires a supported explicit settlement currency")
        if self._margin not in {"cross", "isolated"}:
            raise VenueLedgerError("SoDEX margin mode is invalid")

    def _get(self, suffix: str, params: dict | None = None):
        return self._http.get(
            f"/accounts/{self._wallet}/{suffix}", {"accountID": self._account, **(params or {})}
        )

    def _state(self) -> dict:
        state = self._get("state")
        if (
            not isinstance(state, dict)
            or str(state.get("user", "")).lower() != self._wallet.lower()
            or state.get("aid") != self._account
        ):
            raise VenueLedgerError(
                "SoDEX account state does not match the configured wallet/account"
            )
        return state

    def validate_account(self) -> None:
        state = self._state()
        if not self._perpetual:
            if self._leverage != 1:
                raise VenueLedgerError("SoDEX spot does not support leveraged sizing")
        configs = {str(row["s"]): row for row in rows(state.get("S") or [])}
        markets = {str(row["name"]): row for row in rows(self._http.get("/markets/symbols", {}))}
        for symbol in self._symbols:
            if symbol not in markets:
                raise VenueLedgerError("SoDEX configured symbol is unavailable")
            quote = str(markets[symbol].get("quoteCoin", "")).upper()
            if quote != self._settlement:
                raise VenueLedgerError(
                    "SoDEX instrument settlement differs from the signed configuration"
                )
            if not self._perpetual:
                continue
            config = configs.get(symbol)
            if config is None:
                raise VenueLedgerError(
                    "SoDEX requires explicit account leverage and margin settings"
                )
            leverage = config["l"]
            if int(leverage) != self._leverage:
                raise VenueLedgerError(
                    "SoDEX account leverage differs from the signed deployment; configure it before starting"
                )
            if config is not None:
                margin = {1: "isolated", 2: "cross", "ISOLATED": "isolated", "CROSS": "cross"}.get(
                    config.get("m")
                )
                if margin != self._margin:
                    raise VenueLedgerError(
                        "SoDEX account margin mode differs from the signed deployment"
                    )

    def _history(self, suffix: str, symbol: str, start: int, end: int) -> list[dict]:
        pending = [(start, end)]
        result: dict[str, dict] = {}
        requests = 0
        limit = 500 if suffix == "positions/history" else 1000
        while pending:
            lo, hi = pending.pop()
            requests += 1
            if requests > 1000:
                raise VenueLedgerError("SoDEX history exceeds bounded pagination")
            page = rows(
                self._get(
                    suffix, {"symbol": symbol, "startTime": lo, "endTime": hi, "limit": limit}
                )
            )
            if len(page) >= limit:
                if lo == hi:
                    raise VenueLedgerError("SoDEX history is truncated within one millisecond")
                midpoint = (lo + hi) // 2
                pending.extend([(lo, midpoint), (midpoint + 1, hi)])
                continue
            for row in page:
                at = int(
                    row[
                        {
                            "trades": "time",
                            "fundings": "timestamp",
                            "positions/history": "updatedAt",
                        }[suffix]
                    ]
                )
                if row.get("symbol") != symbol or not lo <= at <= hi:
                    raise VenueLedgerError("SoDEX history escaped the requested interval/symbol")
                key = (
                    str(row["tradeID"])
                    if suffix == "trades"
                    else str(row["id"])
                    if suffix == "positions/history"
                    else f"{row['positionID']}:{row['positionSide']}:{at}"
                )
                if key in result and result[key] != row:
                    raise VenueLedgerError("SoDEX history has conflicting identities")
                result[key] = row
        return list(result.values())

    async def collect(self, coverage_from: datetime, closed_at: datetime) -> VenueLedgerEvidence:
        return await asyncio.to_thread(self._collect, coverage_from, closed_at)

    def _collect(self, start: datetime, end: datetime) -> VenueLedgerEvidence:
        begin, finish = period_bounds(start, end)
        state = self._state()
        snapshot = self._get("balances")
        if not isinstance(snapshot, dict) or int(snapshot.get("blockHeight", 0)) <= 0:
            raise VenueLedgerError("SoDEX balance snapshot has no valid chain height")
        observed = timestamp_ms(snapshot.get("blockTime"))
        if observed < end:
            raise VenueLedgerError("SoDEX snapshot precedes the requested close")
        balances = []
        available = {str(row["a"]).upper(): row for row in rows(state.get("B") or [])}
        for row in rows(snapshot.get("balances")):
            currency = str(row["coin"]).upper()
            total = decimal(row["total"], "total")
            if currency not in SUPPORTED_CURRENCIES:
                if total:
                    raise VenueLedgerError("SoDEX balance currency is unsupported")
                continue
            if self._perpetual:
                value = available.get(currency)
                if value is None or decimal(value["wb"], "wallet balance") != total:
                    raise VenueLedgerError("SoDEX balance changed across the collection window")
                free = decimal(value["aw"], "available wallet balance")
            else:
                free = total - decimal(row["locked"], "locked balance")
            balances.append(
                {
                    "asset": currency,
                    "currency": currency,
                    "total": str(total),
                    "available": str(free),
                }
            )
        positions, fills, fees = [], [], []
        if self._perpetual:
            current = self._get("positions")
            if current.get("blockHeight") != snapshot["blockHeight"]:
                raise VenueLedgerError(
                    "SoDEX position and balance snapshots have different chain heights"
                )
            for row in rows(current.get("positions")):
                symbol = str(row["symbol"])
                if symbol not in self._symbols or not row["active"]:
                    continue
                qty = decimal(row["size"], "position size")
                if not qty:
                    continue
                side = str(row["positionSide"])
                if side not in {"BOTH", "LONG", "SHORT"}:
                    raise VenueLedgerError("SoDEX position side is invalid")
                positions.append(
                    {
                        "venue_position_id": str(row["id"]),
                        "instrument": symbol + "." + self._venue,
                        "side": "sell"
                        if side == "SHORT" or (side == "BOTH" and qty < 0)
                        else "buy",
                        "quantity": str(abs(qty)),
                        "avg_entry_price": str(decimal(row["avgEntryPrice"], "entry price")),
                        "currency": self._settlement,
                    }
                )
        for symbol in self._symbols:
            quote = self._settlement
            for row in self._history("trades", symbol, begin, finish):
                fee_currency = str(row["feeCoin"]).upper()
                if fee_currency not in SUPPORTED_CURRENCIES:
                    raise VenueLedgerError("SoDEX fee currency is unsupported")
                if decimal(row.get("builderFee", "0"), "builder fee"):
                    raise VenueLedgerError(
                        "SoDEX builder fees require separately supported commission evidence"
                    )
                fee, at = str(decimal(row["fee"], "fee")), timestamp_ms(row["time"]).isoformat()
                trade_id = str(row["tradeID"])
                fills.append(
                    {
                        "venue_trade_id": trade_id,
                        "venue_order_id": str(row["orderID"]),
                        "instrument": symbol + "." + self._venue,
                        "side": str(row["side"]).lower(),
                        "quantity": str(decimal(row["quantity"], "quantity")),
                        "price": str(decimal(row["price"], "price")),
                        "fee": fee,
                        "currency": quote,
                        "fee_currency": fee_currency,
                        "occurred_at": at,
                    }
                )
                fees.append(
                    {
                        "fee_id": f"trade:{symbol}:{trade_id}:commission",
                        "kind": "commission",
                        "amount": fee,
                        "currency": fee_currency,
                        "occurred_at": at,
                    }
                )
            if self._perpetual:
                for row in self._history("positions/history", symbol, begin, finish):
                    if row.get("active") is not False or decimal(row["size"], "closed size"):
                        raise VenueLedgerError(
                            "SoDEX closed position history contains an open position"
                        )
                    if row.get("isTakenOver") is not False:
                        raise VenueLedgerError(
                            "SoDEX liquidation requires independent loss reconciliation"
                        )
                    pnl = decimal(row["realizedPnL"], "realized position PnL")
                    fees.append(
                        {
                            "fee_id": f"position:{symbol}:{row['id']}:realized",
                            "kind": "realized_pnl_credit" if pnl >= 0 else "realized_pnl_debit",
                            "amount": str(abs(pnl)),
                            "currency": self._settlement,
                            "occurred_at": timestamp_ms(row["updatedAt"]).isoformat(),
                        }
                    )
                for row in self._history("fundings", symbol, begin, finish):
                    amount = decimal(row["fundingFee"], "funding fee")
                    currency = str(row["feeCoin"]).upper()
                    if currency not in SUPPORTED_CURRENCIES:
                        raise VenueLedgerError("SoDEX funding currency is unsupported")
                    fees.append(
                        {
                            "fee_id": f"funding:{symbol}:{row['positionID']}:{row['timestamp']}",
                            "kind": "funding_cost" if amount >= 0 else "funding_credit",
                            "amount": str(abs(amount)),
                            "currency": currency,
                            "occurred_at": timestamp_ms(row["timestamp"]).isoformat(),
                        }
                    )
        watermark = hashlib.sha256(
            json.dumps(
                [snapshot["blockHeight"], balances, positions, fills, fees], sort_keys=True
            ).encode()
        ).hexdigest()
        return VenueLedgerEvidence(
            venue=self._venue,
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
