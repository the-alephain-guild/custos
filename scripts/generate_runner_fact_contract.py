#!/usr/bin/env python3
"""Generate the canonical first-production RunnerFact V1 authority assets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_fact import (
    RUNNER_FACT_KIND_PROJECTORS,
    RUNNER_FACT_PROJECTOR_CONTRACTS,
    RUNNER_FACT_SCHEMA_VERSION,
    RUNNER_FACT_SIGNING_DOMAIN,
    RUNNER_FACT_SIGNING_HEADER_FIELDS,
    RunnerFactIdentity,
    capability_binding_evidence_digest,
    capability_scope_binding_values,
    command_lifecycle_event_id,
    equity_snapshot,
    execution_fill,
    heartbeat,
    normalize_capability_scope_bindings,
    position_closed,
    position_snapshot,
    reconciliation_period_closed,
    runner_fact_event_id,
    runner_fact_signing_header,
    runner_fact_signing_preimage,
    settlement_fee,
    settlement_fill,
    settlement_period_closed,
    venue_ledger_snapshot_facts,
)

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_COORDINATE = "custos.runner-fact.v1"
PRODUCER_ASSET_COMMIT = "fcf8df5fd3592257c48cfa22464c1ef6cedf23cf"
TENANT_ID = "acme"
MODE = "sandbox"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("20000000-0000-4000-8000-000000000002")
SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
CAPABILITY_ID = UUID("50000000-0000-4000-8000-000000000005")
SPEC_DIGEST = "a" * 64
POLICY_DIGEST = "c" * 64
COMMAND_FINGERPRINT = "e" * 64
BATCH_ID = UUID("60000000-0000-4000-8000-000000000006")
EMITTED_AT = "2026-07-15T08:00:00Z"
PRIVATE_KEY_BYTES = bytes(range(1, 33))

SCHEMA_PATH = Path("docs/gateway-contract/v1/runner_fact_batch_v1.schema.json")
GOLDEN_PATH = Path("docs/authority/runner-fact-golden-v1.json")
CAPABILITY_MANIFEST_PATH = Path("docs/authority/runner-fact-capability-manifest-v1.json")
CAPABILITY_RECEIPT_PATH = Path("docs/authority/runner-fact-capability-receipt-golden-v1.json")
PARITY_PATH = Path("docs/authority/runner-fact-parity-matrix-v1.json")
SIGNING_PREIMAGE_PATH = Path("docs/authority/runner-fact-signing-preimage-golden-v1.json")
INDEX_PATH = Path("docs/authority/runner-fact-contract-assets-v1.json")
RECEIPT_PATH = Path("docs/authority/receipts/custos-runner-fact-v1-producer-receipt.json")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pretty(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sidecar(path: Path, value: bytes) -> bytes:
    return f"{_sha256(value)}  {path.name}\n".encode("ascii")


def _object_schema(
    kind: str,
    fields: dict[str, Any],
    *,
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    properties = {
        "kind": {"const": kind},
        "event_id": {"$ref": "#/$defs/uuid"},
        "seq": {"type": "integer", "minimum": 1},
        **fields,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [name for name in properties if name not in optional],
        "properties": properties,
    }


def _schema() -> dict[str, Any]:
    decimal = {"type": "string", "pattern": r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"}
    unsigned_decimal = {
        "type": "string",
        "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$",
    }
    currency = {"enum": ["USD", "USDT", "USDC", "BTC", "ETH"]}
    timestamp = {
        "type": "string",
        "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,9})?Z$",
    }
    non_empty = {"type": "string", "minLength": 1}
    uuid = {
        "type": "string",
        "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    }
    digest = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
    side = {"enum": ["buy", "sell"]}
    balance = {
        "type": "object",
        "additionalProperties": False,
        "required": ["asset", "currency", "total", "available"],
        "properties": {
            "asset": non_empty,
            "currency": currency,
            "total": unsigned_decimal,
            "available": unsigned_decimal,
        },
    }
    ledger_position = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "venue_position_id",
            "instrument",
            "side",
            "quantity",
            "avg_entry_price",
            "currency",
        ],
        "properties": {
            "venue_position_id": non_empty,
            "instrument": non_empty,
            "side": side,
            "quantity": unsigned_decimal,
            "avg_entry_price": {"oneOf": [unsigned_decimal, {"type": "null"}]},
            "currency": currency,
        },
    }
    ledger_fill = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "venue_trade_id",
            "venue_order_id",
            "instrument",
            "side",
            "quantity",
            "price",
            "fee",
            "currency",
            "occurred_at",
        ],
        "properties": {
            "venue_trade_id": non_empty,
            "venue_order_id": non_empty,
            "instrument": non_empty,
            "side": side,
            "quantity": unsigned_decimal,
            "price": unsigned_decimal,
            "fee": unsigned_decimal,
            "currency": currency,
            "occurred_at": timestamp,
        },
    }
    ledger_fee = {
        "type": "object",
        "additionalProperties": False,
        "required": ["fee_id", "kind", "currency", "amount", "occurred_at"],
        "properties": {
            "fee_id": non_empty,
            "kind": non_empty,
            "currency": currency,
            "amount": unsigned_decimal,
            "occurred_at": timestamp,
        },
    }
    completeness = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "balances_complete",
            "positions_complete",
            "fills_complete",
            "fees_complete",
        ],
        "properties": {
            name: {"type": "boolean"}
            for name in (
                "balances_complete",
                "positions_complete",
                "fills_complete",
                "fees_complete",
            )
        },
    }
    position_row = {
        "type": "object",
        "additionalProperties": False,
        "required": ["instrument", "quantity", "mark_price", "currency"],
        "properties": {
            "instrument": non_empty,
            "quantity": decimal,
            "mark_price": unsigned_decimal,
            "currency": currency,
        },
    }
    venue_ref = {
        "type": "object",
        "additionalProperties": False,
        "required": ["venue", "snapshot_id"],
        "properties": {"venue": non_empty, "snapshot_id": uuid},
    }
    facts = {
        "execution_fill": _object_schema(
            "execution_fill",
            {
                "venue": non_empty,
                "venue_trade_id": non_empty,
                "client_order_id": {"oneOf": [non_empty, {"type": "null"}]},
                "venue_order_id": non_empty,
                "instrument": non_empty,
                "side": side,
                "quantity": unsigned_decimal,
                "price": unsigned_decimal,
                "fee": unsigned_decimal,
                "currency": currency,
                "occurred_at": timestamp,
            },
        ),
        "fill": _object_schema(
            "fill",
            {
                "fill_id": uuid,
                "order_type": non_empty,
                "category": non_empty,
                "price": unsigned_decimal,
                "avg_fill_price": unsigned_decimal,
                "currency": currency,
                "filled_at": timestamp,
            },
        ),
        "position_closed": _object_schema(
            "position_closed",
            {
                "position_id": uuid,
                "realized_pnl": decimal,
                "currency": currency,
                "opened_at": timestamp,
                "closed_at": timestamp,
            },
        ),
        "fee": _object_schema(
            "fee",
            {
                "fill_id": uuid,
                "amount": unsigned_decimal,
                "currency": currency,
                "assessed_at": timestamp,
            },
        ),
        "equity_snapshot": _object_schema(
            "equity_snapshot",
            {"amount": decimal, "currency": currency, "observed_at": timestamp},
        ),
        "position_snapshot": _object_schema(
            "position_snapshot",
            {
                "positions": {"type": "array", "items": position_row},
                "observed_at": timestamp,
            },
        ),
        "heartbeat": _object_schema(
            "heartbeat",
            {"status": {"enum": ["online", "degraded", "offline"]}, "observed_at": timestamp},
        ),
        "period_closed": _object_schema(
            "period_closed", {"period": non_empty, "closed_at": timestamp}
        ),
        "venue_ledger_snapshot_manifest": _object_schema(
            "venue_ledger_snapshot_manifest",
            {
                "snapshot_id": uuid,
                "venue": non_empty,
                "source": {"enum": ["venue_api", "drop_copy"]},
                "watermark": non_empty,
                "coverage_from": timestamp,
                "observed_through": timestamp,
                "completeness": completeness,
                "balances_count": {"type": "integer", "minimum": 0},
                "positions_count": {"type": "integer", "minimum": 0},
                "fills_count": {"type": "integer", "minimum": 0},
                "fees_count": {"type": "integer", "minimum": 0},
                "chunk_count": {"type": "integer", "minimum": 1, "maximum": 4096},
                "content_digest": digest,
            },
        ),
        "venue_ledger_snapshot_chunk": _object_schema(
            "venue_ledger_snapshot_chunk",
            {
                "snapshot_id": uuid,
                "chunk_index": {"type": "integer", "minimum": 0},
                "chunk_count": {"type": "integer", "minimum": 1, "maximum": 4096},
                "balances": {"type": "array", "items": balance},
                "positions": {"type": "array", "items": ledger_position},
                "fills": {"type": "array", "items": ledger_fill},
                "fees": {"type": "array", "items": ledger_fee},
                "chunk_digest": digest,
            },
        ),
        "reconciliation_period_closed": _object_schema(
            "reconciliation_period_closed",
            {
                "period": non_empty,
                "period_started_at": timestamp,
                "closed_at": timestamp,
                "venue_snapshots": {
                    "type": "array",
                    "minItems": 1,
                    "items": venue_ref,
                },
            },
        ),
        "RunnerDeploymentLifecycleFact.v1": _object_schema(
            "RunnerDeploymentLifecycleFact.v1",
            {
                "occurred_at": timestamp,
                "tenant_id": non_empty,
                "mode": {"enum": ["live", "sandbox", "testnet"]},
                "runner_id": uuid,
                "deployment_instance_id": uuid,
                "deployment_spec_id": uuid,
                "deployment_spec_digest": digest,
                "generation": {"type": "integer", "minimum": 1},
                "lifecycle_state": {"enum": ["running", "paused", "stopped", "archived"]},
                "command_fingerprint": digest,
                "outcome": {"enum": ["applied", "conflict", "stale", "retry_exhausted"]},
                "observed_at": timestamp,
            },
        ),
        "RunnerRuntimeLogFact.v1": _object_schema(
            "RunnerRuntimeLogFact.v1",
            {
                "occurred_at": timestamp,
                "level": {"enum": ["DEBUG", "INFO", "WARN", "ERROR"]},
                "component": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"},
                "message": {"type": "string", "minLength": 1, "maxLength": 4096},
                "structured_fields": {
                    "type": "object",
                    "additionalProperties": {"$ref": "#/$defs/canonical_json_value"},
                },
                "correlation_id": uuid,
                "causation_id": {"oneOf": [uuid, {"type": "null"}]},
            },
        ),
    }
    definitions: dict[str, Any] = {
        "uuid": uuid,
        "digest": digest,
        "canonical_json_value": {
            "oneOf": [
                {"type": "null"},
                {"type": "boolean"},
                {"type": "integer"},
                {"type": "string"},
                {
                    "type": "array",
                    "items": {"$ref": "#/$defs/canonical_json_value"},
                },
                {
                    "type": "object",
                    "additionalProperties": {"$ref": "#/$defs/canonical_json_value"},
                },
            ]
        },
        **{f"fact_{name}": value for name, value in facts.items()},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "custos://gateway-contract/v1/runner_fact_batch_v1.schema.json",
        "title": "Custos RunnerFactBatchV1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "batch_id",
            "tenant_id",
            "trading_mode",
            "runner_id",
            "deployment_instance_id",
            "deployment_spec_id",
            "deployment_spec_digest",
            "generation",
            "strategy_id",
            "capability_version_id",
            "capability_version",
            "capability_manifest_digest",
            "key_id",
            "emitted_at",
            "source_seq_start",
            "source_seq_end",
            "payload_digest",
            "facts",
            "signature",
        ],
        "properties": {
            "schema_version": {"const": RUNNER_FACT_SCHEMA_VERSION},
            "batch_id": uuid,
            "tenant_id": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$"},
            "trading_mode": {"enum": ["live", "sandbox", "testnet"]},
            "runner_id": uuid,
            "deployment_instance_id": uuid,
            "deployment_spec_id": uuid,
            "deployment_spec_digest": digest,
            "generation": {"type": "integer", "minimum": 1},
            "strategy_id": uuid,
            "capability_version_id": uuid,
            "capability_version": {"type": "integer", "minimum": 1},
            "capability_manifest_digest": digest,
            "key_id": non_empty,
            "emitted_at": timestamp,
            "source_seq_start": {"type": "integer", "minimum": 1},
            "source_seq_end": {"type": "integer", "minimum": 1},
            "payload_digest": digest,
            "facts": {
                "type": "array",
                "minItems": 1,
                "maxItems": 512,
                "items": {
                    "oneOf": [
                        {"$ref": f"#/$defs/fact_{kind}"} for kind in RUNNER_FACT_KIND_PROJECTORS
                    ]
                },
            },
            "signature": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{86}$"},
        },
        "$defs": definitions,
        "x-custos-invariants": {
            "subject": "crucible.runner_fact.{trading_mode}.{tenant_id}.{runner_id}.{deployment_instance_id}",
            "stream_identity_fields": [
                "tenant_id",
                "trading_mode",
                "runner_id",
                "deployment_instance_id",
            ],
            "signed_fencing_fields": [
                "deployment_spec_id",
                "deployment_spec_digest",
                "generation",
            ],
            "sequence_rule": "facts[i].seq == source_seq_start + i",
            "generation_resets_sequence": False,
            "signing_domain_base64": base64.b64encode(RUNNER_FACT_SIGNING_DOMAIN).decode("ascii"),
            "signing_header_fields": list(RUNNER_FACT_SIGNING_HEADER_FIELDS),
            "signing_header_excluded_batch_fields": ["facts", "signature"],
            "payload_digest_formula": "sha256(canonical_json(facts))",
            "signing_preimage_formula": "DOMAIN || canonical_json(header)",
            "canonicalization": {
                "encoding": "UTF-8",
                "object_member_order": "ascending Unicode code point order",
                "array_order": "preserved",
                "item_separator": ",",
                "key_value_separator": ":",
                "whitespace": "none",
                "ensure_ascii": False,
                "allow_nan": False,
                "binary_float_allowed": False,
                "number_policy": "JSON integer or canonical decimal string",
                "string_escaping": (
                    "JSON-escape control characters, quotation mark, and reverse solidus; "
                    "emit all other Unicode as UTF-8"
                ),
                "trailing_newline": False,
            },
        },
    }


def _scope(spec_id: UUID, spec_digest: str) -> dict[str, Any]:
    return {
        "trading_mode": MODE,
        "deployment_instance_id": str(INSTANCE_ID),
        "deployment_spec_id": str(spec_id),
        "deployment_spec_digest": spec_digest,
        "strategy_id": str(STRATEGY_ID),
    }


def _capability_manifest(spec_id: UUID, spec_digest: str) -> dict[str, Any]:
    base = _scope(spec_id, spec_digest)
    return {
        "schema_version": 1,
        "agent_version": "fixture-v1",
        "contract": "custos.runner_fact.capability.v1",
        "closed_fact_union": True,
        "unknown_fact_kind": "terminal_unsupported_contract",
        "fact_kind_projectors": dict(RUNNER_FACT_KIND_PROJECTORS),
        "runner_fact_contracts": {
            projector: dict(contract)
            for projector, contract in RUNNER_FACT_PROJECTOR_CONTRACTS.items()
        },
        "settlement_scope_bindings": [dict(base)],
        "risk_scope_bindings": [
            {
                **base,
                "resource_type": "deployment_instance",
                "resource_id": str(INSTANCE_ID),
            }
        ],
        "reconciliation_scope_bindings": [
            {
                **base,
                "source_policy_digest": POLICY_DIGEST,
                "required_venues": [{"venue": "BINANCE", "ledger_source": "venue_api"}],
            }
        ],
        "health_scope_bindings": [{**base, "expected_cadence_seconds": 30, "grace_seconds": 10}],
        "deployment_lifecycle_scope_bindings": [dict(base)],
    }


def _capability_receipt(
    manifest: dict[str, Any], capability_id: UUID, key_id: str, public_key: bytes
) -> dict[str, Any]:
    bindings = normalize_capability_scope_bindings(manifest)
    return {
        "schema_version": 1,
        "tenant_id": TENANT_ID,
        "runner_id": str(RUNNER_ID),
        "capability_version_id": str(capability_id),
        "capability_version": 1,
        "manifest_digest": _sha256(_canonical(manifest)),
        "key_id": key_id,
        "key_version": 1,
        "algorithm": "ed25519",
        "public_key_digest": _sha256(public_key),
        "binding_status": "validated",
        "binding_evidence_digest": capability_binding_evidence_digest(
            TENANT_ID, RUNNER_ID, bindings
        ),
        "capability_manifest": manifest,
        "scope_bindings": capability_scope_binding_values(bindings),
    }


def _facts() -> list[dict[str, Any]]:
    fill_id = UUID("70000000-0000-4000-8000-000000000007")
    snapshot_id = UUID("71000000-0000-4000-8000-000000000007")
    timestamp = "2026-07-15T07:59:00Z"
    correlation_id = UUID("73000000-0000-4000-8000-000000000007")
    runtime_log_identity = {
        "level": "WARN",
        "component": "local_cap",
        "message": "risk-increasing order denied by verified runner policy",
        "structured_fields": {
            "reason_code": "runner_cap_exceeded",
            "policy_digest": POLICY_DIGEST,
        },
        "correlation_id": str(correlation_id),
        "causation_id": None,
    }
    facts = [
        execution_fill(
            event_id=UUID("80000000-0000-4000-8000-000000000001"),
            venue="BINANCE",
            venue_trade_id="trade-1",
            client_order_id="client-1",
            venue_order_id="venue-order-1",
            instrument="BTC-USDT",
            side="buy",
            quantity="0.01",
            price="60000",
            fee="0.6",
            currency="USDT",
            occurred_at=timestamp,
        ),
        settlement_fill(
            event_id=UUID("80000000-0000-4000-8000-000000000002"),
            fill_id=fill_id,
            order_type="market",
            category="taker",
            price="60000",
            avg_fill_price="60000",
            currency="USDT",
            filled_at=timestamp,
        ),
        position_closed(
            event_id=UUID("80000000-0000-4000-8000-000000000003"),
            position_id=UUID("72000000-0000-4000-8000-000000000007"),
            realized_pnl="12.5",
            currency="USDT",
            opened_at="2026-07-15T07:00:00Z",
            closed_at=timestamp,
        ),
        settlement_fee(
            event_id=UUID("80000000-0000-4000-8000-000000000004"),
            fill_id=fill_id,
            amount="0.6",
            currency="USDT",
            assessed_at=timestamp,
        ),
        settlement_period_closed(
            event_id=UUID("80000000-0000-4000-8000-000000000005"),
            period="20260715T070000Z_20260715T080000Z",
            closed_at=EMITTED_AT,
        ),
        equity_snapshot(
            event_id=UUID("80000000-0000-4000-8000-000000000006"),
            amount="10012.5",
            currency="USDT",
            observed_at=timestamp,
        ),
        position_snapshot(
            event_id=UUID("80000000-0000-4000-8000-000000000007"),
            positions=[
                {
                    "instrument": "BTC-USDT",
                    "quantity": "0.01",
                    "mark_price": "60000",
                    "currency": "USDT",
                }
            ],
            observed_at=timestamp,
        ),
        heartbeat(
            event_id=UUID("80000000-0000-4000-8000-000000000008"),
            status="online",
            observed_at=timestamp,
        ),
        {
            "kind": "RunnerRuntimeLogFact.v1",
            "event_id": str(
                runner_fact_event_id(
                    "runtime_log",
                    TENANT_ID,
                    MODE,
                    RUNNER_ID,
                    INSTANCE_ID,
                    correlation_id,
                    _sha256(_canonical(runtime_log_identity)),
                )
            ),
            "occurred_at": timestamp,
            **runtime_log_identity,
        },
    ]
    ledger = venue_ledger_snapshot_facts(
        snapshot_id=snapshot_id,
        venue="BINANCE",
        source="venue_api",
        watermark="ledger-1",
        coverage_from="2026-07-15T07:00:00Z",
        observed_through=timestamp,
        completeness={
            "balances_complete": True,
            "positions_complete": True,
            "fills_complete": True,
            "fees_complete": True,
        },
        balances=[{"asset": "USDT", "currency": "USDT", "total": "10012.5", "available": "9412.5"}],
        positions=[],
        fills=[],
        fees=[],
    )
    facts.extend(ledger)
    facts.append(
        reconciliation_period_closed(
            event_id=UUID("80000000-0000-4000-8000-000000000012"),
            period="20260715T070000Z_20260715T080000Z",
            period_started_at="2026-07-15T07:00:00Z",
            closed_at=EMITTED_AT,
            venue_snapshots=[{"venue": "BINANCE", "snapshot_id": snapshot_id}],
        )
    )
    facts.append(
        {
            "kind": "RunnerDeploymentLifecycleFact.v1",
            "event_id": str(
                command_lifecycle_event_id(
                    tenant_id=TENANT_ID,
                    trading_mode=MODE,
                    runner_id=RUNNER_ID,
                    deployment_instance_id=INSTANCE_ID,
                    deployment_spec_id=SPEC_ID,
                    deployment_spec_digest=SPEC_DIGEST,
                    generation=7,
                    lifecycle_state="running",
                    command_fingerprint=COMMAND_FINGERPRINT,
                    outcome="applied",
                )
            ),
            "occurred_at": timestamp,
            "tenant_id": TENANT_ID,
            "mode": MODE,
            "runner_id": str(RUNNER_ID),
            "deployment_instance_id": str(INSTANCE_ID),
            "deployment_spec_id": str(SPEC_ID),
            "deployment_spec_digest": SPEC_DIGEST,
            "generation": 7,
            "lifecycle_state": "running",
            "command_fingerprint": COMMAND_FINGERPRINT,
            "outcome": "applied",
            "observed_at": timestamp,
        }
    )
    if set(RUNNER_FACT_KIND_PROJECTORS) != {fact["kind"] for fact in facts}:
        raise RuntimeError("golden does not contain the closed RunnerFact kind union")
    return facts


def _batch(
    *,
    facts: list[dict[str, Any]],
    batch_id: UUID,
    emitted_at: str,
    source_seq_start: int,
    spec_id: UUID,
    spec_digest: str,
    generation: int,
    capability_id: UUID,
    capability_manifest_digest: str,
    identity: RunnerFactIdentity,
) -> dict[str, Any]:
    sequenced = [{**fact, "seq": source_seq_start + offset} for offset, fact in enumerate(facts)]
    source_seq_end = source_seq_start + len(sequenced) - 1
    signing_header = runner_fact_signing_header(
        {
            "schema_version": RUNNER_FACT_SCHEMA_VERSION,
            "batch_id": str(batch_id),
            "tenant_id": TENANT_ID,
            "trading_mode": MODE,
            "runner_id": str(RUNNER_ID),
            "deployment_instance_id": str(INSTANCE_ID),
            "deployment_spec_id": str(spec_id),
            "deployment_spec_digest": spec_digest,
            "generation": generation,
            "strategy_id": str(STRATEGY_ID),
            "capability_version_id": str(capability_id),
            "capability_version": 1,
            "capability_manifest_digest": capability_manifest_digest,
            "key_id": identity.key_id,
            "emitted_at": emitted_at,
            "source_seq_start": source_seq_start,
            "source_seq_end": source_seq_end,
            "payload_digest": _sha256(_canonical(sequenced)),
        }
    )
    return {
        **signing_header,
        "facts": sequenced,
        "signature": identity.sign_batch_payload(_canonical(signing_header)),
    }


def _parity() -> dict[str, Any]:
    sources = {
        "execution_fill": "Nautilus OrderFilled execution identity",
        "fill": "Nautilus OrderFilled settlement projection",
        "position_closed": "Nautilus PositionClosed realized PnL",
        "fee": "Nautilus OrderFilled commission",
        "period_closed": "runner settlement period timer",
        "equity_snapshot": "canonical portfolio equity snapshot",
        "position_snapshot": "canonical trusted-mark position snapshot",
        "heartbeat": "runner observability cadence",
        "RunnerRuntimeLogFact.v1": "sanitized local deny reject and runtime diagnostics",
        "venue_ledger_snapshot_manifest": "authoritative venue ledger snapshot manifest",
        "venue_ledger_snapshot_chunk": "bounded authoritative venue ledger chunk",
        "reconciliation_period_closed": "completed reconciliation evidence period",
        "RunnerDeploymentLifecycleFact.v1": "local engine lifecycle observation",
    }
    return {
        "schema_version": 1,
        "authority_coordinate": AUTHORITY_COORDINATE,
        "closed_fact_union": True,
        "unknown_fact_kind": "terminal_unsupported_contract",
        "unsigned_telemetry_fallback": False,
        "python_float_payload_allowed": False,
        "cross_language_numeric_policy": "integer-or-canonical-decimal-string",
        "local_deny_reject_fact_kind": "RunnerRuntimeLogFact.v1",
        "rows": [
            {
                "runtime_source": sources[kind],
                "fact_kind": kind,
                "capability_projector": projector,
                "signed_batch": "RunnerFactBatchV1",
                "canonical_owner_after_ingest": "crucible-rust",
            }
            for kind, projector in RUNNER_FACT_KIND_PROJECTORS.items()
        ],
    }


def build_assets() -> dict[Path, bytes]:
    private_key = Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY_BYTES)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = f"ed25519-{_sha256(public_key)[:32]}"
    identity = RunnerFactIdentity.from_private_bytes(PRIVATE_KEY_BYTES, key_id)
    manifest = _capability_manifest(SPEC_ID, SPEC_DIGEST)
    manifest_digest = _sha256(_canonical(manifest))
    capability_receipt = _capability_receipt(manifest, CAPABILITY_ID, key_id, public_key)
    golden = _batch(
        facts=_facts(),
        batch_id=BATCH_ID,
        emitted_at=EMITTED_AT,
        source_seq_start=1,
        spec_id=SPEC_ID,
        spec_digest=SPEC_DIGEST,
        generation=7,
        capability_id=CAPABILITY_ID,
        capability_manifest_digest=manifest_digest,
        identity=identity,
    )
    signing_header = runner_fact_signing_header(golden)
    canonical_header = _canonical(signing_header)
    signing_preimage = runner_fact_signing_preimage(golden)
    signing_vector = {
        "schema_version": 1,
        "contract": "custos.runner_fact.batch-signing-preimage.v1",
        "authority_coordinate": AUTHORITY_COORDINATE,
        "signing_domain_base64": base64.b64encode(RUNNER_FACT_SIGNING_DOMAIN).decode("ascii"),
        "signing_header_fields": list(RUNNER_FACT_SIGNING_HEADER_FIELDS),
        "excluded_batch_fields": ["facts", "signature"],
        "canonical_json_rules": {
            "encoding": "UTF-8",
            "object_member_order": "ascending Unicode code point order",
            "array_order": "preserved",
            "item_separator": ",",
            "key_value_separator": ":",
            "whitespace": "none",
            "ensure_ascii": False,
            "allow_nan": False,
            "binary_float_allowed": False,
            "number_policy": "JSON integer or canonical decimal string",
            "string_escaping": (
                "JSON-escape control characters, quotation mark, and reverse solidus; "
                "emit all other Unicode as UTF-8"
            ),
            "trailing_newline": False,
        },
        "payload_digest": {
            "algorithm": "sha256",
            "formula": "sha256(canonical_json(facts))",
            "value": golden["payload_digest"],
        },
        "header": signing_header,
        "canonical_header_json_base64": base64.b64encode(canonical_header).decode("ascii"),
        "canonical_header_json_sha256": _sha256(canonical_header),
        "signing_preimage_formula": "DOMAIN || canonical_json(header)",
        "signing_preimage_base64": base64.b64encode(signing_preimage).decode("ascii"),
        "signing_preimage_sha256": _sha256(signing_preimage),
        "synthetic_signature": {
            "algorithm": "ed25519",
            "key_id": key_id,
            "public_key_base64": base64.b64encode(public_key).decode("ascii"),
            "signature_encoding": "base64url-unpadded",
            "signature_base64url_unpadded": golden["signature"],
        },
        "runtime_evidence": False,
    }

    schema = _schema()
    parity = _parity()
    objects = {
        SCHEMA_PATH: schema,
        GOLDEN_PATH: golden,
        CAPABILITY_MANIFEST_PATH: manifest,
        CAPABILITY_RECEIPT_PATH: capability_receipt,
        PARITY_PATH: parity,
        SIGNING_PREIMAGE_PATH: signing_vector,
    }
    assets: dict[Path, bytes] = {path: _pretty(value) for path, value in objects.items()}
    roles = {
        SCHEMA_PATH: "runner_fact_batch_schema",
        GOLDEN_PATH: "runner_fact_batch_golden",
        CAPABILITY_MANIFEST_PATH: "runner_fact_capability_manifest",
        CAPABILITY_RECEIPT_PATH: "runner_fact_capability_receipt_golden",
        PARITY_PATH: "runtime_event_fact_parity_matrix",
        SIGNING_PREIMAGE_PATH: "runner_fact_signing_preimage_golden",
    }
    index = {
        "asset_index_schema_version": 1,
        "authority_coordinate": AUTHORITY_COORDINATE,
        "status": "CANONICAL_V1_PENDING_RUNTIME_RECEIPTS",
        "runtime_rc": False,
        "real_runtime_round_trip_ready": False,
        "live_ready": False,
        "runtime_ready": False,
        "production_ready": False,
        "schema_version": RUNNER_FACT_SCHEMA_VERSION,
        "canonicalization": "utf8-json-sort-keys-compact-v1",
        "cross_language_numeric_policy": "integer-or-canonical-decimal-string",
        "signing_domain_base64": base64.b64encode(RUNNER_FACT_SIGNING_DOMAIN).decode("ascii"),
        "signing_header_fields": list(RUNNER_FACT_SIGNING_HEADER_FIELDS),
        "signing_header_excluded_batch_fields": ["facts", "signature"],
        "signing_preimage_formula": "DOMAIN || canonical_json(header)",
        "golden_subject": (
            f"crucible.runner.fact.v1.{golden['tenant_id']}."
            f"{golden['runner_id']}.{golden['trading_mode']}"
        ),
        "stream_identity_fields": [
            "tenant_id",
            "trading_mode",
            "runner_id",
            "deployment_instance_id",
        ],
        "signed_fencing_fields": [
            "deployment_spec_id",
            "deployment_spec_digest",
            "generation",
        ],
        "fact_kind_projectors": dict(RUNNER_FACT_KIND_PROJECTORS),
        "synthetic_signature": {
            "algorithm": "ed25519",
            "key_id": key_id,
            "public_key_base64": base64.b64encode(public_key).decode("ascii"),
            "runtime_evidence": False,
        },
        "assets": [
            {
                "role": roles[path],
                "path": str(path),
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
            }
            for path, payload in assets.items()
        ],
    }
    index_payload = _pretty(index)
    assets[INDEX_PATH] = index_payload
    assets[RECEIPT_PATH] = _pretty(
        {
            "receipt_schema_version": 1,
            "canonical_name": "Custos RunnerFact V1 producer receipt",
            "status": "READY_FOR_CRUCIBLE_CONSUMER_VALIDATION",
            "authority_coordinate": AUTHORITY_COORDINATE,
            "producer_commit": PRODUCER_ASSET_COMMIT,
            "asset_index": {
                "path": str(INDEX_PATH),
                "sha256": _sha256(index_payload),
                "size_bytes": len(index_payload),
            },
            "consumer_receipts": {
                "crucible_rust": None,
            },
            "runtime_rc": False,
            "real_runtime_round_trip_ready": False,
            "live_ready": False,
            "runtime_ready": False,
            "production_ready": False,
            "open_blockers": [
                "Crucible RunnerFact V1 exact-byte consumer receipt",
                "real runtime round-trip receipt",
                "immutable runtime RC receipt",
            ],
        }
    )
    for path, payload in tuple(assets.items()):
        assets[path.with_name(path.name + ".sha256")] = _sidecar(path, payload)
    return assets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_assets()
    drift: list[str] = []
    for relative_path, payload in expected.items():
        path = ROOT / relative_path
        if args.check:
            if not path.is_file() or path.read_bytes() != payload:
                drift.append(str(relative_path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if drift:
        print("RunnerFact candidate drift:", file=sys.stderr)
        for drift_path in drift:
            print(f"  - {drift_path}", file=sys.stderr)
        return 1
    if args.check:
        print("RunnerFact V1 authority assets are exact")
    else:
        print(f"generated {len(expected)} RunnerFact V1 authority assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
