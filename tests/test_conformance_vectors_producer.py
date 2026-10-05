"""The conformance vectors Custos publishes are what its production code writes.

Consumers execute these vectors against their own parsers. A ``producer-canonical``
vector is only worth that if the runner really writes those bytes, so each one is
re-rendered here from its ``producer_input`` through the runner's own fact paths:
the lifecycle and runtime-log facts read the clock, the event bridge derives the
lifecycle identity, and the fact builders render decimals. Every vector is also
checked against the published RunnerFact schema: accepted vectors validate, and
vectors refused while decoding do not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import runpy
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from custos_toolkit.contracts.strategy_execution import canonical_json_bytes
from jsonschema import Draft202012Validator

from custos.core import runner_deployment_lifecycle_fact as lifecycle_module
from custos.core import runtime_log_fact as runtime_log_module
from custos.core.runner_deployment_lifecycle_fact import RunnerDeploymentLifecycleFact
from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactContractError,
    equity_snapshot,
    execution_fill,
    position_closed,
    runner_fact_signing_preimage,
)
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactEventBridge
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor

ROOT = Path(__file__).resolve().parents[1]
CONFORMANCE = ROOT / "docs/authority/conformance"
RUNNER_FACT_VECTORS = CONFORMANCE / "runner-fact-batch-v1.vectors.json"
STRATEGY_VECTORS = CONFORMANCE / "strategy-canonical-json-v1.vectors.json"
SCHEMA = ROOT / "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json"
LIFECYCLE_KIND = "RunnerDeploymentLifecycleFact.v1"
RUNTIME_LOG_KIND = "RunnerRuntimeLogFact.v1"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


RUNNER_FACT_DOCUMENT = _json(RUNNER_FACT_VECTORS)
VECTORS = {vector["id"]: vector for vector in RUNNER_FACT_DOCUMENT["vectors"]}


def _vectors(predicate) -> list[dict[str, Any]]:
    return [vector for vector in VECTORS.values() if predicate(vector)]


def _ids(vectors: list[dict[str, Any]]) -> list[str]:
    return [vector["id"] for vector in vectors]


def _batch(vector: dict[str, Any]) -> dict[str, Any]:
    return json.loads(vector["raw"])


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


def _fact(batch: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(fact for fact in batch["facts"] if fact["kind"] == kind)


def test_vector_files_are_exactly_what_the_generator_writes() -> None:
    generator = runpy.run_path(
        str(ROOT / "scripts/generate_contract_conformance.py"), run_name="conformance_test"
    )
    for relative, payload in generator["build_assets"]().items():
        assert (ROOT / relative).read_bytes() == payload, relative


def test_vector_sets_name_their_contract_revision() -> None:
    schema = _json(SCHEMA)
    assert RUNNER_FACT_DOCUMENT["contract_id"] == schema["x-contract-id"]
    assert RUNNER_FACT_DOCUMENT["contract_revision"] == schema["x-contract-revision"]
    assert RUNNER_FACT_DOCUMENT["test_only"] is True
    for path in (RUNNER_FACT_VECTORS, STRATEGY_VECTORS):
        sidecar = path.with_name(path.name + ".sha256").read_text(encoding="ascii")
        assert sidecar == f"{_sha256(path.read_bytes())}  {path.name}\n"


@pytest.mark.parametrize("vector", _vectors(lambda v: True), ids=_ids(_vectors(lambda v: True)))
def test_every_vector_meets_the_published_schema_as_expected(vector: dict[str, Any]) -> None:
    validator = Draft202012Validator(_json(SCHEMA))
    for prior in vector.get("raw_prior", []):
        validator.validate(json.loads(prior))
    batch = _batch(vector)
    expected = vector["expected"]
    if expected.get("schema_outcome") == "accept":
        # A bound the schema states only approximately; the producer test below
        # shows the runner refuses the value before signing.
        assert expected["outcome"] == "reject"
        validator.validate(batch)
    elif expected["outcome"] == "reject" and expected["stage"] == "decode":
        assert list(validator.iter_errors(batch)), "schema accepts a vector refused at decode"
    else:
        validator.validate(batch)


ACCEPTED = _vectors(lambda v: v["expected"]["outcome"] == "accept")


@pytest.mark.parametrize("vector", ACCEPTED, ids=_ids(ACCEPTED))
def test_accepted_vectors_carry_the_runners_digests_and_a_valid_signature(
    vector: dict[str, Any],
) -> None:
    batch = _batch(vector)
    expected = vector["expected"]
    assert _canonical(batch).decode("utf-8") == vector["raw"]
    assert _sha256(_canonical(batch["facts"])) == expected["payload_digest"]
    assert batch["payload_digest"] == expected["payload_digest"]
    assert _sha256(_canonical(batch)) == expected["envelope_digest"]
    preimage = runner_fact_signing_preimage(batch)
    assert preimage.hex() == expected["signing_preimage_hex"]
    public_key = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(RUNNER_FACT_DOCUMENT["test_public_key_hex"])
    )
    signature = batch["signature"] + "=" * (-len(batch["signature"]) % 4)
    public_key.verify(base64.urlsafe_b64decode(signature), preimage)


def _clock(epoch_ns: int) -> ExitStack:
    stack = ExitStack()
    for module in (lifecycle_module, runtime_log_module):
        stack.enter_context(mock.patch.object(module.time, "time_ns", return_value=epoch_ns))
    return stack


UTC_CANONICAL = _vectors(
    lambda v: v["id"].startswith("utc-") and v["origin"] == "producer-canonical"
)


@pytest.mark.parametrize("vector", UTC_CANONICAL, ids=_ids(UTC_CANONICAL))
def test_signed_instants_are_rendered_by_the_runners_own_fact_paths(
    vector: dict[str, Any],
) -> None:
    batch = _batch(vector)
    authority = _authority(batch)
    lifecycle = _fact(batch, LIFECYCLE_KIND)
    runtime_log = _fact(batch, RUNTIME_LOG_KIND)
    with _clock(vector["producer_input"]["occurred_at_epoch_ns"]):
        observed = RunnerDeploymentLifecycleFact.observed(
            authority,
            generation=lifecycle["generation"],
            lifecycle_state=lifecycle["lifecycle_state"],
            command_fingerprint=lifecycle["command_fingerprint"],
            outcome=lifecycle["outcome"],
        ).to_wire()
        emitter = object.__new__(RunnerRuntimeLogEmitter)
        emitter._redactor = RuntimeLogRedactor()
        logged = emitter._fact(
            authority,
            level=runtime_log["level"],
            component=runtime_log["component"],
            message=runtime_log["message"],
            structured_fields=runtime_log["structured_fields"],
            correlation_id=runtime_log["correlation_id"],
            causation_id=runtime_log["causation_id"],
        )
    assert observed["occurred_at"] == lifecycle["occurred_at"]
    assert observed["observed_at"] == lifecycle["observed_at"]
    assert logged["occurred_at"] == runtime_log["occurred_at"]
    assert batch["emitted_at"] == lifecycle["occurred_at"]


class _Capture:
    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []

    def emit_sync(self, _authority, facts):
        self.facts.extend(facts)
        return tuple(facts)


class PositionClosed:
    def __init__(self, fields: dict[str, Any]) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


def test_a_netting_slot_reopened_by_another_order_closes_as_two_lifecycles() -> None:
    vector = VECTORS["netting-reopen-two-lifecycles"]
    batch = _batch(vector)
    authority = _authority(batch)
    capture = _Capture()
    bridge = RunnerFactEventBridge(
        emitter=capture,
        deployment=RunnerFactDeployment(
            authority=authority,
            deployment_instance_id=str(authority.deployment_instance_id),
            deployment_spec_id=str(authority.deployment_spec_id),
            deployment_spec_digest=authority.deployment_spec_digest,
            venue="BINANCE",
            currency="USDT",
            reconciliation_available=True,
            strategy_version="v1",
            timeframe="1-MINUTE",
        ),
    )
    for event in vector["producer_input"]["position_closed_events"]:
        bridge._on_position_event(PositionClosed(event))
    signed = [fact["position_id"] for fact in batch["facts"]]
    assert [fact["position_id"] for fact in capture.facts] == signed
    assert len(set(signed)) == 2


def test_a_decimal_held_in_exponent_form_is_written_in_plain_notation() -> None:
    vector = VECTORS["decimal-from-scientific-input"]
    signed = _batch(vector)["facts"][0]
    rendered = position_closed(
        event_id=UUID(signed["event_id"]),
        position_id=UUID(signed["position_id"]),
        realized_pnl=Decimal(vector["producer_input"]["realized_pnl"]),
        currency=signed["currency"],
        opened_at=signed["opened_at"],
        closed_at=signed["closed_at"],
    )
    assert rendered["realized_pnl"] == signed["realized_pnl"] == "0.00000001"


def test_a_required_nullable_member_enters_the_digest_as_null() -> None:
    fact = _batch(VECTORS["optional-null"])["facts"][0]
    assert "client_order_id" in fact and fact["client_order_id"] is None
    omitted = {key: value for key, value in fact.items() if key != "client_order_id"}
    assert _sha256(_canonical([fact])) != _sha256(_canonical([omitted]))


IDENTIFIER_VECTORS = _vectors(lambda v: v["id"].startswith("identifier-"))


def _rebuild_fill(fact: dict[str, Any], venue_trade_id: str) -> dict[str, Any]:
    return execution_fill(
        venue=fact["venue"],
        venue_trade_id=venue_trade_id,
        venue_order_id=fact["venue_order_id"],
        instrument=fact["instrument"],
        side=fact["side"],
        quantity=fact["quantity"],
        price=fact["price"],
        fee=fact["fee"],
        currency=fact["currency"],
        occurred_at=fact["occurred_at"],
        client_order_id=fact["client_order_id"],
        event_id=UUID(fact["event_id"]),
    )


@pytest.mark.parametrize("vector", IDENTIFIER_VECTORS, ids=_ids(IDENTIFIER_VECTORS))
def test_the_runner_signs_exactly_the_identifiers_the_consumer_accepts(
    vector: dict[str, Any],
) -> None:
    """The same identifier inputs the consumer decodes, put through the runner's
    fact builder: what the consumer accepts is signed byte for byte, what it
    refuses is refused before signing."""

    signed = _batch(vector)["facts"][0]
    value = signed["venue_trade_id"]
    if vector["expected"]["outcome"] == "accept":
        assert vector["producer_input"]["venue_trade_id"] == value
        rendered = _rebuild_fill(signed, value)
        assert rendered == {key: item for key, item in signed.items() if key != "seq"}
    else:
        with pytest.raises(RunnerFactContractError, match="venue_trade_id"):
            _rebuild_fill(signed, value)


RANGE_VECTOR_IDS = {
    "decimal-largest-mantissa",
    "decimal-scale-28",
    "decimal-mantissa-overflow",
    "decimal-thirty-digits",
}
RANGE_VECTORS = _vectors(lambda v: v["id"] in RANGE_VECTOR_IDS)


@pytest.mark.parametrize("vector", RANGE_VECTORS, ids=_ids(RANGE_VECTORS))
def test_the_runner_signs_exactly_the_decimals_the_consumer_can_hold(
    vector: dict[str, Any],
) -> None:
    signed = _batch(vector)["facts"][0]
    assert signed["kind"] == "equity_snapshot"

    def rebuild() -> dict[str, Any]:
        return equity_snapshot(
            event_id=UUID(signed["event_id"]),
            amount=signed["amount"],
            currency=signed["currency"],
            observed_at=signed["observed_at"],
        )

    if vector["expected"]["outcome"] == "accept":
        assert vector["producer_input"]["equity_amount"] == signed["amount"]
        assert rebuild()["amount"] == signed["amount"]
    else:
        with pytest.raises(RunnerFactContractError, match="representable decimal range"):
            rebuild()


STRATEGY_DOCUMENT = _json(STRATEGY_VECTORS)


def _tagged(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$float"}:
            return float(value["$float"])
        return {key: _tagged(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_tagged(item) for item in value]
    return value


@pytest.mark.parametrize(
    "vector", STRATEGY_DOCUMENT["vectors"], ids=[v["id"] for v in STRATEGY_DOCUMENT["vectors"]]
)
def test_strategy_canonical_json_vectors_match_the_toolkit_encoder(vector: dict[str, Any]) -> None:
    expected = vector["expected"]
    if expected["outcome"] == "reject":
        with pytest.raises(TypeError):
            canonical_json_bytes(_tagged(vector["input"]))
        return
    encoded = canonical_json_bytes(_tagged(vector["input"]))
    assert encoded.decode("utf-8") == expected["canonical_utf8"]
    assert _sha256(encoded) == expected["sha256"]
