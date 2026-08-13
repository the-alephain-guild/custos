from __future__ import annotations

from custos.engines.nautilus.binance_ledger import BinanceVenueLedgerSource


def _source() -> BinanceVenueLedgerSource:
    return BinanceVenueLedgerSource(
        spec={
            "trading_mode": "testnet",
            "connector": "binance_perpetual",
            "pairs": ["BTC-USDT"],
        },
        credential={
            "api_key": "test-key",
            "api_secret": "test-secret",
            "key_type": "HMAC",
        },
    )


def test_income_rows_preserve_realized_pnl_and_funding_direction() -> None:
    rows = _source()._income_fee_rows(
        [
            {
                "tranId": 1,
                "incomeType": "REALIZED_PNL",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "12.5",
                "time": 1_786_000_000_000,
            },
            {
                "tranId": 2,
                "incomeType": "REALIZED_PNL",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "-3.25",
                "time": 1_786_000_001_000,
            },
            {
                "tranId": 3,
                "incomeType": "FUNDING_FEE",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "0.75",
                "time": 1_786_000_002_000,
            },
            {
                "tranId": 4,
                "incomeType": "FUNDING_FEE",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "-0.5",
                "time": 1_786_000_003_000,
            },
        ]
    )

    assert [(row["kind"], row["amount"]) for row in rows] == [
        ("realized_pnl_credit", "12.5"),
        ("realized_pnl_debit", "3.25"),
        ("funding_credit", "0.75"),
        ("funding_cost", "0.5"),
    ]
    assert len({row["fee_id"] for row in rows}) == 4


def test_income_rows_ignore_unrelated_account_income_and_other_symbols() -> None:
    rows = _source()._income_fee_rows(
        [
            {
                "tranId": 5,
                "incomeType": "TRANSFER",
                "symbol": "BTCUSDT",
                "asset": "USDT",
                "income": "100",
                "time": 1_786_000_004_000,
            },
            {
                "tranId": 6,
                "incomeType": "REALIZED_PNL",
                "symbol": "ETHUSDT",
                "asset": "USDT",
                "income": "9",
                "time": 1_786_000_005_000,
            },
        ]
    )

    assert rows == []


def test_perpetual_account_balances_only_include_settlement_currencies() -> None:
    balances, positions = _source()._account_rows(
        {
            "assets": [
                {
                    "asset": "BTC",
                    "marginBalance": "0.01",
                    "availableBalance": "0.01",
                },
                {
                    "asset": "USDT",
                    "marginBalance": "4462.00816174",
                    "availableBalance": "4462.00816174",
                },
            ],
            "positions": [],
        }
    )

    assert balances == [
        {
            "asset": "USDT",
            "currency": "USDT",
            "total": "4462.00816174",
            "available": "4462.00816174",
        }
    ]
    assert positions == []
