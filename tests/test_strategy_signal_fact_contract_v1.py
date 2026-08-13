from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from custos.core.runner_fact import (
    RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN,
    RunnerFactAuthority,
    RunnerFactContractError,
    RunnerFactIdentity,
    signed_strategy_signal_fact,
    strategy_signal_signing_payload,
)


def _identity() -> RunnerFactIdentity:
    key = bytes(range(32))
    public = RunnerFactIdentity.from_private_bytes(
        key,
        "ed25519-56475aa75463474c0285df5dbf2bcab7",
    )
    return public


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="tenant-a",
        trading_mode="testnet",
        runner_id=UUID("10000000-0000-4000-8000-000000000001"),
        deployment_instance_id=UUID("20000000-0000-4000-8000-000000000001"),
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000001"),
        deployment_spec_digest="a" * 64,
        generation=3,
        strategy_id=UUID("40000000-0000-4000-8000-000000000001"),
        capability_version_id=UUID("50000000-0000-4000-8000-000000000001"),
        capability_version=2,
        capability_manifest_digest="b" * 64,
    )


def _fact(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "fact_id": UUID("60000000-0000-4000-8000-000000000001"),
        "instrument": "BTC-USDT",
        "timeframe": "1m",
        "direction": "long",
        "occurred_at": datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
        "source_sequence": 7,
        "input_digest": "c" * 64,
        "strategy_version": "supertrend-v1",
        "trace_id": UUID("70000000-0000-4000-8000-000000000001"),
    }
    values.update(overrides)
    return signed_strategy_signal_fact(_authority(), _identity(), **values)


def test_signal_fact_is_scope_complete_signed_and_secret_free() -> None:
    fact = _fact()
    assert fact["schema_version"] == 1
    assert fact["tenant_id"] == "tenant-a"
    assert fact["trading_mode"] == "testnet"
    assert fact["deployment_instance_id"] == "20000000-0000-4000-8000-000000000001"
    assert fact["strategy_version"] == "supertrend-v1"
    assert fact["subject"] == (
        "crucible.runner.strategy-signal.v1.tenant-a.10000000-0000-4000-8000-000000000001.testnet"
    )
    assert not ({"secret", "private_key", "api_key"} & set(fact))

    payload = strategy_signal_signing_payload(fact)
    preimage = (
        RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN
        + json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    signature = base64.urlsafe_b64decode(str(fact["signature"]) + "==")
    Ed25519PublicKey.from_public_bytes(_identity().public_key_bytes).verify(signature, preimage)


def test_signal_fact_rejects_invalid_direction_digest_and_sequence() -> None:
    with pytest.raises(RunnerFactContractError):
        _fact(direction="buy")
    with pytest.raises(RunnerFactContractError):
        _fact(input_digest=hashlib.sha256(b"x").hexdigest().upper())
    with pytest.raises(RunnerFactContractError):
        _fact(source_sequence=0)
    with pytest.raises(RunnerFactContractError):
        _fact(timeframe="x" * 33)
    with pytest.raises(RunnerFactContractError):
        _fact(client_order_id="x" * 513)
