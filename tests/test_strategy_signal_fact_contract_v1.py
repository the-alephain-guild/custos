from __future__ import annotations

import base64
import hashlib
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator

from custos.core.runner_fact import (
    RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS,
    RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN,
    RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS,
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


# The published contract: schema, golden and conformance vectors.

ROOT = Path(__file__).resolve().parents[1]
SIGNAL_SCHEMA = ROOT / "docs/gateway-contract/v1/strategy_signal_fact_v1.schema.json"
SIGNAL_GOLDEN = ROOT / "docs/authority/strategy-signal-golden-v1.json"
SIGNAL_VECTORS = ROOT / "docs/authority/conformance/strategy-signal-fact-v1.vectors.json"
SIGNAL_DOCUMENT = json.loads(SIGNAL_VECTORS.read_text(encoding="utf-8"))
SIGNAL_GENERATOR = runpy.run_path(
    str(ROOT / "scripts/generate_strategy_signal_contract.py"), run_name="signal_contract_test"
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _signal_vectors(predicate) -> list[dict[str, object]]:
    return [vector for vector in SIGNAL_DOCUMENT["vectors"] if predicate(vector)]


def test_signal_contract_assets_are_exactly_what_the_generator_writes() -> None:
    for relative, payload in SIGNAL_GENERATOR["build_assets"]().items():
        assert (ROOT / relative).read_bytes() == payload, relative


def test_signal_schema_names_the_vectors_revision_and_the_signed_fields() -> None:
    schema = json.loads(SIGNAL_SCHEMA.read_text(encoding="utf-8"))
    assert schema["x-contract-id"] == SIGNAL_DOCUMENT["contract_id"]
    assert schema["x-contract-revision"] == SIGNAL_DOCUMENT["contract_revision"]
    assert schema["x-custos-invariants"]["signing_payload_fields"] == list(
        RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS
    )


def test_signal_golden_is_a_valid_fact_the_runner_signs() -> None:
    golden = json.loads(SIGNAL_GOLDEN.read_text(encoding="utf-8"))
    Draft202012Validator(json.loads(SIGNAL_SCHEMA.read_text(encoding="utf-8"))).validate(golden)
    assert golden == SIGNAL_GENERATOR["signal"]()


ALL_SIGNAL_VECTORS = _signal_vectors(lambda vector: True)


@pytest.mark.parametrize(
    "vector", ALL_SIGNAL_VECTORS, ids=[str(v["id"]) for v in ALL_SIGNAL_VECTORS]
)
def test_every_signal_vector_meets_the_schema_as_expected(vector: dict[str, Any]) -> None:
    validator = Draft202012Validator(json.loads(SIGNAL_SCHEMA.read_text(encoding="utf-8")))
    fact = json.loads(vector["raw"])
    if vector["expected"]["outcome"] == "accept" or vector["expected"].get("schema_outcome"):
        # schema_outcome marks a rule the schema cannot state, such as an exact product.
        validator.validate(fact)
    else:
        assert list(validator.iter_errors(fact)), "schema accepts a vector refused at decode"


ACCEPTED_SIGNALS = _signal_vectors(lambda vector: vector["expected"]["outcome"] == "accept")


@pytest.mark.parametrize("vector", ACCEPTED_SIGNALS, ids=[str(v["id"]) for v in ACCEPTED_SIGNALS])
def test_accepted_signal_vectors_carry_the_preimage_and_a_valid_signature(
    vector: dict[str, Any],
) -> None:
    fact = json.loads(vector["raw"])
    assert _canonical(fact).decode("utf-8") == vector["raw"]
    assert hashlib.sha256(_canonical(fact)).hexdigest() == vector["expected"]["envelope_digest"]
    preimage = RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN + _canonical(
        strategy_signal_signing_payload(fact)
    )
    assert preimage.hex() == vector["expected"]["signing_preimage_hex"]
    signature = str(fact["signature"]) + "=" * (-len(str(fact["signature"])) % 4)
    Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(SIGNAL_DOCUMENT["test_public_key_hex"])
    ).verify(base64.urlsafe_b64decode(signature), preimage)


REFUSED_SIGNALS = _signal_vectors(lambda vector: vector["expected"]["outcome"] == "reject")


@pytest.mark.parametrize("vector", REFUSED_SIGNALS, ids=[str(v["id"]) for v in REFUSED_SIGNALS])
def test_the_runner_never_signs_a_refused_signal_vector(vector: dict[str, Any]) -> None:
    fact = json.loads(vector["raw"])
    try:
        payload = strategy_signal_signing_payload(fact)
        decision = {
            name: payload[name]
            for name in RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS
            if name in payload
            and name not in {"target_quantity", "reversal_close_quantity", "residue_min_quantity"}
        }
        rebuilt = signed_strategy_signal_fact(
            SIGNAL_GENERATOR["authority"](),
            SIGNAL_GENERATOR["identity"](),
            fact_id=payload["fact_id"],
            instrument=payload["instrument"],
            timeframe=payload["timeframe"],
            direction=payload["direction"],
            occurred_at=payload["occurred_at"],
            source_sequence=payload["source_sequence"],
            input_digest=payload["input_digest"],
            strategy_version=payload["strategy_version"],
            trace_id=payload["trace_id"],
            client_order_id=payload["client_order_id"],
            **decision,
        )
    except RunnerFactContractError:
        return
    assert rebuilt != fact, "the runner would sign this refused vector"


ACCEPTED_DECISIONS = _signal_vectors(
    lambda vector: vector["expected"]["outcome"] == "accept" and "signal_id" in vector["raw"]
)


@pytest.mark.parametrize(
    "vector", ACCEPTED_DECISIONS, ids=[str(v["id"]) for v in ACCEPTED_DECISIONS]
)
def test_accepted_decisions_are_what_the_runner_builds_from_their_input(
    vector: dict[str, Any],
) -> None:
    built = SIGNAL_GENERATOR["signal"](source_sequence=2, **vector["producer_input"])
    assert built == json.loads(vector["raw"])


def test_a_signal_without_decision_signs_the_revision_one_bytes() -> None:
    golden = json.loads(SIGNAL_GOLDEN.read_text(encoding="utf-8"))
    assert not set(golden) & set(RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS)
    assert SIGNAL_GENERATOR["signal"]() == golden
    vector = next(v for v in SIGNAL_DOCUMENT["vectors"] if v["id"] == "signal-golden")
    assert vector["raw"] == _canonical(golden).decode("utf-8")


DECISION = {
    "signal_id": UUID("80000000-0000-4000-8000-000000000001"),
    "signal_event": "decided",
    "signal_action": "enter",
}


def test_base_quantities_are_native_times_multiplier_exactly() -> None:
    fact = _fact(**DECISION, target_quantity_native="37", target_quantity_multiplier="1.3")
    assert fact["target_quantity"] == "48.1"
    with pytest.raises(RunnerFactContractError, match="target_quantity"):
        _fact(
            **DECISION,
            target_quantity_native="7000000000000000000000000001",
            target_quantity_multiplier="1.3",
        )
    with pytest.raises(RunnerFactContractError, match="computed"):
        _fact(**DECISION, target_quantity="48.1")


def test_a_decision_needs_its_signal_and_one_target() -> None:
    signal_id = DECISION["signal_id"]
    with pytest.raises(RunnerFactContractError, match="signal_id"):
        _fact(signal_action="enter")
    with pytest.raises(RunnerFactContractError, match="exactly one"):
        _fact(**DECISION)
    with pytest.raises(RunnerFactContractError, match="does not carry"):
        _fact(signal_id=signal_id, signal_event="withdrawn", signal_action="exit")
    with pytest.raises(RunnerFactContractError, match="closed_reason"):
        _fact(signal_id=signal_id, signal_event="closed")
