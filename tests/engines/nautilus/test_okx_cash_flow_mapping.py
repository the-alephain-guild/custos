"""A mapping is an interpretation of raw evidence, not account authorization."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import localcontext
from uuid import uuid4

import pytest

from custos.core.runner_fact import venue_ledger_snapshot_facts
from custos.engines.nautilus import okx_cash_flow_mapping
from tests.engines.nautilus.test_okx_bill_window import END, START, START_MS, Source, bill

ORIGIN = "https://openapi.okx.com"
MODE = "testnet"


def transfer(identity="9007199254740993", **changes):
    return bill(
        identity, **({"type": "1", "subType": "11", "fee": "0", "from": "6", "to": "18"} | changes)
    )


async def raw_window(records):
    return await Source({None: records}, identities=[("42", "42")] * 2).collect_trading_bills(
        START, END, expected_uid="42", expected_main_uid="42"
    )


async def test_actual_raw_main_account_bill_maps_one_exact_partial_transfer():
    raw = await raw_window([transfer()])
    result = okx_cash_flow_mapping.map_main_account_transfers(
        raw, api_origin=ORIGIN, trading_mode=MODE
    )
    flow = result.cash_flows[0]
    assert flow["kind"] == "internal_transfer"
    assert flow["from"] == {"wallet_type": "funding", "sub_account": None}
    assert flow["to"] == {"wallet_type": "trading", "sub_account": None}
    assert flow["amount"] == "100.000000000000000001"
    assert flow["fee"] == "0" and flow["fee_currency"] == "USDT"
    assert (
        flow["cash_flow_id"]
        == f"okx:{MODE}:{hashlib.sha256(ORIGIN.encode()).hexdigest()}:42:trading:9007199254740993"
    )
    assert flow["counterparty_uid"] is None and flow["destination"] is None
    assert flow["external_reference"] is None
    assert result.raw_window is raw and result.unmapped == ()
    assert result.cash_flows_complete is False


def map_window(window):
    return okx_cash_flow_mapping.map_main_account_transfers(
        window, api_origin=ORIGIN, trading_mode=MODE
    )


async def test_reverse_direction_uses_exact_negative_change_and_booking_timestamp():
    raw = await raw_window(
        [transfer(subType="12", **{"from": "18", "to": "6", "balChg": "-7.125"})]
    )
    flow = map_window(raw).cash_flows[0]
    assert flow["amount"] == "7.125"
    assert flow["from"]["wallet_type"] == "trading" and flow["to"]["wallet_type"] == "funding"
    assert flow["occurred_at"] == "2026-11-19T00:00:00Z"


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("79228162514264337593543950335", "79228162514264337593543950335"),
        ("0.0000000000000000000000000001", "0.0000000000000000000000000001"),
        ("1.000000000000000000000000000000000", "1"),
    ],
)
async def test_wire_boundaries_are_preserved_without_decimal_context_rounding(amount, expected):
    raw = await raw_window(
        [transfer(subType="12", **{"from": "18", "to": "6", "balChg": "-" + amount})]
    )
    with localcontext() as context:
        context.prec = 6
        assert map_window(raw).cash_flows[0]["amount"] == expected


async def test_unmapped_original_rows_and_reasons_survive_next_to_a_mapped_row():
    records = [
        transfer("1"),
        transfer("2", type="2"),
        transfer("3", subType="999"),
        transfer("4", fee=""),
        transfer("5", fee="0.1"),
        transfer("6", ccy="FUTURE"),
    ]
    raw = await raw_window(records)
    result = map_window(raw)
    assert len(result.cash_flows) == 1
    assert [(item.bill_id, item.reason) for item in result.unmapped] == [
        ("2", "unsupported_bill_type"),
        ("3", "unsupported_transfer_subtype"),
        ("4", "fee_unavailable"),
        ("5", "nonzero_fee_unsupported"),
        ("6", "unsupported_currency"),
    ]
    assert [json.loads(item.raw_bill) for item in result.unmapped] == records[1:]
    assert result.raw_window.raw_bills == raw.raw_bills and result.cash_flows_complete is False


async def test_identity_is_stable_across_observation_and_window_metadata():
    row = transfer()
    first = await raw_window([row])
    second = await Source(
        {None: [row]}, clock=START_MS + 5000, identities=[("42", "42")] * 2
    ).collect_trading_bills(
        START - timedelta(seconds=1),
        END + timedelta(seconds=1),
        expected_uid="42",
        expected_main_uid="42",
    )
    assert map_window(first).cash_flows == map_window(second).cash_flows
    assert first.content_digest != second.content_digest


async def test_numeric_bill_id_and_actual_uid_define_independent_namespaces():
    raw = await raw_window([transfer()])
    a = map_window(raw).cash_flows[0]["cash_flow_id"]
    raw_b = await Source({None: [transfer()]}, identities=[("43", "43")] * 2).collect_trading_bills(
        START, END, expected_uid="43", expected_main_uid="43"
    )
    b = map_window(raw_b).cash_flows[0]["cash_flow_id"]
    assert a != b and ":42:trading:" in a and ":43:trading:" in b


async def test_mapping_and_nested_endpoints_are_immutable_and_repeated_rows_do_not_double_count():
    raw = await raw_window([transfer()])
    doubled = replace(raw, raw_bills=raw.raw_bills * 2)
    result = map_window(doubled)
    assert len(result.cash_flows) == 1 and len(result.raw_window.raw_bills) == 2
    with pytest.raises(FrozenInstanceError):
        result.unmapped = ()
    with pytest.raises(TypeError):
        result.cash_flows[0]["amount"] = "999"
    with pytest.raises(TypeError):
        result.cash_flows[0]["from"]["wallet_type"] = "other"


async def test_mapped_rows_enter_the_existing_single_v1_snapshot_builder_as_partial():
    result = map_window(await raw_window([transfer()]))
    facts = venue_ledger_snapshot_facts(
        snapshot_id=uuid4(),
        venue="OKX",
        sub_account=None,
        source="venue_api",
        watermark="engineering-raw-proof",
        coverage_from=START,
        observed_through=END,
        completeness={
            "balances_complete": True,
            "positions_complete": True,
            "fills_complete": True,
            "fees_complete": True,
            "cash_flows_complete": False,
        },
        balances=(),
        positions=(),
        fills=(),
        fees=(),
        cash_flows=result.cash_flows,
    )
    assert (
        facts[0]["cash_flows_count"] == 1
        and facts[0]["completeness"]["cash_flows_complete"] is False
    )
    assert facts[1]["cash_flows"][0]["amount"] == "100.000000000000000001"
    json.dumps(facts, allow_nan=False)


@pytest.mark.parametrize(
    "origin,mode", [("https://eea.okx.com", MODE), (ORIGIN, "live"), ("https://us.okx.com", "live")]
)
async def test_pure_scope_namespaces_are_distinct_and_stable_for_origin_and_mode(origin, mode):
    original = await raw_window([transfer()])
    scoped = replace(original, api_origin=origin, trading_mode=mode)
    a = okx_cash_flow_mapping.map_main_account_transfers(
        scoped, api_origin=origin, trading_mode=mode
    ).cash_flows[0]
    b = okx_cash_flow_mapping.map_main_account_transfers(
        scoped, api_origin=origin, trading_mode=mode
    ).cash_flows[0]
    assert a["cash_flow_id"] == b["cash_flow_id"]
    assert a["cash_flow_id"] != map_window(original).cash_flows[0]["cash_flow_id"]
    assert (
        a["cash_flow_id"]
        == f"okx:{mode}:{hashlib.sha256(origin.encode()).hexdigest()}:42:trading:9007199254740993"
    )


async def test_maximum_uid_and_bill_id_still_fit_the_canonical_identity_budget():
    uid, identity = "9" * 64, "8" * 64
    raw = await Source(
        {None: [transfer(identity)]}, identities=[(uid, uid)] * 2
    ).collect_trading_bills(START, END, expected_uid=uid, expected_main_uid=uid)
    value = map_window(raw).cash_flows[0]["cash_flow_id"]
    assert len(value.encode("utf-8")) == 214 and len(value.encode("utf-8")) <= 256
    assert f":{uid}:trading:{identity}" in value


async def test_redundant_long_zero_tails_are_trimmed_exactly_without_context_rounding():
    raw = await raw_window([transfer(balChg="1." + "0" * 1000, fee="0." + "0" * 1000)])
    with localcontext() as context:
        context.prec = 6
        flow = map_window(raw).cash_flows[0]
    assert flow["amount"] == "1" and flow["fee"] == "0"
