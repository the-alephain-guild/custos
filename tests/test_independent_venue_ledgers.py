"""Independent venue evidence must retain units, identity and complete pagination."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custos.engines.nautilus.ledger_http import VenueLedgerError, decimal
from custos.engines.nautilus.sodex_ledger import SodexVenueLedgerSource


def sodex(perpetual=True):
    return SodexVenueLedgerSource(
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
                    "margin_mode": "cross",
                }
            },
        },
        {},
    )


def okx(perpetual=True):
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

    return OkxVenueLedgerSource(
        {
            "connector": "okx_perpetual" if perpetual else "okx",
            "trading_mode": "testnet",
            "pairs": ["BTC-USDT"],
            "leverage": 3 if perpetual else 1,
        },
        {"api_key": "key", "api_secret": "secret", "api_passphrase": "phrase"},
    )


@pytest.mark.parametrize("value", [1.1, True, None, "NaN", "Infinity"])
def test_inexact_or_nonfinite_ledger_values_are_rejected(value):
    with pytest.raises(VenueLedgerError):
        decimal(value, "amount")


def test_sodex_account_state_is_bound_to_the_wallet_and_account(monkeypatch):
    source = sodex()
    monkeypatch.setattr(source, "_get", lambda *a: {"user": "0x" + "b" * 40, "aid": 42})
    with pytest.raises(VenueLedgerError, match="does not match"):
        source.validate_account()


def test_sodex_saturated_history_cannot_claim_completeness(monkeypatch):
    source = sodex()
    monkeypatch.setattr(source, "_get", lambda *a: [{}] * 1000)
    with pytest.raises(VenueLedgerError, match="one millisecond"):
        source._history("trades", "BTC-USD", 100, 100)


def test_sodex_history_bisects_without_losing_boundary_trades(monkeypatch):
    source = sodex()
    calls = []

    def get(_suffix, params):
        lo, hi = params["startTime"], params["endTime"]
        calls.append((lo, hi))
        if hi > lo:
            return [{}] * 1000
        return [{"symbol": "BTC-USD", "tradeID": lo, "time": lo}]

    monkeypatch.setattr(source, "_get", get)
    assert {r["tradeID"] for r in source._history("trades", "BTC-USD", 100, 101)} == {100, 101}
    assert sorted(calls) == [(100, 100), (100, 101), (101, 101)]


def test_sodex_spot_validates_actual_settlement_coin(monkeypatch):
    source = sodex(False)
    monkeypatch.setattr(source, "_state", lambda: {})
    monkeypatch.setattr(
        source._http, "get", lambda *a: [{"name": "vBTC_vUSDC", "quoteCoin": "USDT"}]
    )
    with pytest.raises(VenueLedgerError, match="settlement"):
        source.validate_account()


def test_okx_history_cannot_accept_another_instrument(monkeypatch):
    source = okx()
    monkeypatch.setattr(
        source, "_get", lambda *a: [{"billId": "1", "ts": "101", "instId": "ETH-USDT-SWAP"}]
    )
    with pytest.raises(VenueLedgerError, match="instrument"):
        source._history("/fills", {"instId": "BTC-USDT-SWAP"}, 100, 102)


def test_okx_linear_contract_multiplier_is_exact(monkeypatch):
    source = okx()
    monkeypatch.setattr(
        source,
        "_get",
        lambda *a, **kw: [
            {
                "instId": "BTC-USDT-SWAP",
                "ctType": "linear",
                "ctValCcy": "BTC",
                "ctVal": "0.01",
                "ctMult": "1",
            }
        ],
    )
    assert source._multiplier("BTC-USDT-SWAP") == Decimal("0.01")


def test_okx_perpetual_fill_carries_base_quantity_and_independent_fee_currency(monkeypatch):
    source = okx()
    now = datetime.now(UTC).replace(microsecond=0)
    stamp = int(now.timestamp() * 1000)

    def get(path, params, **kw):
        if path.endswith("/time"):
            return [{"ts": str(stamp + 1000)}]
        if path.endswith("/balance"):
            return [{"details": [{"ccy": "USDT", "cashBal": "100", "availBal": "90"}]}]
        if path.endswith("/instruments"):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ctType": "linear",
                    "ctValCcy": "BTC",
                    "ctVal": "0.01",
                    "ctMult": "1",
                }
            ]
        if path.endswith("/fills-history"):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "billId": "1",
                    "tradeId": "2",
                    "ordId": "3",
                    "ts": str(stamp - 1),
                    "side": "buy",
                    "fillSz": "3",
                    "fillPx": "60000",
                    "fee": "0.000001",
                    "feeCcy": "BTC",
                    "fillPnl": "0",
                }
            ]
        return []

    monkeypatch.setattr(source, "_get", get)
    evidence = source._collect(now - timedelta(seconds=10), now)
    fill = evidence.fills[0]
    assert Decimal(fill["quantity"]) == Decimal("0.03")
    assert fill["currency"] == "USDT"
    assert fill["fee_currency"] == "BTC"
    assert Decimal(fill["fee"]) == Decimal("-0.000001")
    assert evidence.fees[0]["currency"] == "BTC"


def test_okx_sandbox_refuses_environment_credential_fallback(monkeypatch):
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.venue_okx import build_data_client_config_for_mode

    monkeypatch.setenv("OKX_API_KEY", "must-not-be-used")
    with pytest.raises(ValueError, match="inherit"):
        build_data_client_config_for_mode(
            {"connector": "okx", "pairs": ["BTC-USDT"]}, {}, "sandbox"
        )


def test_okx_position_history_paginates_by_time_and_excludes_funding(monkeypatch):
    source = okx()
    calls = []

    def get(path, params, **kw):
        calls.append(params)
        return [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "7",
                "uTime": "150",
                "cTime": "110",
                "type": "2",
                "pnl": "12",
                "fee": "-2",
                "fundingFee": "-1",
                "liqPenalty": "0",
                "settledPnl": "0",
                "ccy": "USDT",
            }
        ]

    monkeypatch.setattr(source, "_get", get)
    result = source._closed_position_pnl("BTC-USDT-SWAP", 100, 200)
    assert result[0]["amount"] == "10"
    assert result[0]["kind"] == "realized_pnl_credit"
    assert calls[0]["after"] == 201


def test_sodex_closed_position_history_has_its_own_page_limit(monkeypatch):
    source = sodex()
    calls = []

    def get(path, params):
        calls.append(params)
        return [{"symbol": "BTC-USD", "id": 5, "updatedAt": 100}]

    monkeypatch.setattr(source, "_get", get)
    assert len(source._history("positions/history", "BTC-USD", 100, 101)) == 1
    assert calls[0]["limit"] == 500


def test_okx_order_id_accepts_full_uuid_but_refuses_punctuation():
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.venue_okx import (
        client_order_id_is_valid,
        client_order_id_len_limit,
    )

    assert client_order_id_is_valid("a" * 32)
    assert len("a" * 32) < client_order_id_len_limit()
    assert not client_order_id_is_valid("uuid-with-hyphens")
    assert not client_order_id_is_valid("\u8ba2\u5355")
