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
    fill_correction_fact,
    order_event_fact,
    position_closed,
    runner_fact_signing_preimage,
)
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactEventBridge
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
from tests import _lineage_frozen_tables as frozen

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
        # A bound the schema states only approximately, or a rule across fields it
        # does not state; the producer test below shows the runner refuses the
        # value before signing.
        assert expected["outcome"] == "reject" and expected["stage"] == "decode"
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


# Order lineage and fill correction.

ORDER_EVENT_KIND = "RunnerOrderEventFact.v1"
FILL_CORRECTION_KIND = "RunnerFillCorrectionFact.v1"
SEQUENCES = _json(CONFORMANCE / "runner-fact-batch-v1.sequences.json")
_DERIVED = {
    "kind",
    "event_id",
    "seq",
    "base_quantity",
    "requested_base_quantity",
    "voided_base_quantity",
    "leaves_base_quantity_after",
}


def _rebuild(fact: dict[str, Any], authority: RunnerFactAuthority) -> dict[str, Any]:
    """The fact the runner's builder writes from the same inputs."""

    inputs = {name: value for name, value in fact.items() if name not in _DERIVED}
    if fact["kind"] == ORDER_EVENT_KIND:
        return order_event_fact(authority, **inputs)
    return fill_correction_fact(authority, **inputs)


def _lineage_facts(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        fact for fact in batch["facts"] if fact["kind"] in {ORDER_EVENT_KIND, FILL_CORRECTION_KIND}
    ]


LINEAGE_ACCEPTED = _vectors(
    lambda v: v["expected"]["outcome"] == "accept" and _lineage_facts(_batch(v))
)


@pytest.mark.parametrize("vector", LINEAGE_ACCEPTED, ids=_ids(LINEAGE_ACCEPTED))
def test_accepted_lineage_vectors_are_what_the_runner_builds(vector: dict[str, Any]) -> None:
    assert vector["origin"] == "producer-canonical"
    batch = _batch(vector)
    for fact in _lineage_facts(batch):
        stored = {name: value for name, value in fact.items() if name != "seq"}
        assert _rebuild(fact, _authority(batch)) == stored


LINEAGE_REFUSED = _vectors(
    lambda v: (
        v["expected"]["outcome"] == "reject"
        and v["expected"]["error_class"] != "capability_mismatch"
        and _lineage_facts(_batch(v))
    )
)


@pytest.mark.parametrize("vector", LINEAGE_REFUSED, ids=_ids(LINEAGE_REFUSED))
def test_the_runner_never_signs_a_refused_lineage_vector(vector: dict[str, Any]) -> None:
    batch = _batch(vector)
    refused = 0
    for fact in _lineage_facts(batch):
        stored = {name: value for name, value in fact.items() if name != "seq"}
        try:
            rebuilt = _rebuild(fact, _authority(batch))
        except (RunnerFactContractError, TypeError, ValueError):
            refused += 1
            continue
        refused += rebuilt != stored
    assert refused, "the runner would sign this refused vector"


def test_every_refusal_names_a_defined_class_and_stage() -> None:
    document = RUNNER_FACT_DOCUMENT
    assert list(document["error_class_definitions"]) == document["error_classes"]
    assert set(document["stages"]) == {"decode", "admission", "projection"}
    for vector in VECTORS.values():
        expected = vector["expected"]
        if expected["outcome"] == "reject":
            assert expected["error_class"] in document["error_classes"], vector["id"]
            assert expected["stage"] in document["stages"], vector["id"]


def test_a_capability_mismatch_is_decided_at_admission_and_nowhere_else() -> None:
    # decode sees the batch bytes alone; the capability record is not in them.
    stages = {
        vector["expected"]["stage"]
        for vector in VECTORS.values()
        if vector["expected"].get("error_class") == "capability_mismatch"
    }
    assert stages == {"admission"}
    assert all(
        vector["expected"].get("error_class") == "capability_mismatch"
        for vector in VECTORS.values()
        if vector["expected"].get("stage") == "admission"
    )


def _matrix_vector(event: str, field: str, verdict: str) -> dict[str, Any]:
    return VECTORS[f"order-event-{event}-{verdict}-{field.replace('_', '-')}"]


FROZEN_FORBIDDEN = [
    (event, field)
    for event in frozen.EVENTS
    for field in frozen.WIRE_FIELDS
    if frozen.cell(event, field) == "-"
]
FROZEN_REQUIRED = [
    (event, field)
    for event in frozen.EVENTS
    for field in frozen.WIRE_FIELDS
    if frozen.cell(event, field) == "R"
]


@pytest.mark.parametrize(
    ("event", "field"), FROZEN_FORBIDDEN, ids=[f"{e}-{f}" for e, f in FROZEN_FORBIDDEN]
)
def test_every_forbidden_cell_of_the_frozen_matrix_has_its_vector(event: str, field: str) -> None:
    vector = _matrix_vector(event, field, "forbids")
    assert vector["expected"] == {
        "outcome": "reject",
        "error_class": "schema_violation",
        "stage": "decode",
    }
    (fact,) = _batch(vector)["facts"]
    assert fact["order_event"] == event and field in fact


@pytest.mark.parametrize(
    ("event", "field"), FROZEN_FORBIDDEN, ids=[f"{e}-{f}" for e, f in FROZEN_FORBIDDEN]
)
def test_a_forbidden_cell_is_refused_for_its_field_alone(event: str, field: str) -> None:
    # The vector is the event's smallest valid fact plus the field and the
    # fields that must come with it, all of them: take those away and what is
    # left is the accepted smallest fact. A consumer that let the event carry
    # the whole group would accept it, so it tests the event's column and
    # nothing else.
    batch = _batch(_matrix_vector(event, field, "forbids"))
    (fact,) = batch["facts"]
    smallest = VECTORS[f"order-event-{event}"]
    assert smallest["expected"]["outcome"] == "accept"
    (base,) = _batch(smallest)["facts"]
    added = set(fact) - set(base)
    group = frozen.partners(field)
    assert field in added and added <= group, sorted(added)
    assert group <= set(fact), sorted(group - set(fact))
    # The added members carry values valid together: a base quantity is its
    # native quantity times the multiplier the fact carries (the sample's when it
    # carries none), and a commission is in the sample's currency.
    multiplier = Decimal(fact.get("quantity_multiplier", frozen.SAMPLE_MULTIPLIER))
    for native, base_name in frozen.BASE_OF.items():
        if base_name in added:
            assert Decimal(fact[base_name]) == Decimal(fact[native]) * multiplier, base_name
    for currency in ("commission_voided_currency", "fill_commission_currency"):
        if currency in added:
            assert fact[currency] == frozen.SAMPLE_CURRENCY, currency
    stripped = {name: value for name, value in fact.items() if name not in added}
    assert stripped == base
    Draft202012Validator(_json(SCHEMA)).validate({**batch, "facts": [stripped]})


@pytest.mark.parametrize(
    ("event", "field"), FROZEN_REQUIRED, ids=[f"{e}-{f}" for e, f in FROZEN_REQUIRED]
)
def test_every_required_cell_of_the_frozen_matrix_has_its_vector(event: str, field: str) -> None:
    vector = _matrix_vector(event, field, "needs")
    assert vector["expected"]["outcome"] == "reject"
    (fact,) = _batch(vector)["facts"]
    assert fact["order_event"] == event and field not in fact


def test_the_matrix_vectors_are_exactly_the_frozen_cells() -> None:
    matrix = {
        vector_id for vector_id in VECTORS if "-forbids-" in vector_id or "-needs-" in vector_id
    }
    matrix = {vector_id for vector_id in matrix if vector_id.startswith("order-event-")}
    expected = {
        _matrix_vector(event, field, "forbids")["id"] for event, field in FROZEN_FORBIDDEN
    } | {_matrix_vector(event, field, "needs")["id"] for event, field in FROZEN_REQUIRED}
    assert matrix == expected


@pytest.mark.parametrize("order_type", sorted(frozen.PRICES))
def test_creation_price_vectors_follow_the_frozen_price_table(order_type: str) -> None:
    (limit, trigger), _ = frozen.PRICES[order_type]
    accepted = _batch(VECTORS[f"order-price-{order_type}"])["facts"][0]
    assert ("limit_price" in accepted) == (limit != "-")
    assert ("trigger_price" in accepted) == (trigger != "-")
    for field, rule in (("limit_price", limit), ("trigger_price", trigger)):
        name = field.replace("_", "-")
        verdicts = {
            verdict
            for verdict in ("needs", "forbids")
            if f"order-price-{order_type}-{verdict}-{name}" in VECTORS
        }
        assert verdicts == {"R": {"needs"}, "-": {"forbids"}, "O": set()}[rule], field


def test_capability_shape_verdicts_are_the_frozen_shape_table() -> None:
    shapes = RUNNER_FACT_DOCUMENT["capability_shapes"]
    assert len(shapes) == 16
    for shape in shapes:
        declared = tuple(shape["declares"][flag] for flag in frozen.CAPABILITY_FLAGS)
        expected = "accept" if declared in frozen.ACCEPTED_CAPABILITY_SHAPES else "reject"
        assert shape["expected"] == expected, shape["id"]


@pytest.mark.parametrize(
    ("vector_id", "kind"),
    [
        ("lineage-kinds-under-the-canonical-capability", ORDER_EVENT_KIND),
        ("fill-correction-under-the-canonical-capability", FILL_CORRECTION_KIND),
    ],
)
def test_an_undeclared_capability_is_the_canonical_one(vector_id: str, kind: str) -> None:
    vector = VECTORS[vector_id]
    assert vector["expected"] == {
        "outcome": "reject",
        "error_class": "capability_mismatch",
        "stage": "admission",
    }
    batch = _batch(vector)
    manifest = _json(ROOT / "docs/authority/runner-fact-capability-manifest-v1.json")
    assert batch["capability_manifest_digest"] == _sha256(_canonical(manifest))
    assert [fact["kind"] for fact in batch["facts"]] == [kind]
    assert kind not in manifest["fact_kind_projectors"]


def test_sequences_name_the_contract_and_only_documented_expectations() -> None:
    assert SEQUENCES["contract_id"] == RUNNER_FACT_DOCUMENT["contract_id"]
    assert SEQUENCES["contract_revision"] == RUNNER_FACT_DOCUMENT["contract_revision"]
    keys = set(SEQUENCES["expected_keys"])
    for sequence in SEQUENCES["sequences"]:
        for step in sequence["steps"]:
            assert step["expected"]["outcome"] in {"accept", "reject"}
            assert set(step["expected"]) <= keys, sequence["id"]


STEPS = [
    (sequence["id"], index, step)
    for sequence in SEQUENCES["sequences"]
    for index, step in enumerate(sequence["steps"])
]


@pytest.mark.parametrize(
    ("sequence_id", "index", "step"), STEPS, ids=[f"{s}-{i}" for s, i, _ in STEPS]
)
def test_every_sequence_step_is_a_valid_message_on_its_own(
    sequence_id: str, index: int, step: dict[str, Any]
) -> None:
    # A step the fold refuses is still a well-formed message; only the history
    # before it makes it wrong. Well-formed means the runner would sign it: the
    # schema admits it and the builders, which hold the rules a single fact can
    # decide, rebuild it unchanged.
    batch = json.loads(step["raw"])
    Draft202012Validator(_json(SCHEMA)).validate(batch)
    assert batch["payload_digest"] == _sha256(_canonical(batch["facts"]))
    for fact in _lineage_facts(batch):
        stored = {name: value for name, value in fact.items() if name != "seq"}
        assert _rebuild(fact, _authority(batch)) == stored, fact["event_id"]


def test_an_order_once_voided_stays_voided() -> None:
    # The engine keeps a voided order voided whatever later voids say, reopened
    # or not; a sequence that brings it back would teach a consumer otherwise.
    for sequence in SEQUENCES["sequences"]:
        voided = False
        for step in sequence["steps"]:
            state = step["expected"].get("order_state")
            if voided and step["expected"]["outcome"] == "accept":
                assert state == "voided", sequence["id"]
            voided = voided or state == "voided"


def test_an_accepted_void_leaves_the_order_in_its_signed_state() -> None:
    for sequence in SEQUENCES["sequences"]:
        for step in sequence["steps"]:
            voids = [
                fact
                for fact in json.loads(step["raw"])["facts"]
                if fact.get("order_event") == "fill_voided"
            ]
            if voids and step["expected"]["outcome"] == "accept":
                assert step["expected"]["order_state"] == voids[-1]["order_status_after"]


def test_a_refused_amendment_step_names_the_values_it_keeps() -> None:
    covered = set()
    for sequence in SEQUENCES["sequences"]:
        for step in sequence["steps"]:
            for fact in json.loads(step["raw"])["facts"]:
                if fact.get("order_event") not in {"modify_refused", "modify_rejected"}:
                    continue
                if "requested_quantity" in fact:
                    assert "order_confirmed_base_quantity" in step["expected"], sequence["id"]
                    covered.add("quantity")
                if "requested_limit_price" in fact:
                    assert "order_confirmed_limit_price" in step["expected"], sequence["id"]
                    covered.add("limit_price")
    assert covered == {"quantity", "limit_price"}


def _base(value: str) -> Decimal:
    return Decimal(value)


def test_the_fills_effective_quantity_is_its_quantity_less_the_last_applied_void() -> None:
    checked = 0
    for sequence in SEQUENCES["sequences"]:
        fills: dict[str, Decimal] = {}
        applied: dict[str, Decimal] = {}
        for step in sequence["steps"]:
            expected = step["expected"]
            facts = json.loads(step["raw"])["facts"]
            for fact in facts:
                if fact["kind"] == "execution_fill":
                    fills[fact["venue_trade_id"]] = _base(fact["quantity"])
            voids = [fact for fact in facts if fact.get("order_event") == "fill_voided"]
            for void in voids:
                trade = void["voided_venue_trade_id"]
                if expected["outcome"] == "accept" and trade in fills:
                    applied[trade] = _base(void["voided_base_quantity"])
            if "fill_effective_base_quantity" not in expected or len(fills) != 1:
                continue
            ((trade, quantity),) = fills.items()
            effective = quantity - applied.get(trade, Decimal(0))
            assert _base(expected["fill_effective_base_quantity"]) == effective, (
                sequence["id"],
                expected,
            )
            checked += 1
    assert checked >= 40


def test_settlement_steps_add_up_and_match_the_order_side() -> None:
    checked = 0
    for sequence in SEQUENCES["sequences"]:
        settled_quantity = None
        removed = Decimal(0)
        last_effective = None
        corrections = 0
        # The cumulative void and commission reversal of the version last applied.
        applied_void = Decimal(0)
        applied_commission = Decimal(0)
        for step in sequence["steps"]:
            expected = step["expected"]
            facts = json.loads(step["raw"])["facts"]
            for fact in facts:
                if fact["kind"] == "execution_fill":
                    settled_quantity = _base(fact["quantity"])
            if "fill_effective_base_quantity" in expected:
                last_effective = _base(expected["fill_effective_base_quantity"])
            if not any(fact["kind"] == FILL_CORRECTION_KIND for fact in facts):
                continue
            step_quantity = _base(expected["settled_fill_void_base_quantity_step"])
            settled_commission = expected["settled_commission_reversal_step"]
            (correction,) = [fact for fact in facts if fact["kind"] == FILL_CORRECTION_KIND]
            if expected["outcome"] == "reject":
                # A refused version applies nothing on either side.
                assert step_quantity == 0, sequence["id"]
                assert _base(settled_commission["amount"]) == 0, sequence["id"]
                assert "commission_reversal_step" not in expected or (
                    _base(expected["commission_reversal_step"]["amount"]) == 0
                ), sequence["id"]
            else:
                # The settled fill and the order fold reverse the same commission.
                assert settled_commission == expected["commission_reversal_step"], (
                    sequence["id"],
                    expected,
                )
                # Each applied step takes off what its version adds to the version
                # last applied, so steps that trade amounts with each other fail.
                voided = _base(correction["voided_base_quantity"])
                commission = _base(correction.get("commission_voided", "0"))
                assert step_quantity == voided - applied_void, (sequence["id"], expected)
                assert _base(settled_commission["amount"]) == commission - applied_commission, (
                    sequence["id"],
                    expected,
                )
                applied_void, applied_commission = voided, commission
            removed += step_quantity
            corrections += 1
        checked += corrections
        if corrections:
            assert settled_quantity is not None and last_effective is not None
            # What the corrections took off the settled fill is what the void took
            # off the fill.
            assert removed == settled_quantity - last_effective, sequence["id"]
    assert checked >= 9


def test_settlement_corrections_are_preceded_by_their_settled_fill() -> None:
    covered = 0
    for sequence in SEQUENCES["sequences"]:
        settled: set[str] = set()
        for step in sequence["steps"]:
            for fact in json.loads(step["raw"])["facts"]:
                if fact["kind"] == "fill":
                    settled.add(fact["fill_id"])
                if fact["kind"] == FILL_CORRECTION_KIND:
                    assert fact["fill_id"] in settled, sequence["id"]
                    assert "settled_fill_void_base_quantity_step" in step["expected"]
                    assert "settled_commission_reversal_step" in step["expected"]
                    covered += 1
    assert covered >= 9


def test_sequences_continue_one_stream() -> None:
    for sequence in SEQUENCES["sequences"]:
        expected_start = 1
        for step in sequence["steps"]:
            batch = json.loads(step["raw"])
            assert batch["source_seq_start"] == expected_start, sequence["id"]
            expected_start = batch["source_seq_end"] + 1
