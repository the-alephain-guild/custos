"""The builders of RunnerOrderEventFact.v1 and RunnerFillCorrectionFact.v1.

Every rule is tried on a fact that is otherwise valid, so a refusal can only come
from the rule under test, and every rule has an accepted case first. Which cells
of the field and price tables to try comes from the frozen tables typed in by
hand, not from the rule tables the builders read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from custos.core.runner_fact import (
    FILL_CORRECTION_KIND,
    ORDER_EVENT_FIELDS,
    ORDER_EVENT_KIND,
    ORDER_EVENTS,
    ORDER_PRICE_RULES,
    RunnerFactAuthority,
    RunnerFactContractError,
    fill_correction_fact,
    order_event_fact,
    runner_fact_event_id,
)
from tests import _lineage_frozen_tables as frozen

AUTHORITY = RunnerFactAuthority(
    tenant_id="acme",
    trading_mode="testnet",
    runner_id=UUID("10000000-0000-4000-8000-000000000001"),
    deployment_instance_id=UUID("20000000-0000-4000-8000-000000000002"),
    deployment_spec_id=UUID("30000000-0000-4000-8000-000000000003"),
    deployment_spec_digest="a" * 64,
    generation=1,
    strategy_id=UUID("40000000-0000-4000-8000-000000000004"),
    capability_version_id=UUID("50000000-0000-4000-8000-000000000005"),
    capability_version=1,
    capability_manifest_digest="b" * 64,
)
SOURCE = "c0000000-0000-4000-8000-0000000000aa"
SIGNAL = "d0000000-0000-4000-8000-0000000000bb"

# One valid value for every field an order event can take.
SAMPLE: dict[str, Any] = {
    "instrument": "BTCUSDT-PERP.BINANCE",
    "side": "buy",
    "order_type": "limit",
    "time_in_force": "gtc",
    "reduce_only": False,
    "order_purpose": "entry",
    "execution_relation": "original",
    "attribution_source": "order_tag",
    "signal_id": SIGNAL,
    "unattributed_reason": "missing_signal_tag",
    "derived_signal_fact_id": "e0000000-0000-4000-8000-0000000000cc",
    "closes_signal_id": "f0000000-0000-4000-8000-0000000000dd",
    "parent_client_order_id": "O-0",
    "retry_of_client_order_id": "O-00",
    "replaces_client_order_id": "O-000",
    "exec_spawn_id": "O-SPAWN",
    "order_list_id": "OL-1",
    "venue_parent_order_id": "VP-1",
    "contingency_type": "oco",
    "quantity": "100",
    "quantity_multiplier": "0.01",
    "limit_price": "60000",
    "trigger_price": "59000",
    "requested_values_known": True,
    "requested_quantity": "50",
    "requested_limit_price": "61000",
    "requested_trigger_price": "58000",
    "reason_code": "price_band",
    "last_trigger_price": "58500",
    "voided_venue_trade_id": "T-1",
    "correction_id": "C-1",
    "voided_quantity": "30",
    "is_reopened": True,
    "from_reconciliation": False,
    "order_status_after": "partially_filled",
    "leaves_quantity_after": "40",
    "commission_voided": "0.3",
    "commission_voided_currency": "USDT",
    "fill_commission": "0.6",
    "fill_commission_currency": "USDT",
}

# The smallest valid input of each event.
MINIMAL: dict[str, dict[str, Any]] = {
    "initialized": {
        name: SAMPLE[name]
        for name in (
            "instrument",
            "side",
            "order_type",
            "time_in_force",
            "reduce_only",
            "order_purpose",
            "execution_relation",
            "attribution_source",
            "signal_id",
            "quantity",
            "quantity_multiplier",
            "limit_price",
        )
    },
    "submitted": {},
    "updated": {"quantity": "80", "quantity_multiplier": "0.01"},
    "rejected": {"reason_code": "insufficient_margin"},
    "denied": {"reason_code": "risk_denied"},
    "canceled": {},
    "expired": {},
    "modify_refused": {
        "requested_values_known": True,
        "requested_quantity": "50",
        "quantity_multiplier": "0.01",
        "reason_code": "runner_cap_exceeded",
    },
    "modify_rejected": {"requested_values_known": False, "reason_code": "venue_rejected"},
    "cancel_rejected": {"reason_code": "order_not_found"},
    "fill_voided": {
        name: SAMPLE[name]
        for name in (
            "instrument",
            "voided_venue_trade_id",
            "correction_id",
            "voided_quantity",
            "quantity_multiplier",
            "is_reopened",
            "from_reconciliation",
            "order_status_after",
            "leaves_quantity_after",
        )
    },
}
MINIMAL["refused"] = {**MINIMAL["initialized"], "reason_code": "runner_cap_exceeded"}


def _order(event: str, **fields: Any) -> dict[str, Any]:
    return order_event_fact(
        AUTHORITY,
        order_event=event,
        client_order_id="O-1",
        source_event_id=SOURCE,
        occurred_at="2026-10-19T08:00:00.123456Z",
        **fields,
    )


def _with(event: str, **changes: Any) -> dict[str, Any]:
    fields = {**MINIMAL[event], **changes}
    return {name: value for name, value in fields.items() if value is not None}


def test_the_matrix_names_every_event_and_only_known_fields() -> None:
    assert set(ORDER_EVENT_FIELDS) == set(ORDER_EVENTS) == set(MINIMAL)
    for required, optional in ORDER_EVENT_FIELDS.values():
        assert not required & optional
        assert required | optional <= set(SAMPLE)


@pytest.mark.parametrize("event", ORDER_EVENTS)
def test_each_event_builds_from_its_smallest_input(event: str) -> None:
    fact = _order(event, **MINIMAL[event])
    assert fact["kind"] == ORDER_EVENT_KIND
    assert fact["order_event"] == event
    assert fact["event_id"] == str(
        runner_fact_event_id(AUTHORITY.stream_key, "order_event", "O-1", event, SOURCE)
    )
    assert None not in fact.values()


def test_the_rule_tables_are_the_frozen_tables() -> None:
    assert sorted(ORDER_EVENTS) == sorted(frozen.EVENTS)
    for event, (required, optional) in ORDER_EVENT_FIELDS.items():
        assert required == {f for f in frozen.INPUT_FIELDS if frozen.cell(event, f) == "R"}, event
        assert optional == {f for f in frozen.INPUT_FIELDS if frozen.cell(event, f) == "O"}, event
    assert set(SAMPLE) == set(frozen.INPUT_FIELDS)
    for order_type, (creation, update) in frozen.PRICES.items():
        rule = {"R": "required", "O": "optional", "-": "absent"}
        assert ORDER_PRICE_RULES[order_type] == tuple(rule[c] for c in (*creation, *update))
    assert set(ORDER_PRICE_RULES) == set(frozen.PRICES)


FORBIDDEN = [
    (event, field)
    for event in frozen.EVENTS
    for field in frozen.INPUT_FIELDS
    if frozen.cell(event, field) == "-"
]


@pytest.mark.parametrize(("event", "field"), FORBIDDEN, ids=[f"{e}-{f}" for e, f in FORBIDDEN])
def test_a_field_outside_an_events_column_is_refused(event: str, field: str) -> None:
    with pytest.raises(RunnerFactContractError, match="does not carry"):
        _order(event, **_with(event, **{field: SAMPLE[field]}))


REQUIRED = [
    (event, field)
    for event in frozen.EVENTS
    for field in frozen.INPUT_FIELDS
    if frozen.cell(event, field) == "R"
]


@pytest.mark.parametrize(("event", "field"), REQUIRED, ids=[f"{e}-{f}" for e, f in REQUIRED])
def test_a_required_field_left_out_is_refused(event: str, field: str) -> None:
    with pytest.raises(RunnerFactContractError, match="needs"):
        _order(event, **_with(event, **{field: None}))


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(RunnerFactContractError, match="not RunnerOrderEventFact.v1 fields"):
        _order("submitted", venue_reason="free text")


# The creation columns of the price table, tried on both events that create an order.
_RULE = {"R": "required", "O": "optional", "-": "absent"}
PRICE_CASES = [
    (event, order_type, _RULE[limit], _RULE[trigger])
    for event in ("initialized", "refused")
    for order_type, ((limit, trigger), _) in frozen.PRICES.items()
]


@pytest.mark.parametrize(
    ("event", "order_type", "limit", "trigger"),
    PRICE_CASES,
    ids=[f"{e}-{t}" for e, t, _, _ in PRICE_CASES],
)
def test_creation_prices_follow_the_order_type(
    event: str, order_type: str, limit: str, trigger: str
) -> None:
    def prices(with_limit: bool, with_trigger: bool) -> dict[str, Any]:
        return _with(
            event,
            order_type=order_type,
            limit_price="60000" if with_limit else None,
            trigger_price="59000" if with_trigger else None,
        )

    accepted = prices(limit != "absent", trigger != "absent")
    assert _order(event, **accepted)["order_type"] == order_type
    for field, rule, flip in (
        ("limit_price", limit, prices(limit == "absent", trigger != "absent")),
        ("trigger_price", trigger, prices(limit != "absent", trigger == "absent")),
    ):
        if rule == "optional":
            _order(event, **flip)
        else:
            with pytest.raises(RunnerFactContractError, match=field):
                _order(event, **flip)


def test_an_update_carries_prices_without_naming_its_type() -> None:
    fact = _order("updated", **_with("updated", limit_price="61000", trigger_price="59500"))
    assert (fact["limit_price"], fact["trigger_price"]) == ("61000", "59500")
    assert "order_type" not in fact


ATTRIBUTION = [
    ("order_tag", "entry", {"signal_id": SIGNAL}),
    ("derived", "entry", {"derived_signal_fact_id": SAMPLE["derived_signal_fact_id"]}),
    ("derived", "reduce", {"derived_signal_fact_id": SAMPLE["derived_signal_fact_id"]}),
    ("derived", "protective_stop", {"unattributed_reason": "legacy_without_signal"}),
    ("derived", "take_profit", {"unattributed_reason": "legacy_without_signal"}),
    ("none", "flatten", {"unattributed_reason": "runner_shutdown_flatten"}),
]
LINKS = ("signal_id", "derived_signal_fact_id", "unattributed_reason")


@pytest.mark.parametrize(("source", "purpose", "links"), ATTRIBUTION)
@pytest.mark.parametrize("event", ["initialized", "refused"])
def test_attribution_carries_exactly_its_own_link(
    event: str, source: str, purpose: str, links: dict[str, Any]
) -> None:
    base = {**MINIMAL[event], "attribution_source": source, "order_purpose": purpose}
    for link in LINKS:
        base.pop(link, None)
    fact = _order(event, **base, **links)
    assert {link: fact[link] for link in LINKS if link in fact} == links
    with pytest.raises(RunnerFactContractError):
        _order(event, **base)
    for link in set(LINKS) - set(links):
        with pytest.raises(RunnerFactContractError):
            _order(event, **base, **links, **{link: SAMPLE[link]})


def test_a_protective_order_under_a_derived_signal_is_a_legacy_order() -> None:
    base = {
        **_with("initialized", signal_id=None),
        "attribution_source": "derived",
        "order_purpose": "protective_stop",
    }
    with pytest.raises(RunnerFactContractError, match="legacy_without_signal"):
        _order("initialized", **base, unattributed_reason="missing_signal_tag")


@pytest.mark.parametrize("event", ["modify_refused", "modify_rejected"])
def test_a_known_request_names_what_it_moved(event: str) -> None:
    known = {"requested_values_known": True, "reason_code": "price_band"}
    price_only = _order(event, **known, requested_limit_price="61000")
    assert "requested_quantity" not in price_only
    assert "quantity_multiplier" not in price_only
    sized = _order(event, **known, requested_quantity="50", quantity_multiplier="0.01")
    assert sized["requested_base_quantity"] == "0.5"
    with pytest.raises(RunnerFactContractError, match="at least one"):
        _order(event, **known)
    with pytest.raises(RunnerFactContractError, match="quantity_multiplier"):
        _order(event, **known, requested_quantity="50")
    with pytest.raises(RunnerFactContractError, match="quantity_multiplier"):
        _order(event, **known, requested_limit_price="61000", quantity_multiplier="0.01")


@pytest.mark.parametrize("event", ["modify_refused", "modify_rejected"])
def test_an_unknown_request_carries_no_requested_value(event: str) -> None:
    unknown = {"requested_values_known": False, "reason_code": "price_band"}
    assert "requested_quantity" not in _order(event, **unknown)
    for field in ("requested_quantity", "requested_limit_price", "requested_trigger_price"):
        with pytest.raises(RunnerFactContractError, match="unknown request"):
            _order(event, **unknown, **{field: SAMPLE[field]})
    with pytest.raises(RunnerFactContractError, match="unknown request"):
        _order(event, **unknown, quantity_multiplier="0.01")


@pytest.mark.parametrize("reason", ["Price band", "price-band", "", "a" * 65, "9lives"])
def test_a_reason_code_is_a_code_not_text(reason: str) -> None:
    with pytest.raises(RunnerFactContractError, match="normalised code"):
        _order("rejected", reason_code=reason)


def test_every_base_quantity_is_native_times_multiplier() -> None:
    created = _order("initialized", **_with("initialized", quantity="100"))
    assert (created["quantity"], created["base_quantity"]) == ("100", "1")
    voided = _order("fill_voided", **MINIMAL["fill_voided"])
    assert voided["voided_base_quantity"] == "0.3"
    assert voided["leaves_base_quantity_after"] == "0.4"


def test_a_product_the_consumer_cannot_hold_is_refused_not_rounded() -> None:
    # 9100000000000000000000000001.3 has a 29-digit mantissa past 2**96 - 1. In
    # the default 28-digit context the product rounds to a value that fits.
    with pytest.raises(RunnerFactContractError, match="base_quantity"):
        _order(
            "initialized",
            **_with(
                "initialized", quantity="7000000000000000000000000001", quantity_multiplier="1.3"
            ),
        )


def test_an_exact_product_outside_the_scale_is_refused() -> None:
    with pytest.raises(RunnerFactContractError, match="base_quantity"):
        _order(
            "initialized",
            **_with("initialized", quantity="0." + "0" * 27 + "1", quantity_multiplier="0.1"),
        )


def test_a_void_must_move_some_quantity() -> None:
    with pytest.raises(RunnerFactContractError, match="voided_quantity"):
        _order("fill_voided", **_with("fill_voided", voided_quantity="0"))


def test_a_voided_order_has_nothing_left() -> None:
    fact = _order(
        "fill_voided",
        **_with("fill_voided", order_status_after="voided", leaves_quantity_after="0"),
    )
    assert fact["leaves_base_quantity_after"] == "0"
    with pytest.raises(RunnerFactContractError, match="nothing left"):
        _order("fill_voided", **_with("fill_voided", order_status_after="voided"))


@pytest.mark.parametrize("status", ["filled", "accepted", "triggered"])
def test_a_void_never_leaves_a_filled_or_engine_only_state(status: str) -> None:
    with pytest.raises(RunnerFactContractError, match="order_status_after"):
        _order("fill_voided", **_with("fill_voided", order_status_after=status))


def _fee(voided: str | None, charged: str | None, currency: str = "USDT") -> dict[str, Any]:
    return {
        "commission_voided": voided,
        "commission_voided_currency": currency if voided is not None else None,
        "fill_commission": charged,
        "fill_commission_currency": "USDT" if charged is not None else None,
    }


ACCEPTED_FEES = [
    ("no commission at all", None, None),
    ("nothing charged, zero reversed", "0", None),
    ("charged, nothing reversed yet", None, "0.6"),
    ("part of a charge reversed", "0.3", "0.6"),
    ("all of a charge reversed", "0.6", "0.6"),
    ("part of a rebate reversed", "-0.1", "-0.2"),
    ("a zero charge reversed by zero", "0", "0"),
    # 29 significant digits: more than the default decimal context keeps.
    (
        "all of a charge, 29 digits",
        "1.0000000000000000000000000001",
        "1.0000000000000000000000000001",
    ),
]
REFUSED_FEES = [
    ("nothing charged, something reversed", "0.1", None, "USDT"),
    ("reversal in another currency", "0.3", "0.6", "USDC"),
    ("charge reversed as a rebate", "-0.3", "0.6", "USDT"),
    ("rebate reversed as a charge", "0.1", "-0.2", "USDT"),
    ("a charge reversed by zero", "0", "0.6", "USDT"),
    ("a rebate reversed by zero", "0", "-0.2", "USDT"),
    ("a zero charge reversed by something", "0.1", "0", "USDT"),
    ("more than the charge", "0.7", "0.6", "USDT"),
    ("more than the rebate", "-0.3", "-0.2", "USDT"),
    # One unit in the 29th digit, which abs() in a 28-digit context rounds away.
    (
        "more than the charge, 29 digits",
        "1.0000000000000000000000000002",
        "1.0000000000000000000000000001",
        "USDT",
    ),
    (
        "more than the rebate, 29 digits",
        "-1.0000000000000000000000000002",
        "-1.0000000000000000000000000001",
        "USDT",
    ),
]


@pytest.mark.parametrize(("case", "voided", "charged"), ACCEPTED_FEES)
def test_a_commission_correction_the_engine_takes_is_built(
    case: str, voided: str | None, charged: str | None
) -> None:
    fields = _with("fill_voided", **_fee(voided, charged))
    _order("fill_voided", **fields)
    fill_correction_fact(AUTHORITY, **_correction(**_fee(voided, charged)))


@pytest.mark.parametrize(("case", "voided", "charged", "currency"), REFUSED_FEES)
def test_a_commission_correction_the_engine_refuses_is_refused(
    case: str, voided: str, charged: str | None, currency: str
) -> None:
    fee = _fee(voided, charged, currency)
    with pytest.raises(RunnerFactContractError, match="commission"):
        _order("fill_voided", **_with("fill_voided", **fee))
    with pytest.raises(RunnerFactContractError, match="commission"):
        fill_correction_fact(AUTHORITY, **_correction(**fee))


@pytest.mark.parametrize(
    "half",
    [
        "commission_voided",
        "commission_voided_currency",
        "fill_commission",
        "fill_commission_currency",
    ],
)
def test_a_commission_amount_and_its_currency_come_together(half: str) -> None:
    fee = {**_fee("0.3", "0.6"), half: None}
    with pytest.raises(RunnerFactContractError, match="come together"):
        _order("fill_voided", **_with("fill_voided", **fee))


def test_a_native_quantity_without_its_multiplier_is_a_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The request rule normally refuses this first; without it, the base
    # quantity must still not be computed, and the refusal must survive python -O.
    import custos.core.runner_fact as runner_fact

    monkeypatch.setattr(runner_fact, "_require_order_request", lambda fields: None)
    fields = _with("modify_refused", quantity_multiplier=None)
    with pytest.raises(
        RunnerFactContractError, match="requested_quantity needs quantity_multiplier"
    ):
        _order("modify_refused", **fields)


def _correction(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "fill_id": "a0000000-0000-4000-8000-000000000011",
        "venue": "BINANCE",
        "voided_venue_trade_id": "T-1",
        "instrument": "BTCUSDT-PERP.BINANCE",
        "order_side": "buy",
        "last_px": "60000",
        "source_event_id": SOURCE,
        "correction_id": "C-1",
        "voided_quantity": "30",
        "quantity_multiplier": "0.01",
        "is_reopened": True,
        "from_reconciliation": False,
        "occurred_at": "2026-10-19T08:00:01Z",
    }
    fields.update(changes)
    return {name: value for name, value in fields.items() if value is not None}


def test_a_correction_carries_the_void_in_the_same_units_as_the_order_event() -> None:
    correction = fill_correction_fact(AUTHORITY, **_correction(**_fee("0.3", "0.6")))
    void = _order("fill_voided", **_with("fill_voided", **_fee("0.3", "0.6")))
    assert correction["kind"] == FILL_CORRECTION_KIND
    for field in (
        "voided_venue_trade_id",
        "instrument",
        "correction_id",
        "voided_quantity",
        "voided_base_quantity",
        "quantity_multiplier",
        "is_reopened",
        "from_reconciliation",
        "source_event_id",
        "commission_voided",
        "commission_voided_currency",
        "fill_commission",
        "fill_commission_currency",
    ):
        assert correction[field] == void[field], field
    assert correction["voided_base_quantity"] == "0.3"


def test_a_correction_and_its_order_event_never_share_an_event_id() -> None:
    correction = fill_correction_fact(AUTHORITY, **_correction())
    void = _order("fill_voided", **MINIMAL["fill_voided"])
    assert correction["event_id"] != void["event_id"]
    assert correction["event_id"] == str(
        runner_fact_event_id(
            AUTHORITY.stream_key,
            "fill_correction",
            "BINANCE",
            "BTCUSDT-PERP.BINANCE",
            "T-1",
            SOURCE,
        )
    )


def test_each_engine_void_of_one_correction_is_its_own_fact() -> None:
    first = fill_correction_fact(AUTHORITY, **_correction())
    replayed = fill_correction_fact(AUTHORITY, **_correction())
    completed = fill_correction_fact(
        AUTHORITY,
        **_correction(source_event_id="c0000000-0000-4000-8000-0000000000ab", **_fee("0.3", "0.6")),
    )
    assert first["event_id"] == replayed["event_id"]
    assert completed["event_id"] != first["event_id"]
    assert completed["correction_id"] == first["correction_id"]


def test_a_synthetic_void_trade_id_is_refused_as_defence_in_depth() -> None:
    # Not the rule that decides whether to sign: any trade id yields a fill_id,
    # so whether the fill was signed is the caller's check.
    with pytest.raises(RunnerFactContractError, match="synthetic void"):
        fill_correction_fact(AUTHORITY, **_correction(voided_venue_trade_id="VOID-V-1"))
    assert _order(
        "fill_voided", **_with("fill_voided", voided_venue_trade_id="VOID-V-1", is_reopened=False)
    )


def test_a_correction_names_a_position_lifecycle_or_nothing() -> None:
    assert "position_id" not in fill_correction_fact(AUTHORITY, **_correction())
    lifecycle = "a0000000-0000-5000-8000-000000000022"
    assert (
        fill_correction_fact(AUTHORITY, **_correction(position_id=lifecycle))["position_id"]
        == lifecycle
    )
    with pytest.raises(RunnerFactContractError, match="position_id"):
        fill_correction_fact(AUTHORITY, **_correction(position_id="BTCUSDT-PERP.BINANCE-NETTING"))


@pytest.mark.parametrize(
    ("given", "written"),
    [
        ("C0000000-0000-4000-8000-0000000000AA", SOURCE),
        ("c00000000000400080000000000000aa", SOURCE),
        ("{c0000000-0000-4000-8000-0000000000aa}", SOURCE),
    ],
)
def test_a_uuid_is_written_in_the_one_wire_spelling(given: str, written: str) -> None:
    fact = order_event_fact(
        AUTHORITY,
        order_event="submitted",
        client_order_id="O-1",
        source_event_id=given,
        occurred_at="2026-10-19T08:00:00Z",
    )
    assert fact["source_event_id"] == written


@pytest.mark.parametrize(
    "given",
    [
        "00000000-0000-0000-0000-000000000000",
        "c0000000-0000-0000-8000-0000000000aa",
        "c0000000-0000-9000-8000-0000000000aa",
        "c0000000-0000-4000-c000-0000000000aa",
        "c0000000-0000-4000-8000-0000000000a",
        "c0000000-0000-4000-8000-0000000000aaa",
    ],
)
def test_a_uuid_the_consumer_cannot_parse_is_refused(given: str) -> None:
    with pytest.raises(RunnerFactContractError, match="source_event_id"):
        order_event_fact(
            AUTHORITY,
            order_event="submitted",
            client_order_id="O-1",
            source_event_id=given,
            occurred_at="2026-10-19T08:00:00Z",
        )


@pytest.mark.parametrize(
    ("instrument", "accepted"),
    [
        ("I" * 127, True),
        ("I" * 128, True),
        ("I" * 129, False),
        # 50 characters, 150 UTF-8 bytes: within a character bound, past the byte bound.
        ("€" * 50, False),
        ("€" * 42 + "II", True),
    ],
)
def test_instrument_is_bounded_in_utf8_bytes(instrument: str, accepted: bool) -> None:
    fields = _with("fill_voided", instrument=instrument)
    if accepted:
        assert _order("fill_voided", **fields)["instrument"] == instrument
    else:
        with pytest.raises(RunnerFactContractError, match="instrument"):
            _order("fill_voided", **fields)
