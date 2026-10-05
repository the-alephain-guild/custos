"""Canonical first-production RunnerFact V1 contract."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import sys
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
TERMINAL_BOUNDARY_PATH = ROOT / "docs/authority/runner-fact-terminal-boundary-golden-v1.json"
TERMINAL_KIND = "RunnerInstanceTerminalValuationFact.v1"

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
    "RunnerValuationCheckpointFact.v1": "reconciliation",
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
    "RunnerInstanceTerminalValuationFact.v1": "settlement",
}
# The terminal valuation fact must be the last fact of a stop batch, so the
# single-batch golden cannot carry it; the stop-boundary asset does.
SINGLE_BATCH_GOLDEN_KINDS = set(EXPECTED_KINDS) - {TERMINAL_KIND}


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
            TERMINAL_BOUNDARY_PATH,
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

    assert receipt["status"] == "PHASE_A_CONSUMER_ACCEPTED_RUNTIME_OPEN"
    assert receipt["producer_commit"] == "199bb6475eae87b78d2e1db27eff319a5a3ebe6b"
    crucible_payload = CRUCIBLE_CONSUMER_RECEIPT_PATH.read_bytes()
    assert receipt["consumer_receipts"] == {
        "crucible_rust": {
            "repository": "tesseract-trading/crucible-rust",
            "commit": "61733e5f1f2387ca58a728c9b2f26c8a01f92c8d",
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
        crucible_receipt["producer"]["asset_commit"] == "199bb6475eae87b78d2e1db27eff319a5a3ebe6b"
    )
    assert "producer_receipt" not in crucible_receipt["producer"]
    assert receipt["asset_index"] == {
        "path": "docs/authority/runner-fact-contract-assets-v1.json",
        "sha256": crucible_receipt["producer"]["asset_index"]["sha256"],
        "size_bytes": crucible_receipt["producer"]["asset_index"]["size_bytes"],
    }


def _generator() -> Any:
    name = "generate_runner_fact_contract"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_generator_reproduces_every_asset_and_keeps_the_historical_receipt() -> None:
    generator = _generator()
    expected = generator.build_assets()
    drift = [
        str(path) for path, payload in expected.items() if (ROOT / path).read_bytes() != payload
    ]
    assert drift == []
    # The recorded producer acceptance is copied, never regenerated.
    assert expected[generator.RECEIPT_PATH] == RECEIPT_PATH.read_bytes()


def test_capability_manifest_declares_one_complete_runtime() -> None:
    manifest = _json(CAPABILITY_MANIFEST_PATH)
    runtime = manifest["runtime"]
    assert set(runtime) == {
        "distribution",
        "image_digest",
        "source_revision",
        "engine",
        "engine_version",
    }
    assert runtime["distribution"] == "oci_image"
    assert runtime["engine"] == "nautilus"
    assert _json(CAPABILITY_RECEIPT_PATH)["capability_manifest"] == manifest
    batch = _json(GOLDEN_PATH)
    assert batch["capability_manifest_digest"] == hashlib.sha256(_canonical(manifest)).hexdigest()
    for terminal_batch in json.loads(TERMINAL_BOUNDARY_PATH.read_text(encoding="utf-8")):
        assert terminal_batch["capability_manifest_digest"] == batch["capability_manifest_digest"]


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
    assert {fact["kind"] for fact in facts} == SINGLE_BATCH_GOLDEN_KINDS
    assert _authority(batch).subject == index["golden_subject"]
    subject_template = schema["x-custos-invariants"]["subject"]
    assert subject_template.format(**batch) == _authority(batch).subject

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


# --- RunnerInstanceTerminalValuationFact.v1 -------------------------------------------

TERMINAL_FIELDS = {
    "kind",
    "event_id",
    "tenant_id",
    "mode",
    "runner_id",
    "deployment_instance_id",
    "deployment_spec_id",
    "deployment_spec_digest",
    "generation",
    "issuer_key_id",
    "command_fingerprint",
    "closes_generation",
    "outcome",
    "reason_code",
    "account_scope",
    "valuation_source",
    "position_policy",
    "period",
    "stop_requested_at",
    "valuation_observed_at",
    "marks_oldest_at",
    "stop_effective_at",
    "equity",
    "open_positions",
    "prior_equity",
    "valuation_digest",
}
TERMINAL_REASON_PRIORITY = (
    "no_prior_running_generation",
    "engine_not_running_at_stop",
    "process_exit_before_confirmation",
    "stop_timeout",
    "engine_task_failed",
    "capture_persist_failed",
    "valuation_unavailable",
    "no_prior_signed_equity",
    "valuation_unreliable",
    "state_changed_after_valuation",
    "valuation_gap_exceeded",
    "valuation_out_of_contract",
)
STOP_FINGERPRINT = "d" * 64
POSITION = {"instrument": "BTC-USDT", "quantity": "0.01", "mark_price": "61000", "currency": "USDT"}


def _terminal_kwargs(**overrides: Any) -> dict[str, Any]:
    golden = _json(GOLDEN_PATH)
    values: dict[str, Any] = {
        "authority": replace(_authority(golden), generation=golden["generation"] + 1),
        "issuer_key_id": golden["key_id"],
        "command_fingerprint": STOP_FINGERPRINT,
        "closes_generation": golden["generation"],
        "outcome": "confirmed",
        "reason_code": None,
        "account_scope": {
            "venue": "BINANCE",
            "credential_scope_id": "91000000-0000-4000-8000-000000000011",
            "credential_scope_digest": "a" * 64,
            "sub_account": None,
            "wallet_type": "spot",
        },
        "valuation_source": "simulated_account",
        "position_policy": "flatten",
        "stop_requested_at": "2026-08-14T10:05:00Z",
        "valuation_observed_at": "2026-08-14T10:05:00.500Z",
        "marks_oldest_at": None,
        "stop_effective_at": "2026-08-14T10:05:00.800Z",
        "equity": {"amount": "10060", "currency": "USDT"},
        "open_positions": [],
        "prior_equity": {
            "event_id": "81000000-0000-4000-8000-000000000001",
            "seq": 20,
            "amount": "10050",
            "currency": "USDT",
            "observed_at": "2026-08-14T09:59:30Z",
        },
    }
    values.update(overrides)
    return values


def _unconfirmed_kwargs(reason: str = "stop_timeout", **overrides: Any) -> dict[str, Any]:
    return _terminal_kwargs(
        outcome="valuation_unconfirmed",
        reason_code=reason,
        valuation_observed_at=None,
        marks_oldest_at=None,
        equity=None,
        open_positions=None,
        **overrides,
    )


def _digest_body(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fact.items()
        if key not in {"kind", "event_id", "seq", "valuation_digest"}
    }


def _boundary() -> list[dict[str, Any]]:
    value = json.loads(TERMINAL_BOUNDARY_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def _ts(value: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_terminal_valuation_is_a_settlement_kind_with_its_own_capability_flag() -> None:
    assert runner_fact_module.RUNNER_FACT_KIND_PROJECTORS[TERMINAL_KIND] == "settlement"
    contracts = runner_fact_module.RUNNER_FACT_PROJECTOR_CONTRACTS
    assert contracts["settlement"]["terminal_valuation"] == "v1"
    assert runner_fact_module.MAX_TERMINAL_VALUATION_GAP_SECONDS == 60


def test_closed_union_holds_across_the_golden_and_the_terminal_boundary_asset() -> None:
    index = _json(INDEX_PATH)
    golden_kinds = {fact["kind"] for fact in _json(GOLDEN_PATH)["facts"]}
    boundary_kinds = {fact["kind"] for batch in _boundary() for fact in batch["facts"]}
    assert TERMINAL_KIND not in golden_kinds
    assert golden_kinds | boundary_kinds == set(EXPECTED_KINDS)
    assert index["closed_fact_union_assets"] == [
        str(GOLDEN_PATH.relative_to(ROOT)),
        str(TERMINAL_BOUNDARY_PATH.relative_to(ROOT)),
    ]


def test_terminal_boundary_asset_is_one_signed_stop_boundary_on_the_golden_stream() -> None:
    index = _json(INDEX_PATH)
    schema = _json(SCHEMA_PATH)
    golden = _json(GOLDEN_PATH)
    run_batch, stop_batch = _boundary()
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(index["synthetic_signature"]["public_key_base64"])
    )
    for batch in (run_batch, stop_batch):
        Draft202012Validator(schema).validate(batch)
        public_key.verify(
            _unpadded_urlsafe(batch["signature"]),
            runner_fact_module.runner_fact_signing_preimage(batch),
        )
        for field in (
            "tenant_id",
            "trading_mode",
            "runner_id",
            "deployment_instance_id",
            "deployment_spec_id",
            "deployment_spec_digest",
            "strategy_id",
            "capability_version_id",
            "capability_manifest_digest",
            "key_id",
        ):
            assert batch[field] == golden[field]
        assert [fact["seq"] for fact in batch["facts"]] == list(
            range(batch["source_seq_start"], batch["source_seq_end"] + 1)
        )
    # One stream: the boundary continues the golden's sequence without a gap.
    assert run_batch["source_seq_start"] == golden["source_seq_end"] + 1
    assert stop_batch["source_seq_start"] == run_batch["source_seq_end"] + 1
    assert run_batch["generation"] == golden["generation"]
    assert stop_batch["generation"] == run_batch["generation"] + 1

    lifecycle, terminal = stop_batch["facts"]
    equities = [fact for fact in run_batch["facts"] if fact["kind"] == "equity_snapshot"]
    assert equities
    last_equity = equities[-1]
    assert lifecycle["kind"] == "RunnerDeploymentLifecycleFact.v1"
    assert (lifecycle["lifecycle_state"], lifecycle["outcome"]) == ("stopped", "applied")
    assert lifecycle["generation"] == stop_batch["generation"]
    assert terminal["kind"] == TERMINAL_KIND
    assert set(terminal) == TERMINAL_FIELDS | {"seq"}
    assert terminal["seq"] == stop_batch["source_seq_end"]
    assert terminal["command_fingerprint"] == lifecycle["command_fingerprint"]
    assert terminal["outcome"] == "confirmed"
    assert terminal["reason_code"] is None
    # Body identity repeats the signed header exactly.
    assert terminal["tenant_id"] == stop_batch["tenant_id"]
    assert terminal["mode"] == stop_batch["trading_mode"]
    for field in (
        "runner_id",
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
    ):
        assert terminal[field] == stop_batch[field]
    assert terminal["issuer_key_id"] == stop_batch["key_id"]
    assert terminal["event_id"] == str(
        runner_fact_module.runner_fact_event_id(
            _authority(stop_batch).stream_key,
            "terminal_valuation",
            stop_batch["generation"],
            terminal["command_fingerprint"],
        )
    )
    # The stop closes the running generation, whose last equity it cites.
    assert terminal["closes_generation"] == run_batch["generation"]
    assert terminal["prior_equity"] == {
        "event_id": last_equity["event_id"],
        "seq": last_equity["seq"],
        "amount": last_equity["amount"],
        "currency": last_equity["currency"],
        "observed_at": last_equity["observed_at"],
    }
    assert (
        terminal["valuation_digest"]
        == hashlib.sha256(_canonical(_digest_body(terminal))).hexdigest()
    )
    # Every time inequality the consumer enforces holds on the shared vector.
    requested = _ts(terminal["stop_requested_at"])
    observed = _ts(terminal["valuation_observed_at"])
    effective = _ts(terminal["stop_effective_at"])
    assert _ts(last_equity["observed_at"]) <= observed
    assert requested <= observed <= effective
    assert (effective - observed).total_seconds() <= 60
    assert effective <= _ts(lifecycle["observed_at"]) <= _ts(stop_batch["emitted_at"])
    assert terminal["period"] == effective.strftime("%Y-%m")
    assert terminal["position_policy"] == "flatten"
    assert terminal["open_positions"] == []
    assert terminal["marks_oldest_at"] is None
    assert terminal["valuation_source"] == "simulated_account"
    assert stop_batch["trading_mode"] == "sandbox"


def test_terminal_valuation_fields_are_fixed_and_depend_on_outcome() -> None:
    confirmed = runner_fact_module.terminal_valuation(**_terminal_kwargs())
    assert set(confirmed) == TERMINAL_FIELDS
    assert confirmed["period"] == "2026-08"
    assert confirmed["valuation_digest"] == (
        hashlib.sha256(_canonical(_digest_body(confirmed))).hexdigest()
    )
    preserved = runner_fact_module.terminal_valuation(
        **_terminal_kwargs(
            position_policy="preserve",
            open_positions=[POSITION],
            marks_oldest_at="2026-08-14T10:04:59Z",
        )
    )
    assert preserved["open_positions"] == [POSITION]

    unconfirmed = runner_fact_module.terminal_valuation(**_unconfirmed_kwargs())
    assert set(unconfirmed) == TERMINAL_FIELDS
    for field in ("period", "valuation_observed_at", "marks_oldest_at", "equity"):
        assert unconfirmed[field] is None
    assert unconfirmed["open_positions"] is None
    assert unconfirmed["reason_code"] == "stop_timeout"

    # A stop that never had a running generation still seals the stream.
    never_ran = runner_fact_module.terminal_valuation(
        **_unconfirmed_kwargs(
            "no_prior_running_generation", closes_generation=None, prior_equity=None
        )
    )
    assert never_ran["closes_generation"] is None
    exited = runner_fact_module.terminal_valuation(
        **_unconfirmed_kwargs("process_exit_before_confirmation", stop_effective_at=None)
    )
    assert exited["stop_effective_at"] is None
    # Negative equity is a settlement decision, not a wire error.
    negative = runner_fact_module.terminal_valuation(
        **_terminal_kwargs(equity={"amount": "-5", "currency": "USDT"})
    )
    assert negative["equity"]["amount"] == "-5"
    # The cited snapshot keeps its own wire rule, which bounds equity snapshots to
    # the consumer's decimal type: 28 significant digits pass, 30 fractional
    # digits are refused.
    fine = "10050." + "1" * 23
    cited = runner_fact_module.terminal_valuation(
        **_terminal_kwargs(prior_equity={**_terminal_kwargs()["prior_equity"], "amount": fine})
    )
    assert cited["prior_equity"]["amount"] == fine
    with pytest.raises(RunnerFactContractError, match="prior_equity.amount"):
        runner_fact_module.terminal_valuation(
            **_terminal_kwargs(
                prior_equity={**_terminal_kwargs()["prior_equity"], "amount": "10050." + "1" * 30}
            )
        )


def test_terminal_valuation_identity_is_independent_of_content() -> None:
    confirmed = runner_fact_module.terminal_valuation(**_terminal_kwargs())
    unconfirmed = runner_fact_module.terminal_valuation(**_unconfirmed_kwargs())
    assert confirmed["event_id"] == unconfirmed["event_id"]
    assert confirmed["valuation_digest"] != unconfirmed["valuation_digest"]
    assert confirmed["event_id"] == str(
        runner_fact_module.terminal_valuation_event_id(
            _terminal_kwargs()["authority"], STOP_FINGERPRINT
        )
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"reason_code": "stop_timeout"}, "reason_code"),
        ({"outcome": "stop_unconfirmed"}, "outcome"),
        ({"equity": None}, "equity"),
        ({"open_positions": None}, "open_positions"),
        ({"valuation_observed_at": None}, "valuation_observed_at"),
        ({"stop_effective_at": None}, "stop_effective_at"),
        ({"closes_generation": None}, "closes_generation"),
        ({"closes_generation": 8}, "closes_generation"),
        ({"closes_generation": 0}, "closes_generation"),
        ({"prior_equity": None}, "prior_equity"),
        (
            {
                "prior_equity": {
                    "event_id": "81000000-0000-4000-8000-000000000001",
                    "seq": 20,
                    "amount": "10050",
                    "currency": "USDT",
                    "observed_at": "2026-08-14T10:05:00.501Z",
                }
            },
            "prior_equity",
        ),
        ({"open_positions": [POSITION]}, "flatten"),
        (
            {"position_policy": "preserve", "open_positions": [POSITION]},
            "marks_oldest_at",
        ),
        ({"marks_oldest_at": "2026-08-14T10:03:59.500Z"}, "marks_oldest_at"),
        ({"marks_oldest_at": "2026-08-14T10:05:00.501Z"}, "marks_oldest_at"),
        ({"stop_effective_at": "2026-08-14T10:05:00.400Z"}, "stop_effective_at"),
        ({"stop_requested_at": "2026-08-14T10:05:00.600Z"}, "stop_requested_at"),
        ({"stop_effective_at": "2026-08-14T10:06:00.501Z"}, "stop_effective_at"),
        ({"equity": {"amount": 10060.0, "currency": "USDT"}}, "float"),
        ({"equity": {"amount": "1e-29", "currency": "USDT"}}, "decimal range"),
        ({"equity": {"amount": "10060", "currency": "DOGE"}}, "currency"),
        ({"equity": {"amount": "10060", "currency": "USDT", "extra": "1"}}, "equity"),
        ({"valuation_source": "venue_ledger"}, "valuation_source"),
        ({"position_policy": "close"}, "position_policy"),
        ({"authority": None}, "authority"),
        ({"command_fingerprint": "D" * 64}, "command_fingerprint"),
        ({"issuer_key_id": " "}, "issuer_key_id"),
        (
            {
                "account_scope": {
                    "venue": "BINANCE",
                    "credential_scope_id": "91000000-0000-4000-8000-000000000011",
                    "credential_scope_digest": "z" * 64,
                    "sub_account": None,
                    "wallet_type": "spot",
                }
            },
            "credential_scope_digest",
        ),
        (
            {
                "account_scope": {
                    "venue": "BINANCE",
                    "credential_scope_id": "91000000-0000-4000-8000-000000000011",
                    "credential_scope_digest": "a" * 64,
                    "sub_account": None,
                }
            },
            "account_scope",
        ),
    ],
)
def test_confirmed_terminal_valuation_rejects_bad_fields(
    overrides: dict[str, Any], match: str
) -> None:
    with pytest.raises(RunnerFactContractError, match=match):
        runner_fact_module.terminal_valuation(**_terminal_kwargs(**overrides))


def test_simulated_valuation_is_refused_outside_sandbox() -> None:
    authority = _terminal_kwargs()["authority"]
    with pytest.raises(RunnerFactContractError, match="simulated_account"):
        runner_fact_module.terminal_valuation(
            **_terminal_kwargs(authority=replace(authority, trading_mode="testnet"))
        )
    testnet = runner_fact_module.terminal_valuation(
        **_terminal_kwargs(
            authority=replace(authority, trading_mode="testnet"),
            valuation_source="venue_account",
        )
    )
    assert testnet["mode"] == "testnet"


@pytest.mark.parametrize(
    ("reason", "overrides", "match"),
    [
        (None, {}, "reason_code"),
        ("valuation_late", {}, "reason_code"),
        ("stop_timeout", {"equity": {"amount": "1", "currency": "USDT"}}, "equity"),
        ("stop_timeout", {"open_positions": []}, "open_positions"),
        ("stop_timeout", {"valuation_observed_at": "2026-08-14T10:05:00Z"}, "valuation_observed"),
        ("stop_timeout", {"marks_oldest_at": "2026-08-14T10:05:00Z"}, "marks_oldest_at"),
        ("no_prior_running_generation", {}, "closes_generation"),
        ("no_prior_signed_equity", {}, "prior_equity"),
        ("stop_timeout", {"stop_effective_at": "2026-08-14T10:04:59Z"}, "stop_effective_at"),
    ],
)
def test_unconfirmed_terminal_valuation_rejects_bad_fields(
    reason: str | None, overrides: dict[str, Any], match: str
) -> None:
    values = _unconfirmed_kwargs("stop_timeout")
    values["reason_code"] = reason
    values.update(overrides)
    with pytest.raises(RunnerFactContractError, match=match):
        runner_fact_module.terminal_valuation(**values)


def test_terminal_reason_codes_resolve_overlaps_by_one_fixed_priority() -> None:
    choose = runner_fact_module.terminal_valuation_reason_code
    assert runner_fact_module.TERMINAL_VALUATION_REASON_CODES == TERMINAL_REASON_PRIORITY
    assert choose({"valuation_out_of_contract", "valuation_gap_exceeded"}) == (
        "valuation_gap_exceeded"
    )
    assert choose({"no_prior_signed_equity", "valuation_unavailable"}) == "valuation_unavailable"
    assert choose(set(TERMINAL_REASON_PRIORITY)) == "no_prior_running_generation"
    with pytest.raises(RunnerFactContractError, match="reason"):
        choose(set())
    with pytest.raises(RunnerFactContractError, match="reason"):
        choose({"stop_timeout", "valuation_late"})


def _boundary_terminal() -> dict[str, Any]:
    return copy.deepcopy(_boundary()[1]["facts"][1])


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_key",
        "missing_key",
        "confirmed_without_equity",
        "unconfirmed_with_equity",
        "float_amount",
        "stop_unconfirmed",
        "flatten_with_positions",
        "positions_without_mark_watermark",
        "no_running_generation_with_closes_generation",
    ],
)
def test_schema_rejects_terminal_valuation_outside_its_field_rules(mutation: str) -> None:
    schema = _json(SCHEMA_PATH)
    batch = copy.deepcopy(_boundary()[1])
    fact = batch["facts"][1]
    if mutation == "extra_key":
        fact["evidence_level"] = "self_attested"
    elif mutation == "missing_key":
        del fact["marks_oldest_at"]
    elif mutation == "confirmed_without_equity":
        fact["equity"] = None
    elif mutation == "unconfirmed_with_equity":
        fact.update(outcome="valuation_unconfirmed", reason_code="stop_timeout", period=None)
        fact.update(valuation_observed_at=None, open_positions=None)
    elif mutation == "float_amount":
        fact["equity"]["amount"] = 10060.5
    elif mutation == "stop_unconfirmed":
        fact["outcome"] = "stop_unconfirmed"
    elif mutation == "flatten_with_positions":
        fact.update(open_positions=[POSITION], marks_oldest_at=fact["valuation_observed_at"])
    elif mutation == "positions_without_mark_watermark":
        fact.update(position_policy="preserve", open_positions=[POSITION])
    else:
        fact.update(outcome="valuation_unconfirmed", reason_code="no_prior_running_generation")
        fact.update(period=None, valuation_observed_at=None, equity=None, open_positions=None)
    assert list(Draft202012Validator(schema).iter_errors(batch))


def test_schema_accepts_every_terminal_outcome_shape() -> None:
    schema = _json(SCHEMA_PATH)
    batch = copy.deepcopy(_boundary()[1])
    for kwargs in (
        _terminal_kwargs(
            position_policy="preserve",
            open_positions=[POSITION],
            marks_oldest_at="2026-08-14T10:04:59Z",
        ),
        _unconfirmed_kwargs(),
        _unconfirmed_kwargs("no_prior_running_generation", closes_generation=None),
        _unconfirmed_kwargs("process_exit_before_confirmation", stop_effective_at=None),
    ):
        fact = runner_fact_module.terminal_valuation(**kwargs)
        batch["facts"][1] = {**fact, "seq": batch["source_seq_end"]}
        Draft202012Validator(schema).validate(batch)


def test_same_batch_with_different_terminal_bytes_fails_verification() -> None:
    index = _json(INDEX_PATH)
    _, stop_batch = _boundary()
    tampered = copy.deepcopy(stop_batch)
    fact = tampered["facts"][1]
    fact["equity"]["amount"] = "10061"
    fact["valuation_digest"] = hashlib.sha256(_canonical(_digest_body(fact))).hexdigest()
    with pytest.raises(RunnerFactContractError, match="payload digest"):
        runner_fact_module.runner_fact_signing_header(tampered)
    tampered["payload_digest"] = hashlib.sha256(_canonical(tampered["facts"])).hexdigest()
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(index["synthetic_signature"]["public_key_base64"])
    )
    from cryptography.exceptions import InvalidSignature

    with pytest.raises(InvalidSignature):
        public_key.verify(
            _unpadded_urlsafe(tampered["signature"]),
            runner_fact_module.runner_fact_signing_preimage(tampered),
        )


def _stop_authority() -> RunnerFactAuthority:
    return _authority(_boundary()[1])


def _stop_input() -> list[dict[str, Any]]:
    return [
        {key: value for key, value in fact.items() if key != "seq"}
        for fact in _boundary()[1]["facts"]
    ]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("terminal_not_last", "last fact"),
        ("lifecycle_retry_exhausted", "stopped and applied"),
        ("lifecycle_paused", "stopped and applied"),
        ("fingerprint", "command_fingerprint"),
        ("no_lifecycle", "stopped and applied"),
        ("body_generation", "generation"),
        ("body_runner", "runner_id"),
        ("issuer", "issuer_key_id"),
        ("digest", "valuation_digest"),
        ("event_id", "event_id"),
        ("two_terminals", "last fact"),
    ],
)
@pytest.mark.asyncio
async def test_outbox_refuses_terminal_valuation_the_consumer_would_refuse(
    tmp_path: Path, mutation: str, match: str
) -> None:
    golden = _json(GOLDEN_PATH)
    identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), golden["key_id"])
    facts = _stop_input()
    lifecycle, terminal = facts
    if mutation == "terminal_not_last":
        facts = [
            lifecycle,
            terminal,
            {
                "kind": "heartbeat",
                "event_id": "90000000-0000-4000-8000-000000000011",
                "status": "online",
                "observed_at": "2026-08-14T10:05:01Z",
            },
        ]
    elif mutation == "lifecycle_retry_exhausted":
        lifecycle["outcome"] = "retry_exhausted"
    elif mutation == "lifecycle_paused":
        lifecycle["lifecycle_state"] = "paused"
    elif mutation == "fingerprint":
        lifecycle["command_fingerprint"] = "c" * 64
    elif mutation == "no_lifecycle":
        facts = [terminal]
    elif mutation == "body_generation":
        terminal["generation"] = 9
    elif mutation == "body_runner":
        terminal["runner_id"] = "10000000-0000-4000-8000-000000000099"
    elif mutation == "issuer":
        terminal["issuer_key_id"] = "ed25519-other"
    elif mutation == "digest":
        terminal["valuation_digest"] = "0" * 64
    elif mutation == "event_id":
        terminal["event_id"] = "90000000-0000-4000-8000-000000000012"
    else:
        facts = [
            lifecycle,
            terminal,
            {**terminal, "event_id": "90000000-0000-4000-8000-000000000013"},
        ]
    outbox = RunnerFactOutbox(tmp_path / f"terminal-{mutation}.sqlite3")
    with pytest.raises(RunnerFactContractError, match=match):
        await outbox.enqueue(_stop_authority(), identity, facts)
    assert await outbox.pending() == []


@pytest.mark.asyncio
async def test_production_outbox_reproduces_the_terminal_boundary_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _json(GOLDEN_PATH)
    run_batch, stop_batch = _boundary()
    batches = (golden, run_batch, stop_batch)
    outbox = RunnerFactOutbox(tmp_path / "terminal-boundary.sqlite3")
    batch_ids = iter(UUID(batch["batch_id"]) for batch in batches)
    emitted_at = iter(batch["emitted_at"] for batch in batches)
    monkeypatch.setattr(runner_fact_module, "uuid4", lambda: next(batch_ids))
    monkeypatch.setattr(runner_fact_module, "_utc_now", lambda: next(emitted_at))
    identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), golden["key_id"])
    for batch in batches:
        facts = [
            {key: value for key, value in fact.items() if key != "seq"} for fact in batch["facts"]
        ]
        await outbox.enqueue(_authority(batch), identity, facts)
    pending = await outbox.pending()
    assert [json.loads(item.payload) for item in pending] == list(batches)


def _receipt_with(tmp_path: Path, name: str, edit: Any) -> Path:
    document = copy.deepcopy(_json(CAPABILITY_RECEIPT_PATH))
    manifest = document["capability_manifest"]
    edit(manifest)
    document["manifest_digest"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    path = tmp_path / f"capability-{name}.json"
    path.write_bytes(json.dumps(document).encode("utf-8"))
    return path


def test_capability_loader_treats_terminal_valuation_as_one_optional_flag(
    tmp_path: Path,
) -> None:
    def without_terminal(manifest: dict[str, Any]) -> None:
        manifest["fact_kind_projectors"].pop(TERMINAL_KIND)
        manifest["runner_fact_contracts"]["settlement"].pop("terminal_valuation")

    def legacy(manifest: dict[str, Any]) -> None:
        without_terminal(manifest)
        manifest["fact_kind_projectors"].pop("RunnerValuationCheckpointFact.v1")
        manifest["runner_fact_contracts"]["reconciliation"].pop("valuation_checkpoint")

    def kind_without_flag(manifest: dict[str, Any]) -> None:
        manifest["runner_fact_contracts"]["settlement"].pop("terminal_valuation")

    def flag_without_kind(manifest: dict[str, Any]) -> None:
        manifest["fact_kind_projectors"].pop(TERMINAL_KIND)

    current = RunnerCapabilityReceipt.load(CAPABILITY_RECEIPT_PATH)
    assert (
        current.capability_manifest["runner_fact_contracts"]["settlement"]["terminal_valuation"]
        == "v1"
    )
    for name, edit in (("without-terminal", without_terminal), ("legacy", legacy)):
        RunnerCapabilityReceipt.load(_receipt_with(tmp_path, name, edit))
    for name, edit in (
        ("kind-without-flag", kind_without_flag),
        ("flag-without-kind", flag_without_kind),
    ):
        with pytest.raises(RunnerFactContractError, match="closed fact projector contract"):
            RunnerCapabilityReceipt.load(_receipt_with(tmp_path, name, edit))
