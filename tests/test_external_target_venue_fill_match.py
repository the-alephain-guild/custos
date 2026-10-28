"""Independent ledger comparison never turns a local callback into final execution."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from custos.core.runner_fact_producer import VenueLedgerEvidence
from custos.engines.nautilus.external_target_inbox import ExternalTargetInbox
from custos.engines.nautilus.external_target_venue_fill_match import compare_known_order_fills
from tests.test_external_target_inbox import FIRST, NOW
from tests.test_external_target_order_lineage import queued


def report(tmp_path: Path, *, filled: bool = True):
    inbox = ExternalTargetInbox(tmp_path / "target.sqlite")
    delivery = queued(inbox, FIRST)
    assert inbox.record_order_initialized(
        FIRST,
        delivery.delivery_id,
        client_order_id="O-1",
        instrument="ETH-USDT-SWAP.OKX",
        event_id="init-1",
        at=NOW,
    )
    if filled:
        assert inbox.record_order_fill(
            FIRST,
            client_order_id="O-1",
            event_id="fill-1",
            venue_order_id="V-1",
            venue_trade_id="T-1",
            quantity="2",
            at=NOW + timedelta(seconds=1),
        )
    result = inbox.target_execution(FIRST, delivery.delivery_id)
    assert result is not None
    return result


def ledger(*, fills=None, complete=True, venue="OKX", start=None, observed=None):
    return VenueLedgerEvidence(
        venue=venue,
        source="venue_api",
        watermark="ledger-test",
        coverage_from=start or NOW - timedelta(seconds=1),
        observed_through=observed or NOW + timedelta(seconds=31),
        completeness={"fills_complete": complete},
        balances=(),
        positions=(),
        fills=fills
        if fills is not None
        else [
            {
                "venue_order_id": "V-1",
                "venue_trade_id": "T-1",
                "instrument": "ETH-USDT-SWAP.OKX",
                "quantity": "0.2",
            }
        ],
        fees=(),
    )


def test_known_order_fills_match_independent_venue_ids(tmp_path: Path) -> None:
    comparison = compare_known_order_fills(
        report(tmp_path), ledger(), closed_at=NOW + timedelta(seconds=30)
    )
    assert comparison.status == "matched_known_order_fills"
    assert comparison.matched_trade_ids == ("T-1",)
    assert comparison.local_only_trade_ids == comparison.venue_only_trade_ids == ()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"fills": []}, "local_only_trade_ids"),
        (
            {
                "fills": [
                    {
                        "venue_order_id": "V-1",
                        "venue_trade_id": "T-1",
                        "instrument": "ETH-USDT-SWAP.OKX",
                    },
                    {
                        "venue_order_id": "V-1",
                        "venue_trade_id": "T-2",
                        "instrument": "ETH-USDT-SWAP.OKX",
                    },
                ]
            },
            "venue_only_trade_ids",
        ),
    ],
)
def test_missing_or_extra_venue_fill_is_disagreement(tmp_path: Path, changes, expected) -> None:
    comparison = compare_known_order_fills(
        report(tmp_path), replace(ledger(), **changes), closed_at=NOW + timedelta(seconds=30)
    )
    assert comparison.status == "disagreement"
    assert getattr(comparison, expected)


@pytest.mark.parametrize(
    "evidence",
    [
        {"complete": False},
        {"venue": "BINANCE"},
        {"start": NOW + timedelta(seconds=1)},
        {"observed": NOW + timedelta(seconds=1)},
    ],
)
def test_wrong_or_incomplete_ledger_remains_unknown(tmp_path: Path, evidence) -> None:
    comparison = compare_known_order_fills(
        report(tmp_path), ledger(**evidence), closed_at=NOW + timedelta(seconds=30)
    )
    assert comparison.status == "unknown"


def test_unfilled_order_cannot_be_called_matched(tmp_path: Path) -> None:
    comparison = compare_known_order_fills(
        report(tmp_path, filled=False), ledger(fills=[]), closed_at=NOW + timedelta(seconds=30)
    )
    assert comparison.status == "unknown"
