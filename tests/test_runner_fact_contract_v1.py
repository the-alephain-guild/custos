"""Canonical first-production RunnerFact V1 contract."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator

import custos.core.runner_fact as runner_fact_module
from custos.core.runner_fact import (
    RUNNER_FACT_SIGNING_DOMAIN,
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactContractError,
    RunnerFactIdentity,
    RunnerFactOutbox,
    normalize_capability_scope_bindings,
    reconciliation_period_closed,
    settlement_period_closed,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "docs/authority/runner-fact-contract-assets-v1.json"
RECEIPT_PATH = ROOT / "docs/authority/receipts/custos-runner-fact-v1-producer-receipt.json"
CRUCIBLE_CONSUMER_RECEIPT_PATH = (
    ROOT / "docs/authority/receipts/vendor/crucible-runner-fact-v1-consumer-receipt.json"
)
SCHEMA_PATH = ROOT / "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json"
GOLDEN_PATH = ROOT / "docs/authority/runner-fact-golden-v1.json"
CAPABILITY_MANIFEST_PATH = ROOT / "docs/authority/runner-fact-capability-manifest-v1.json"
CAPABILITY_RECEIPT_PATH = ROOT / "docs/authority/runner-fact-capability-receipt-golden-v1.json"
PARITY_PATH = ROOT / "docs/authority/runner-fact-parity-matrix-v1.json"
SIGNING_PREIMAGE_PATH = ROOT / "docs/authority/runner-fact-signing-preimage-golden-v1.json"

EXPECTED_SIGNING_HEADER_FIELDS = [
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
]

EXPECTED_KINDS = {
    "execution_fill": "reconciliation",
    "fill": "settlement",
    "position_closed": "settlement",
    "fee": "settlement",
    "period_closed": "settlement",
    "equity_snapshot": "risk",
    "position_snapshot": "risk",
    "heartbeat": "health",
    "RunnerRuntimeLogFact.v1": "health",
    "venue_ledger_snapshot_manifest": "reconciliation",
    "venue_ledger_snapshot_chunk": "reconciliation",
    "reconciliation_period_closed": "reconciliation",
    "RunnerDeploymentLifecycleFact.v1": "deployment_lifecycle",
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _unpadded_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _authority(batch: dict[str, Any]) -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id=batch["tenant_id"],
        trading_mode=batch["trading_mode"],
        runner_id=UUID(batch["runner_id"]),
        deployment_instance_id=UUID(batch["deployment_instance_id"]),
        deployment_spec_id=UUID(batch["deployment_spec_id"]),
        deployment_spec_digest=batch["deployment_spec_digest"],
        generation=batch["generation"],
        strategy_id=UUID(batch["strategy_id"]),
        capability_version_id=UUID(batch["capability_version_id"]),
        capability_version=batch["capability_version"],
        capability_manifest_digest=batch["capability_manifest_digest"],
    )


def test_v1_inventory_is_complete_and_byte_pinned() -> None:
    index = _json(INDEX_PATH)
    receipt = _json(RECEIPT_PATH)
    assert index["authority_coordinate"] == "custos.runner-fact.v1"
    assert "supersedes_candidate_coordinate" not in index
    assert "superseded_candidate_status" not in index
    assert index["status"] == "CANONICAL_V1_PENDING_RUNTIME_RECEIPTS"
    assert index["stream_identity_fields"] == [
        "tenant_id",
        "trading_mode",
        "runner_id",
        "deployment_instance_id",
    ]
    assert index["signed_fencing_fields"] == [
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
    ]
    assert index["runtime_rc"] is False
    assert index["live_ready"] is False
    assert index["runtime_ready"] is False
    assert index["production_ready"] is False

    expected_paths = {
        str(path.relative_to(ROOT))
        for path in (
            SCHEMA_PATH,
            GOLDEN_PATH,
            CAPABILITY_MANIFEST_PATH,
            CAPABILITY_RECEIPT_PATH,
            PARITY_PATH,
            SIGNING_PREIMAGE_PATH,
        )
    }
    assets = {asset["path"]: asset for asset in index["assets"]}
    assert set(assets) == expected_paths
    for relative_path, asset in assets.items():
        path = ROOT / relative_path
        payload = path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == asset["sha256"]
        assert len(payload) == asset["size_bytes"]
        sidecar = path.with_name(path.name + ".sha256")
        assert sidecar.read_text(encoding="ascii") == (f"{asset['sha256']}  {path.name}\n")
    index_payload = INDEX_PATH.read_bytes()
    assert receipt["status"] == "PHASE_A_CONSUMER_ACCEPTED_RUNTIME_OPEN"
    assert receipt["producer_commit"] == "cce76931884de36c9606db02d94cf4124e7164b5"
    crucible_payload = CRUCIBLE_CONSUMER_RECEIPT_PATH.read_bytes()
    assert receipt["consumer_receipts"] == {
        "crucible_rust": {
            "repository": "tesseract-trading/crucible-rust",
            "commit": "491c632bd9395363a22543f3592a8eea6bd0be09",
            "producer_path": (
                "docs/authority/receipts/crucible-runner-fact-v1-consumer-receipt.json"
            ),
            "local_path": (
                "docs/authority/receipts/vendor/crucible-runner-fact-v1-consumer-receipt.json"
            ),
            "sha256": hashlib.sha256(crucible_payload).hexdigest(),
            "size_bytes": len(crucible_payload),
            "status": "EXACT_CUSTOS_RUNNER_FACT_V1_ACCEPTED_RUNTIME_OPEN",
        }
    }
    crucible_receipt = _json(CRUCIBLE_CONSUMER_RECEIPT_PATH)
    assert (
        crucible_receipt["producer"]["asset_commit"] == "cce76931884de36c9606db02d94cf4124e7164b5"
    )
    assert "producer_receipt" not in crucible_receipt["producer"]
    assert receipt["asset_index"] == {
        "path": "docs/authority/runner-fact-contract-assets-v1.json",
        "sha256": hashlib.sha256(index_payload).hexdigest(),
        "size_bytes": len(index_payload),
    }


def test_schema_golden_capability_and_signature_are_one_exact_contract() -> None:
    index = _json(INDEX_PATH)
    schema = _json(SCHEMA_PATH)
    batch = _json(GOLDEN_PATH)
    manifest = _json(CAPABILITY_MANIFEST_PATH)
    capability = RunnerCapabilityReceipt.load(CAPABILITY_RECEIPT_PATH)

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(batch)
    assert batch["capability_manifest_digest"] == hashlib.sha256(_canonical(manifest)).hexdigest()
    assert capability.manifest_digest == batch["capability_manifest_digest"]
    assert capability.capability_version_id == UUID(batch["capability_version_id"])
    assert capability.runner_id == UUID(batch["runner_id"])
    assert {binding.projector for binding in capability.scope_bindings} == {
        "settlement",
        "risk",
        "health",
        "reconciliation",
        "deployment_lifecycle",
    }

    facts = batch["facts"]
    periods = {fact["kind"]: fact["period"] for fact in facts if "period" in fact}
    assert periods["period_closed"] == "2026-07"
    assert periods["reconciliation_period_closed"] == "20260715T070000Z_20260715T080000Z"
    with pytest.raises(RunnerFactContractError, match="settlement period must use YYYY-MM"):
        settlement_period_closed(
            event_id=UUID("80000000-0000-4000-8000-000000000099"),
            period=periods["reconciliation_period_closed"],
            closed_at=batch["emitted_at"],
        )
    assert (
        reconciliation_period_closed(
            event_id=UUID("80000000-0000-4000-8000-000000000098"),
            period=periods["reconciliation_period_closed"],
            period_started_at="2026-07-15T07:00:00Z",
            closed_at="2026-07-15T08:00:00Z",
            venue_snapshots=[
                {
                    "venue": "BINANCE",
                    "snapshot_id": UUID("70000000-0000-4000-8000-000000000007"),
                }
            ],
        )["period"]
        == periods["reconciliation_period_closed"]
    )
    assert hashlib.sha256(_canonical(facts)).hexdigest() == batch["payload_digest"]
    assert [fact["seq"] for fact in facts] == list(
        range(batch["source_seq_start"], batch["source_seq_end"] + 1)
    )
    assert {fact["kind"] for fact in facts} == set(EXPECTED_KINDS)
    assert _authority(batch).subject == index["golden_subject"]

    preimage = runner_fact_module.runner_fact_signing_preimage(batch)
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(index["synthetic_signature"]["public_key_base64"])
    )
    public_key.verify(_unpadded_urlsafe(batch["signature"]), preimage)
    assert index["synthetic_signature"]["runtime_evidence"] is False


def test_signing_preimage_golden_is_exact_and_matches_production() -> None:
    index = _json(INDEX_PATH)
    batch = _json(GOLDEN_PATH)
    vector = _json(SIGNING_PREIMAGE_PATH)
    header_fields = list(runner_fact_module.RUNNER_FACT_SIGNING_HEADER_FIELDS)
    header = runner_fact_module.runner_fact_signing_header(batch)
    canonical_header = _canonical(header)
    preimage = runner_fact_module.runner_fact_signing_preimage(batch)

    assert header_fields == EXPECTED_SIGNING_HEADER_FIELDS
    assert vector["signing_header_fields"] == EXPECTED_SIGNING_HEADER_FIELDS
    assert list(header) == EXPECTED_SIGNING_HEADER_FIELDS
    assert set(batch) == set(EXPECTED_SIGNING_HEADER_FIELDS) | {"facts", "signature"}
    assert vector["excluded_batch_fields"] == ["facts", "signature"]
    assert vector["header"] == header
    assert base64.b64decode(vector["canonical_header_json_base64"]) == canonical_header
    assert vector["canonical_header_json_sha256"] == hashlib.sha256(canonical_header).hexdigest()
    assert base64.b64decode(vector["signing_preimage_base64"]) == preimage
    assert vector["signing_preimage_sha256"] == hashlib.sha256(preimage).hexdigest()
    assert preimage == RUNNER_FACT_SIGNING_DOMAIN + canonical_header
    assert vector["payload_digest"]["formula"] == "sha256(canonical_json(facts))"
    assert (
        vector["payload_digest"]["value"] == hashlib.sha256(_canonical(batch["facts"])).hexdigest()
    )
    assert vector["synthetic_signature"]["signature_base64url_unpadded"] == batch["signature"]
    assert (
        vector["synthetic_signature"]["public_key_base64"]
        == index["synthetic_signature"]["public_key_base64"]
    )
    Ed25519PublicKey.from_public_bytes(
        base64.b64decode(vector["synthetic_signature"]["public_key_base64"])
    ).verify(_unpadded_urlsafe(batch["signature"]), preimage)
    assert vector["runtime_evidence"] is False


@pytest.mark.parametrize("mutation", ["projector", "unknown_disposition"])
def test_capability_loader_pins_closed_projector_contract_exactly(
    tmp_path: Path,
    mutation: str,
) -> None:
    document = copy.deepcopy(_json(CAPABILITY_RECEIPT_PATH))
    manifest = document["capability_manifest"]
    if mutation == "projector":
        manifest["fact_kind_projectors"]["heartbeat"] = "risk"
    else:
        manifest["unknown_fact_kind"] = "ignore"
    document["manifest_digest"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    mutated = tmp_path / f"capability-{mutation}.json"
    mutated.write_bytes(json.dumps(document).encode("utf-8"))

    with pytest.raises(RunnerFactContractError, match="closed fact projector contract"):
        RunnerCapabilityReceipt.load(mutated)


@pytest.mark.asyncio
async def test_v1_outbox_continues_instance_sequence_across_generation_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _json(GOLDEN_PATH)
    schema = _json(SCHEMA_PATH)
    next_batch_id = UUID("60000000-0000-4000-8000-000000000099")
    next_emitted_at = "2026-07-15T08:01:00Z"
    outbox = RunnerFactOutbox(tmp_path / "runner-fact-v1.sqlite3")
    batch_ids = iter((UUID(golden["batch_id"]), next_batch_id))
    emitted_at = iter((golden["emitted_at"], next_emitted_at))
    monkeypatch.setattr(runner_fact_module, "uuid4", lambda: next(batch_ids))
    monkeypatch.setattr(runner_fact_module, "_utc_now", lambda: next(emitted_at))

    identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), golden["key_id"])
    first_authority = _authority(golden)
    next_authority = replace(
        first_authority,
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000099"),
        deployment_spec_digest="d" * 64,
        generation=first_authority.generation + 1,
        capability_version_id=UUID("50000000-0000-4000-8000-000000000099"),
        capability_manifest_digest="f" * 64,
    )
    first_input = [
        {key: value for key, value in fact.items() if key != "seq"} for fact in golden["facts"]
    ]
    second_input = [
        {
            "kind": "heartbeat",
            "event_id": "80000000-0000-4000-8000-000000000099",
            "status": "online",
            "observed_at": next_emitted_at,
        }
    ]

    await outbox.enqueue(first_authority, identity, first_input)
    await outbox.enqueue(next_authority, identity, second_input)
    pending = await outbox.pending()
    after = json.loads(pending[1].payload)

    assert json.loads(pending[0].payload) == golden
    Draft202012Validator(schema).validate(after)
    assert pending[0].subject == pending[1].subject == first_authority.subject
    assert pending[0].stream_key == pending[1].stream_key == first_authority.stream_key
    assert golden["deployment_spec_id"] != after["deployment_spec_id"]
    assert golden["deployment_spec_digest"] != after["deployment_spec_digest"]
    assert golden["generation"] != after["generation"]
    assert after["source_seq_start"] == golden["source_seq_end"] + 1
    assert after["deployment_spec_id"] == str(next_authority.deployment_spec_id)
    assert after["generation"] == next_authority.generation


def test_parity_and_capability_matrices_are_closed_and_deny_unknown() -> None:
    manifest = _json(CAPABILITY_MANIFEST_PATH)
    parity = _json(PARITY_PATH)
    assert manifest["fact_kind_projectors"] == EXPECTED_KINDS
    assert {row["fact_kind"] for row in parity["rows"]} == set(EXPECTED_KINDS)
    assert parity["unknown_fact_kind"] == "terminal_unsupported_contract"
    assert parity["unsigned_telemetry_fallback"] is False
    assert parity["local_deny_reject_fact_kind"] == "RunnerRuntimeLogFact.v1"
    assert {binding.projector for binding in normalize_capability_scope_bindings(manifest)} == {
        "settlement",
        "risk",
        "health",
        "reconciliation",
        "deployment_lifecycle",
    }


@pytest.mark.asyncio
async def test_unknown_fact_kind_is_rejected_before_durable_enqueue(tmp_path: Path) -> None:
    golden = _json(GOLDEN_PATH)
    outbox = RunnerFactOutbox(tmp_path / "runner-fact-unknown.sqlite3")
    identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), golden["key_id"])
    with pytest.raises(RunnerFactContractError, match="unsupported runner fact kind"):
        await outbox.enqueue(
            _authority(golden),
            identity,
            [{"kind": "unknown_fact.v1", "event_id": "90000000-0000-4000-8000-000000000009"}],
        )


@pytest.mark.asyncio
async def test_python_float_is_rejected_recursively_before_durable_enqueue(
    tmp_path: Path,
) -> None:
    golden = _json(GOLDEN_PATH)
    schema = _json(SCHEMA_PATH)
    invalid = json.loads(json.dumps(golden))
    runtime_log = next(
        fact for fact in invalid["facts"] if fact["kind"] == "RunnerRuntimeLogFact.v1"
    )
    runtime_log["structured_fields"]["unsafe_cross_language_number"] = 0.1
    assert list(Draft202012Validator(schema).iter_errors(invalid))

    outbox = RunnerFactOutbox(tmp_path / "runner-fact-float.sqlite3")
    identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), golden["key_id"])
    with pytest.raises(RunnerFactContractError, match="must not contain Python float"):
        await outbox.enqueue(
            _authority(golden),
            identity,
            [
                {
                    "kind": "heartbeat",
                    "event_id": "90000000-0000-4000-8000-000000000010",
                    "status": "online",
                    "observed_at": "2026-07-15T00:00:00.000000Z",
                    "structured": {"nested": [{"unsafe": 0.1}]},
                }
            ],
        )
