"""Opening decisions have one closed, signed RunnerFact representation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from custos.core.runner_fact import RunnerFactContractError, runner_fact_capability_shape
from tests.test_opening_inventory_contract import _cash_candidate
from tests.test_runner_fact_store import _runner_authority, _verified_command

ROOT = Path(__file__).resolve().parents[1]
KIND = "RunnerOpeningReleaseFact.v1"


def observation():
    _, _, desired = _verified_command()
    authority = _runner_authority(desired)
    inventory, read = _cash_candidate(authority)
    inventory["observations"]["positions"] = read([])
    inventory["observations"]["ordinary_orders"] = read([])
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    return authority, inventory


def fact_arguments():
    authority, inventory = observation()
    return {
        "authority": authority,
        "release_id": str(UUID(int=1)),
        "release_command_event_id": str(UUID(int=2)),
        "basis_version_digest": "a" * 64,
        "outcome": "acknowledged",
        "reason": None,
        "amount": "1",
        "currency": "USDT",
        "observed_at": inventory["equity_binding"]["observed_at"],
        "account_inventory": inventory,
    }


def test_opening_decision_is_an_explicit_capability_and_preserves_existing_shapes():
    projectors, contracts = runner_fact_capability_shape()
    assert KIND not in projectors
    assert "opening_release" not in contracts["risk"]
    declared, declared_contracts = runner_fact_capability_shape(opening_release=True)
    assert declared == {**projectors, KIND: "risk"}
    assert declared_contracts["risk"] == {**contracts["risk"], "opening_release": "v1"}


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("acknowledged", None),
        ("refused", "inventory_unproven"),
        ("refused", "account_mismatch"),
        ("refused", "balance_mismatch"),
    ],
)
def test_producer_decisions_validate_against_the_published_closed_union(outcome, reason):
    from custos.core.runner_opening_release_fact import opening_release_fact

    arguments = fact_arguments()
    arguments.update(outcome=outcome, reason=reason)
    fact = opening_release_fact(**arguments)
    assert fact == opening_release_fact(**arguments)
    schema = json.loads(
        (ROOT / "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json").read_text()
    )
    Draft202012Validator({"$ref": f"#/$defs/fact_{KIND}", "$defs": schema["$defs"]}).validate(
        {**fact, "seq": 1}
    )
    assert (fact["outcome"], fact["reason"]) == (outcome, reason)
    fact["account_inventory"]["scope"]["tenant_id"] = "changed"
    assert opening_release_fact(**arguments)["account_inventory"]["scope"]["tenant_id"] != "changed"


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "released"},
        {"outcome": "acknowledged", "reason": "inventory_unproven"},
        {"outcome": "refused", "reason": None},
        {"outcome": "refused", "reason": "unknown"},
        {"release_id": str(UUID(int=0))},
        {"release_command_event_id": str(UUID(int=0))},
        {"basis_version_digest": "A" * 64},
        {"amount": "1.0"},
        {"amount": "-1"},
        {"amount": 1.0},
        {"currency": "usdt"},
        {"observed_at": "2026-11-04T00:00:00"},
        {"observed_at": "2026-11-04T00:00:00.000Z"},
        {"amount": "79228162514264337593543950336"},
    ],
)
def test_invalid_decisions_are_rejected_before_they_can_be_signed(change):
    from custos.core.runner_opening_release_fact import opening_release_fact

    arguments = fact_arguments()
    arguments.update(change)
    with pytest.raises(RunnerFactContractError):
        opening_release_fact(**arguments)


@pytest.mark.parametrize("case", ("amount", "time", "scope", "capability", "incomplete"))
def test_acknowledgement_requires_the_exact_complete_observation(case):
    from custos.core.opening_inventory_contract import unavailable_opening_inventory
    from custos.core.runner_opening_release_fact import opening_release_fact

    arguments = fact_arguments()
    inventory = copy.deepcopy(arguments["account_inventory"])
    if case == "amount":
        arguments["amount"] = "2"
    elif case == "time":
        arguments["observed_at"] = "2026-11-04T00:00:01Z"
    elif case == "scope":
        inventory["scope"]["generation"] += 1
    elif case == "capability":
        inventory["capability_binding"]["capability_manifest_digest"] = "f" * 64
    else:
        inventory = unavailable_opening_inventory(arguments["authority"], "binance")
    arguments["account_inventory"] = inventory
    with pytest.raises(RunnerFactContractError):
        opening_release_fact(**arguments)


def test_outbox_cannot_sign_a_decision_outside_its_durable_release_transaction(tmp_path):
    from custos.core.runner_fact import RunnerFactOutbox
    from custos.core.runner_opening_release_fact import opening_release_fact
    from tests.test_runner_fact_store import _identity

    arguments = fact_arguments()
    outbox = RunnerFactOutbox(tmp_path / "runner.sqlite3")
    with pytest.raises(RunnerFactContractError, match="exclusive durable transaction"):
        outbox.enqueue_sync(
            arguments["authority"], _identity(), [opening_release_fact(**arguments)]
        )


def test_shared_opening_vectors_are_rendered_and_decoded_by_the_real_producer():
    from dataclasses import replace

    from pydantic import ValidationError

    from custos.core.runner_fact import RunnerFactAuthority
    from custos.core.runner_opening_release_fact import (
        RunnerOpeningReleaseFactV1,
        opening_release_fact,
    )

    document = json.loads(
        (ROOT / "docs/authority/conformance/runner-fact-batch-v1.vectors.json").read_text()
    )
    vectors = [v for v in document["vectors"] if v["id"].startswith("opening-release-")]
    assert len(vectors) >= 18
    for vector in vectors:
        batch = json.loads(vector["raw"])
        fact = {k: v for k, v in batch["facts"][0].items() if k != "seq"}
        if vector["expected"].get("stage") == "decode":
            with pytest.raises((ValidationError, RunnerFactContractError)):
                RunnerOpeningReleaseFactV1.model_validate(fact)
        else:
            parsed = RunnerOpeningReleaseFactV1.model_validate(fact)
            assert parsed.model_dump(mode="json") == fact
            authority, _ = observation()
            authority = RunnerFactAuthority(
                **{
                    field: UUID(batch[field])
                    if isinstance(getattr(authority, field), UUID)
                    else batch[field]
                    for field in authority.__dataclass_fields__
                }
            )
            authority = replace(
                authority, capability_manifest_digest=batch["capability_manifest_digest"]
            )
            assert (
                opening_release_fact(
                    authority=authority, **vector["producer_input"]["opening_decision"]
                )
                == fact
            )
