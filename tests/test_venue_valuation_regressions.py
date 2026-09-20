"""Regression fixtures for venue wallet and independent common-mark valuation."""

from datetime import UTC, datetime, timedelta

import pytest

from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource
from custos.engines.nautilus.sodex_ledger import SodexVenueLedgerSource

NOW = datetime.now(UTC).replace(microsecond=0)
START, END = NOW - timedelta(seconds=60), NOW
STAMP = int((END + timedelta(seconds=1)).timestamp() * 1000)


def source(venue, perpetual):
    if venue == "OKX":
        result = OkxVenueLedgerSource(
            {
                "connector": "okx_perpetual" if perpetual else "okx",
                "trading_mode": "testnet",
                "pairs": ["BTC-USDT"],
                "leverage": 1,
            },
            {"api_key": "fixture", "api_secret": "fixture", "api_passphrase": "fixture"},
        )

        def get(path, params, **kwargs):
            if path.endswith("/time"):
                return [{"ts": str(STAMP)}]
            if path.endswith("/balance"):
                rows = [{"ccy": "USDT", "cashBal": "100", "availBal": "90"}]
                if not perpetual:
                    rows.append({"ccy": "BTC", "cashBal": "1", "availBal": "1"})
                return [{"details": rows}]
            if path.endswith("/instruments"):
                return [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "ctType": "linear",
                        "ctValCcy": "BTC",
                        "ctVal": "1",
                        "ctMult": "1",
                    }
                ]
            if path.endswith("/positions"):
                return [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "pos": "1",
                        "posId": "1",
                        "posSide": "net",
                        "avgPx": "90",
                        "markPx": "100",
                    }
                ]
            return []

        result._get = get
        return result
    result = SodexVenueLedgerSource(
        {
            "connector": "sodex_perpetual" if perpetual else "sodex",
            "trading_mode": "testnet",
            "pairs": ["BTC-USD" if perpetual else "vBTC_vUSDC"],
            "leverage": 1,
            "nautilus_config": {
                "venue": {
                    "wallet_address": "0x" + "a" * 40,
                    "sodex_account_id": 42,
                    "settlement_currency": "VUSDC",
                }
            },
        },
        {},
    )
    result._state = lambda: {"B": [{"a": "vUSDC", "wb": "100", "aw": "90"}]}

    def get(path, params=None):
        if path == "balances":
            rows = [{"coin": "vUSDC", "total": "100", "locked": "0"}]
            if not perpetual:
                rows.append({"coin": "vBTC", "total": "1", "locked": "0"})
            return {"blockHeight": 42, "blockTime": STAMP, "balances": rows}
        if path == "positions":
            return {
                "blockHeight": 42,
                "positions": [
                    {
                        "symbol": "BTC-USD",
                        "active": True,
                        "size": "1",
                        "positionSide": "BOTH",
                        "id": 1,
                        "avgEntryPrice": "90",
                    }
                ],
            }
        return []

    result._get = get
    result._http.get = lambda *args: [
        {"symbol": "BTC-USD", "markPrice": "100", "lastPx": "100", "bidPx": "99", "askPx": "101"}
    ]
    return result


@pytest.mark.parametrize("venue", ["OKX", "SODEX"])
def test_perpetual_ledger_supplies_wallet_cost_and_common_mark(venue):
    evidence = source(venue, True)._collect(START, END)
    currency = "USDT" if venue == "OKX" else "VUSDC"
    assert evidence.valuation_collection_started_at is not None
    assert evidence.venue_wallet_balances[currency] == "100"
    assert evidence.valuation_positions[0]["mark_price"] == "100"
    assert evidence.valuation_positions[0]["avg_entry_price"] == "90"
    assert evidence.valuation_positions[0]["quantity"] == "1"


@pytest.mark.parametrize("venue", ["OKX", "SODEX"])
def test_perpetual_missing_independent_mark_refuses_evidence(venue):
    from custos.engines.nautilus.ledger_http import VenueLedgerError

    ledger = source(venue, True)
    if venue == "OKX":
        original = ledger._get

        def get(path, *args, **kwargs):
            data = original(path, *args, **kwargs)
            if path.endswith("/positions"):
                data[0].pop("markPx")
            return data

        ledger._get = get
    else:
        ledger._http.get = lambda *args: [{"symbol": "BTC-USD"}]
    with pytest.raises(VenueLedgerError):
        ledger._collect(START, END)
