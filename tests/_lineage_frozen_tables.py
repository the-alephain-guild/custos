"""The order event contract as it was frozen, written out by hand.

The builders, the schema generator and the vector generator all read the rule
tables in ``custos.core.runner_fact``; a wrong cell there would flow into every
one of them and into their tests alike. These tables are typed in from the
frozen design instead, so the tests can hold the rule tables to them.
"""

from __future__ import annotations

EVENTS = (
    "initialized",
    "refused",
    "submitted",
    "updated",
    "rejected",
    "denied",
    "canceled",
    "expired",
    "modify_refused",
    "modify_rejected",
    "cancel_rejected",
    "fill_voided",
)

# One letter per event, in the order of EVENTS: R must, O may, - must not. A cell
# whose rule depends on other fields (attribution links, prices by order type,
# the multiplier of a requested quantity, requested values, commissions) is O.
_MATRIX = {
    "instrument": "RR---------R",
    "side": "RR----------",
    "order_type": "RR----------",
    "time_in_force": "RR----------",
    "reduce_only": "RR----------",
    "order_purpose": "RR----------",
    "execution_relation": "RR----------",
    "attribution_source": "RR----------",
    "signal_id": "OO----------",
    "unattributed_reason": "OO----------",
    "derived_signal_fact_id": "OO----------",
    "closes_signal_id": "OO----------",
    "parent_client_order_id": "OO----------",
    "retry_of_client_order_id": "OO----------",
    "replaces_client_order_id": "OO----------",
    "exec_spawn_id": "OO----------",
    "order_list_id": "OO----------",
    "venue_parent_order_id": "OO----------",
    "contingency_type": "OO----------",
    "quantity": "RR-R--------",
    "quantity_multiplier": "RR-R----OO-R",
    "limit_price": "OO-O--------",
    "trigger_price": "OO-O--------",
    "requested_values_known": "--------RR--",
    "requested_quantity": "--------OO--",
    "requested_limit_price": "--------OO--",
    "requested_trigger_price": "--------OO--",
    "reason_code": "-R--RR--RRR-",
    "last_trigger_price": "------OO----",
    "voided_venue_trade_id": "-----------R",
    "correction_id": "-----------R",
    "voided_quantity": "-----------R",
    "is_reopened": "-----------R",
    "from_reconciliation": "-----------R",
    "order_status_after": "-----------R",
    "leaves_quantity_after": "-----------R",
    "commission_voided": "-----------O",
    "commission_voided_currency": "-----------O",
    "fill_commission": "-----------O",
    "fill_commission_currency": "-----------O",
}
# A base quantity goes wherever its native quantity goes.
BASE_OF = {
    "quantity": "base_quantity",
    "requested_quantity": "requested_base_quantity",
    "voided_quantity": "voided_base_quantity",
    "leaves_quantity_after": "leaves_base_quantity_after",
}


def cell(event: str, field: str) -> str:
    """R, O or - for a field of an event, base quantities included."""

    native = {base: native for native, base in BASE_OF.items()}.get(field, field)
    return _MATRIX[native][EVENTS.index(event)]


INPUT_FIELDS = tuple(_MATRIX)
WIRE_FIELDS = (*INPUT_FIELDS, *BASE_OF.values())

# (limit_price, trigger_price) by order type, first on creation (initialized,
# refused), then on an update. R must, O may, - must not.
PRICES = {
    "market": (("-", "-"), ("-", "-")),
    "market_to_limit": (("-", "-"), ("O", "-")),
    "limit": (("R", "-"), ("O", "-")),
    "stop_market": (("-", "R"), ("-", "O")),
    "market_if_touched": (("-", "R"), ("-", "O")),
    "stop_limit": (("R", "R"), ("O", "O")),
    "limit_if_touched": (("R", "R"), ("O", "O")),
    "trailing_stop_market": (("-", "O"), ("-", "O")),
    "trailing_stop_limit": (("O", "O"), ("O", "O")),
}

# The capability shapes both sides accept, as which of the four optional flags
# they declare: terminal valuation, valuation checkpoint, order lineage, fill
# correction. Every other combination is refused.
ACCEPTED_CAPABILITY_SHAPES = {
    (False, False, False, False),
    (False, True, False, False),
    (True, True, False, False),
    (True, True, True, False),
    (True, True, False, True),
    (True, True, True, True),
}
CAPABILITY_FLAGS = (
    "terminal_valuation",
    "valuation_checkpoint",
    "order_lineage",
    "fill_correction",
)

# Fields that only come together, from the frozen rules: a native quantity and
# its base quantity; each commission amount with its currency. A requested
# quantity also needs the multiplier, but the multiplier does not need it.
PAIRED = (
    frozenset({"quantity", "base_quantity"}),
    frozenset({"voided_quantity", "voided_base_quantity"}),
    frozenset({"leaves_quantity_after", "leaves_base_quantity_after"}),
    frozenset({"requested_quantity", "requested_base_quantity"}),
    frozenset({"commission_voided", "commission_voided_currency"}),
    frozenset({"fill_commission", "fill_commission_currency"}),
)


def partners(field: str) -> frozenset[str]:
    """The field and every field that must come with it."""

    group = {field}
    for pair in PAIRED:
        if field in pair:
            group |= pair
    if field in {"requested_quantity", "requested_base_quantity"}:
        group.add("quantity_multiplier")
    return frozenset(group)
