"""OKX reports the same commission twice, once disguised as profit.

``positions-history`` hands back ``pnl`` and ``fee`` as separate fields, and the
ledger folds them together into one realized-PnL row. But the very same
commission is already emitted on its own, one ``kind="commission"`` row per fill
from ``fills-history``. Inside a window that contains both the open and the
close, that money is counted twice.

Binance is the reference: its ``REALIZED_PNL`` income is gross, because
``COMMISSION`` is a separate income type there. OKX is the odd one out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime.now(UTC).replace(microsecond=0)
START, END = NOW - timedelta(seconds=60), NOW
STAMP = int((END + timedelta(seconds=1)).timestamp() * 1000)
TRADE_TS = int((NOW - timedelta(seconds=30)).timestamp() * 1000)


def _okx_that_opened_and_closed_one_position():
    """A window holding the whole life of one position: its fills and its close.

    Every number below is what OKX itself returns, in OKX's own shape -- gross
    ``pnl`` of 12, ``fee`` of -2 on the position, and the two fills whose
    commissions add up to exactly that -2.
    """
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource

    source = OkxVenueLedgerSource(
        {
            "connector": "okx_perpetual",
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
            return [{"details": [{"ccy": "USDT", "cashBal": "100", "availBal": "100"}]}]
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
            return []  # The position is closed; nothing is open any more.
        if path.endswith("/positions-history"):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "7",
                    "uTime": str(TRADE_TS),
                    "cTime": str(TRADE_TS - 1000),
                    "type": "2",
                    "pnl": "12",
                    "fee": "-2",
                    "fundingFee": "0",
                    "liqPenalty": "0",
                    "settledPnl": "0",
                    "ccy": "USDT",
                }
            ]
        if path.endswith("/fills-history"):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "billId": "b1",
                    "tradeId": "t1",
                    "ordId": "o1",
                    "side": "buy",
                    "fillSz": "1",
                    "fillPx": "100",
                    "fee": "-1",
                    "feeCcy": "USDT",
                    "ts": str(TRADE_TS - 500),
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "billId": "b2",
                    "tradeId": "t2",
                    "ordId": "o2",
                    "side": "sell",
                    "fillSz": "1",
                    "fillPx": "112",
                    "fee": "-1",
                    "feeCcy": "USDT",
                    "ts": str(TRADE_TS),
                },
            ]
        return []

    source._get = get
    return source


def test_the_closed_position_reports_profit_before_its_commission() -> None:
    """Gross, because the commission has a row of its own.

    12 rather than 10: the two units of commission belong to the commission
    rows, and adding them here would be the second place they are told.
    """
    source = _okx_that_opened_and_closed_one_position()
    realized = [
        row
        for row in source._closed_position_pnl(
            "BTC-USDT-SWAP", int(START.timestamp() * 1000), STAMP
        )
        if row["kind"].startswith("realized_pnl")
    ]

    assert len(realized) == 1
    assert realized[0]["kind"] == "realized_pnl_credit"
    assert realized[0]["amount"] == "12"


def test_one_commission_is_told_once_across_both_collection_paths() -> None:
    """The point of the whole fix, and the reason the test spans a full collect.

    Asserting the realized-PnL row alone cannot see this: the double count only
    exists because a second path reports the same money. Put the two units of
    commission and the 12 of gross profit in one window, and the arithmetic has
    to close.
    """
    evidence = _okx_that_opened_and_closed_one_position()._collect(START, END)
    fees = evidence.fees

    commission = sum(float(row["amount"]) for row in fees if row["kind"] == "commission")
    realized = sum(float(row["amount"]) for row in fees if row["kind"] == "realized_pnl_credit")
    assert commission == 2.0, "both fills' commissions must be present exactly once"
    assert realized == 12.0, "the realized row must not have absorbed the commission"
    assert realized - commission == 10.0, (
        "net profit is what the consumer computes from the two rows, not what we pre-compute"
    )
