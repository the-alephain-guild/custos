"""Venue ledger snapshots carry cash flows and wallet scope in the V1 contract."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from custos.core.runner_fact import (
    MAX_VENUE_LEDGER_ITEMS_PER_CHUNK,
    RunnerFactContractError,
    venue_ledger_snapshot_facts,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs/gateway-contract/v1/runner_fact_batch_v1.schema.json"
GOLDEN_PATH = ROOT / "docs/authority/runner-fact-golden-v1.json"

SNAPSHOT_ID = UUID("72000000-0000-4000-8000-000000000001")
COMPLETE = {
    "balances_complete": True,
    "positions_complete": True,
    "fills_complete": True,
    "fees_complete": True,
    "cash_flows_complete": True,
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _balance(
    asset: str = "USDT",
    *,
    wallet_type: str = "spot",
    sub_account: str | None = None,
    total: str = "10",
) -> dict[str, Any]:
    return {
        "wallet_type": wallet_type,
        "sub_account": sub_account,
        "asset": asset,
        "currency": asset,
        "total": total,
        "available": total,
    }


def _endpoint(wallet_type: str = "spot", sub_account: str | None = None) -> dict[str, Any]:
    return {"wallet_type": wallet_type, "sub_account": sub_account}


def _flow(cash_flow_id: str = "flow-1", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cash_flow_id": cash_flow_id,
        "kind": "internal_transfer",
        "from": _endpoint("spot"),
        "to": _endpoint("usdm_futures"),
        "counterparty_uid": None,
        "currency": "USDT",
        "amount": "100",
        "fee": "0",
        "fee_currency": "USDT",
        "occurred_at": "2026-07-15T07:30:00Z",
        "external_reference": None,
        "destination": None,
    }
    row.update(overrides)
    return row


def _snapshot(
    *,
    balances: list[dict[str, Any]] | None = None,
    cash_flows: list[dict[str, Any]] | None = None,
    completeness: dict[str, bool] | None = None,
    sub_account: str | None = None,
) -> list[dict[str, Any]]:
    return venue_ledger_snapshot_facts(
        snapshot_id=SNAPSHOT_ID,
        venue="BINANCE",
        sub_account=sub_account,
        source="venue_api",
        watermark="w",
        coverage_from="2026-07-15T07:00:00Z",
        observed_through="2026-07-15T08:00:00Z",
        completeness=completeness or COMPLETE,
        balances=balances if balances is not None else [_balance()],
        positions=[],
        fills=[],
        fees=[],
        cash_flows=cash_flows if cash_flows is not None else [],
    )


def _manifest(facts: list[dict[str, Any]]) -> dict[str, Any]:
    return next(fact for fact in facts if fact["kind"] == "venue_ledger_snapshot_manifest")


def _chunks(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [fact for fact in facts if fact["kind"] == "venue_ledger_snapshot_chunk"]


def _golden_chunk(batch: dict[str, Any]) -> dict[str, Any]:
    return next(fact for fact in batch["facts"] if fact["kind"] == "venue_ledger_snapshot_chunk")


# --- schema and golden -------------------------------------------------------


def test_schema_declares_wallet_scope_cash_flows_and_their_completeness() -> None:
    schema = _json(SCHEMA_PATH)
    manifest = schema["$defs"]["fact_venue_ledger_snapshot_manifest"]
    chunk = schema["$defs"]["fact_venue_ledger_snapshot_chunk"]
    assert {"sub_account", "cash_flows_count"} <= set(manifest["required"])
    assert "cash_flows_complete" in manifest["properties"]["completeness"]["required"]
    assert "cash_flows" in chunk["required"]
    balance = chunk["properties"]["balances"]["items"]
    assert {"wallet_type", "sub_account"} <= set(balance["required"])


def test_golden_shows_a_sub_account_transfer_a_withdrawal_and_two_wallet_scopes() -> None:
    batch = _json(GOLDEN_PATH)
    Draft202012Validator(_json(SCHEMA_PATH)).validate(batch)
    chunk = _golden_chunk(batch)
    kinds = {row["kind"] for row in chunk["cash_flows"]}
    assert {"sub_account_transfer", "withdrawal"} <= kinds
    withdrawal = next(row for row in chunk["cash_flows"] if row["kind"] == "withdrawal")
    assert withdrawal["destination"]["address"]
    scopes = {(row["wallet_type"], row["sub_account"]) for row in chunk["balances"]}
    wallets = {wallet for wallet, _ in scopes}
    assert any(
        (wallet, None) in scopes and any(s is not None for w, s in scopes if w == wallet)
        for wallet in wallets
    )


def _mutated_golden(mutate: Any) -> dict[str, Any]:
    batch = copy.deepcopy(_json(GOLDEN_PATH))
    mutate(_golden_chunk(batch))
    return batch


def _first_flow(chunk: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(row for row in chunk["cash_flows"] if row["kind"] == kind)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: c["cash_flows"][0].update(amount="-1"), id="negative-amount"),
        pytest.param(lambda c: c["cash_flows"][0].update(fee="-0.1"), id="negative-fee"),
        pytest.param(lambda c: c["cash_flows"][0].update(kind="airdrop"), id="unknown-kind"),
        pytest.param(lambda c: c["cash_flows"][0].pop("occurred_at"), id="missing-field"),
        pytest.param(lambda c: c["cash_flows"][0].update(extra=1), id="unknown-field"),
        pytest.param(
            lambda c: _first_flow(c, "uid_transfer_out").update(counterparty_uid=None),
            id="uid-transfer-without-counterparty",
        ),
        pytest.param(
            lambda c: _first_flow(c, "sub_account_transfer").update(counterparty_uid="123"),
            id="counterparty-on-non-uid-kind",
        ),
        pytest.param(
            lambda c: _first_flow(c, "sub_account_transfer").update(
                destination={"address": "0xabc", "network": None, "memo": None}
            ),
            id="destination-on-non-withdrawal",
        ),
        pytest.param(
            lambda c: _first_flow(c, "withdrawal")["destination"].update(address=""),
            id="empty-destination-address",
        ),
        pytest.param(lambda c: c["balances"][0].update(sub_account=""), id="empty-sub-account"),
        pytest.param(lambda c: c["balances"][0].update(wallet_type="Spot"), id="bad-wallet"),
        pytest.param(lambda c: c["balances"][0].pop("wallet_type"), id="balance-no-wallet"),
    ],
)
def test_schema_rejects_malformed_cash_flows_and_wallet_scope(mutate: Any) -> None:
    validator = Draft202012Validator(_json(SCHEMA_PATH))
    assert not validator.is_valid(_mutated_golden(mutate))


# --- producer assembly -------------------------------------------------------


def test_balances_in_two_wallets_are_ordered_by_wallet_then_sub_account() -> None:
    facts = _snapshot(
        balances=[
            _balance(wallet_type="usdm_futures"),
            _balance(wallet_type="spot", sub_account="alpha"),
            _balance(wallet_type="spot"),
        ]
    )
    rows = _chunks(facts)[0]["balances"]
    assert [(row["wallet_type"], row["sub_account"]) for row in rows] == [
        ("spot", None),
        ("spot", "alpha"),
        ("usdm_futures", None),
    ]


def test_same_asset_in_the_same_wallet_scope_is_rejected() -> None:
    with pytest.raises(RunnerFactContractError, match="duplicate"):
        _snapshot(balances=[_balance(), _balance(total="11")])


def test_duplicate_cash_flow_id_is_rejected_even_at_different_times() -> None:
    with pytest.raises(RunnerFactContractError, match="duplicate cash_flow_id"):
        _snapshot(
            cash_flows=[
                _flow("same", occurred_at="2026-07-15T07:10:00Z"),
                _flow("same", occurred_at="2026-07-15T07:20:00Z"),
            ]
        )


def test_cash_flows_are_ordered_by_time_then_id() -> None:
    facts = _snapshot(
        cash_flows=[
            _flow("b", occurred_at="2026-07-15T07:20:00Z"),
            _flow("c", occurred_at="2026-07-15T07:10:00Z"),
            _flow("a", occurred_at="2026-07-15T07:20:00Z"),
        ]
    )
    assert [row["cash_flow_id"] for row in _chunks(facts)[0]["cash_flows"]] == ["c", "a", "b"]


@pytest.mark.parametrize(
    ("flow", "message"),
    [
        (_flow(kind="uid_transfer_out", to=None), "counterparty_uid"),
        (_flow(counterparty_uid="42"), "counterparty_uid"),
        (_flow(destination={"address": "0xabc", "network": None, "memo": None}), "destination"),
        (_flow(kind="deposit"), "from"),
        (_flow(kind="withdrawal"), "to"),
        (_flow(to=_endpoint("spot", "alpha")), "same sub_account"),
        (_flow(kind="sub_account_transfer", to=_endpoint("spot")), "different sub_account"),
        (_flow(amount="0"), "amount"),
        (_flow(amount="-1"), "amount"),
        (_flow(fee="-1"), "fee"),
        (_flow(kind="airdrop"), "kind"),
    ],
)
def test_producer_rejects_cash_flows_that_break_the_kind_rules(
    flow: dict[str, Any], message: str
) -> None:
    with pytest.raises(RunnerFactContractError, match=message):
        _snapshot(cash_flows=[flow])


def test_withdrawal_keeps_its_destination() -> None:
    flow = _flow(
        kind="withdrawal",
        to=None,
        external_reference="0xhash",
        destination={"address": "0xdest", "network": "ETH", "memo": None},
    )
    row = _chunks(_snapshot(cash_flows=[flow]))[0]["cash_flows"][0]
    assert row["destination"] == {"address": "0xdest", "network": "ETH", "memo": None}


def test_empty_sub_account_is_rejected() -> None:
    with pytest.raises(RunnerFactContractError, match="sub_account"):
        _snapshot(balances=[_balance(sub_account="")])


def test_mixed_items_share_one_chunk_limit() -> None:
    balances = [_balance(wallet_type=f"w{i:03d}") for i in range(511)]
    one = _chunks(_snapshot(balances=balances, cash_flows=[_flow("f1")]))
    assert len(one) == 1
    assert len(one[0]["balances"]) + len(one[0]["cash_flows"]) == MAX_VENUE_LEDGER_ITEMS_PER_CHUNK
    two = _chunks(_snapshot(balances=balances, cash_flows=[_flow("f1"), _flow("f2")]))
    assert len(two) == 2
    assert sum(len(chunk["cash_flows"]) for chunk in two) == 2


def test_manifest_counts_cash_flows_and_carries_sub_account() -> None:
    manifest = _manifest(_snapshot(cash_flows=[_flow("f1"), _flow("f2")], sub_account="alpha"))
    assert manifest["cash_flows_count"] == 2
    assert manifest["sub_account"] == "alpha"
    assert manifest["completeness"]["cash_flows_complete"] is True


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param({"balances": [_balance(wallet_type="funding")]}, id="balance-wallet"),
        pytest.param({"balances": [_balance(sub_account="alpha")]}, id="balance-sub-account"),
        pytest.param({"sub_account": "alpha"}, id="manifest-sub-account"),
        pytest.param({"cash_flows": [_flow(amount="101")]}, id="cash-flow-amount"),
        pytest.param({"cash_flows": [_flow(to=_endpoint("funding"))]}, id="cash-flow-wallet"),
        pytest.param(
            {"completeness": {**COMPLETE, "cash_flows_complete": False}},
            id="cash-flows-completeness",
        ),
    ],
)
def test_every_new_field_moves_the_content_digest(variant: dict[str, Any]) -> None:
    base = {"cash_flows": [_flow()]}
    baseline = _manifest(_snapshot(**base))["content_digest"]
    assert _manifest(_snapshot(**{**base, **variant}))["content_digest"] != baseline


def test_cash_flows_completeness_is_required() -> None:
    legacy = {key: value for key, value in COMPLETE.items() if key != "cash_flows_complete"}
    with pytest.raises(RunnerFactContractError, match="cash_flows_complete"):
        _snapshot(completeness=legacy)


def test_empty_and_complete_cash_flows_is_a_valid_zero_activity_window() -> None:
    manifest = _manifest(_snapshot(cash_flows=[]))
    assert manifest["cash_flows_count"] == 0
    assert manifest["completeness"]["cash_flows_complete"] is True


def test_normalized_bytes_are_fixed_for_fractions_zeros_and_unicode() -> None:
    name = "Konto-Ä-α"
    flow = _flow(
        amount="100.500",
        fee="0.10",
        occurred_at="2026-07-15T07:30:00.250+00:00",
    )
    flow["from"] = _endpoint("spot", name)
    flow["to"] = _endpoint("usdm_futures", name)
    chunk = _chunks(_snapshot(cash_flows=[flow]))[0]
    row = chunk["cash_flows"][0]
    assert row["amount"] == "100.5"
    assert row["fee"] == "0.1"
    assert row["occurred_at"] == "2026-07-15T07:30:00.250Z"
    assert row["from"]["sub_account"] == name
    unsigned = {
        key: value
        for key, value in chunk.items()
        if key not in {"kind", "event_id", "chunk_digest"}
    }
    digest = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert digest == chunk["chunk_digest"]
