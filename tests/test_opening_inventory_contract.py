from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest

from custos.core.opening_inventory_contract import (
    normalize_opening_inventory,
    unavailable_opening_inventory,
)
from custos.core.runner_fact import RunnerFactAuthority, RunnerFactContractError, equity_snapshot

AT = datetime(2026, 11, 4, tzinfo=UTC)


@pytest.fixture
def authority():
    return RunnerFactAuthority(
        tenant_id="tenant-a",
        trading_mode="testnet",
        runner_id=UUID(int=1),
        deployment_instance_id=UUID(int=2),
        deployment_spec_id=UUID(int=3),
        deployment_spec_digest="a" * 64,
        generation=1,
        strategy_id=UUID(int=5),
        capability_version_id=UUID(int=6),
        capability_version=1,
        capability_manifest_digest="b" * 64,
    )


@pytest.mark.parametrize("connector", ["binance", "binance_perpetual", "okx", "okx_perpetual"])
def test_equity_carries_unavailable_inventory_without_replacing_risk_equity(authority, connector):
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1234.5",
        currency="USDT",
        observed_at=AT,
        trading_gate="awaiting_release",
        account_inventory=unavailable_opening_inventory(authority, connector),
    )
    assert fact["amount"] == "1234.5"
    assert fact["trading_gate"] == "awaiting_release"
    inventory = fact["account_inventory"]
    assert inventory["scope"]["source"] is None
    assert inventory["scope"]["connector"] == connector
    assert inventory["inventory_complete"] is False
    assert inventory["equity_binding"]["status"] == "unproven"
    assert all(read["rows"] is None for read in inventory["observations"].values())


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other"),
        ("trading_mode", "sandbox"),
        ("runner_id", str(UUID(int=7))),
        ("deployment_instance_id", str(UUID(int=8))),
        ("deployment_spec_id", str(UUID(int=9))),
        ("deployment_spec_digest", "c" * 64),
        ("generation", 2),
    ],
)
def test_scope_must_match_the_signing_authority(authority, field, value):
    inventory = unavailable_opening_inventory(authority, "okx")
    inventory["scope"][field] = value
    with pytest.raises(RunnerFactContractError, match="scope"):
        normalize_opening_inventory(inventory, amount="1", currency="USDT", authority=authority)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i["scope"].pop("source"),
        lambda i: i["scope"].update(source={"kind": "sodex_venue_account"}),
        lambda i: i.update(inventory_complete=True),
        lambda i: i["completeness"].update(identity=1),
        lambda i: i["observations"]["balances"].update(rows=[]),
        lambda i: i["observations"]["balances"].update(extra="hidden"),
        lambda i: i["equity_binding"].update(status="coherent"),
        lambda i: i["scope"].update(generation=True),
        lambda i: i["scope"].update(runner_id=str(UUID(int=0))),
    ],
)
def test_missing_extra_fabricated_and_coerced_values_are_refused(authority, mutate):
    inventory = deepcopy(unavailable_opening_inventory(authority, "binance"))
    mutate(inventory)
    with pytest.raises(RunnerFactContractError):
        normalize_opening_inventory(inventory, amount="1", currency="USDT", authority=authority)


@pytest.fixture
def observation(authority):
    from decimal import Decimal

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.engines.nautilus.opening_inventory import (
        InventoryAccountScope,
        InventoryBalance,
        InventoryPosition,
        InventoryRead,
        InventoryReportedAccount,
        OpeningAccountInventory,
    )

    lifecycle = EngineLifecycleAuthority(
        authority.deployment_instance_id,
        authority.deployment_spec_id,
        authority.deployment_spec_digest,
        authority.generation,
        authority.trading_mode,
    )
    scope = InventoryAccountScope(
        "tenant-a", authority.runner_id, lifecycle, "sodex_perpetual", "0x" + "a" * 40, 42
    )

    def read(rows):
        return InventoryRead(tuple(rows), AT, AT, 42, AT, None, "d" * 64)

    return OpeningAccountInventory(
        scope,
        AT,
        AT,
        read([InventoryReportedAccount(scope.wallet_address, scope.account_id)]),
        read(
            [
                InventoryBalance(1, "USDT", Decimal("100"), None, Decimal("10"), None),
                InventoryBalance(2, "BTC", Decimal("0.5"), None, Decimal("0"), None),
            ]
        ),
        read([InventoryPosition(3, "ETH-USD", Decimal("2"), True, "BOTH", Decimal("100"))]),
        read([]),
        (),
    )


def test_nonzero_other_assets_and_positions_keep_original_risk_equity(authority, observation):
    from custos.engines.nautilus.opening_inventory_fact import opening_inventory_observation

    inventory = opening_inventory_observation(authority, "sodex_perpetual", observation)
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1234.5",
        currency="USDT",
        observed_at=AT,
        trading_gate="awaiting_release",
        account_inventory=inventory,
    )
    assert fact["amount"] == "1234.5"
    assert inventory["observations"]["balances"]["rows"][0]["total"] == "100"
    assert inventory["observations"]["balances"]["rows"][1]["raw_asset_code"] == "BTC"
    assert inventory["observations"]["positions"]["rows"][0]["quantity"] == "2"
    assert inventory["equity_binding"]["status"] == "unproven"
    assert inventory["inventory_complete"] is False
    assert all(row["denomination"] is None for row in inventory["observations"]["balances"]["rows"])


@pytest.mark.parametrize("amount", ["79228162514264337593543950335", "0." + "0" * 27 + "1"])
def test_nested_amounts_accept_the_exact_decimal_edges(authority, observation, amount):
    from dataclasses import replace
    from decimal import Decimal

    from custos.engines.nautilus.opening_inventory_fact import opening_inventory_observation

    rows = (replace(observation.balances.rows[0], total=Decimal(amount)),)
    observed = replace(observation, balances=replace(observation.balances, rows=rows))
    inventory = opening_inventory_observation(authority, "sodex_perpetual", observed)
    normalized = normalize_opening_inventory(
        inventory, amount="1234.5", currency="USDT", authority=authority
    )
    assert normalized["observations"]["balances"]["rows"][0]["total"] == amount


@pytest.mark.parametrize("amount", ["79228162514264337593543950336", "0." + "0" * 28 + "1"])
def test_nonrepresentable_dimension_does_not_erase_other_observations(
    authority, observation, amount
):
    from dataclasses import replace
    from decimal import Decimal

    from custos.engines.nautilus.opening_inventory_fact import opening_inventory_observation

    rows = (replace(observation.balances.rows[0], total=Decimal(amount)),)
    observed = replace(observation, balances=replace(observation.balances, rows=rows))
    inventory = opening_inventory_observation(authority, "sodex_perpetual", observed)
    normalized = normalize_opening_inventory(
        inventory, amount="1234.5", currency="USDT", authority=authority
    )
    assert normalized["observations"]["balances"]["rows"] is None
    assert normalized["observations"]["balances"]["reason"] == "wire_dimension_unavailable"
    assert normalized["observations"]["positions"]["rows"][0]["quantity"] == "2"
    assert normalized["inventory_complete"] is False


async def test_deep_inventory_tampering_invalidates_the_existing_facts_digest(tmp_path, authority):
    from custos.core.runner_fact import RunnerFactOutbox, runner_fact_signing_preimage
    from tests.test_runner_fact_outbox import _identity

    outbox = RunnerFactOutbox(tmp_path / "facts.sqlite3")
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1234.5",
        currency="USDT",
        observed_at=AT,
        trading_gate="awaiting_release",
        account_inventory=unavailable_opening_inventory(authority, "binance"),
    )
    import json

    batch_id = await outbox.enqueue(authority, _identity(), [fact])
    pending = await outbox.pending()
    assert pending[0].batch_id == batch_id
    batch = json.loads(pending[0].payload)
    batch["facts"][0]["account_inventory"]["reasons"] = ["capture_unavailable"]
    with pytest.raises(RunnerFactContractError, match="digest"):
        runner_fact_signing_preimage(batch)


def _cash_candidate(authority):
    # Future parser fixture only; no native handle or complete capability is acquired.
    inventory = unavailable_opening_inventory(authority, "binance")
    inventory["scope"].update(
        trading_mode="sandbox",
        source={
            "kind": "sandbox_engine",
            "incarnation": str(UUID(int=10)),
            "client_id": "BINANCE",
            "account_id": "BINANCE-001",
            "venue": "BINANCE",
            "account_type": "CASH",
            "api_revision": "execution-account-capture-v1",
            "model_revision": "sandbox-account-state-v1",
            "runtime_version": "2.0.0",
            "configuration_json": '{"allow_borrowing":false}',
            "configuration_sha256": sha256(b'{"allow_borrowing":false}').hexdigest(),
        },
    )
    inventory["capability_binding"].update(
        capture_api_revision="execution-account-capture-v1",
        model_revision="sandbox-account-state-v1",
        mapping_revision="native_currency_codes_v1",
        runtime_artifact_digest="e" * 64,
    )
    inventory["native_capture"] = {
        "node_instance_id": str(UUID(int=12)),
        "environment": "SANDBOX",
        "capture_sequence": 1,
        "frontier": [
            {"channel": channel, "applied": 0, "admitted": 0}
            for channel in (
                "time_events",
                "system_events",
                "system_commands",
                "execution_events",
                "execution_commands",
                "data_events",
                "data_commands",
                "external_events",
                "maintenance",
            )
        ],
        "observed_at_ns": 1793750400000000000,
        "no_borrowing_model": True,
    }
    token = sha256(
        json.dumps(
            {"source": inventory["scope"]["source"], "native_capture": inventory["native_capture"]},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    inventory["status"] = "observed"
    inventory["reasons"] = []
    inventory["completeness"] = dict.fromkeys(inventory["completeness"], True)
    inventory["inventory_complete"] = True
    inventory["equity_binding"] = {
        "status": "coherent",
        "capture_token": token,
        "currency": "USDT",
        "amount": "1",
        "observed_at": "2026-11-04T00:00:00Z",
        "valuation_rule": "settlement_cash_total_v1",
        "reason": None,
    }

    def read(rows):
        return {
            "status": "observed",
            "rows": rows,
            "requested_at": None,
            "received_at": None,
            "source_time": "2026-11-04T00:00:00Z",
            "block_height": None,
            "capture_token": token,
            "response_digest": None,
            "pagination": "owner_full",
            "reason": None,
        }

    source = inventory["scope"]["source"]
    inventory["observations"]["identity"] = read(
        [
            {
                "kind": "sandbox_account",
                **{
                    k: source[k]
                    for k in (
                        "incarnation",
                        "client_id",
                        "account_id",
                        "venue",
                        "account_type",
                        "configuration_sha256",
                    )
                },
            }
        ]
    )
    inventory["observations"]["balances"] = read(
        [
            {
                "raw_asset_id": None,
                "raw_asset_code": "USDT",
                "native_currency": {"precision": 8, "currency_type": "CRYPTO", "iso4217": 0},
                "namespace": "native.currency",
                "denomination": "USDT",
                "mapping_revision": "native_currency_codes_v1",
                "total": "1",
                "locked": "0",
                "collateral": None,
                "available": "1",
            }
        ]
    )
    return inventory, read


def test_complete_flags_do_not_replace_missing_capture_dimensions(authority):
    inventory, _ = _cash_candidate(authority)
    with pytest.raises(RunnerFactContractError):
        normalize_opening_inventory(inventory, amount="1", currency="USDT")


@pytest.mark.parametrize(
    "mutation",
    [
        "other_asset",
        "position",
        "liability",
        "mapping_revision",
        "cap_mapping_revision",
        "source_identity",
    ],
)
def test_cash_coherent_cannot_hide_asset_debt_position_or_mapping(authority, mutation):
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    if mutation == "other_asset":
        inventory["observations"]["balances"]["rows"].append(
            {
                **inventory["observations"]["balances"]["rows"][0],
                "raw_asset_code": "BTC",
                "denomination": "BTC",
                "total": "100",
            }
        )
    elif mutation == "position":
        inventory["observations"]["positions"] = read(
            [
                {
                    "position_id": "P-1",
                    "instrument": "BTCUSDT",
                    "quantity": "1",
                    "active": True,
                    "position_side": "LONG",
                    "average_entry_price": None,
                    "settlement_currency": "USDT",
                }
            ]
        )
    elif mutation == "liability":
        inventory["observations"]["liabilities"] = read(
            [
                {
                    "raw_asset_id": None,
                    "raw_asset_code": "USDT",
                    "namespace": "native.currency",
                    "amount": "1",
                }
            ]
        )
    elif mutation == "mapping_revision":
        inventory["observations"]["balances"]["rows"][0]["mapping_revision"] = "wrong_mapping"
    elif mutation == "cap_mapping_revision":
        inventory["capability_binding"]["mapping_revision"] = "wrong_mapping"
    else:
        inventory["scope"]["source"]["incarnation"] = str(UUID(int=11))
        _retoken(inventory)
    with pytest.raises(RunnerFactContractError):
        normalize_opening_inventory(inventory, amount="1", currency="USDT")


def test_cash_contract_positive_control_has_every_real_dimension_and_mapping(authority):
    # This is a parser fixture, not a native capture or runtime acceptance.
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    result = normalize_opening_inventory(inventory, amount="1", currency="USDT")
    assert result["inventory_complete"] is True


def test_complete_inventory_dimensions_can_keep_unproven_original_multicurrency_equity(authority):
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    inventory["observations"]["balances"]["rows"].append(
        {
            **inventory["observations"]["balances"]["rows"][0],
            "raw_asset_code": "BTC",
            "denomination": "BTC",
            "total": "100",
        }
    )
    inventory["equity_binding"] = {
        "status": "unproven",
        "capture_token": None,
        "currency": None,
        "amount": None,
        "observed_at": None,
        "valuation_rule": None,
        "reason": "independent_risk_observation",
    }
    inventory["inventory_complete"] = False
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1234.5",
        currency="USDT",
        observed_at=AT,
        trading_gate="awaiting_release",
        account_inventory=inventory,
    )
    assert fact["amount"] == "1234.5"
    assert fact["account_inventory"]["observations"]["balances"]["rows"][1]["total"] == "100"
    assert fact["account_inventory"]["inventory_complete"] is False


def _large_inventory(authority):
    from dataclasses import replace

    from custos.core.opening_inventory_contract import MAX_INVENTORY_ROWS

    authority = replace(authority, trading_mode="testnet")
    inventory, read = _cash_candidate(authority)
    inventory["scope"].update(
        trading_mode="testnet",
        connector="sodex",
        source={
            "kind": "sodex_venue_account",
            "network": "testnet",
            "main_wallet": "0x" + "1" * 40,
            "account_id": 1,
            "account_type": "spot",
        },
    )
    inventory["native_capture"] = None
    inventory["observations"]["identity"] = read(
        [
            {
                "kind": "sodex_account",
                "main_wallet": "0x" + "1" * 40,
                "account_id": 1,
            }
        ]
    )
    inventory["completeness"] = dict.fromkeys(inventory["completeness"])
    inventory["inventory_complete"] = False
    inventory["equity_binding"] = unavailable_opening_inventory(authority, "binance")[
        "equity_binding"
    ]
    amount = "79228162514264337593543950335"
    inventory["observations"]["balances"] = read(
        [
            {
                "raw_asset_id": str(i).zfill(128),
                "raw_asset_code": "A" * 64,
                "native_currency": None,
                "namespace": "n" * 128,
                "denomination": None,
                "mapping_revision": None,
                "total": amount,
                "locked": amount,
                "collateral": amount,
                "available": amount,
            }
            for i in range(MAX_INVENTORY_ROWS)
        ]
    )
    inventory["observations"]["positions"] = read(
        [
            {
                "position_id": str(i).zfill(256),
                "instrument": "i" * 128,
                "quantity": amount,
                "active": True,
                "position_side": "p" * 128,
                "average_entry_price": amount,
                "settlement_currency": None,
            }
            for i in range(MAX_INVENTORY_ROWS)
        ]
    )
    inventory["observations"]["ordinary_orders"] = read(
        [
            {
                "order_id": str(i).zfill(256),
                "client_order_id": None,
                "venue_order_id": None,
                "trigger_price": None,
                "in_matching_engine": None,
                "pending": None,
                "instrument": "i" * 128,
                "side": "s" * 128,
                "order_type": "t" * 128,
                "status": "s" * 128,
                "original_quantity": amount,
                "executed_quantity": amount,
                "price": amount,
            }
            for i in range(MAX_INVENTORY_ROWS)
        ]
    )
    inventory["observations"]["liabilities"] = read(
        [
            {
                "raw_asset_id": str(i).zfill(128),
                "raw_asset_code": "A" * 64,
                "namespace": "n" * 128,
                "amount": amount,
            }
            for i in range(MAX_INVENTORY_ROWS)
        ]
    )
    return authority, inventory


def test_inventory_row_budget_leaves_room_for_the_signed_observability_batch(authority):
    import json

    from custos.core.runner_fact import MAX_BATCH_BYTES

    authority, inventory = _large_inventory(authority)
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1234.5",
        currency="USDT",
        observed_at=AT,
        trading_gate="awaiting_release",
        account_inventory=inventory,
    )
    # Leave ten percent for the envelope and the ordinary companion facts.
    assert len(json.dumps(fact, separators=(",", ":")).encode()) <= MAX_BATCH_BYTES * 9 // 10


def test_future_cash_shape_is_not_authorized_by_the_current_manifest(authority):
    from custos.core.opening_inventory_contract import require_opening_inventory_support
    from custos.core.runner_fact import runner_fact_capability_shape

    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    closed = normalize_opening_inventory(inventory, amount="1", currency="USDT")
    manifest = {"runner_fact_contracts": runner_fact_capability_shape()[1]}
    assert manifest["runner_fact_contracts"]["risk"]["opening_inventory"]["complete_models"] == []
    with pytest.raises(RunnerFactContractError, match="registered"):
        require_opening_inventory_support(closed, manifest)


def test_inventory_schema_revision_is_an_integer_not_a_boolean(authority):
    inventory = unavailable_opening_inventory(authority, "binance")
    inventory["capability_binding"]["inventory_schema_revision"] = True
    with pytest.raises(RunnerFactContractError):
        normalize_opening_inventory(inventory, amount="1", currency="USDT")


def test_cash_parser_preserves_one_exact_nanosecond_capture_time(authority):
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    at = "2026-11-04T00:00:00.123456789Z"
    inventory["native_capture"]["observed_at_ns"] += 123456789
    _retoken(inventory)
    for observation in inventory["observations"].values():
        observation["source_time"] = at
    inventory["equity_binding"]["observed_at"] = at
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1",
        currency="USDT",
        observed_at=at,
        trading_gate="awaiting_release",
        account_inventory=inventory,
    )
    assert fact["observed_at"] == at
    assert fact["account_inventory"]["equity_binding"]["observed_at"] == at


@pytest.mark.parametrize(
    "source,expected",
    [
        ("2026-11-04T08:00:00.123456789+08:00", "2026-11-04T00:00:00.123456789Z"),
        ("1969-12-31T23:59:59.999999999Z", "1969-12-31T23:59:59.999999999Z"),
        ("2026-11-04T00:00:00.100000000Z", "2026-11-04T00:00:00.100Z"),
        ("2026-11-04T00:00:00.123456000Z", "2026-11-04T00:00:00.123456Z"),
        ("2026-11-04T00:00:00.000000000Z", "2026-11-04T00:00:00Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
    ],
)
def test_equity_observation_time_uses_exact_utc_autosi(authority, source, expected):
    fact = equity_snapshot(
        event_id=UUID(int=4),
        amount="1",
        currency="USDT",
        observed_at=source,
        trading_gate="awaiting_release",
        account_inventory=unavailable_opening_inventory(authority, "binance"),
    )
    assert fact["observed_at"] == expected


@pytest.mark.parametrize(
    "source", ["2026-11-04T00:00:00", "2026-11-04T00:00:00.1234567891Z", "2026-11-31T00:00:00Z"]
)
def test_equity_time_rejects_naive_overprecise_and_invalid_instants(authority, source):
    with pytest.raises(RunnerFactContractError):
        equity_snapshot(
            event_id=UUID(int=4),
            amount="1",
            currency="USDT",
            observed_at=source,
            trading_gate="awaiting_release",
            account_inventory=unavailable_opening_inventory(authority, "binance"),
        )


def test_terminal_prior_reference_keeps_the_signed_equity_nanoseconds():
    from custos.core.runner_fact import _terminal_prior_equity

    prior = {
        "event_id": str(UUID(int=4)),
        "seq": 1,
        "amount": "1",
        "currency": "USDT",
        "observed_at": "2026-11-04T00:00:00.123456789Z",
    }
    assert _terminal_prior_equity(prior) == prior


def test_terminal_prior_cannot_follow_valuation_within_the_same_microsecond():
    from custos.core.runner_fact import _require_terminal_time_order

    prior = {"observed_at": "2026-11-04T00:00:00.123456789Z"}
    with pytest.raises(RunnerFactContractError, match="prior_equity"):
        _require_terminal_time_order(
            "2026-11-04T00:00:00Z",
            "2026-11-04T00:00:00.123456700Z",
            None,
            "2026-11-04T00:00:01Z",
            prior,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_capture",
        "missing_account_type",
        "wrong_environment",
        "bool_sequence",
        "pending_frontier",
        "missing_channel",
        "duplicate_channel",
        "extra_channel",
        "false_no_borrowing",
        "margin_cash_claim",
        "wrong_config_bytes",
        "wrong_api",
        "wrong_model",
        "wrong_token",
        "wrong_time",
        "currency_metadata_collision",
    ],
)
def test_cash_capture_requires_actual_closed_native_descriptor(authority, mutation):
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    capture = inventory["native_capture"]
    source = inventory["scope"]["source"]
    if mutation == "missing_capture":
        inventory.pop("native_capture")
    elif mutation == "missing_account_type":
        source.pop("account_type")
    elif mutation == "wrong_environment":
        capture["environment"] = "LIVE"
    elif mutation == "bool_sequence":
        capture["capture_sequence"] = True
    elif mutation == "pending_frontier":
        capture["frontier"][0]["admitted"] = 1
    elif mutation == "missing_channel":
        capture["frontier"].pop()
    elif mutation == "duplicate_channel":
        capture["frontier"][-1] = dict(capture["frontier"][0])
    elif mutation == "extra_channel":
        capture["frontier"][0]["channel"] = "unknown_events"
    elif mutation == "false_no_borrowing":
        capture["no_borrowing_model"] = False
    elif mutation == "margin_cash_claim":
        source["account_type"] = "MARGIN"
    elif mutation == "wrong_config_bytes":
        source["configuration_json"] += " "
    elif mutation == "wrong_api":
        source["api_revision"] = "invented-api"
    elif mutation == "wrong_model":
        source["model_revision"] = "invented-model"
    elif mutation == "wrong_token":
        inventory["observations"]["balances"]["capture_token"] = "opaque-token"
    elif mutation == "wrong_time":
        capture["observed_at_ns"] += 1
    else:
        inventory["observations"]["balances"]["rows"][0]["native_currency"]["precision"] = 2
    if mutation not in {"missing_capture", "wrong_token"}:
        _retoken(inventory)
    with pytest.raises(RunnerFactContractError):
        normalize_opening_inventory(inventory, amount="1", currency="USDT")


def _retoken(inventory):
    from custos.core.opening_inventory_contract import native_capture_token

    token = native_capture_token(inventory["scope"]["source"], inventory["native_capture"])
    for observation in inventory["observations"].values():
        if observation["status"] != "unavailable":
            observation["capture_token"] = token
    if inventory["equity_binding"]["status"] == "coherent":
        inventory["equity_binding"]["capture_token"] = token


@pytest.mark.parametrize("account_type", ["MARGIN", "BETTING", "WALLET"])
def test_non_cash_native_source_preserves_risk_without_claiming_no_liabilities(
    authority, account_type
):
    inventory, read = _cash_candidate(authority)
    inventory["scope"]["source"]["account_type"] = account_type
    inventory["observations"]["identity"]["rows"][0]["account_type"] = account_type
    inventory["native_capture"]["no_borrowing_model"] = False
    inventory["observations"].update(positions=read([]), ordinary_orders=read([]))
    inventory["completeness"]["liabilities"] = None
    inventory["completeness"]["pagination"] = None
    inventory["completeness"]["common_boundary"] = None
    inventory["inventory_complete"] = False
    inventory["equity_binding"] = unavailable_opening_inventory(authority, "binance")[
        "equity_binding"
    ]
    _retoken(inventory)
    result = normalize_opening_inventory(inventory, amount="1234.5", currency="USDT")
    assert result["scope"]["source"]["account_type"] == account_type
    assert result["completeness"]["liabilities"] is None
    assert result["inventory_complete"] is False
    inventory["completeness"]["liabilities"] = True
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    _retoken(inventory)
    with pytest.raises(RunnerFactContractError, match="liabilities"):
        normalize_opening_inventory(inventory, amount="1234.5", currency="USDT")


def test_order_kind_completeness_requires_the_actual_order_enumeration(authority):
    inventory, _ = _cash_candidate(authority)
    inventory["completeness"] = dict.fromkeys(inventory["completeness"])
    inventory["completeness"]["order_kinds"] = True
    inventory["inventory_complete"] = False
    inventory["equity_binding"] = unavailable_opening_inventory(authority, "binance")[
        "equity_binding"
    ]
    with pytest.raises(RunnerFactContractError, match="order"):
        normalize_opening_inventory(inventory, amount="1234.5", currency="USDT")


@pytest.mark.parametrize(
    "dimension", ["identity", "balances", "liabilities", "positions", "ordinary_orders"]
)
def test_unavailable_source_cannot_invent_inapplicable_dimensions(authority, dimension):
    inventory = unavailable_opening_inventory(authority, "binance")
    inventory["observations"][dimension].update(
        status="not_applicable", reason="model_not_applicable"
    )
    with pytest.raises(RunnerFactContractError, match="inapplicable"):
        normalize_opening_inventory(inventory, amount="1234.5", currency="USDT")


def test_native_exact_fields_preserve_missing_price_and_pending_order(authority):
    inventory, read = _cash_candidate(authority)
    inventory["observations"]["positions"] = read(
        [
            {
                "position_id": "P-1",
                "instrument": "BTCUSDT.BINANCE",
                "quantity": "1",
                "active": True,
                "position_side": "LONG",
                "average_entry_price": None,
                "settlement_currency": "USDT",
            }
        ]
    )
    inventory["observations"]["ordinary_orders"] = read(
        [
            {
                "order_id": "C-1",
                "client_order_id": "C-1",
                "venue_order_id": None,
                "instrument": "BTCUSDT.BINANCE",
                "side": "BUY",
                "order_type": "MARKET",
                "status": "SUBMITTED",
                "original_quantity": "1",
                "executed_quantity": "0",
                "price": None,
                "trigger_price": None,
                "in_matching_engine": False,
                "pending": True,
            }
        ]
    )
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    inventory["inventory_complete"] = False
    inventory["equity_binding"] = unavailable_opening_inventory(authority, "binance")[
        "equity_binding"
    ]
    result = normalize_opening_inventory(inventory, amount="1234.5", currency="USDT")
    assert result["observations"]["positions"]["rows"][0]["average_entry_price"] is None
    order = result["observations"]["ordinary_orders"]["rows"][0]
    assert order["price"] is None and order["venue_order_id"] is None and order["pending"] is True
    assert result["inventory_complete"] is False


@pytest.mark.parametrize(
    "mutation",
    [None, "legacy_status", "consumer_ready", "lost_blocker", "wrong_revision", "runtime_ready"],
)
def test_current_revision_readiness_gate_preserves_open_blockers(monkeypatch, mutation):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "opening_authority_gate", root / "scripts/check-authority-docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    load_json = module.load_json
    index = load_json(root / "docs/authority/runner-fact-contract-assets-v1.json")
    if mutation == "legacy_status":
        index["status"] = "CANONICAL_V1_PENDING_RUNTIME_RECEIPTS"
    elif mutation == "consumer_ready":
        index["consumer_ready"] = True
    elif mutation == "lost_blocker":
        index["open_blockers"].pop()
    elif mutation == "wrong_revision":
        index["contract_revision"] = 6
    elif mutation == "runtime_ready":
        index["runtime_ready"] = True
    monkeypatch.setattr(
        module,
        "load_json",
        lambda path: (
            index
            if path == root / "docs/authority/runner-fact-contract-assets-v1.json"
            else load_json(path)
        ),
    )
    errors = []
    module.verify_runner_fact_contract({}, errors)
    if mutation is None:
        assert errors == []
    else:
        assert errors


def _zero_cash_candidate(authority, balances):
    inventory, read = _cash_candidate(authority)
    inventory["observations"].update(
        balances=read(balances), positions=read([]), ordinary_orders=read([])
    )
    inventory["observations"]["liabilities"] = read(None)
    inventory["observations"]["liabilities"].update(
        status="not_applicable", reason="model_not_applicable"
    )
    inventory["equity_binding"]["amount"] = "0"
    return inventory, read


@pytest.mark.parametrize("other_zero_asset", [False, True])
def test_complete_current_native_account_can_prove_zero_without_a_settlement_row(
    authority, other_zero_asset
):
    inventory, _ = _zero_cash_candidate(authority, [])
    if other_zero_asset:
        inventory["observations"]["balances"]["rows"].append(
            {
                "raw_asset_id": None,
                "raw_asset_code": "BTC",
                "namespace": "native.currency",
                "native_currency": {"precision": 8, "currency_type": "CRYPTO", "iso4217": 0},
                "denomination": "BTC",
                "mapping_revision": "native_currency_codes_v1",
                "total": "0",
                "locked": "0",
                "collateral": None,
                "available": "0",
            }
        )
    rows = deepcopy(inventory["observations"]["balances"]["rows"])
    result = normalize_opening_inventory(inventory, amount="0", currency="USDT")
    assert result["observations"]["balances"]["rows"] == rows
    assert result["equity_binding"]["amount"] == "0"
    assert result["inventory_complete"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_account",
        "balances_unavailable",
        "balances_incomplete",
        "boundary_incomplete",
        "nonzero_other_asset",
        "unknown_nonzero_asset",
        "unmapped_settlement",
        "nonzero_liability",
        "effective_position",
        "wrong_source",
        "wrong_token",
        "wrong_time",
        "nonzero_equity",
    ],
)
def test_absent_settlement_row_never_turns_unknown_or_nonzero_state_into_zero(authority, mutation):
    inventory, read = _zero_cash_candidate(authority, [])
    if mutation == "missing_account":
        inventory["observations"]["identity"] = read([])
    elif mutation == "balances_unavailable":
        inventory["observations"]["balances"] = unavailable_opening_inventory(authority, "binance")[
            "observations"
        ]["balances"]
    elif mutation == "balances_incomplete":
        inventory["completeness"]["balances"] = False
        inventory["inventory_complete"] = False
    elif mutation == "boundary_incomplete":
        inventory["completeness"]["common_boundary"] = None
        inventory["inventory_complete"] = False
    elif mutation in {"nonzero_other_asset", "unknown_nonzero_asset"}:
        inventory["observations"]["balances"] = read(
            [
                {
                    "raw_asset_id": None,
                    "raw_asset_code": "BTC" if mutation == "nonzero_other_asset" else "SOL",
                    "namespace": "native.currency",
                    "native_currency": {"precision": 8, "currency_type": "CRYPTO", "iso4217": 0},
                    "denomination": "BTC" if mutation == "nonzero_other_asset" else None,
                    "mapping_revision": "native_currency_codes_v1"
                    if mutation == "nonzero_other_asset"
                    else None,
                    "total": "1",
                    "locked": "0",
                    "collateral": None,
                    "available": "1",
                }
            ]
        )
    elif mutation == "unmapped_settlement":
        inventory["equity_binding"]["currency"] = "VUSDC"
    elif mutation == "nonzero_liability":
        inventory["observations"]["liabilities"] = read(
            [
                {
                    "raw_asset_id": None,
                    "raw_asset_code": "USDT",
                    "namespace": "native.currency",
                    "amount": "1",
                }
            ]
        )
    elif mutation == "effective_position":
        inventory["observations"]["positions"] = read(
            [
                {
                    "position_id": "P-1",
                    "instrument": "BTCUSDT.BINANCE",
                    "quantity": "1",
                    "active": True,
                    "position_side": "LONG",
                    "average_entry_price": None,
                    "settlement_currency": "USDT",
                }
            ]
        )
    elif mutation == "wrong_source":
        inventory["scope"]["source"]["account_id"] = "BINANCE-other"
        _retoken(inventory)
    elif mutation == "wrong_token":
        inventory["observations"]["balances"]["capture_token"] = "wrong"
    elif mutation == "wrong_time":
        inventory["observations"]["balances"]["source_time"] = "2026-11-04T00:00:00.000000001Z"
    else:
        inventory["equity_binding"]["amount"] = "1"
    with pytest.raises(RunnerFactContractError):
        normalize_opening_inventory(
            inventory,
            amount=inventory["equity_binding"]["amount"],
            currency=inventory["equity_binding"]["currency"],
        )
