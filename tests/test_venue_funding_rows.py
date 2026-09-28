"""Perpetual funding reaches the signed venue ledger with its direction intact.

Crucible sums these rows into a settlement's observed funding: a
``funding_cost`` counts against the account and a ``funding_credit`` for it.
Each venue signs the payment differently, so each mapping is pinned here.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from custos.core.runner_fact import venue_ledger_snapshot_facts
from tests.test_independent_venue_ledgers import okx, sodex


def _funding(fees):
    return {
        row["fee_id"]: (row["kind"], Decimal(row["amount"]), row["currency"])
        for row in fees
        if row["kind"].startswith("funding_")
    }


def test_okx_funding_bills_keep_their_direction(monkeypatch):
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
        return []

    history = source._history

    def funding_history(path, params, start, end):
        if not path.endswith("/bills-archive"):
            return history(path, params, start, end)
        assert params == {"instType": "SWAP", "type": "8"}
        return [
            {
                "instId": "BTC-USDT-SWAP",
                "billId": "1",
                "ccy": "USDT",
                "balChg": "-0.5",
                "ts": str(stamp - 3),
            },
            {
                "instId": "BTC-USDT-SWAP",
                "billId": "2",
                "ccy": "USDT",
                "balChg": "0.2",
                "ts": str(stamp - 2),
            },
            {
                "instId": "ETH-USDT-SWAP",
                "billId": "3",
                "ccy": "USDT",
                "balChg": "-9",
                "ts": str(stamp - 1),
            },
        ]

    monkeypatch.setattr(source, "_get", get)
    monkeypatch.setattr(source, "_history", funding_history)
    evidence = source._collect(now - timedelta(seconds=10), now)

    assert _funding(evidence.fees) == {
        "funding:1": ("funding_cost", Decimal("0.5"), "USDT"),
        "funding:2": ("funding_credit", Decimal("0.2"), "USDT"),
    }


def test_sodex_funding_treats_a_positive_fee_as_a_cost(monkeypatch):
    """SoDEX reports what the account paid, so a positive fee is a cost."""
    source = sodex()
    now = datetime.now(UTC).replace(microsecond=0)
    stamp = int(now.timestamp() * 1000)
    monkeypatch.setattr(source, "_state", lambda: {})
    monkeypatch.setattr(
        source,
        "_get",
        lambda suffix, params=None: (
            {"blockHeight": 1, "blockTime": stamp + 1000, "balances": []}
            if suffix == "balances"
            else {"blockHeight": 1, "blockTime": stamp + 1000, "positions": []}
        ),
    )

    def history(suffix, symbol, start, end):
        if suffix != "fundings":
            return []
        return [
            {"positionID": 7, "timestamp": stamp - 2, "fundingFee": "0.3", "feeCoin": "vusdc"},
            {"positionID": 7, "timestamp": stamp - 1, "fundingFee": "-0.1", "feeCoin": "vusdc"},
        ]

    monkeypatch.setattr(source, "_history", history)
    evidence = source._collect(now - timedelta(seconds=10), now)

    assert _funding(evidence.fees) == {
        f"funding:BTC-USD:7:{stamp - 2}": ("funding_cost", Decimal("0.3"), "VUSDC"),
        f"funding:BTC-USD:7:{stamp - 1}": ("funding_credit", Decimal("0.1"), "VUSDC"),
    }


def test_funding_rows_travel_in_the_signed_snapshot_chunks():
    at = datetime(2026, 8, 2, tzinfo=UTC)
    fees = [
        {
            "fee_id": "funding:1",
            "kind": "funding_cost",
            "currency": "USDT",
            "amount": "0.5",
            "occurred_at": at,
        },
        {
            "fee_id": "funding:2",
            "kind": "funding_credit",
            "currency": "USDT",
            "amount": "0.2",
            "occurred_at": at,
        },
    ]
    facts = venue_ledger_snapshot_facts(
        snapshot_id=uuid4(),
        venue="BINANCE",
        sub_account=None,
        source="venue_api",
        watermark="w",
        coverage_from=datetime(2026, 8, 1, tzinfo=UTC),
        observed_through=datetime(2026, 9, 1, tzinfo=UTC),
        completeness={
            "balances_complete": True,
            "positions_complete": True,
            "fills_complete": True,
            "fees_complete": True,
            "cash_flows_complete": False,
        },
        balances=[],
        positions=[],
        fills=[],
        fees=fees,
        cash_flows=[],
    )

    manifest = next(f for f in facts if f["kind"] == "venue_ledger_snapshot_manifest")
    chunk_fees = [
        row for f in facts if f["kind"] == "venue_ledger_snapshot_chunk" for row in f["fees"]
    ]
    assert manifest["fees_count"] == 2
    assert {(row["fee_id"], row["kind"], Decimal(row["amount"])) for row in chunk_fees} == {
        ("funding:1", "funding_cost", Decimal("0.5")),
        ("funding:2", "funding_credit", Decimal("0.2")),
    }


@pytest.fixture(autouse=True)
def _nautilus_for_okx(request):
    if "okx" in request.node.name:
        pytest.importorskip("nautilus_trader")
