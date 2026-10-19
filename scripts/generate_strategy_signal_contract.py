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
    RUNNER_STRATEGY_SIGNAL_SCHEMA_VERSION,
    RUNNER_STRATEGY_SIGNAL_SIGNING_DOMAIN,
    RUNNER_STRATEGY_SIGNAL_SIGNING_FIELDS,
    RunnerFactAuthority,
    RunnerFactIdentity,
    signed_strategy_signal_fact,
    strategy_signal_signing_payload,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ID = "alephain.custos.strategy_signal.v1"
# Raised only when the wire shape or the meaning of a field changes.
CONTRACT_REVISION = 1
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

ERROR_CLASSES = ("unknown_field", "missing_field", "schema_violation")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,9})?Z$"


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
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "custos://gateway-contract/v1/strategy_signal_fact_v1.schema.json",
        "x-contract-id": CONTRACT_ID,
        "x-contract-revision": CONTRACT_REVISION,
        "title": "Custos StrategySignalFact.v1",
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
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
        },
    }


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
    return result


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
