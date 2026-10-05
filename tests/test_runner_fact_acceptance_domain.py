"""The runner signs only values its consumer accepts.

The consumer refuses an identifier that is blank, longer than its byte bound, or
carries a control character, and it holds decimals in a type with a 96-bit
mantissa and at most 28 fractional digits. A fact the runner signs outside
either domain would be refused at ingest and take its whole batch with it, so
every wire field is held to the consumer's domain before signing. The bounds
here are the consumer's own: its identifier check counts UTF-8 bytes and treats
Unicode category Cc as control, and its decimal type stops at 2**96 - 1 and 28
fractional digits.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from custos.core.runner_fact import (
    RunnerFactContractError,
    _decimal,
    equity_snapshot,
    execution_fill,
    position_closed,
    position_snapshot,
    reconciliation_period_closed,
    settlement_fee,
    settlement_fill,
    valuation_checkpoint,
    venue_ledger_snapshot_facts,
)

ROOT = Path(__file__).resolve().parents[1]
EVENT = UUID("92000000-0000-4000-8000-0000000000a1")
SNAPSHOT = UUID("92000000-0000-4000-8000-0000000000a2")
AT = "2026-10-05T01:02:03Z"
COMPLETE = {
    "balances_complete": True,
    "positions_complete": True,
    "fills_complete": True,
    "fees_complete": True,
    "cash_flows_complete": True,
}


def _fill(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "venue": "BINANCE",
        "venue_trade_id": "T-1",
        "venue_order_id": "V-1",
        "instrument": "BTCUSDT-PERP",
        "side": "BUY",
        "quantity": "0.5",
        "price": "60000",
        "fee": "0.01",
        "currency": "USDT",
        "occurred_at": AT,
        "client_order_id": "C-1",
        "event_id": EVENT,
    }
    arguments.update(overrides)
    return execution_fill(**arguments)


def _settlement_fill(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "event_id": EVENT,
        "fill_id": EVENT,
        "order_type": "MARKET",
        "category": "ENTRY",
        "price": "60000",
        "avg_fill_price": "60000",
        "currency": "USDT",
        "filled_at": AT,
    }
    arguments.update(overrides)
    return settlement_fill(**arguments)


def _ledger(section: str = "balances", **overrides: Any) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {
        "balances": {
            "wallet_type": "spot",
            "sub_account": None,
            "asset": "USDT",
            "currency": "USDT",
            "total": "10",
            "available": "10",
        },
        "positions": {
            "venue_position_id": "P-1",
            "instrument": "BTCUSDT-PERP",
            "side": "buy",
            "quantity": "1",
            "avg_entry_price": "60000",
            "currency": "USDT",
        },
        "fills": {
            "venue_trade_id": "T-1",
            "venue_order_id": "V-1",
            "instrument": "BTCUSDT-PERP",
            "side": "buy",
            "quantity": "1",
            "price": "60000",
            "fee": "0.01",
            "currency": "USDT",
            "occurred_at": AT,
        },
        "fees": {
            "fee_id": "F-1",
            "kind": "funding",
            "currency": "USDT",
            "amount": "0.5",
            "occurred_at": AT,
        },
    }
    manifest = {key: overrides.pop(key) for key in ("venue", "watermark") if key in overrides}
    row = {**rows[section], **overrides}
    return venue_ledger_snapshot_facts(
        snapshot_id=SNAPSHOT,
        venue=manifest.get("venue", "BINANCE"),
        sub_account=None,
        source="venue_api",
        watermark=manifest.get("watermark", "w-1"),
        coverage_from="2026-10-05T00:00:00Z",
        observed_through=AT,
        completeness=COMPLETE,
        balances=[row] if section == "balances" else [],
        positions=[row] if section == "positions" else [],
        fills=[row] if section == "fills" else [],
        fees=[row] if section == "fees" else [],
        cash_flows=[],
    )


def _reconciliation(**overrides: Any) -> dict[str, Any]:
    reference = {"venue": overrides.pop("venue", "BINANCE"), "snapshot_id": SNAPSHOT}
    return reconciliation_period_closed(
        event_id=EVENT,
        period=overrides.pop("period", "2026-10-05T00"),
        period_started_at="2026-10-05T00:00:00Z",
        closed_at=AT,
        venue_snapshots=[reference],
    )


def _checkpoint(**overrides: Any) -> dict[str, Any]:
    position = {
        "instrument": overrides.pop("instrument", "BTCUSDT-PERP"),
        "currency": "USDT",
        "internal_quantity": overrides.pop("internal_quantity", "1"),
        "internal_avg_entry_price": overrides.pop("internal_avg_entry_price", "60000"),
        "internal_mark_price": overrides.pop("internal_mark_price", "60100"),
        "venue_quantity": overrides.pop("venue_quantity", "1"),
        "venue_avg_entry_price": overrides.pop("venue_avg_entry_price", "60000"),
        "common_mark_price": overrides.pop("common_mark_price", "60100"),
    }
    arguments: dict[str, Any] = {
        "event_id": EVENT,
        "checkpoint_id": EVENT,
        "venue_snapshot_id": SNAPSHOT,
        "venue": "BINANCE",
        "currency": "USDT",
        "venue_watermark": "w-1",
        "collection_started_at": "2026-10-05T01:00:00Z",
        "observed_at": AT,
        "internal_equity": "100",
        "venue_wallet_balance": "100",
        "positions": [position],
    }
    arguments.update(overrides)
    return valuation_checkpoint(**arguments)


def _cash_checkpoint(**overrides: Any) -> dict[str, Any]:
    row = {
        "asset": "USDT",
        "internal_quantity": overrides.pop("internal_quantity", "100"),
        "venue_quantity": overrides.pop("venue_quantity", "100"),
        "internal_mark_price": "1",
        "common_mark_price": "1",
    }
    equity = overrides.pop("internal_equity", row["internal_quantity"])
    return valuation_checkpoint(
        event_id=EVENT,
        checkpoint_id=EVENT,
        venue_snapshot_id=SNAPSHOT,
        venue="BINANCE",
        currency="USDT",
        venue_watermark="w-1",
        collection_started_at="2026-10-05T01:00:00Z",
        observed_at=AT,
        internal_equity=equity,
        venue_wallet_balance=row["venue_quantity"],
        positions=[],
        cash_inventory=[row],
    )


# (field, consumer byte bound, build a fact carrying the value in that field)
IDENTIFIERS: tuple[tuple[str, int, Callable[[str], Any]], ...] = (
    ("execution_fill.venue", 128, lambda v: _fill(venue=v)),
    ("execution_fill.venue_trade_id", 256, lambda v: _fill(venue_trade_id=v)),
    ("execution_fill.client_order_id", 256, lambda v: _fill(client_order_id=v)),
    ("execution_fill.venue_order_id", 256, lambda v: _fill(venue_order_id=v)),
    ("execution_fill.instrument", 128, lambda v: _fill(instrument=v)),
    ("fill.order_type", 64, lambda v: _settlement_fill(order_type=v)),
    ("fill.category", 64, lambda v: _settlement_fill(category=v)),
    (
        "position_snapshot.instrument",
        128,
        lambda v: position_snapshot(
            event_id=EVENT,
            positions=[{"instrument": v, "quantity": "1", "mark_price": "1", "currency": "USDT"}],
            observed_at=AT,
        ),
    ),
    ("venue_ledger_snapshot_manifest.venue", 128, lambda v: _ledger(venue=v)),
    ("venue_ledger_snapshot_manifest.watermark", 512, lambda v: _ledger(watermark=v)),
    ("balances.asset", 64, lambda v: _ledger("balances", asset=v)),
    ("positions.venue_position_id", 256, lambda v: _ledger("positions", venue_position_id=v)),
    ("positions.instrument", 128, lambda v: _ledger("positions", instrument=v)),
    ("fills.venue_trade_id", 256, lambda v: _ledger("fills", venue_trade_id=v)),
    ("fills.venue_order_id", 256, lambda v: _ledger("fills", venue_order_id=v)),
    ("fills.instrument", 128, lambda v: _ledger("fills", instrument=v)),
    ("fees.fee_id", 256, lambda v: _ledger("fees", fee_id=v)),
    ("fees.kind", 128, lambda v: _ledger("fees", kind=v)),
    ("reconciliation_period_closed.period", 128, lambda v: _reconciliation(period=v)),
    ("reconciliation_period_closed.venue", 128, lambda v: _reconciliation(venue=v)),
    ("valuation_checkpoint.venue", 128, lambda v: _checkpoint(venue=v)),
    ("valuation_checkpoint.venue_watermark", 512, lambda v: _checkpoint(venue_watermark=v)),
    ("valuation_checkpoint.instrument", 128, lambda v: _checkpoint(instrument=v)),
)
IDENTIFIER_IDS = [field for field, _, _ in IDENTIFIERS]


def _two_byte(count: int) -> str:
    return "é" * count  # U+00E9 is two UTF-8 bytes


@pytest.mark.parametrize(("field", "bound", "build"), IDENTIFIERS, ids=IDENTIFIER_IDS)
@pytest.mark.parametrize(
    "refused",
    [
        pytest.param("a\u0001b", id="c0-control"),
        pytest.param("a\u007fb", id="delete"),
        pytest.param("a\u0085b", id="c1-control"),
        pytest.param("a\tb", id="tab"),
    ],
)
def test_an_identifier_with_a_control_character_is_refused_before_signing(
    field: str, bound: int, build: Callable[[str], Any], refused: str
) -> None:
    with pytest.raises(RunnerFactContractError, match="control characters"):
        build(refused)


@pytest.mark.parametrize(("field", "bound", "build"), IDENTIFIERS, ids=IDENTIFIER_IDS)
def test_an_identifier_is_bounded_in_utf8_bytes_as_the_consumer_counts_them(
    field: str, bound: int, build: Callable[[str], Any]
) -> None:
    # Two-byte characters separate a byte bound from a character bound: at the
    # bound the value has half as many characters as bytes.
    build(_two_byte(bound // 2))
    with pytest.raises(RunnerFactContractError, match="UTF-8 bytes"):
        build(_two_byte(bound // 2) + "a")
    with pytest.raises(RunnerFactContractError, match="UTF-8 bytes"):
        build("a" * (bound + 1))


# (field, signed, build a fact carrying the value in that field)
DECIMALS: tuple[tuple[str, bool, Callable[[str], Any]], ...] = (
    ("execution_fill.quantity", False, lambda v: _fill(quantity=v)),
    ("execution_fill.price", False, lambda v: _fill(price=v)),
    ("execution_fill.fee", True, lambda v: _fill(fee=v)),
    ("fill.price", False, lambda v: _settlement_fill(price=v)),
    ("fill.avg_fill_price", False, lambda v: _settlement_fill(avg_fill_price=v)),
    (
        "position_closed.realized_pnl",
        True,
        lambda v: position_closed(
            event_id=EVENT,
            position_id=EVENT,
            realized_pnl=v,
            currency="USDT",
            opened_at=AT,
            closed_at=AT,
        ),
    ),
    (
        "fee.amount",
        True,
        lambda v: settlement_fee(
            event_id=EVENT, fill_id=EVENT, amount=v, currency="USDT", assessed_at=AT
        ),
    ),
    (
        "equity_snapshot.amount",
        True,
        lambda v: equity_snapshot(event_id=EVENT, amount=v, currency="USDT", observed_at=AT),
    ),
    (
        "position_snapshot.quantity",
        True,
        lambda v: position_snapshot(
            event_id=EVENT,
            positions=[{"instrument": "X", "quantity": v, "mark_price": "1", "currency": "USDT"}],
            observed_at=AT,
        ),
    ),
    (
        "position_snapshot.mark_price",
        False,
        lambda v: position_snapshot(
            event_id=EVENT,
            positions=[{"instrument": "X", "quantity": "1", "mark_price": v, "currency": "USDT"}],
            observed_at=AT,
        ),
    ),
    ("balances.total", False, lambda v: _ledger("balances", total=v, available="0")),
    ("balances.available", False, lambda v: _ledger("balances", available=v)),
    ("positions.quantity", False, lambda v: _ledger("positions", quantity=v)),
    ("positions.avg_entry_price", False, lambda v: _ledger("positions", avg_entry_price=v)),
    ("fills.quantity", False, lambda v: _ledger("fills", quantity=v)),
    ("fills.price", False, lambda v: _ledger("fills", price=v)),
    ("fills.fee", True, lambda v: _ledger("fills", fee=v)),
    ("fees.amount", True, lambda v: _ledger("fees", amount=v)),
    ("valuation_checkpoint.internal_equity", True, lambda v: _checkpoint(internal_equity=v)),
    (
        "valuation_checkpoint.venue_wallet_balance",
        True,
        lambda v: _checkpoint(venue_wallet_balance=v),
    ),
    ("checkpoint.internal_quantity", True, lambda v: _checkpoint(internal_quantity=v)),
    (
        "checkpoint.internal_avg_entry_price",
        False,
        lambda v: _checkpoint(internal_avg_entry_price=v),
    ),
    ("checkpoint.internal_mark_price", False, lambda v: _checkpoint(internal_mark_price=v)),
    ("checkpoint.venue_quantity", True, lambda v: _checkpoint(venue_quantity=v)),
    ("checkpoint.venue_avg_entry_price", False, lambda v: _checkpoint(venue_avg_entry_price=v)),
    ("checkpoint.common_mark_price", False, lambda v: _checkpoint(common_mark_price=v)),
    (
        "cash_inventory.internal_quantity",
        False,
        lambda v: _cash_checkpoint(internal_quantity=v),
    ),
    ("cash_inventory.venue_quantity", False, lambda v: _cash_checkpoint(venue_quantity=v)),
)
DECIMAL_IDS = [field for field, _, _ in DECIMALS]
LARGEST = "79228162514264337593543950335"  # 2**96 - 1
SMALLEST_STEP = "0." + "0" * 27 + "1"  # 28 fractional digits
# The cash checkpoint re-multiplies inventory at Python's 28 significant digits,
# so its own equity check cannot hold a 29-digit quantity; the bound is the same.
LARGEST_FOR = {"cash_inventory.internal_quantity": "9" * 28}


@pytest.mark.parametrize(("field", "signed", "build"), DECIMALS, ids=DECIMAL_IDS)
def test_a_wire_decimal_stays_inside_the_consumers_decimal_type(
    field: str, signed: bool, build: Callable[[str], Any]
) -> None:
    build(LARGEST_FOR.get(field, LARGEST))
    build(SMALLEST_STEP)
    for refused in (
        "79228162514264337593543950336",  # 2**96
        "0." + "0" * 28 + "1",  # 29 fractional digits
        "1" + "0" * 29,  # 30 digits
    ):
        with pytest.raises(RunnerFactContractError, match="representable decimal range"):
            build(refused)


def test_the_shared_decimal_helper_still_takes_long_tails_off_the_wire() -> None:
    """Off the wire the runner keeps exact arithmetic: a risk ledger notional or a
    conversion rate can carry more fractional digits than the consumer's type.
    Only wire fields are bounded, so the shared helper's default is unchanged."""

    rate = Decimal(1) / Decimal(60000)
    assert len(str(rate).split(".")[1]) > 28
    assert _decimal(rate, "requested_notional") == str(rate)
