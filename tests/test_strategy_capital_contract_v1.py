from __future__ import annotations

import copy
import importlib
import json
import runpy
import shutil
from decimal import localcontext
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = "packages/custos-strategy-toolkit/src/custos_toolkit/contracts/strategy_capital.py"
INDEX = "docs/authority/strategy-contract-assets-v1.json"
UID = "11000000-0000-4000-8000-000000000001"
OTHER = "22000000-0000-4000-8000-000000000002"
AT = "2026-11-05T12:00:00.123456789Z"
CURRENCIES = ("USD", "USDT", "USDC", "BTC", "ETH", "VUSDC", "VBTC", "VETH")


def contract():
    return importlib.import_module("custos_toolkit.contracts.strategy_capital")


def provenance(observation=False):
    source = {
        "kind": "capital_state",
        "command_event_id": UID,
        "command_digest": "a" * 64,
        "capital_revision": 1,
        "capital_state_digest": "b" * 64,
        "source_frontier": 0,
        "source_ledger_digest": "c" * 64,
        "principal_lineage_digest": "d" * 64,
        "phase": "committed",
        "operation_id": None,
    }
    if observation:
        source = {
            "kind": "runner_observation",
            "batch_id": UID,
            "seq_start": 1,
            "seq_end": 2,
            "observation_id": "e" * 64,
            "pnl_basis_digest": "f" * 64,
            "source_prefix_digest": "c" * 64,
            "capital_revision": 1,
            "capital_state_digest": "b" * 64,
        }
    return {
        "scope": {
            "tenant_id": "tenant-one",
            "trading_mode": "sandbox",
            "runner_id": UID,
            "strategy_id": UID,
            "deployment_instance_id": UID,
            "deployment_spec_id": UID,
            "deployment_spec_digest": "a" * 64,
            "generation": 1,
        },
        "period": {
            "code": "2026-11",
            "start_utc": "2026-11-01T00:00:00Z",
            "end_exclusive_utc": "2026-12-01T00:00:00Z",
            "trading_mode": "sandbox",
        },
        "as_of": AT,
        "source": source,
    }


def document():
    def known(amount, obs):
        return {
            "status": "known",
            "amount": amount,
            "currency": "USD",
            "reason": None,
            "provenance": provenance(obs),
        }

    entries = []
    for number, (kind, amount) in enumerate(
        (
            ("contribution", "100"),
            ("redemption", "-1"),
            ("internal_transfer", "3"),
            ("profit_distribution", "-2"),
        ),
        1,
    ):
        entries.append(
            {
                "change_id": f"33000000-0000-4000-8000-{number:012d}",
                "kind": kind,
                "amount": amount,
                "currency": "USD",
                "effective_at": AT,
                "operation_id": None,
                "reversal_of_change_id": None,
                "source": {
                    "kind": kind,
                    "request_id": UID,
                    "effect_id": UID,
                    "version": 1,
                    "digest": "a" * 64,
                    "allocation_id": UID if number < 3 else None,
                    "allocation_version": 1 if number < 3 else None,
                    "allocation_target_id": UID if number < 3 else None,
                },
            }
        )
    return {
        "allocated_capital": known("100", False),
        "realized_pnl": known("-2", True),
        "unrealized_pnl": known("5", True),
        "capital_changes": {
            "status": "known",
            "entries": entries,
            "reason": None,
            "provenance": provenance(),
        },
    }


def parse(value):
    return contract().StrategyCapitalViewV1.model_validate(value)


def put(value, path, item):
    cursor = value
    for component in path[:-1]:
        cursor = cursor[component]
    cursor[path[-1]] = item
    return value


def test_four_fields_round_trip_and_nanoseconds_remain_exact():
    raw = document()
    model = parse(raw)
    assert model.model_dump(mode="json") == raw
    assert model.realized_pnl.provenance.as_of == AT
    assert set(type(model).model_fields) == set(raw)


def test_pnl_unknown_does_not_destroy_trusted_allocated_capital():
    raw = document()
    for name in ("realized_pnl", "unrealized_pnl"):
        raw[name] = {
            "status": "unavailable",
            "amount": None,
            "currency": "USD",
            "reason": "period_boundary_missing",
            "provenance": None,
        }
    model = parse(raw)
    assert model.allocated_capital.amount == "100"
    assert model.realized_pnl.amount is None
    assert model.unrealized_pnl.amount is None


@pytest.mark.parametrize(
    "amount", ["0", "0.0000000000000000000000000001", "79228162514264337593543950335"]
)
def test_exact_decimal_extremes_ignore_ambient_decimal_precision(amount):
    with localcontext() as context:
        context.prec = 2
        raw = document()
        raw["allocated_capital"]["amount"] = amount
        assert parse(raw).allocated_capital.amount == amount


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1,
        1.5,
        "1e2",
        "NaN",
        "Infinity",
        "-0",
        "-0.0",
        "+1",
        "01",
        "1.0",
        "1.00",
        " 1",
        "79228162514264337593543950336",
        "0.00000000000000000000000000001",
        "-1",
    ],
)
def test_allocated_amount_requires_exact_bounded_nonnegative_wire(value):
    with pytest.raises(ValidationError):
        parse(put(document(), ["allocated_capital", "amount"], value))


@pytest.mark.parametrize("currency", CURRENCIES)
def test_exact_eight_currency_domain(currency):
    raw = document()
    for name in ("allocated_capital", "realized_pnl", "unrealized_pnl"):
        raw[name]["currency"] = currency
    for entry in raw["capital_changes"]["entries"]:
        entry["currency"] = currency
    parse(raw)


@pytest.mark.parametrize(
    "path,value",
    [
        (["allocated_capital", "currency"], "usd"),
        (["realized_pnl", "currency"], "USDT"),
        (
            ["allocated_capital", "provenance", "scope", "runner_id"],
            "00000000-0000-0000-0000-000000000000",
        ),
        (["allocated_capital", "provenance", "scope", "tenant_id"], "x" * 65),
        (["allocated_capital", "provenance", "scope", "tenant_id"], "\u79df\u6237"),
        (["allocated_capital", "provenance", "scope", "generation"], True),
        (["allocated_capital", "provenance", "source", "capital_revision"], 2**63),
        (["allocated_capital", "provenance", "source", "source_frontier"], -1),
        (["allocated_capital", "provenance", "source", "phase"], "released"),
        (["allocated_capital", "provenance", "source", "command_digest"], "A" * 64),
        (["allocated_capital", "provenance", "as_of"], "2026-11-05T12:00:00.123456789+00:00"),
        (["allocated_capital", "provenance", "as_of"], "2026-11-05T12:00:00.123000Z"),
        (["allocated_capital", "provenance", "as_of"], "2026-11-05T12:00:00.1Z"),
        (
            ["allocated_capital", "provenance", "period", "end_exclusive_utc"],
            "2026-12-02T00:00:00Z",
        ),
        (["realized_pnl", "provenance", "source", "seq_start"], 3),
        (["realized_pnl", "provenance", "source", "observation_id"], "1" * 64),
        (["capital_changes", "provenance", "source", "capital_revision"], 2),
        (["capital_changes", "provenance", "source", "command_event_id"], OTHER),
        (["capital_changes", "provenance", "source", "phase"], "preparing"),
    ],
)
def test_field_and_cross_observation_mismatches_are_rejected(path, value):
    with pytest.raises(ValidationError):
        parse(put(document(), path, value))


def test_required_nullable_fields_are_not_optional_and_extras_are_rejected():
    for path in (
        ["realized_pnl", "reason"],
        ["capital_changes", "entries", 0, "operation_id"],
        ["capital_changes", "entries", 0, "source", "allocation_version"],
    ):
        raw = document()
        cursor = raw
        for component in path[:-1]:
            cursor = cursor[component]
        del cursor[path[-1]]
        with pytest.raises(ValidationError):
            parse(raw)
    with pytest.raises(ValidationError):
        parse({**document(), "hwm": "100"})
    with pytest.raises(ValidationError):
        parse(put(document(), ["realized_pnl", "provenance", "source", "allow_transfer"], True))


def test_existing_allocation_reversals_do_not_open_payment_reversal_writers():
    raw = document()
    for entry in raw["capital_changes"]["entries"][:2]:
        entry["reversal_of_change_id"] = OTHER
        entry["amount"] = "-100" if entry["kind"] == "contribution" else "1"
    parse(raw)
    raw["capital_changes"]["entries"][-1]["reversal_of_change_id"] = OTHER
    raw["capital_changes"]["entries"][-1]["amount"] = "2"
    with pytest.raises(ValidationError):
        parse(raw)


def test_immutable_models_do_not_retain_input_or_dump_aliases():
    raw = document()
    model = parse(raw)
    before = model.model_dump(mode="json")
    raw["capital_changes"]["entries"][0]["source"]["version"] = 99
    raw["allocated_capital"]["provenance"]["source"]["capital_revision"] = 99
    raw["capital_changes"]["entries"].clear()
    dump = model.model_dump(mode="json")
    dump["realized_pnl"]["provenance"]["source"]["batch_id"] = OTHER
    assert model.model_dump(mode="json") == before
    assert isinstance(model.capital_changes.entries, tuple)
    with pytest.raises(ValidationError):
        model.capital_changes.entries[0].source.version = 99


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_supported", True),
        ("capital_change_supported", True),
        ("runtime_supported", 0),
        ("schema_version", True),
    ],
)
def test_schema_contract_support_cannot_claim_runtime_capability(field, value):
    raw = {
        "schema_version": 1,
        "contract_id": "alephain.custos.strategy_capital_view.v1",
        "runtime_supported": False,
        "capital_change_supported": False,
    }
    support = contract().StrategyCapitalContractSupportV1
    assert support.model_validate(raw).runtime_supported is False
    with pytest.raises(ValidationError):
        support.model_validate({**raw, field: value})


def test_generated_current_assets_register_contract_only_and_both_actual_sources():
    module = contract()
    generator = runpy.run_path(str(ROOT / "scripts/generate_strategy_contract_assets.py"))
    outputs = generator["build_v1_contract_assets"]()
    index = json.loads(outputs[INDEX])
    assert index["current_contracts"]["strategy_capital_view"]["contract_only"] is True
    assert index["current_contracts"]["strategy_capital_contract_support"]["contract_only"] is True
    models = {item["path"]: item for item in index["producer"]["source_models"]}
    assert set(models) == {MODEL_PATH, str(generator["SOURCE_MODEL"].relative_to(ROOT))}
    schema = module.StrategyCapitalViewV1.model_json_schema(mode="validation")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document())
    with pytest.raises(SchemaError):
        Draft202012Validator(schema).validate({**document(), "hwm": "100"})
    assert (
        outputs["docs/gateway-contract/v1/strategy_capital_view_v1.schema.json"]
        == (ROOT / "docs/gateway-contract/v1/strategy_capital_view_v1.schema.json").read_bytes()
    )


def authority_tree(tmp_path):
    contract()
    checker = runpy.run_path(str(ROOT / "scripts/check-authority-docs.py"))
    verify = checker["strategy_capital_contract_errors"]
    index = json.loads((ROOT / INDEX).read_bytes())
    paths = [item["path"] for item in index["producer"]["source_models"]]
    paths += [item["path"] for item in index["assets"] if "capital" in item["path"]]
    for name in [*paths, INDEX]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return verify, index


def test_current_authority_rejects_capital_source_drift_and_false_readiness(tmp_path):
    verify, index = authority_tree(tmp_path)
    assert verify(tmp_path) == []
    source = tmp_path / MODEL_PATH
    source.write_bytes(source.read_bytes() + b"\n# changed current source\n")
    assert any("source" in error for error in verify(tmp_path))
    shutil.copyfile(ROOT / MODEL_PATH, source)
    support_path = "docs/authority/strategy-capital-contract-support-v1.golden.json"
    support = json.loads((tmp_path / support_path).read_bytes())
    support["runtime_supported"] = True
    (tmp_path / support_path).write_text(json.dumps(support))
    # Even a synchronized current index cannot turn a schema-only declaration into readiness.
    for item in index["assets"]:
        if item["path"] == support_path:
            import hashlib

            payload = (tmp_path / support_path).read_bytes()
            item.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    (tmp_path / INDEX).write_text(json.dumps(index))
    assert any("runtime support" in error for error in verify(tmp_path))


@pytest.mark.parametrize(
    "observed,accepted",
    [
        ("2026-11-05T12:00:00.123456788Z", False),
        (AT, True),
        ("2026-11-05T12:00:00.123456790Z", True),
    ],
)
def test_pnl_observation_cannot_precede_its_capital_state_by_one_nanosecond(observed, accepted):
    raw = document()
    for name in ("realized_pnl", "unrealized_pnl"):
        raw[name]["provenance"]["as_of"] = observed
    if accepted:
        parse(raw)
    else:
        with pytest.raises(ValidationError, match="precedes"):
            parse(raw)


@pytest.mark.parametrize("fraction", [None, "001", "000001", "000000001", "999999999"])
def test_supported_autosi_nanoseconds_are_not_truncated(fraction):
    raw = document()
    at = f"2026-11-05T12:00:00.{fraction}Z" if fraction else "2026-11-05T12:00:00Z"
    for value in raw.values():
        value["provenance"]["as_of"] = at
    for entry in raw["capital_changes"]["entries"]:
        entry["effective_at"] = at
    assert parse(raw).realized_pnl.provenance.as_of == at
    Draft202012Validator(contract().StrategyCapitalViewV1.model_json_schema()).validate(raw)


def test_known_states_cannot_mix_preparing_operations_or_valid_different_months():
    raw = document()
    for name in ("allocated_capital", "capital_changes"):
        raw[name]["provenance"]["source"].update(phase="preparing", operation_id=UID)
    parse(raw)
    raw["capital_changes"]["provenance"]["source"]["operation_id"] = OTHER
    with pytest.raises(ValidationError, match="control states"):
        parse(raw)
    raw = document()
    for name in ("realized_pnl", "unrealized_pnl"):
        raw[name]["provenance"]["period"] = {
            "code": "2026-10",
            "start_utc": "2026-10-01T00:00:00Z",
            "end_exclusive_utc": "2026-11-01T00:00:00Z",
            "trading_mode": "sandbox",
        }
        raw[name]["provenance"]["as_of"] = "2026-10-31T23:59:59.999999999Z"
    with pytest.raises(ValidationError, match="current periods"):
        parse(raw)


@pytest.mark.parametrize(
    "change_index,field,value",
    [
        (0, "amount", "-1"),
        (1, "amount", "1"),
        (3, "amount", "2"),
        (2, "amount", "0"),
        (0, "change_id", "00000000-0000-0000-0000-000000000000"),
        (2, "reversal_of_change_id", OTHER),
    ],
)
def test_change_effects_keep_the_four_kind_acceptance_domain(change_index, field, value):
    raw = document()
    raw["capital_changes"]["entries"][change_index][field] = value
    with pytest.raises(ValidationError):
        parse(raw)


def test_unknown_fields_cannot_carry_a_money_value_or_fake_empty_history():
    raw = document()
    raw["realized_pnl"].update(status="unavailable", reason="period_boundary_missing")
    with pytest.raises(ValidationError):
        parse(raw)
    raw["realized_pnl"]["amount"] = None
    parse(raw)
    raw["capital_changes"].update(status="unavailable", reason="journal_prefix_incomplete")
    with pytest.raises(ValidationError):
        parse(raw)
    raw["capital_changes"]["entries"] = None
    parse(raw)


def test_generated_vectors_cover_schema_and_strict_runtime_boundaries():
    schema = contract().StrategyCapitalViewV1.model_json_schema(mode="validation")
    validator = Draft202012Validator(schema)
    golden = json.loads((ROOT / "docs/authority/strategy-capital-view-v1.golden.json").read_bytes())
    vectors = json.loads(
        (ROOT / "docs/authority/strategy-capital-view-v1.negative.json").read_bytes()
    )
    assert vectors["contract_only"] is True
    validator.validate(golden)
    parse(golden)
    for vector in vectors["vectors"]:
        changed = put(copy.deepcopy(golden), vector["path"], vector["value"])
        with pytest.raises(ValidationError):
            parse(changed)
        assert bool(list(validator.iter_errors(changed))) is vector["schema_rejects"], vector[
            "name"
        ]


@pytest.mark.parametrize(
    "schema_path,key,value",
    [
        (
            "docs/gateway-contract/v1/strategy_capital_view_v1.schema.json",
            "x-runtime-supported",
            True,
        ),
        (
            "docs/gateway-contract/v1/strategy_capital_view_v1.schema.json",
            "x-contract-revision",
            True,
        ),
    ],
)
def test_current_gate_rejects_schema_readiness_even_after_hash_refresh(
    tmp_path, schema_path, key, value
):
    import hashlib

    verify, index = authority_tree(tmp_path)
    schema = json.loads((tmp_path / schema_path).read_bytes())
    schema[key] = value
    payload = json.dumps(schema).encode()
    (tmp_path / schema_path).write_bytes(payload)
    for entry in index["assets"]:
        if entry["path"] == schema_path:
            entry.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    (tmp_path / INDEX).write_text(json.dumps(index))
    assert verify(tmp_path)


@pytest.mark.parametrize("separator", ["\n", "\r", "\u2028", "\u2029"])
def test_timestamp_schema_rejects_terminal_line_separators_like_the_model(separator):
    raw = document()
    raw["allocated_capital"]["provenance"]["as_of"] += separator
    model = contract().StrategyCapitalViewV1
    with pytest.raises(ValidationError):
        model.model_validate(raw)
    validator = Draft202012Validator(model.model_json_schema(mode="validation"))
    assert list(validator.iter_errors(raw)), repr(separator)
