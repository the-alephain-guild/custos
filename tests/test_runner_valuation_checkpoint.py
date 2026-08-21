from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from custos.core.runner_fact import (
    RUNNER_FACT_KIND_PROJECTORS,
    RUNNER_FACT_PROJECTOR_CONTRACTS,
    RunnerFactContractError,
    valuation_checkpoint,
)


def _checkpoint(**overrides):
    values = {
        "event_id": UUID("00000000-0000-4000-8000-000000000001"),
        "checkpoint_id": UUID("00000000-0000-4000-8000-000000000002"),
        "venue_snapshot_id": UUID("00000000-0000-4000-8000-000000000003"),
        "venue": "BINANCE",
        "currency": "USDT",
        "venue_watermark": "a" * 64,
        "collection_started_at": datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
        "observed_at": datetime(2026, 8, 14, 12, 0, 1, tzinfo=UTC),
        "internal_equity": "1010",
        "venue_wallet_balance": "1000",
        "positions": (
            {
                "instrument": "BTCUSDT-PERP.BINANCE",
                "currency": "USDT",
                "internal_quantity": "2",
                "internal_avg_entry_price": "95",
                "internal_mark_price": "100",
                "venue_quantity": "2",
                "venue_avg_entry_price": "95",
                "common_mark_price": "101",
            },
        ),
    }
    values.update(overrides)
    return valuation_checkpoint(**values)


def test_checkpoint_is_one_digest_bound_owner_fact_with_sequence_and_time() -> None:
    fact = _checkpoint()

    assert fact["kind"] == "RunnerValuationCheckpointFact.v1"
    assert fact["checkpoint_id"] == "00000000-0000-4000-8000-000000000002"
    assert fact["venue_snapshot_id"] == "00000000-0000-4000-8000-000000000003"
    assert fact["venue_watermark"] == "a" * 64
    assert fact["collection_started_at"] == "2026-08-14T12:00:00Z"
    assert fact["observed_at"] == "2026-08-14T12:00:01Z"
    digest_payload = {
        key: value
        for key, value in fact.items()
        if key not in {"kind", "event_id", "checkpoint_digest"}
    }
    expected = hashlib.sha256(
        json.dumps(digest_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert fact["checkpoint_digest"] == expected


def test_checkpoint_rejects_duplicate_instruments_mixed_currency_and_float() -> None:
    row = _checkpoint()["positions"][0]
    with pytest.raises(RunnerFactContractError, match="duplicate"):
        _checkpoint(positions=(row, row))
    with pytest.raises(RunnerFactContractError, match="currency"):
        _checkpoint(positions=({**row, "currency": "USD"},))
    with pytest.raises(RunnerFactContractError, match="float"):
        _checkpoint(internal_equity=1010.0)


def test_active_capability_advertises_checkpoint_without_rewriting_legacy_receipts() -> None:
    assert RUNNER_FACT_KIND_PROJECTORS["RunnerValuationCheckpointFact.v1"] == "reconciliation"
    assert RUNNER_FACT_PROJECTOR_CONTRACTS["reconciliation"]["valuation_checkpoint"] == "v1"
