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
    TERMINAL_VALUATION_KIND,
    TERMINAL_VALUATION_REASON_CODES,
    RunnerFactAuthority,
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
    terminal_valuation,
    valuation_checkpoint,
    venue_ledger_snapshot_facts,
)

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_COORDINATE = "custos.runner-fact.v1"
# Raised only when the wire shape or the meaning of a field changes; a change to
# descriptive text alone keeps the revision.
CONTRACT_ID = "alephain.custos.runner_fact_batch.v1"
CONTRACT_REVISION = 2
PRODUCER_ASSET_COMMIT = "199bb6475eae87b78d2e1db27eff319a5a3ebe6b"
CRUCIBLE_CONSUMER_RECEIPT = {
    "repository": "tesseract-trading/crucible-rust",
    "commit": "61733e5f1f2387ca58a728c9b2f26c8a01f92c8d",
    "producer_path": ("docs/authority/receipts/crucible-runner-fact-v1-consumer-receipt.json"),
    "local_path": ("docs/authority/receipts/vendor/crucible-runner-fact-v1-consumer-receipt.json"),
    "sha256": "e900e3babdbb43b3e74e956c8461a77662bf8ef30bb4dc22aa49de4874248c35",
    "size_bytes": 14597,
    "status": "EXACT_CUSTOS_RUNNER_FACT_V1_ACCEPTED_RUNTIME_OPEN",
}
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
# The stop-boundary vector continues the golden stream: one more batch in the
# running generation, then the stop generation's lifecycle and terminal facts.
STOP_COMMAND_FINGERPRINT = "d" * 64
TERMINAL_RUN_BATCH_ID = UUID("60000000-0000-4000-8000-000000000007")
TERMINAL_STOP_BATCH_ID = UUID("60000000-0000-4000-8000-000000000008")
CREDENTIAL_SCOPE_ID = "91000000-0000-4000-8000-000000000011"
CREDENTIAL_SCOPE_DIGEST = "a" * 64
# A synthetic image runtime: a well-formed declaration, not a published image.
# A runner writes its own observed runtime when it publishes; nothing copies this.
RUNTIME = {
    "distribution": "oci_image",
    "image_digest": "sha256:" + "7" * 64,
    "source_revision": "8" * 40,
    "engine": "nautilus",
    "engine_version": "2.0.0rc5+sodex.2",
}

SCHEMA_PATH = Path("docs/gateway-contract/v1/runner_fact_batch_v1.schema.json")
GOLDEN_PATH = Path("docs/authority/runner-fact-golden-v1.json")
CAPABILITY_MANIFEST_PATH = Path("docs/authority/runner-fact-capability-manifest-v1.json")
CAPABILITY_RECEIPT_PATH = Path("docs/authority/runner-fact-capability-receipt-golden-v1.json")
PARITY_PATH = Path("docs/authority/runner-fact-parity-matrix-v1.json")
SIGNING_PREIMAGE_PATH = Path("docs/authority/runner-fact-signing-preimage-golden-v1.json")
TERMINAL_BOUNDARY_PATH = Path("docs/authority/runner-fact-terminal-boundary-golden-v1.json")
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
    # Zero has no sign: the runner writes "0", and a consumer reading a decimal
    # type cannot keep the sign of "-0".
    decimal = {
        "type": "string",
        "pattern": r"^(?:0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$",
    }
    unsigned_decimal = {
        "type": "string",
        "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$",
    }
    currency = {"enum": ["USD", "USDT", "USDC", "BTC", "ETH", "VUSDC", "VBTC", "VETH"]}
    timestamp = {
        "type": "string",
        "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,9})?Z$",
    }
    non_empty = {"type": "string", "minLength": 1}
    settlement_period = {
        "type": "string",
        "pattern": r"^[0-9]{4}-(?:0[1-9]|1[0-2])$",
    }
    uuid = {
        "type": "string",
        "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    }
    digest = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
    side = {"enum": ["buy", "sell"]}
    wallet_type = {"type": "string", "pattern": r"^[a-z][a-z0-9_]{0,63}$"}
    sub_account = {
        "oneOf": [
            {
                "type": "string",
                "pattern": r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]{0,126}[^\s\x00-\x1f\x7f])?$",
            },
            {"type": "null"},
        ]
    }
    optional_non_empty = {"oneOf": [non_empty, {"type": "null"}]}
    positive_decimal = {
        "type": "string",
        "pattern": r"^(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9])$",
    }
    balance = {
        "type": "object",
        "additionalProperties": False,
        "required": ["wallet_type", "sub_account", "asset", "currency", "total", "available"],
        "properties": {
            "wallet_type": wallet_type,
            "sub_account": sub_account,
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
    completeness_fields = (
        "balances_complete",
        "positions_complete",
        "fills_complete",
        "fees_complete",
        "cash_flows_complete",
    )
    completeness = {
        "type": "object",
        "additionalProperties": False,
        "required": list(completeness_fields),
        "properties": {name: {"type": "boolean"} for name in completeness_fields},
    }
    cash_flow_endpoint = {
        "type": "object",
        "additionalProperties": False,
        "required": ["wallet_type", "sub_account"],
        "properties": {"wallet_type": wallet_type, "sub_account": sub_account},
    }
    endpoint_or_null = {"oneOf": [cash_flow_endpoint, {"type": "null"}]}
    destination = {
        "type": "object",
        "additionalProperties": False,
        "required": ["address", "network", "memo"],
        "properties": {
            "address": non_empty,
            "network": optional_non_empty,
            "memo": optional_non_empty,
        },
    }

    def cash_flow_rule(
        kinds: list[str], *, source: bool, target: bool, uid: bool, withdrawal: bool
    ) -> dict[str, Any]:
        present = cash_flow_endpoint
        absent = {"type": "null"}
        return {
            "if": {"properties": {"kind": {"enum": kinds}}},
            "then": {
                "properties": {
                    "from": present if source else absent,
                    "to": present if target else absent,
                    "counterparty_uid": non_empty if uid else absent,
                    **({} if withdrawal else {"destination": absent}),
                }
            },
        }

    ledger_cash_flow = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "cash_flow_id",
            "kind",
            "from",
            "to",
            "counterparty_uid",
            "currency",
            "amount",
            "fee",
            "fee_currency",
            "occurred_at",
            "external_reference",
            "destination",
        ],
        "properties": {
            # The byte limit is exact in the assembler; JSON Schema counts
            # characters, so this bound is only a first check.
            "cash_flow_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "pattern": r"^[^\u0000-\u001f\u007f-\u009f]+$",
            },
            "kind": {
                "enum": [
                    "internal_transfer",
                    "sub_account_transfer",
                    "uid_transfer_out",
                    "uid_transfer_in",
                    "deposit",
                    "withdrawal",
                ]
            },
            "from": endpoint_or_null,
            "to": endpoint_or_null,
            "counterparty_uid": optional_non_empty,
            "currency": currency,
            "amount": positive_decimal,
            "fee": unsigned_decimal,
            "fee_currency": currency,
            "occurred_at": timestamp,
            "external_reference": optional_non_empty,
            "destination": {"oneOf": [destination, {"type": "null"}]},
        },
        "allOf": [
            cash_flow_rule(
                ["internal_transfer", "sub_account_transfer"],
                source=True,
                target=True,
                uid=False,
                withdrawal=False,
            ),
            cash_flow_rule(
                ["uid_transfer_out"], source=True, target=False, uid=True, withdrawal=False
            ),
            cash_flow_rule(
                ["uid_transfer_in"], source=False, target=True, uid=True, withdrawal=False
            ),
            cash_flow_rule(["deposit"], source=False, target=True, uid=False, withdrawal=False),
            cash_flow_rule(["withdrawal"], source=True, target=False, uid=False, withdrawal=True),
        ],
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
                "position_id": {
                    **uuid,
                    "description": (
                        "Identifies exactly one position lifecycle, from open to close; "
                        "no two lifecycles share it. Under a NETTING account, where the "
                        "engine keeps one position id across every reopen of an instrument, "
                        "it is derived from that position id, the order that opened the "
                        "lifecycle and the instant it opened."
                    ),
                },
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
            "period_closed", {"period": settlement_period, "closed_at": timestamp}
        ),
        "venue_ledger_snapshot_manifest": _object_schema(
            "venue_ledger_snapshot_manifest",
            {
                "snapshot_id": uuid,
                "venue": non_empty,
                "sub_account": sub_account,
                "source": {"enum": ["venue_api", "drop_copy"]},
                "watermark": non_empty,
                "coverage_from": timestamp,
                "observed_through": timestamp,
                "completeness": completeness,
                "balances_count": {"type": "integer", "minimum": 0},
                "positions_count": {"type": "integer", "minimum": 0},
                "fills_count": {"type": "integer", "minimum": 0},
                "fees_count": {"type": "integer", "minimum": 0},
                "cash_flows_count": {"type": "integer", "minimum": 0},
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
                "cash_flows": {"type": "array", "items": ledger_cash_flow},
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
    valuation_position = {
        "instrument": non_empty,
        "currency": currency,
        "internal_quantity": decimal,
        "internal_avg_entry_price": unsigned_decimal,
        "internal_mark_price": unsigned_decimal,
        "venue_quantity": decimal,
        "venue_avg_entry_price": unsigned_decimal,
        "common_mark_price": unsigned_decimal,
    }
    facts["RunnerValuationCheckpointFact.v1"] = _object_schema(
        "RunnerValuationCheckpointFact.v1",
        {
            "checkpoint_id": uuid,
            "venue_snapshot_id": uuid,
            "venue": non_empty,
            "currency": currency,
            "venue_watermark": non_empty,
            "collection_started_at": timestamp,
            "observed_at": timestamp,
            "internal_equity": decimal,
            "venue_wallet_balance": decimal,
            "checkpoint_digest": digest,
            "positions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(valuation_position),
                    "properties": valuation_position,
                },
            },
        },
    )
    inventory_row = {
        "asset": currency,
        "internal_quantity": unsigned_decimal,
        "venue_quantity": unsigned_decimal,
        "internal_mark_price": unsigned_decimal,
        "common_mark_price": unsigned_decimal,
    }
    facts["RunnerValuationCheckpointFact.v1"]["properties"]["cash_inventory"] = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": list(inventory_row),
            "properties": inventory_row,
        },
    }
    definitions["fact_RunnerValuationCheckpointFact.v1"] = facts["RunnerValuationCheckpointFact.v1"]
    definitions[f"fact_{TERMINAL_VALUATION_KIND}"] = _terminal_valuation_schema(
        non_empty=non_empty,
        uuid=uuid,
        digest=digest,
        timestamp=timestamp,
        decimal=decimal,
        currency=currency,
        settlement_period=settlement_period,
        sub_account=sub_account,
        wallet_type=wallet_type,
        position_row=position_row,
    )
    facts["execution_fill"]["properties"]["fee_currency"] = currency
    facts["execution_fill"]["properties"]["fee"] = decimal
    facts["fee"]["properties"]["amount"] = decimal
    ledger_fill["properties"]["fee_currency"] = currency
    ledger_fill["properties"]["fee"] = decimal
    ledger_fee["properties"]["amount"] = decimal
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "custos://gateway-contract/v1/runner_fact_batch_v1.schema.json",
        "x-contract-id": CONTRACT_ID,
        "x-contract-revision": CONTRACT_REVISION,
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
            "subject": "crucible.runner.fact.v1.{tenant_id}.{runner_id}.{trading_mode}",
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


def _terminal_valuation_schema(
    *,
    non_empty: dict[str, Any],
    uuid: dict[str, Any],
    digest: dict[str, Any],
    timestamp: dict[str, Any],
    decimal: dict[str, Any],
    currency: dict[str, Any],
    settlement_period: dict[str, Any],
    sub_account: dict[str, Any],
    wallet_type: dict[str, Any],
    position_row: dict[str, Any],
) -> dict[str, Any]:
    null = {"type": "null"}
    generation = {"type": "integer", "minimum": 1}
    reason = {"enum": list(TERMINAL_VALUATION_REASON_CODES)}
    account_scope = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "venue",
            "credential_scope_id",
            "credential_scope_digest",
            "sub_account",
            "wallet_type",
        ],
        "properties": {
            "venue": non_empty,
            "credential_scope_id": uuid,
            "credential_scope_digest": digest,
            "sub_account": sub_account,
            "wallet_type": {"oneOf": [wallet_type, null]},
        },
    }
    equity = {
        "type": "object",
        "additionalProperties": False,
        "required": ["amount", "currency"],
        "properties": {"amount": decimal, "currency": currency},
    }
    prior_equity = {
        "type": "object",
        "additionalProperties": False,
        "required": ["event_id", "seq", "amount", "currency", "observed_at"],
        "properties": {
            "event_id": uuid,
            "seq": generation,
            "amount": decimal,
            "currency": currency,
            "observed_at": timestamp,
        },
    }
    positions = {"type": "array", "items": position_row}
    schema = _object_schema(
        TERMINAL_VALUATION_KIND,
        {
            "tenant_id": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$"},
            "mode": {"enum": ["live", "sandbox", "testnet"]},
            "runner_id": uuid,
            "deployment_instance_id": uuid,
            "deployment_spec_id": uuid,
            "deployment_spec_digest": digest,
            "generation": generation,
            "issuer_key_id": non_empty,
            "command_fingerprint": digest,
            "closes_generation": {"oneOf": [generation, null]},
            "outcome": {"enum": ["confirmed", "valuation_unconfirmed"]},
            "reason_code": {"oneOf": [reason, null]},
            "account_scope": account_scope,
            "valuation_source": {"enum": ["venue_account", "simulated_account"]},
            "position_policy": {"enum": ["flatten", "preserve"]},
            "period": {"oneOf": [settlement_period, null]},
            "stop_requested_at": timestamp,
            "valuation_observed_at": {"oneOf": [timestamp, null]},
            "marks_oldest_at": {"oneOf": [timestamp, null]},
            "stop_effective_at": {"oneOf": [timestamp, null]},
            "equity": {"oneOf": [equity, null]},
            "open_positions": {"oneOf": [positions, null]},
            "prior_equity": {"oneOf": [prior_equity, null]},
            "valuation_digest": digest,
        },
    )

    def when(condition: dict[str, Any], then: dict[str, Any]) -> dict[str, Any]:
        return {
            "if": {"required": list(condition), "properties": condition},
            "then": {"properties": then},
        }

    confirmed = {"outcome": {"const": "confirmed"}}
    no_open_positions = {"open_positions": {"maxItems": 0}}
    schema["allOf"] = [
        when(
            confirmed,
            {
                "reason_code": null,
                "closes_generation": generation,
                "period": settlement_period,
                "valuation_observed_at": timestamp,
                "stop_effective_at": timestamp,
                "equity": equity,
                "open_positions": positions,
                "prior_equity": prior_equity,
            },
        ),
        when(
            {"outcome": {"const": "valuation_unconfirmed"}},
            {
                "reason_code": reason,
                "period": null,
                "valuation_observed_at": null,
                "marks_oldest_at": null,
                "equity": null,
                "open_positions": null,
            },
        ),
        when({**confirmed, "position_policy": {"const": "flatten"}}, no_open_positions),
        # Without any price read there is no mark watermark, and so no position.
        when({**confirmed, "marks_oldest_at": null}, no_open_positions),
        when(
            {"reason_code": {"const": "no_prior_running_generation"}},
            {"closes_generation": null},
        ),
    ]
    return schema


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
        "runtime": dict(RUNTIME),
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
            period="2026-07",
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
        sub_account=None,
        source="venue_api",
        watermark="ledger-1",
        coverage_from="2026-07-15T07:00:00Z",
        observed_through=timestamp,
        completeness={
            "balances_complete": True,
            "positions_complete": True,
            "fills_complete": True,
            "fees_complete": True,
            "cash_flows_complete": True,
        },
        balances=[
            {
                "wallet_type": "spot",
                "sub_account": None,
                "asset": "USDT",
                "currency": "USDT",
                "total": "10012.5",
                "available": "9412.5",
            },
            {
                "wallet_type": "spot",
                "sub_account": "payout",
                "asset": "USDT",
                "currency": "USDT",
                "total": "250",
                "available": "250",
            },
        ],
        positions=[],
        fills=[],
        fees=[],
        cash_flows=[
            {
                "cash_flow_id": "transfer-1",
                "kind": "sub_account_transfer",
                "from": {"wallet_type": "spot", "sub_account": None},
                "to": {"wallet_type": "spot", "sub_account": "payout"},
                "counterparty_uid": None,
                "currency": "USDT",
                "amount": "300",
                "fee": "0",
                "fee_currency": "USDT",
                "occurred_at": "2026-07-15T07:20:00Z",
                "external_reference": None,
                "destination": None,
            },
            {
                "cash_flow_id": "uid-out-1",
                "kind": "uid_transfer_out",
                "from": {"wallet_type": "spot", "sub_account": "payout"},
                "to": None,
                "counterparty_uid": "100200300",
                "currency": "USDT",
                "amount": "20",
                "fee": "0",
                "fee_currency": "USDT",
                "occurred_at": "2026-07-15T07:30:00Z",
                "external_reference": None,
                "destination": None,
            },
            {
                "cash_flow_id": "withdraw-1",
                "kind": "withdrawal",
                "from": {"wallet_type": "spot", "sub_account": "payout"},
                "to": None,
                "counterparty_uid": None,
                "currency": "USDT",
                "amount": "30",
                "fee": "1",
                "fee_currency": "USDT",
                "occurred_at": "2026-07-15T07:40:00Z",
                "external_reference": "0x5f1e0000000000000000000000000000000000000000000000000000000000aa",
                "destination": {
                    "address": "0x00000000000000000000000000000000000000b1",
                    "network": "ETH",
                    "memo": None,
                },
            },
        ],
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
        _lifecycle_fact(
            generation=7,
            lifecycle_state="running",
            command_fingerprint=COMMAND_FINGERPRINT,
            observed_at=timestamp,
        )
    )
    facts.append(
        valuation_checkpoint(
            event_id=UUID("80000000-0000-4000-8000-000000000014"),
            checkpoint_id=UUID("80000000-0000-4000-8000-000000000015"),
            venue_snapshot_id=snapshot_id,
            venue="BINANCE",
            currency="USDT",
            venue_watermark="ledger-1",
            collection_started_at=timestamp,
            observed_at=timestamp,
            internal_equity="10012.5",
            venue_wallet_balance="10012.5",
            positions=[],
        )
    )
    # The terminal valuation must close its own stop batch; the stop-boundary
    # asset carries it, and the closed union is asserted across both assets.
    if set(RUNNER_FACT_KIND_PROJECTORS) - {TERMINAL_VALUATION_KIND} != {
        fact["kind"] for fact in facts
    }:
        raise RuntimeError("golden does not contain the single-batch RunnerFact kind union")
    return facts


def _lifecycle_fact(
    *, generation: int, lifecycle_state: str, command_fingerprint: str, observed_at: str
) -> dict[str, Any]:
    return {
        "kind": "RunnerDeploymentLifecycleFact.v1",
        "event_id": str(
            command_lifecycle_event_id(
                tenant_id=TENANT_ID,
                trading_mode=MODE,
                runner_id=RUNNER_ID,
                deployment_instance_id=INSTANCE_ID,
                deployment_spec_id=SPEC_ID,
                deployment_spec_digest=SPEC_DIGEST,
                generation=generation,
                lifecycle_state=lifecycle_state,
                command_fingerprint=command_fingerprint,
                outcome="applied",
            )
        ),
        "occurred_at": observed_at,
        "tenant_id": TENANT_ID,
        "mode": MODE,
        "runner_id": str(RUNNER_ID),
        "deployment_instance_id": str(INSTANCE_ID),
        "deployment_spec_id": str(SPEC_ID),
        "deployment_spec_digest": SPEC_DIGEST,
        "generation": generation,
        "lifecycle_state": lifecycle_state,
        "command_fingerprint": command_fingerprint,
        "outcome": "applied",
        "observed_at": observed_at,
    }


def _terminal_boundary(
    golden: dict[str, Any],
    *,
    capability_manifest_digest: str,
    identity: RunnerFactIdentity,
) -> list[dict[str, Any]]:
    """A flatten stop in the month after the golden's close, valued at the boundary."""

    running_generation = golden["generation"]
    stop_generation = running_generation + 1
    run_batch = _batch(
        facts=[
            equity_snapshot(
                event_id=UUID("81000000-0000-4000-8000-000000000001"),
                amount="10050",
                currency="USDT",
                observed_at="2026-08-14T09:59:30Z",
            )
        ],
        batch_id=TERMINAL_RUN_BATCH_ID,
        emitted_at="2026-08-14T10:00:00Z",
        source_seq_start=golden["source_seq_end"] + 1,
        spec_id=SPEC_ID,
        spec_digest=SPEC_DIGEST,
        generation=running_generation,
        capability_id=CAPABILITY_ID,
        capability_manifest_digest=capability_manifest_digest,
        identity=identity,
    )
    prior = run_batch["facts"][-1]
    stop_authority = RunnerFactAuthority(
        tenant_id=TENANT_ID,
        trading_mode=MODE,
        runner_id=RUNNER_ID,
        deployment_instance_id=INSTANCE_ID,
        deployment_spec_id=SPEC_ID,
        deployment_spec_digest=SPEC_DIGEST,
        generation=stop_generation,
        strategy_id=STRATEGY_ID,
        capability_version_id=CAPABILITY_ID,
        capability_version=1,
        capability_manifest_digest=capability_manifest_digest,
    )
    stop_batch = _batch(
        facts=[
            _lifecycle_fact(
                generation=stop_generation,
                lifecycle_state="stopped",
                command_fingerprint=STOP_COMMAND_FINGERPRINT,
                observed_at="2026-08-14T10:05:01Z",
            ),
            terminal_valuation(
                authority=stop_authority,
                issuer_key_id=identity.key_id,
                command_fingerprint=STOP_COMMAND_FINGERPRINT,
                closes_generation=running_generation,
                outcome="confirmed",
                reason_code=None,
                account_scope={
                    "venue": "BINANCE",
                    "credential_scope_id": CREDENTIAL_SCOPE_ID,
                    "credential_scope_digest": CREDENTIAL_SCOPE_DIGEST,
                    "sub_account": None,
                    "wallet_type": "spot",
                },
                valuation_source="simulated_account",
                position_policy="flatten",
                stop_requested_at="2026-08-14T10:05:00Z",
                valuation_observed_at="2026-08-14T10:05:00.500Z",
                marks_oldest_at=None,
                stop_effective_at="2026-08-14T10:05:00.800Z",
                equity={"amount": "10060", "currency": "USDT"},
                open_positions=[],
                prior_equity={
                    "event_id": prior["event_id"],
                    "seq": prior["seq"],
                    "amount": prior["amount"],
                    "currency": prior["currency"],
                    "observed_at": prior["observed_at"],
                },
            ),
        ],
        batch_id=TERMINAL_STOP_BATCH_ID,
        emitted_at="2026-08-14T10:05:02Z",
        source_seq_start=run_batch["source_seq_end"] + 1,
        spec_id=SPEC_ID,
        spec_digest=SPEC_DIGEST,
        generation=stop_generation,
        capability_id=CAPABILITY_ID,
        capability_manifest_digest=capability_manifest_digest,
        identity=identity,
    )
    return [run_batch, stop_batch]


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
        "RunnerValuationCheckpointFact.v1": "common-mark independent ledger valuation",
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
        TERMINAL_VALUATION_KIND: "same-boundary valuation at a signed instance stop",
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
    terminal_boundary = _terminal_boundary(
        golden, capability_manifest_digest=manifest_digest, identity=identity
    )
    union_kinds = {fact["kind"] for fact in golden["facts"]} | {
        fact["kind"] for batch in terminal_boundary for fact in batch["facts"]
    }
    if union_kinds != set(RUNNER_FACT_KIND_PROJECTORS):
        raise RuntimeError("assets do not contain the closed RunnerFact kind union")
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
        TERMINAL_BOUNDARY_PATH: terminal_boundary,
    }
    assets: dict[Path, bytes] = {path: _pretty(value) for path, value in objects.items()}
    roles = {
        SCHEMA_PATH: "runner_fact_batch_schema",
        GOLDEN_PATH: "runner_fact_batch_golden",
        CAPABILITY_MANIFEST_PATH: "runner_fact_capability_manifest",
        CAPABILITY_RECEIPT_PATH: "runner_fact_capability_receipt_golden",
        PARITY_PATH: "runtime_event_fact_parity_matrix",
        SIGNING_PREIMAGE_PATH: "runner_fact_signing_preimage_golden",
        TERMINAL_BOUNDARY_PATH: "runner_fact_terminal_boundary_golden",
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
        "closed_fact_union_assets": [str(GOLDEN_PATH), str(TERMINAL_BOUNDARY_PATH)],
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
    # Recorded producer acceptance belongs to its original Git revision.
    # Regenerating current schemas must not rewrite that historical receipt.
    assets[RECEIPT_PATH] = (ROOT / RECEIPT_PATH).read_bytes()
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
