from __future__ import annotations

from datetime import UTC, datetime

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


def _spot_source() -> BinanceVenueLedgerSource:
    return BinanceVenueLedgerSource(
        spec={
            "trading_mode": "testnet",
            "connector": "binance",
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
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.0070",
                }
            ],
        },
        futures_positions=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "0.0070",
                "entryPrice": "63567.0",
            }
        ],
    )

    assert balances == [
        {
            "asset": "USDT",
            "currency": "USDT",
            "total": "4462.00816174",
            "available": "4462.00816174",
        }
    ]
    assert positions == [
        {
            "venue_position_id": "BTCUSDT:BOTH",
            "instrument": "BTCUSDT-PERP.BINANCE",
            "side": "buy",
            "quantity": "0.007",
            "avg_entry_price": "63567",
            "currency": "USDT",
        }
    ]


def test_perpetual_trade_rows_use_the_canonical_instrument_identity() -> None:
    fills, fees = _source()._trade_rows(
        [
            {
                "symbol": "BTCUSDT",
                "id": 527_313_311,
                "orderId": 28_540_317_470,
                "side": "BUY",
                "qty": "0.0070",
                "price": "63504.90",
                "commission": "0.17781372",
                "commissionAsset": "USDT",
                "time": 1_786_662_060_971,
            }
        ]
    )

    assert fills[0]["instrument"] == "BTCUSDT-PERP.BINANCE"
    assert fees[0]["amount"] == "0.17781372"


def test_spot_trade_rows_preserve_base_asset_commission_currency() -> None:
    fills, fees = _spot_source()._trade_rows(
        [
            {
                "symbol": "BTCUSDT",
                "id": 1,
                "orderId": 2,
                "isBuyer": True,
                "qty": "1",
                "price": "100",
                "commission": "0.001",
                "commissionAsset": "BTC",
                "time": 1_786_662_060_971,
            }
        ]
    )

    assert fills[0]["currency"] == "USDT"
    assert fills[0]["fee"] == "0.001"
    assert fees[0]["currency"] == "BTC"
    assert fees[0]["amount"] == "0.001"


def test_venue_event_queries_use_a_half_open_reconciliation_period() -> None:
    closed_at = datetime(2026, 8, 13, 23, 2, tzinfo=UTC)

    assert _source()._closed_interval_end_ms(closed_at) == 1_786_662_119_999


def test_perpetual_position_risk_must_match_account_quantity() -> None:
    source = _source()

    try:
        source._account_rows(
            {
                "assets": [],
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": "0.0070",
                    }
                ],
            },
            futures_positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.0060",
                    "entryPrice": "63567.0",
                }
            ],
        )
    except RuntimeError as error:
        assert "position-risk quantity differs" in str(error)
    else:
        raise AssertionError("mismatched account and position-risk quantities must fail closed")


def test_common_valuation_inputs_use_wallet_balance_and_position_risk_mark() -> None:
    source = _source()

    assert source._wallet_balances(
        {
            "assets": [
                {"asset": "BTC", "walletBalance": "1"},
                {"asset": "USDT", "walletBalance": "4466.5"},
            ]
        }
    ) == {"USDT": "4466.5"}
    assert source._valuation_positions(
        [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "-0.0070",
                "entryPrice": "63567.0",
                "markPrice": "63504.9",
            }
        ]
    ) == [
        {
            "instrument": "BTCUSDT-PERP.BINANCE",
            "currency": "USDT",
            "quantity": "-0.007",
            "avg_entry_price": "63567",
            "mark_price": "63504.9",
        }
    ]


def test_a_spot_fill_carries_the_currency_its_fee_was_actually_charged_in() -> None:
    """The fill row and its fee row must not name two different currencies.

    A BTC commission on a USDT-quoted trade currently produces a fill saying
    "fee 0.001 USDT" next to a fee row saying "0.001 BTC" -- the same number,
    two currencies, one of them wrong. `fee_currency` is the existing V1 field
    for exactly this; OKX and SoDEX already fill it in.
    """
    fills, fees = _spot_source()._trade_rows(
        [
            {
                "symbol": "BTCUSDT",
                "id": 1,
                "orderId": 2,
                "isBuyer": True,
                "qty": "1",
                "price": "100",
                "commission": "0.001",
                "commissionAsset": "BTC",
                "time": 1_786_662_060_971,
            }
        ]
    )

    assert fills[0]["currency"] == "USDT", "the trade is still priced in the quote"
    assert fills[0]["fee_currency"] == "BTC", "but the fee was charged in BTC"
    assert fills[0]["fee_currency"] == fees[0]["currency"], (
        "the fill and its fee row must agree about the fee's currency"
    )


def test_the_fee_currency_survives_venue_snapshot_serialization() -> None:
    """Asserting the dict is not enough -- the wire is built by another function.

    `_venue_ledger_snapshot` copies a fixed set of keys; a field it does not know
    about is dropped without complaint. So the assertion has to be made after
    serialization, on the row a consumer would actually read.
    """
    from datetime import UTC, datetime

    from custos.core.runner_fact import venue_ledger_snapshot_facts

    fills, fees = _spot_source()._trade_rows(
        [
            {
                "symbol": "BTCUSDT",
                "id": 1,
                "orderId": 2,
                "isBuyer": True,
                "qty": "1",
                "price": "100",
                "commission": "0.001",
                "commissionAsset": "BTC",
                "time": 1_786_662_060_971,
            }
        ]
    )
    observed = datetime(2026, 9, 21, tzinfo=UTC)
    chunks = venue_ledger_snapshot_facts(
        snapshot_id="90000000-0000-4000-8000-000000000001",
        venue="BINANCE",
        source="venue_api",
        watermark="w-1",
        coverage_from=observed,
        observed_through=observed,
        completeness={
            "balances_complete": True,
            "positions_complete": True,
            "fills_complete": True,
            "fees_complete": True,
        },
        balances=[],
        positions=[],
        fills=fills,
        fees=fees,
    )

    wire_fills = [row for chunk in chunks for row in chunk.get("fills", [])]
    assert len(wire_fills) == 1
    wire_fill = wire_fills[0]
    assert wire_fill["currency"] == "USDT"
    assert wire_fill["fee_currency"] == "BTC"
