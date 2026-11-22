"""No unknown wallet, amount, identity or fee becomes a canonical transfer."""

import json
from dataclasses import replace

import pytest

from custos.engines.nautilus import okx_cash_flow_mapping
from custos.engines.nautilus.ledger_http import VenueLedgerError
from tests.engines.nautilus.test_okx_cash_flow_mapping import (
    MODE,
    ORIGIN,
    map_window,
    raw_window,
    transfer,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"uid": "43"},
        {"main_uid": "7"},
        {"uid": "0", "main_uid": "0"},
        {"uid": "9" * 65, "main_uid": "9" * 65},
        {"uid": "not-decimal", "main_uid": "not-decimal"},
        {"api_origin": "https://elsewhere.invalid"},
        {"trading_mode": "live"},
    ],
)
async def test_raw_window_must_match_the_mapping_scope_and_a_real_main_uid(changes):
    raw = await raw_window([transfer()])
    with pytest.raises(VenueLedgerError):
        map_window(replace(raw, **changes))


@pytest.mark.parametrize(
    "origin,mode",
    [
        ("http://openapi.okx.com", MODE),
        ("https://elsewhere.invalid", MODE),
        (ORIGIN, "sandbox"),
        (ORIGIN, "future"),
    ],
)
async def test_unrecognized_origin_or_mode_is_not_an_authority_override(origin, mode):
    raw = await raw_window([transfer()])
    with pytest.raises(VenueLedgerError):
        okx_cash_flow_mapping.map_main_account_transfers(raw, api_origin=origin, trading_mode=mode)


@pytest.mark.parametrize(
    "changes",
    [
        {"subType": "12"},
        {"from": "18"},
        {"to": "6"},
        {"from": "6", "to": "6"},
        {"from": ""},
        {"to": "future"},
        {"balChg": "-1"},
        {"balChg": "0"},
        {"subType": "12", "from": "18", "to": "6", "balChg": "1"},
    ],
)
async def test_candidate_direction_type_and_sign_contradictions_are_refused(changes):
    raw = await raw_window([transfer(**changes)])
    with pytest.raises(VenueLedgerError, match="direction"):
        map_window(raw)


@pytest.mark.parametrize(
    "amount", ["79228162514264337593543950336", "1e29", "0.00000000000000000000000000001"]
)
async def test_amount_overflow_is_refused_not_rounded_into_the_contract(amount):
    raw = await raw_window([transfer(balChg=amount)])
    with pytest.raises(VenueLedgerError, match="cash-flow wire"):
        map_window(raw)


@pytest.mark.parametrize(
    "fee", ["79228162514264337593543950336", "1e29", "0.00000000000000000000000000001"]
)
async def test_provided_fee_overflow_is_refused_before_the_nonzero_fee_branch(fee):
    raw = await raw_window([transfer(fee=fee)])
    with pytest.raises(VenueLedgerError, match="fee exceeds"):
        map_window(raw)


@pytest.mark.parametrize(
    "field,value", [("fee", "NaN"), ("fee", "Infinity"), ("balChg", "NaN"), ("balChg", "-Infinity")]
)
async def test_illegal_raw_money_cannot_become_a_supported_mapping(field, value):
    raw = await raw_window([transfer()])
    row = json.loads(raw.raw_bills[0])
    row[field] = value
    bad = replace(raw, raw_bills=(json.dumps(row),))
    with pytest.raises(VenueLedgerError):
        map_window(bad)


async def test_same_bill_id_changed_content_refuses_the_whole_mapping():
    raw = await raw_window([transfer()])
    row = json.loads(raw.raw_bills[0])
    row["balChg"] = "9"
    bad = replace(raw, raw_bills=(*raw.raw_bills, json.dumps(row)))
    with pytest.raises(VenueLedgerError, match="identity changed"):
        map_window(bad)


@pytest.mark.parametrize("raw_text", ["not-json", "[]", "null", '{"billId":"1","billId":"2"}'])
async def test_direct_calls_do_not_bypass_raw_structure_or_duplicate_key_checks(raw_text):
    raw = await raw_window([transfer()])
    with pytest.raises(VenueLedgerError):
        map_window(replace(raw, raw_bills=(raw_text,)))


async def test_untyped_input_cannot_pose_as_a_trading_window():
    with pytest.raises(VenueLedgerError, match="trading bill window"):
        map_window({"uid": "42", "main_uid": "42"})


@pytest.mark.parametrize("field,entry", [("fee", "_signed_decimal"), ("balChg", "_cash_flow")])
async def test_extreme_exponents_are_rejected_before_unbounded_wire_arithmetic(
    monkeypatch, field, entry
):
    raw = await raw_window([transfer(**{field: "1e1000000000"})])

    def must_not_reach_wire(*_args, **_kwargs):
        raise AssertionError("unbounded exponent reached common wire arithmetic")

    monkeypatch.setattr(okx_cash_flow_mapping, entry, must_not_reach_wire)
    with pytest.raises(VenueLedgerError, match="wire"):
        map_window(raw)


@pytest.mark.parametrize("identity", ["1" * 65, "not-decimal", "01"])
async def test_out_of_profile_bill_identity_is_rejected_by_the_original_raw_collector(identity):
    with pytest.raises(VenueLedgerError, match="identity"):
        await raw_window([transfer(identity)])


async def test_missing_fee_is_rejected_by_the_original_raw_collector_not_filled_in():
    row = transfer()
    row.pop("fee")
    with pytest.raises(VenueLedgerError, match="fee field is missing"):
        await raw_window([row])


@pytest.mark.parametrize("field", ["fee", "balChg"])
async def test_overlong_numeric_source_text_is_refused_before_the_shared_raw_parser(field):
    raw = await raw_window([transfer()])
    row = json.loads(raw.raw_bills[0])
    row[field] = "1." + "0" * 5000
    with pytest.raises(VenueLedgerError, match="numeric text exceeds"):
        map_window(replace(raw, raw_bills=(json.dumps(row),)))
