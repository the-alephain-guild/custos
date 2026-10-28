"""Closed account observation protocol; synthetic JSON grants no venue readiness."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from custos.core.runtime_authority import RuntimeStorageUnavailable
from tests.test_native_runtime_storage import storage as storage


@pytest.fixture
def observation(storage):
    _, _, native, build = storage
    native.document["revision"] = 10
    reader = build()
    document = {
        "schema_version": 1,
        "journal": deepcopy(native.document),
        "checkpoint_revision": 9,
        "source_epoch": str(uuid4()),
        "account_snapshot_keccak256": "0x" + "11" * 32,
        "account_material_keccak256": "0x" + "22" * 32,
        "account_id": native.account_id,
        "client_id": native.client_id,
        "account_revision": 2,
        "source_block": 100,
        "source_timestamp": 1700000000,
        "wallet_revision": 90,
        "trading_covered_block": 101,
        "reconciliation_complete": True,
        "balance_raw": "900000000",
        "locked_raw": "100000000",
        "queued_raw": "0",
        "consumable_debt_raw": "0",
        "queue_revision_raw": "1",
        "queue_node_count": 0,
        "cashflow_counters": {
            "deposited_raw": "1000000000",
            "withdrawn_raw": "0",
            "rewards_claimed_raw": "0",
        },
        "positions": [
            {
                "position_id": "PT-7",
                "instrument_id": "BTC-USD-PERP.PAPERTRADE",
                "opening_intent_id": "0x" + "33" * 32,
                "is_long": True,
                "use_debt": False,
                "margin_raw": "100000000",
                "leverage": 5,
                "entry_price_raw": "1000001",
                "price_scale_raw": "10",
                "opened_at_ms": 1699999999000,
            }
        ],
        "venue_operation_intent_ids": [],
        "pending_client_order_ids": ["open-7"],
    }

    def read():
        if native.closed:
            raise RuntimeError("injected released Native owner")
        return native.raw if native.raw is not None else json.dumps(document)

    native.account_observation_json = read
    return reader, native, document


def test_keeps_original_raw_collateral_and_source_time(observation):
    reader, _, document = observation
    first = reader.account_observation()
    second = reader.account_observation()
    assert first == second
    assert first.source_timestamp == document["source_timestamp"]
    assert first.balance_raw == 900000000
    assert first.positions[0].entry_price_raw == 1000001
    assert first.positions[0].leverage == 5
    assert first.cashflow_counters == (1000000000, 0, 0)
    assert first.pending_client_order_ids == ("open-7",)
    with pytest.raises(FrozenInstanceError):
        first.balance_raw = 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("checkpoint_revision", 0),
        ("checkpoint_revision", 11),
        ("checkpoint_revision", True),
        ("account_revision", 0),
        ("source_block", 1.0),
        ("source_timestamp", 2**64),
        ("wallet_revision", 101),
        ("trading_covered_block", -1),
        ("reconciliation_complete", 1),
        ("source_epoch", str(uuid4()).upper()),
        ("account_snapshot_keccak256", "0xBAD"),
        ("balance_raw", "01"),
        ("locked_raw", "0"),
        ("queued_raw", 0),
        ("consumable_debt_raw", "-1"),
        ("queue_revision_raw", str(2**256)),
        ("queue_node_count", False),
        ("account_id", "PAPERTRADE-OTHER"),
        ("client_id", "OTHER"),
        ("pending_client_order_ids", ["a", "a"]),
        ("pending_client_order_ids", [str(i) for i in range(257)]),
        ("venue_operation_intent_ids", ["invalid"]),
        ("unknown", "unexpected"),
    ],
)
def test_refuses_invalid_or_cross_route_account_fields(observation, field, value):
    reader, _, document = observation
    document[field] = value
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize(
    "field,value",
    [
        ("position_id", "PT-0"),
        ("position_id", "PT-01"),
        ("instrument_id", "BTC.PAPERTRADE\x00"),
        ("opening_intent_id", "0xBAD"),
        ("is_long", 1),
        ("use_debt", 0),
        ("margin_raw", "0"),
        ("leverage", True),
        ("leverage", 0),
        ("leverage", 2**32),
        ("entry_price_raw", "1e5"),
        ("price_scale_raw", "20"),
        ("price_scale_raw", str(10**19)),
        ("opened_at_ms", 1700000000001),
        ("unknown", None),
    ],
)
def test_refuses_invalid_position_identity_or_raw_parameters(observation, field, value):
    reader, _, document = observation
    document["positions"][0][field] = value
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize("raw", ["[]", "{}", '{"x":1,"x":2}', "x" * 262145])
def test_refuses_ambiguous_or_unbounded_payload(observation, raw):
    reader, native, _ = observation
    native.raw = raw
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


def test_owner_release_and_original_journal_identity_are_checked(observation):
    reader, native, document = observation
    reader.account_observation()
    document["journal"]["journal_id"] = str(uuid4())
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()
    native.closed = True
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize(
    "field",
    [
        "source_block",
        "source_timestamp",
        "wallet_revision",
        "trading_covered_block",
        "checkpoint_revision",
    ],
)
def test_refuses_source_regression_under_a_later_journal_revision(observation, field):
    reader, _, document = observation
    reader.account_observation()
    document["journal"]["revision"] += 1
    document["checkpoint_revision"] += 1
    document[field] -= 2
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


def test_same_checkpoint_cannot_change_source_contents_but_pending_can_change(observation):
    reader, _, document = observation
    reader.account_observation()
    document["journal"]["revision"] += 1
    document["pending_client_order_ids"] = []
    assert reader.account_observation().pending_client_order_ids == ()
    document["balance_raw"] = "900000001"
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


def test_new_epoch_can_reset_local_revision_without_resetting_global_coverage(observation):
    reader, _, document = observation
    reader.account_observation()
    document["journal"]["revision"] += 1
    document["checkpoint_revision"] += 1
    document["source_epoch"] = str(uuid4())
    document["account_revision"] = 1
    assert reader.account_observation().account_revision == 1


def test_new_epoch_cannot_change_material_at_unchanged_global_coverage(observation):
    reader, _, document = observation
    reader.account_observation()
    document["journal"]["revision"] += 1
    document["checkpoint_revision"] += 1
    document["source_epoch"] = str(uuid4())
    document["account_material_keccak256"] = "0x" + "44" * 32
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize("change", ["local_revision", "local_contents", "queue_revision"])
def test_later_checkpoint_cannot_regress_or_conflict_with_source(observation, change):
    reader, _, document = observation
    reader.account_observation()
    document["journal"]["revision"] += 1
    document["checkpoint_revision"] += 1
    if change == "local_revision":
        document["account_revision"] -= 1
    elif change == "local_contents":
        document["account_snapshot_keccak256"] = "0x" + "44" * 32
    else:
        document["queue_revision_raw"] = "0"
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize("change", ["pending_removed", "pending_added", "checkpoint"])
def test_same_journal_revision_cannot_change_observed_contents(observation, change):
    reader, _, document = observation
    reader.account_observation()
    if change == "pending_removed":
        document["pending_client_order_ids"] = []
    elif change == "pending_added":
        document["pending_client_order_ids"].append("open-8")
    else:
        document["checkpoint_revision"] += 1
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize("length", [129, 512])
def test_keeps_the_existing_native_client_order_id_acceptance_bound(observation, length):
    reader, _, document = observation
    document["pending_client_order_ids"] = ["a" * length]
    assert reader.account_observation().pending_client_order_ids == ("a" * length,)


def test_rejects_client_order_id_beyond_the_native_protocol_bound(observation):
    reader, _, document = observation
    document["pending_client_order_ids"] = ["a" * 513]
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_position",
        "position_bound",
        "counter_unknown",
        "counter_noncanonical",
        "missing",
        "nested_duplicate",
    ],
)
def test_refuses_malformed_nested_source(observation, change):
    reader, native, document = observation
    if change == "duplicate_position":
        document["positions"] *= 2
        document["locked_raw"] = "200000000"
    elif change == "position_bound":
        document["positions"] *= 65
    elif change == "counter_unknown":
        document["cashflow_counters"]["unknown"] = "0"
    elif change == "counter_noncanonical":
        document["cashflow_counters"]["withdrawn_raw"] = "00"
    elif change == "missing":
        del document["cashflow_counters"]["withdrawn_raw"]
    else:
        native.raw = json.dumps(document).replace('"deposited_raw":', '"withdrawn_raw":')
    with pytest.raises(RuntimeStorageUnavailable):
        reader.account_observation()
