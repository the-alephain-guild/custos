#!/usr/bin/env python3
"""Generate the StrategySignalFact.v1 contract assets.

The strategy signal travels on its own subject with its own signing domain, one
signed fact per message. Its signed payload is fixed by
``RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS``; this script publishes the schema, a
golden fact and the conformance vectors a consumer runs, so the payload has a
revision a consumer can vendor like the RunnerFact batch.

The golden inputs are fixed literals, including the capability manifest digest:
a later change to any capability manifest leaves these bytes alone.

``--check`` regenerates every asset and compares bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from custos.core.runner_fact import (
    MAX_STRATEGY_SIGNAL_REASON_FIELDS,
    RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS,
    RUNNER_STRATEGY_SIGNAL_SCHEMA_VERSION,
    RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN,
    RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS,
    STRATEGY_SIGNAL_ACTIONS,
    STRATEGY_SIGNAL_ARRIVAL_SOURCES,
    STRATEGY_SIGNAL_CLOSED_REASONS,
    STRATEGY_SIGNAL_EVENTS,
    STRATEGY_SIGNAL_FIELD_GROUPS,
    RunnerFactAuthority,
    RunnerFactIdentity,
    signed_strategy_signal_fact,
    strategy_signal_signing_payload,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ID = "alephain.custos.strategy_signal.v1"
# Raised only when the wire shape or the meaning of a field changes.
CONTRACT_REVISION = 2
SCHEMA_PATH = Path("docs/gateway-contract/v1/strategy_signal_fact_v1.schema.json")
GOLDEN_PATH = Path("docs/authority/strategy-signal-golden-v1.json")
VECTORS_PATH = Path("docs/authority/conformance/strategy-signal-fact-v1.vectors.json")

# A test-only signing seed; it signs nothing outside these assets.
TEST_SIGNING_SEED = bytes(range(1, 33))
TENANT_ID = "acme"
TRADING_MODE = "sandbox"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
CAPABILITY_ID = UUID("50000000-0000-4000-8000-000000000005")
SPEC_DIGEST = "a" * 64
# Fixed, not read from a manifest file: the golden must not move with one.
CAPABILITY_MANIFEST_DIGEST = "b" * 64
GENERATION = 7

ERROR_CLASSES = ("unknown_field", "missing_field", "schema_violation", "invariant_violation")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,9})?Z$"
# A canonical decimal the consumer can hold: at most 28 fractional digits and 29
# digits in all, no trailing fractional zero, no sign on zero.
_DECIMAL_SCALE_AND_DIGITS = r"(?![^.]*\.[0-9]{29})(?=-?(?:0\.0*)?(?:[0-9]\.?){1,29}$)"
POSITIVE_DECIMAL_PATTERN = (
    "^" + _DECIMAL_SCALE_AND_DIGITS + r"(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9])$"
)
DECIMAL_PATTERN = (
    "^" + _DECIMAL_SCALE_AND_DIGITS + r"(?:0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$"
)
CURRENCIES = ["USD", "USDT", "USDC", "BTC", "ETH", "VUSDC", "VBTC", "VETH"]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _pretty(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sidecar(path: Path, payload: bytes) -> bytes:
    return f"{_sha256(payload)}  {path.name}\n".encode("ascii")


def identity() -> RunnerFactIdentity:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public = (
        Ed25519PrivateKey.from_private_bytes(TEST_SIGNING_SEED)
        .public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    )
    return RunnerFactIdentity.from_private_bytes(
        TEST_SIGNING_SEED, f"ed25519-{_sha256(public)[:32]}"
    )


def authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id=TENANT_ID,
        trading_mode=TRADING_MODE,
        runner_id=RUNNER_ID,
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=SPEC_ID,
        deployment_spec_digest=SPEC_DIGEST,
        generation=GENERATION,
        strategy_id=STRATEGY_ID,
        capability_version_id=CAPABILITY_ID,
        capability_version=1,
        capability_manifest_digest=CAPABILITY_MANIFEST_DIGEST,
    )


def _base_inputs() -> dict[str, Any]:
    return {
        "fact_id": UUID("a1000000-0000-4000-8000-000000000001"),
        "instrument": "BTCUSDT-PERP.BINANCE",
        "timeframe": "1-MINUTE",
        "direction": "long",
        "occurred_at": "2026-10-04T01:02:03.632001Z",
        "source_sequence": 1,
        "input_digest": "c" * 64,
        "strategy_version": "trend-v1",
        "trace_id": UUID("a2000000-0000-4000-8000-000000000001"),
        "client_order_id": "O-20261004-001",
    }


def signal(**overrides: Any) -> dict[str, Any]:
    """A fact signed by the runner's own builder."""

    inputs = _base_inputs()
    inputs.update(overrides)
    return signed_strategy_signal_fact(authority(), identity(), **inputs)


def _schema() -> dict[str, Any]:
    uuid = {"type": "string", "pattern": UUID_PATTERN}
    digest = {"type": "string", "pattern": DIGEST_PATTERN}
    positive_integer = {"type": "integer", "minimum": 1}
    properties: dict[str, Any] = {
        "schema_version": {"const": RUNNER_STRATEGY_SIGNAL_SCHEMA_VERSION},
        "fact_id": uuid,
        "subject": {
            "type": "string",
            "pattern": (
                r"^crucible\.runner\.strategy-signal\.v1\.[A-Za-z0-9_-]+\."
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\."
                r"(?:live|sandbox|testnet)$"
            ),
        },
        "tenant_id": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$"},
        "trading_mode": {"enum": ["live", "sandbox", "testnet"]},
        "runner_id": uuid,
        "deployment_instance_id": uuid,
        "deployment_spec_id": uuid,
        "deployment_spec_digest": digest,
        "generation": positive_integer,
        "strategy_id": uuid,
        "capability_version_id": uuid,
        "capability_version": positive_integer,
        "capability_manifest_digest": digest,
        "strategy_version": {"type": "string", "minLength": 1, "maxLength": 120},
        "instrument": {"type": "string", "minLength": 1, "maxLength": 120},
        "client_order_id": {
            "oneOf": [
                {"type": "string", "minLength": 1, "maxLength": 512},
                {"type": "null"},
            ]
        },
        "timeframe": {"type": "string", "minLength": 1, "maxLength": 32},
        "direction": {"enum": ["long", "short", "flat"]},
        "occurred_at": {"type": "string", "pattern": TIMESTAMP_PATTERN},
        "source_sequence": positive_integer,
        "input_digest": digest,
        "trace_id": uuid,
        "key_id": {"type": "string", "minLength": 1},
        "signature": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{86}$"},
    }
    if list(properties)[:-1] != list(RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS):
        raise RuntimeError("schema properties differ from the signed payload fields")
    required = list(properties)
    decision = _decision_properties(uuid)
    if list(decision) != list(RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS):
        raise RuntimeError("schema decision properties differ from the decision fields")
    properties.update(decision)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "custos://gateway-contract/v1/strategy_signal_fact_v1.schema.json",
        "x-contract-id": CONTRACT_ID,
        "x-contract-revision": CONTRACT_REVISION,
        "title": "Custos StrategySignalFact.v1",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
        "dependentRequired": _dependent_required(),
        "allOf": _decision_rules(),
        "x-custos-invariants": {
            "subject": (
                "crucible.runner.strategy-signal.v1.{tenant_id}.{runner_id}.{trading_mode}"
            ),
            "signing_domain_base64": base64.b64encode(RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN).decode(
                "ascii"
            ),
            "signing_payload_fields": list(RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS),
            "signing_preimage_formula": "DOMAIN || canonical_json(payload)",
            "signature_encoding": "base64url-unpadded",
            "sequence_rule": "source_sequence is contiguous per deployment instance",
            "nullable_member": (
                "client_order_id is always present and written as null when absent"
            ),
            "canonicalization": "utf8-json-sort-keys-compact-v1",
            "length_unit": "maxLength counts Unicode characters",
            "absent_decision_member": (
                "a decision field without a value is left out, never written as null, so a "
                "signal without decision fields signs the bytes it signed before they existed"
            ),
            "base_quantity_rule": (
                "target_quantity, reversal_close_quantity and residue_min_quantity are the "
                "matching _native quantity times its _multiplier, exactly"
            ),
        },
    }


def _decision_properties(uuid: dict[str, Any]) -> dict[str, Any]:
    positive = {"type": "string", "pattern": POSITIVE_DECIMAL_PATTERN}
    timestamp = {"type": "string", "pattern": TIMESTAMP_PATTERN}
    currency = {"enum": CURRENCIES}

    def base(what: str) -> dict[str, Any]:
        return {**positive, "description": f"{what}, in base-asset units"}

    def native(what: str) -> dict[str, Any]:
        return {**positive, "description": f"{what}, in the venue's own quantity units"}

    return {
        "signal_id": uuid,
        "signal_event": {"enum": list(STRATEGY_SIGNAL_EVENTS)},
        "closed_reason": {"enum": list(STRATEGY_SIGNAL_CLOSED_REASONS)},
        "signal_action": {"enum": list(STRATEGY_SIGNAL_ACTIONS)},
        "target_quantity": base("The position change the decision aims at"),
        "target_quantity_native": native("The position change the decision aims at"),
        "target_quantity_multiplier": positive,
        "target_notional": positive,
        "target_notional_currency": currency,
        "reversal_close_quantity": base("The old position a reversal closes"),
        "reversal_close_quantity_native": native("The old position a reversal closes"),
        "reversal_close_quantity_multiplier": positive,
        "reversal_closes_signal_id": uuid,
        "arrival_price": positive,
        "arrival_price_source": {"enum": list(STRATEGY_SIGNAL_ARRIVAL_SOURCES)},
        "arrival_observed_at": timestamp,
        "reason_code": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{0,63}$"},
        "reason_fields": {
            "type": "object",
            "maxProperties": MAX_STRATEGY_SIGNAL_REASON_FIELDS,
            "propertyNames": {"pattern": r"^[a-z][a-z0-9_]{0,31}$"},
            "additionalProperties": {"type": "string", "pattern": DECIMAL_PATTERN},
        },
        "valid_until": timestamp,
        "supersedes_signal_id": uuid,
        "residue_min_quantity": base("The smallest quantity the venue takes"),
        "residue_min_quantity_native": native("The smallest quantity the venue takes"),
        "residue_min_quantity_multiplier": positive,
        "residue_min_notional": {
            **positive,
            "description": "The smallest notional the venue takes",
        },
        "residue_min_notional_currency": currency,
    }


def _dependent_required() -> dict[str, list[str]]:
    rules: dict[str, list[str]] = {}
    for group in STRATEGY_SIGNAL_FIELD_GROUPS:
        for field in group:
            rules[field] = [other for other in group if other != field]
    for field in RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS:
        if field not in {"signal_id", "signal_event"}:
            rules.setdefault(field, [])
            rules[field] = sorted({*rules[field], "signal_id"})
    return rules


def _decision_rules() -> list[dict[str, Any]]:
    decided_only = [
        field
        for field in RUNNER_STRATEGY_SIGNAL_DECISION_FIELDS
        if field not in {"signal_id", "signal_event", "closed_reason"}
    ]

    def when(event: str, then: dict[str, Any]) -> dict[str, Any]:
        return {
            "if": {"required": ["signal_event"], "properties": {"signal_event": {"const": event}}},
            "then": then,
        }

    return [
        when(
            "decided",
            {
                "required": ["signal_action"],
                "properties": {"closed_reason": False},
                "oneOf": [{"required": ["target_quantity"]}, {"required": ["target_notional"]}],
            },
        ),
        when(
            "withdrawn",
            {"properties": {field: False for field in [*decided_only, "closed_reason"]}},
        ),
        when(
            "closed",
            {
                "required": ["closed_reason"],
                "properties": {field: False for field in decided_only},
            },
        ),
    ]


def raw(fact: Mapping[str, Any]) -> str:
    return _canonical(fact).decode("utf-8")


def accept(fact: Mapping[str, Any]) -> dict[str, Any]:
    preimage = RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN + _canonical(
        strategy_signal_signing_payload(fact)
    )
    return {
        "outcome": "accept",
        "envelope_digest": _sha256(_canonical(fact)),
        "signing_preimage_hex": preimage.hex(),
    }


def reject(error_class: str) -> dict[str, Any]:
    assert error_class in ERROR_CLASSES
    return {"outcome": "reject", "error_class": error_class, "stage": "decode"}


def _resigned(fact: dict[str, Any]) -> dict[str, Any]:
    """Sign an edited fact again, so only the edit, not the signature, is at fault."""

    unsigned = {key: value for key, value in fact.items() if key != "signature"}
    return {
        **unsigned,
        "signature": identity().sign_strategy_signal_payload(_canonical(unsigned)),
    }


def _edited(edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    fact = signal()
    edit(fact)
    return _resigned(fact)


def vectors() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def produced(vector_id: str, covers: str, producer_input: dict[str, Any], fact: dict) -> None:
        result.append(
            {
                "id": vector_id,
                "origin": "producer-canonical",
                "covers": [covers],
                "producer_input": producer_input,
                "raw": raw(fact),
                "expected": accept(fact),
            }
        )

    def tolerated(vector_id: str, covers: str, fact: dict, expected: dict[str, Any]) -> None:
        result.append(
            {
                "id": vector_id,
                "origin": "tolerance",
                "covers": [covers],
                "raw": raw(fact),
                "expected": expected,
            }
        )

    produced("signal-golden", "the signed payload and its preimage", {}, signal())
    produced(
        "signal-client-order-id-null",
        "client_order_id is written as null when the signal names no order",
        {"client_order_id": None},
        signal(client_order_id=None),
    )
    produced(
        "signal-instrument-at-character-limit",
        "instrument at 120 characters",
        {"instrument": "I" * 120},
        signal(instrument="I" * 120),
    )
    wide = "\u20ac" * 50
    produced(
        "signal-instrument-multibyte",
        "instrument of 50 three-byte characters: 50 characters, 150 UTF-8 bytes",
        {"instrument": wide},
        signal(instrument=wide),
    )

    def put(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
        return lambda fact: fact.__setitem__(field, value)

    def drop(field: str) -> Callable[[dict[str, Any]], None]:
        return lambda fact: fact.pop(field)

    tolerated(
        "signal-instrument-over-character-limit",
        "instrument at 121 characters",
        _edited(put("instrument", "I" * 121)),
        reject("schema_violation"),
    )
    tolerated(
        "signal-unknown-field",
        "a member the payload does not define",
        _edited(put("x_unknown", True)),
        reject("unknown_field"),
    )
    tolerated(
        "signal-client-order-id-omitted",
        "client_order_id is required and may be null; leaving it out is refused",
        _edited(drop("client_order_id")),
        reject("missing_field"),
    )
    tolerated(
        "signal-direction-unknown",
        "direction outside long, short and flat",
        _edited(put("direction", "buy")),
        reject("schema_violation"),
    )
    tolerated(
        "signal-input-digest-uppercase",
        "input_digest in upper case",
        _edited(put("input_digest", "C" * 64)),
        reject("schema_violation"),
    )
    tolerated(
        "signal-source-sequence-zero",
        "source_sequence below one",
        _edited(put("source_sequence", 0)),
        reject("schema_violation"),
    )
    produced(
        "signal-instrument-below-character-limit",
        "instrument at 119 characters",
        {"instrument": "I" * 119},
        signal(instrument="I" * 119),
    )
    _decision_vectors(produced, tolerated)
    return result


SIGNAL_ID = "a3000000-0000-4000-8000-000000000001"
DECIDED: dict[str, Any] = {
    "signal_id": SIGNAL_ID,
    "signal_event": "decided",
    "signal_action": "enter",
    "target_quantity_native": "37",
    "target_quantity_multiplier": "1.3",
}
DECIDED_IN_FULL: dict[str, Any] = {
    **DECIDED,
    "reversal_close_quantity_native": "20",
    "reversal_close_quantity_multiplier": "1.3",
    "reversal_closes_signal_id": "a3000000-0000-4000-8000-000000000000",
    "arrival_price": "60000.5",
    "arrival_price_source": "quote_mid",
    "arrival_observed_at": "2026-10-04T01:02:03.500001Z",
    "reason_code": "supertrend_flip",
    "reason_fields": {"atr": "152.3", "slope": "-0.07"},
    "valid_until": "2026-10-04T01:03:03Z",
    "supersedes_signal_id": "a3000000-0000-4000-8000-0000000000ff",
    "residue_min_quantity_native": "0.001",
    "residue_min_quantity_multiplier": "1.3",
    "residue_min_notional": "5",
    "residue_min_notional_currency": "USDT",
}
BY_NOTIONAL: dict[str, Any] = {
    "signal_id": SIGNAL_ID,
    "signal_event": "decided",
    "signal_action": "scale_in",
    "target_notional": "1500",
    "target_notional_currency": "USDT",
    "residue_min_quantity_native": "0.001",
    "residue_min_quantity_multiplier": "1.3",
    "residue_min_notional": "5",
    "residue_min_notional_currency": "USDT",
}


def _decision_vectors(produced, tolerated) -> None:
    """Revision 2: the decision fields. The runner writes each accepted vector from
    the recorded input; each refused one is an edit of a built signal, signed again."""

    def decided(**inputs: Any) -> dict[str, Any]:
        return signal(source_sequence=2, **inputs)

    def edit(inputs: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> dict:
        fact = decided(**inputs)
        change(fact)
        return _resigned(fact)

    def drop(*fields: str) -> Callable[[dict[str, Any]], None]:
        return lambda fact: [fact.pop(field) for field in fields]

    def put(**fields: Any) -> Callable[[dict[str, Any]], None]:
        return lambda fact: fact.update(fields)

    for vector_id, covers, inputs in (
        ("signal-decided-quantity", "a decision that targets a quantity", DECIDED),
        ("signal-decided-in-full", "a decision with every optional field", DECIDED_IN_FULL),
        (
            "signal-decided-notional-with-residue",
            "a decision that targets a notional, with the venue's minimums",
            BY_NOTIONAL,
        ),
        (
            "signal-withdrawn",
            "a withdrawn signal names itself and nothing else",
            {"signal_id": SIGNAL_ID, "signal_event": "withdrawn"},
        ),
        (
            "signal-closed",
            "a closed signal names its reason",
            {"signal_id": SIGNAL_ID, "signal_event": "closed", "closed_reason": "completed"},
        ),
        (
            "signal-reason-fields-at-limit",
            "sixteen numeric reason fields",
            {
                **DECIDED,
                "reason_fields": {f"f{index:02d}": str(index) for index in range(16)},
            },
        ),
    ):
        produced(vector_id, covers, inputs, decided(**inputs))
    refused: list[tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], None], str]] = [
        (
            "signal-decision-null",
            "a decision field written as null",
            DECIDED,
            put(valid_until=None),
            "schema_violation",
        ),
        (
            "signal-decision-without-signal-id",
            "decision fields without signal_id",
            DECIDED,
            drop("signal_id"),
            "schema_violation",
        ),
        (
            "signal-decided-without-target",
            "a decision with no target",
            DECIDED,
            drop("target_quantity", "target_quantity_native", "target_quantity_multiplier"),
            "schema_violation",
        ),
        (
            "signal-decided-with-both-targets",
            "a decision that targets a quantity and a notional",
            DECIDED,
            put(target_notional="1500", target_notional_currency="USDT"),
            "schema_violation",
        ),
        (
            "signal-decided-without-action",
            "a decision without its action",
            DECIDED,
            drop("signal_action"),
            "schema_violation",
        ),
        (
            "signal-target-without-multiplier",
            "a target quantity without its multiplier",
            DECIDED,
            drop("target_quantity_multiplier"),
            "schema_violation",
        ),
        (
            "signal-reversal-without-signal",
            "a reversal quantity without the signal it closes",
            DECIDED_IN_FULL,
            drop("reversal_closes_signal_id"),
            "schema_violation",
        ),
        (
            "signal-reversal-without-multiplier",
            "a reversal quantity without its multiplier",
            DECIDED_IN_FULL,
            drop("reversal_close_quantity_multiplier"),
            "schema_violation",
        ),
        (
            "signal-arrival-without-time",
            "an arrival price without its time",
            DECIDED_IN_FULL,
            drop("arrival_observed_at"),
            "schema_violation",
        ),
        (
            "signal-residue-without-multiplier",
            "a minimum quantity without its multiplier",
            BY_NOTIONAL,
            drop("residue_min_quantity_multiplier"),
            "schema_violation",
        ),
        (
            "signal-withdrawn-with-action",
            "a withdrawn signal with a decision field",
            {"signal_id": SIGNAL_ID, "signal_event": "withdrawn"},
            put(signal_action="exit"),
            "schema_violation",
        ),
        (
            "signal-closed-without-reason",
            "a closed signal without its reason",
            {"signal_id": SIGNAL_ID, "signal_event": "closed", "closed_reason": "completed"},
            drop("closed_reason"),
            "schema_violation",
        ),
        (
            "signal-reason-fields-over-limit",
            "seventeen reason fields",
            {**DECIDED, "reason_fields": {f"f{index:02d}": str(index) for index in range(16)}},
            lambda fact: fact["reason_fields"].update(f16="16"),
            "schema_violation",
        ),
        (
            "signal-reason-field-key-too-long",
            "a reason field key of 33 characters",
            DECIDED,
            put(reason_fields={"k" * 33: "1"}),
            "schema_violation",
        ),
        (
            "signal-reason-field-key-invalid",
            "a reason field key that is not a code",
            DECIDED,
            put(reason_fields={"ATR": "1"}),
            "schema_violation",
        ),
        (
            "signal-reason-field-text",
            "a reason field that is text",
            DECIDED,
            put(reason_fields={"note": "breakout"}),
            "schema_violation",
        ),
        (
            "signal-reason-field-non-canonical",
            "a reason field in a non-canonical spelling",
            DECIDED,
            put(reason_fields={"atr": "1.50"}),
            "schema_violation",
        ),
        (
            "signal-target-base-differs",
            "a base quantity that is not native times multiplier",
            DECIDED,
            put(target_quantity="48"),
            "invariant_violation",
        ),
    ]
    for vector_id, covers, inputs, change, error_class in refused:
        expected = reject(error_class)
        if error_class == "invariant_violation":
            expected["schema_outcome"] = "accept"
        tolerated(vector_id, covers, edit(dict(inputs), change), expected)


def vector_document() -> dict[str, Any]:
    signer = identity()
    return {
        "vector_set_version": 1,
        "contract_id": CONTRACT_ID,
        "contract_revision": CONTRACT_REVISION,
        "canonicalization": "utf8-json-sort-keys-compact-v1",
        "digest": "sha256-hex",
        "error_classes": list(ERROR_CLASSES),
        "stages": {"decode": "refused while parsing the fact, before any state changes"},
        "test_only": True,
        "test_signing_seed_hex": TEST_SIGNING_SEED.hex(),
        "test_public_key_hex": signer.public_key_bytes.hex(),
        "test_key_id": signer.key_id,
        "vectors": vectors(),
    }


def build_assets() -> dict[Path, bytes]:
    objects: dict[Path, Any] = {
        SCHEMA_PATH: _schema(),
        GOLDEN_PATH: signal(),
        VECTORS_PATH: vector_document(),
    }
    assets: dict[Path, bytes] = {}
    for path, value in objects.items():
        payload = _pretty(value)
        assets[path] = payload
        assets[path.with_name(path.name + ".sha256")] = _sidecar(path, payload)
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    drift: list[str] = []
    for relative, payload in build_assets().items():
        path = ROOT / relative
        if args.check:
            if not path.is_file() or path.read_bytes() != payload:
                drift.append(str(relative))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if drift:
        for relative in drift:
            print(f"strategy signal contract assets differ: {relative}", file=sys.stderr)
        return 1
    print(
        "strategy signal contract assets are exact"
        if args.check
        else "generated strategy signal contract assets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
