"""The published RunnerFact schema states the consumer's acceptance domain.

Identifiers carry the consumer's bound and refuse control characters; decimals
stop at 28 fractional digits and 29 digits. JSON Schema counts characters and
cannot compare a mantissa with 2**96 - 1, so these are first checks; the exact
bounds are enforced where the runner builds each fact
(tests/test_runner_fact_acceptance_domain.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json"
LARGEST = "79228162514264337593543950335"  # 2**96 - 1
SMALLEST_STEP = "0." + "0" * 27 + "1"  # 28 fractional digits

SCHEMA_DOCUMENT = json.loads(SCHEMA.read_text(encoding="utf-8"))


def _property(path: str) -> dict[str, Any]:
    node: Any = SCHEMA_DOCUMENT
    for part in path.split("/"):
        node = node[int(part)] if part.isdigit() else node[part]
    return node


SCHEMA_IDENTIFIERS = (
    ("$defs/fact_execution_fill/properties/venue", 128),
    ("$defs/fact_execution_fill/properties/venue_trade_id", 256),
    ("$defs/fact_execution_fill/properties/client_order_id/oneOf/0", 256),
    ("$defs/fact_execution_fill/properties/venue_order_id", 256),
    ("$defs/fact_execution_fill/properties/instrument", 128),
    ("$defs/fact_fill/properties/order_type", 64),
    ("$defs/fact_fill/properties/category", 64),
    ("$defs/fact_position_snapshot/properties/positions/items/properties/instrument", 128),
    ("$defs/fact_venue_ledger_snapshot_manifest/properties/venue", 128),
    ("$defs/fact_venue_ledger_snapshot_manifest/properties/watermark", 512),
    ("$defs/fact_venue_ledger_snapshot_chunk/properties/balances/items/properties/asset", 64),
    (
        "$defs/fact_venue_ledger_snapshot_chunk/properties/positions/items/properties/"
        "venue_position_id",
        256,
    ),
    (
        "$defs/fact_venue_ledger_snapshot_chunk/properties/positions/items/properties/instrument",
        128,
    ),
    (
        "$defs/fact_venue_ledger_snapshot_chunk/properties/fills/items/properties/venue_trade_id",
        256,
    ),
    (
        "$defs/fact_venue_ledger_snapshot_chunk/properties/fills/items/properties/venue_order_id",
        256,
    ),
    ("$defs/fact_venue_ledger_snapshot_chunk/properties/fills/items/properties/instrument", 128),
    ("$defs/fact_venue_ledger_snapshot_chunk/properties/fees/items/properties/fee_id", 256),
    ("$defs/fact_venue_ledger_snapshot_chunk/properties/fees/items/properties/kind", 128),
    (
        "$defs/fact_venue_ledger_snapshot_chunk/properties/cash_flows/items/properties/"
        "cash_flow_id",
        256,
    ),
    ("$defs/fact_reconciliation_period_closed/properties/period", 128),
    (
        "$defs/fact_reconciliation_period_closed/properties/venue_snapshots/items/properties/venue",
        128,
    ),
    ("$defs/fact_RunnerValuationCheckpointFact.v1/properties/venue", 128),
    ("$defs/fact_RunnerValuationCheckpointFact.v1/properties/venue_watermark", 512),
    (
        "$defs/fact_RunnerValuationCheckpointFact.v1/properties/positions/items/properties/"
        "instrument",
        128,
    ),
)


@pytest.mark.parametrize(
    ("path", "bound"), SCHEMA_IDENTIFIERS, ids=[p for p, _ in SCHEMA_IDENTIFIERS]
)
def test_the_schema_states_each_identifier_bound(path: str, bound: int) -> None:
    """JSON Schema counts characters, so maxLength is only a first check; the
    byte bound is exact in the fact builders above."""

    rule = _property(path)
    validator = Draft202012Validator(rule)
    assert rule["maxLength"] == bound
    assert validator.is_valid("a" * bound)
    assert not validator.is_valid("a" * (bound + 1))
    for refused in ("a\u0001b", "a\u007fb", "a\u0085b", "a\tb", ""):
        assert not validator.is_valid(refused), repr(refused)


DECIMAL_RULES = (
    ("signed", "$defs/fact_position_closed/properties/realized_pnl"),
    ("unsigned", "$defs/fact_execution_fill/properties/price"),
    (
        "positive",
        "$defs/fact_venue_ledger_snapshot_chunk/properties/cash_flows/items/properties/amount",
    ),
)


@pytest.mark.parametrize(("kind", "path"), DECIMAL_RULES, ids=[k for k, _ in DECIMAL_RULES])
def test_the_schema_bounds_decimal_scale_and_digits(kind: str, path: str) -> None:
    validator = Draft202012Validator(_property(path))
    accepted = [LARGEST, SMALLEST_STEP, "1.5", "1234567890123456789012345678.9"]
    if kind != "positive":
        accepted.append("0")
    if kind == "signed":
        accepted += ["-" + LARGEST, "-" + SMALLEST_STEP]
    for value in accepted:
        assert validator.is_valid(value), value
    for value in (
        "0." + "0" * 28 + "1",
        "1" + "0" * 29,
        "1." + "0" * 27 + "1" + "1",
        "-0",
        "1.50",
        "1e-8",
        "+1",
    ):
        assert not validator.is_valid(value), value
