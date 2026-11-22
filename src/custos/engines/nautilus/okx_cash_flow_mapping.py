"""Restricted interpretation of raw evidence; this does not authorize an account."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from custos.core.runner_fact import (
    SUPPORTED_CURRENCIES,
    RunnerFactContractError,
    _cash_flow,
    _signed_decimal,
)
from custos.engines.nautilus.ledger_http import (
    VenueLedgerError,
    _reject_constant,
    _unique_object,
    decimal,
)
from custos.engines.nautilus.okx_bill_window import (
    OkxTradingBillWindow,
    _bill,
    _bounds,
    _identity,
    _time,
)

MAPPING_PROFILE = "okx-main-trading-zero-fee-internal-transfer-v1"
MAX_MAPPING_MONEY_TEXT = 4096
_ORIGINS = frozenset({"https://openapi.okx.com", "https://eea.okx.com", "https://us.okx.com"})


@dataclass(frozen=True, slots=True)
class UnmappedOkxBill:
    bill_id: str
    raw_bill: str
    reason: str


@dataclass(frozen=True, slots=True)
class OkxCashFlowMapping:
    raw_window: OkxTradingBillWindow
    cash_flows: tuple[Mapping[str, Any], ...]
    unmapped: tuple[UnmappedOkxBill, ...]

    @property
    def profile(self) -> str:
        return MAPPING_PROFILE

    @property
    def cash_flows_complete(self) -> Literal[False]:
        return False


def _freeze_flow(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            **value,
            "from": MappingProxyType(value["from"]),
            "to": MappingProxyType(value["to"]),
        }
    )


def _bounded_wire_input(value: Decimal, field: str) -> Decimal:
    if value.is_zero():
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    end = len(digits)
    while end > 1 and digits[end - 1] == 0:
        end -= 1
        exponent += 1
    digits = digits[:end]
    if exponent < -28 or len(digits) + max(exponent, 0) > 29:
        raise VenueLedgerError(f"OKX transfer {field} exceeds the cash-flow wire range")
    # Trim exactly before the shared renderer converts digits or expands powers.
    # The shared wire validator still decides the precise 96-bit limit.
    return Decimal((sign, digits, exponent))


def map_main_account_transfers(
    window: OkxTradingBillWindow, *, api_origin: str, trading_mode: str
) -> OkxCashFlowMapping:
    """Map only proved row semantics; runtime authorization belongs to the caller."""
    if not isinstance(window, OkxTradingBillWindow):
        raise VenueLedgerError("OKX transfer mapping requires a trading bill window")
    if (
        api_origin not in _ORIGINS
        or trading_mode not in {"testnet", "live"}
        or window.api_origin != api_origin
        or window.trading_mode != trading_mode
    ):
        raise VenueLedgerError("OKX transfer mapping differs from its source scope")
    if _identity(window.uid) != _identity(window.main_uid):
        raise VenueLedgerError("OKX transfer mapping supports a proved main account only")
    start, end, _, _ = _bounds(window.coverage_from, window.closed_at)
    flows = []
    unmapped = []
    seen: dict[str, str] = {}
    for raw in window.raw_bills:
        try:
            row = json.loads(
                raw,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (TypeError, ValueError, RecursionError) as error:
            raise VenueLedgerError("OKX transfer mapping has invalid raw JSON") from error
        if not isinstance(row, dict):
            raise VenueLedgerError("OKX transfer mapping raw bill must be an object")
        if any(
            isinstance(row.get(field), str) and len(row[field]) > MAX_MAPPING_MONEY_TEXT
            for field in ("fee", "balChg")
        ):
            raise VenueLedgerError("OKX transfer numeric text exceeds the mapping limit")
        identity, canonical = _bill(row, start, end)
        if identity in seen:
            if seen[identity] != canonical:
                raise VenueLedgerError("OKX transfer bill identity changed within one mapping")
            continue
        seen[identity] = canonical
        if row["type"] != "1":
            unmapped.append(UnmappedOkxBill(identity, raw, "unsupported_bill_type"))
            continue
        if row["subType"] not in {"11", "12"}:
            unmapped.append(UnmappedOkxBill(identity, raw, "unsupported_transfer_subtype"))
            continue
        source, target, positive = (
            ("6", "18", True) if row["subType"] == "11" else ("18", "6", False)
        )
        if (row.get("from"), row.get("to")) != (source, target):
            raise VenueLedgerError("OKX transfer endpoints contradict its declared direction")
        change = decimal(row["balChg"], "transfer balance change")
        if not change or (change > 0) != positive:
            raise VenueLedgerError("OKX transfer balance sign contradicts its declared direction")
        if row["fee"] == "":
            unmapped.append(UnmappedOkxBill(identity, raw, "fee_unavailable"))
            continue
        fee = decimal(row["fee"], "transfer fee")
        fee = _bounded_wire_input(fee, "fee")
        try:
            _signed_decimal(fee, "transfer fee", bounded=True)
        except RunnerFactContractError as error:
            raise VenueLedgerError("OKX transfer fee exceeds the wire range") from error
        if fee:
            unmapped.append(UnmappedOkxBill(identity, raw, "nonzero_fee_unsupported"))
            continue
        if row["ccy"] not in SUPPORTED_CURRENCIES:
            unmapped.append(UnmappedOkxBill(identity, raw, "unsupported_currency"))
            continue
        wallets = {"6": "funding", "18": "trading"}
        try:
            flow = _cash_flow(
                {
                    "cash_flow_id": f"okx:{trading_mode}:{hashlib.sha256(api_origin.encode()).hexdigest()}:{window.uid}:trading:{identity}",
                    "kind": "internal_transfer",
                    "from": {"wallet_type": wallets[source], "sub_account": None},
                    "to": {"wallet_type": wallets[target], "sub_account": None},
                    "counterparty_uid": None,
                    "currency": row["ccy"],
                    "amount": _bounded_wire_input(change.copy_abs(), "amount"),
                    "fee": "0",
                    "fee_currency": row["ccy"],
                    "occurred_at": _time(row["ts"]),
                    "external_reference": None,
                    "destination": None,
                }
            )
        except RunnerFactContractError as error:
            raise VenueLedgerError("OKX transfer cannot fit the cash-flow wire") from error
        flows.append(_freeze_flow(flow))
    flows.sort(key=lambda value: (value["occurred_at"], value["cash_flow_id"]))
    return OkxCashFlowMapping(window, tuple(flows), tuple(unmapped))
