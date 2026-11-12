"""Closed native conversion semantics using explicit future handle fixtures."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

import pytest


def _response():
    from custos.core.opening_inventory_contract import NATIVE_CAPTURE_CHANNELS

    configuration = json.dumps(
        {
            "client_config": {
                "account_id": "BINANCE-001",
                "venue": "BINANCE",
                "account_type": "CASH",
                "base_currency": "USDT",
            },
            "starting_currency_definitions": [
                {"code": "USDT", "precision": 8, "currency_type": "CRYPTO", "iso4217": 0}
            ],
            "base_currency_definition": {
                "code": "USDT",
                "precision": 8,
                "currency_type": "CRYPTO",
                "iso4217": 0,
            },
        }
    )
    source = {
        "incarnation": str(UUID(int=10)),
        "client_id": "BINANCE",
        "account_id": "BINANCE-001",
        "venue": "BINANCE",
        "account_type": "CASH",
        "api_revision": "execution-account-capture-v1",
        "model_revision": "sandbox-account-state-v1",
        "runtime_version": "0.64.0",
        "configuration_json": configuration,
        "configuration_sha256": sha256(configuration.encode()).hexdigest(),
    }
    return {
        "status": "captured",
        "capture": {
            "node_instance_id": str(UUID(int=12)),
            "environment": "SANDBOX",
            "capture_sequence": 1,
            "frontier": [
                {"channel": channel, "applied": 0, "admitted": 0}
                for channel in NATIVE_CAPTURE_CHANNELS
            ],
            "snapshot": {
                "source": source,
                "observed_at": 1793750400123456789,
                "balances": [
                    {
                        "asset_code": "USDT",
                        "asset_precision": 8,
                        "asset_type": "CRYPTO",
                        "asset_iso4217": 0,
                        "total": "1",
                        "locked": "0",
                        "free": "1",
                    }
                ],
                "positions": [],
                "orders": [],
                "no_borrowing_model": True,
            },
        },
    }


def _manifest(complete=False):
    return {
        "runner_fact_contracts": {
            "risk": {
                "opening_inventory": {
                    "schema_revision": 1,
                    "source_models": ["unavailable", "sandbox-account-state-v1"],
                    "capture_apis": ["execution-account-capture-v1"],
                    "mapping_revisions": ["native_currency_codes_v1"],
                    "runtime_artifact_digests": ["e" * 64],
                    "complete_models": ["sandbox-account-state-v1"] if complete else [],
                }
            }
        }
    }


def _collector(response, *, complete=False, bound_source=None):
    from custos.engines.nautilus.native_opening_inventory import (
        NativeInventoryScope,
        NativeOpeningInventoryCollector,
        NativeRuntimeArtifactIdentity,
    )
    from tests.test_runner_fact_outbox import _authority

    class Handle:
        def source_json(self):
            return json.dumps(bound_source or _response()["capture"]["snapshot"]["source"])

        async def capture_json(self, timeout_ms=1000):
            return json.dumps(response, allow_nan=False)

    authority = _authority()
    scope = NativeInventoryScope(authority, "binance", "USDT", "BINANCE", "BINANCE-001", "BINANCE")
    artifact = NativeRuntimeArtifactIdentity("e" * 64, "0.64.0") if complete else None
    return NativeOpeningInventoryCollector(scope, Handle(), _manifest(complete), artifact)


@pytest.mark.parametrize("complete", [False, True])
async def test_native_converter_preserves_the_actual_boundary_and_requires_runtime_admission(
    complete,
):
    result = await _collector(_response(), complete=complete).collect()
    assert result.inventory["status"] == "observed"
    assert result.inventory["native_capture"]["observed_at_ns"] == 1793750400123456789
    assert result.inventory["observations"]["balances"]["rows"][0]["native_currency"] == {
        "precision": 8,
        "currency_type": "CRYPTO",
        "iso4217": 0,
    }
    assert result.inventory["inventory_complete"] is complete
    assert result.inventory["equity_binding"]["status"] == ("coherent" if complete else "unproven")


@pytest.mark.parametrize("mutation", ["extra", "float", "frontier", "source", "environment"])
async def test_native_converter_rejects_closed_shape_and_foreign_boundaries(mutation):
    from custos.core.runner_fact_producer import RunnerFactObservationRetired
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    response = deepcopy(_response())
    if mutation == "extra":
        response["capture"]["snapshot"]["balances"][0]["hidden"] = "1"
    elif mutation == "float":
        response["capture"]["snapshot"]["balances"][0]["total"] = 1.0
    elif mutation == "frontier":
        response["capture"]["frontier"][0]["admitted"] = 1
    elif mutation == "source":
        response["capture"]["snapshot"]["source"]["incarnation"] = str(UUID(int=13))
    else:
        response["capture"]["environment"] = "LIVE"
    if mutation == "source":
        with pytest.raises(RunnerFactObservationRetired):
            await _collector(response).collect()
    else:
        assert isinstance(await _collector(response).collect(), InventoryUnavailable)


def test_host_captures_native_handle_before_the_node_enters_its_hosted_run():
    from types import SimpleNamespace

    from custos.core.engine_protocol import EngineLifecycleAuthority
    from custos.engines.nautilus.host import NtTradingNodeHost
    from custos.engines.nautilus.native_opening_inventory import NativeOpeningInventoryCollector
    from tests.test_runner_fact_outbox import _authority

    source = _response()["capture"]["snapshot"]["source"]
    calls = []

    class Handle:
        def source_json(self):
            return json.dumps(source)

    class Node:
        def account_capture_handle(self, client_id):
            calls.append(str(client_id))
            return Handle()

    authority = _authority()
    lifecycle = EngineLifecycleAuthority(
        authority.deployment_instance_id,
        authority.deployment_spec_id,
        authority.deployment_spec_digest,
        authority.generation,
        authority.trading_mode,
    )
    deployment = SimpleNamespace(authority=authority, currency="USDT", connector="binance")
    host = NtTradingNodeHost(
        capability_receipt=SimpleNamespace(capability_manifest=_manifest(True))
    )
    provider = host._build_opening_inventory_provider(
        {"connector": "binance", "trading_mode": "sandbox"},
        lifecycle,
        node=Node(),
        deployment=deployment,
        client_name="BINANCE",
        exec_config=SimpleNamespace(account_id="BINANCE-001", venue="BINANCE", account_type="CASH"),
    )
    assert isinstance(provider, NativeOpeningInventoryCollector)
    assert calls == ["BINANCE"]
    assert provider.scope.authority == authority
    assert provider.source.configuration_sha256 == source["configuration_sha256"]


def test_host_keeps_the_installed_runtime_without_capture_api_explicitly_unavailable():
    from types import SimpleNamespace

    from custos.engines.nautilus.host import NtTradingNodeHost
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable
    from tests.test_runner_fact_outbox import _authority

    authority = _authority()
    host = NtTradingNodeHost(capability_receipt=SimpleNamespace(capability_manifest=_manifest()))
    result = host._build_opening_inventory_provider(
        {"connector": "binance", "trading_mode": "sandbox"},
        SimpleNamespace(trading_mode="sandbox"),
        node=object(),
        deployment=SimpleNamespace(authority=authority, currency="USDT"),
        client_name="BINANCE",
        exec_config=object(),
    )
    assert isinstance(result, InventoryUnavailable)
    assert result.reason == "native_capture_api_unavailable"


@pytest.mark.parametrize(
    "balances",
    [
        [],
        [
            {
                "asset_code": "BTC",
                "asset_precision": 8,
                "asset_type": "CRYPTO",
                "asset_iso4217": 0,
                "total": "0",
                "locked": "0",
                "free": "0",
            }
        ],
    ],
)
async def test_real_current_zero_account_does_not_need_a_synthetic_settlement_row(balances):
    response = _response()
    response["capture"]["snapshot"]["balances"] = balances
    result = await _collector(response, complete=True).collect()
    assert result.inventory["inventory_complete"] is True
    assert result.inventory["equity_binding"]["amount"] == "0"
    assert len(result.inventory["observations"]["balances"]["rows"]) == len(balances)


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize(
    "case", ["extra_balance", "liability", "position", "pending", "unknown_asset"]
)
async def test_native_rows_are_preserved_without_qualifying_unproved_cash(case, complete):
    response = _response()
    snapshot = response["capture"]["snapshot"]
    if case in {"extra_balance", "liability", "unknown_asset"}:
        snapshot["balances"].append(
            {
                "asset_code": "BTC" if case != "unknown_asset" else "UNKNOWN",
                "asset_precision": 8,
                "asset_type": "CRYPTO",
                "asset_iso4217": 0,
                "total": "-1" if case == "liability" else "1",
                "locked": "0",
                "free": "-1" if case == "liability" else "1",
            }
        )
    elif case == "position":
        snapshot["positions"] = [
            {
                "position_id": "hidden-position",
                "instrument_id": "ETHUSDT.BINANCE",
                "side": "LONG",
                "quantity": "1",
                "settlement_currency": "USDT",
            }
        ]
    else:
        snapshot["orders"] = [
            {
                "client_order_id": "pending-order",
                "venue_order_id": None,
                "instrument_id": "ETHUSDT.BINANCE",
                "order_type": "STOP_MARKET",
                "side": "BUY",
                "status": "SUBMITTED",
                "quantity": "1",
                "filled_quantity": "0",
                "price": None,
                "trigger_price": "100",
                "in_matching_engine": False,
                "pending": True,
            }
        ]
    result = await _collector(response, complete=complete).collect()
    assert result.inventory["status"] == "observed"
    assert result.inventory["inventory_complete"] is False
    assert result.inventory["equity_binding"]["status"] == "unproven"
    assert all(
        value is (True if complete else None) for value in result.inventory["completeness"].values()
    )
    for native_key, observation_key in (
        ("balances", "balances"),
        ("positions", "positions"),
        ("orders", "ordinary_orders"),
    ):
        assert len(result.inventory["observations"][observation_key]["rows"]) == len(
            snapshot[native_key]
        )


async def test_empty_account_with_wrong_currency_definition_cannot_claim_zero_equity():
    response = _response()
    snapshot = response["capture"]["snapshot"]
    snapshot["balances"] = []
    source = snapshot["source"]
    config = json.loads(source["configuration_json"])
    config["base_currency_definition"]["precision"] = 6
    source["configuration_json"] = json.dumps(config)
    source["configuration_sha256"] = sha256(source["configuration_json"].encode()).hexdigest()
    result = await _collector(response, complete=True, bound_source=source).collect()
    assert result.inventory["inventory_complete"] is False
    assert result.inventory["equity_binding"]["status"] == "unproven"
    assert result.inventory["observations"]["balances"]["rows"] == []


@pytest.mark.parametrize("change", ["same_sequence", "new_sequence", "new_node", "older_time"])
async def test_native_capture_sequence_distinguishes_replay_from_a_new_same_time_cut(change):
    from custos.core.runner_fact_producer import RunnerFactObservationRetired

    response = _response()
    collector = _collector(response, complete=True)
    first = await collector.collect()
    if change == "new_sequence":
        response["capture"]["capture_sequence"] += 1
        second = await collector.collect()
        assert (
            second.inventory["equity_binding"]["observed_at"]
            == first.inventory["equity_binding"]["observed_at"]
        )
        assert (
            second.inventory["equity_binding"]["capture_token"]
            != first.inventory["equity_binding"]["capture_token"]
        )
    else:
        if change == "new_node":
            response["capture"]["node_instance_id"] = str(UUID(int=99))
        elif change == "older_time":
            response["capture"]["capture_sequence"] += 1
            response["capture"]["snapshot"]["observed_at"] -= 1
        with pytest.raises(RunnerFactObservationRetired):
            await collector.collect()


@pytest.mark.parametrize(
    "reason", ["account_unavailable", "borrowed", "pending_ingress", "timed_out"]
)
async def test_native_unavailable_never_becomes_an_observed_zero_account(reason):
    from custos.engines.nautilus.opening_inventory import InventoryUnavailable

    assert isinstance(
        await _collector({"status": "unavailable", "reason": reason}, complete=True).collect(),
        InventoryUnavailable,
    )


@pytest.mark.parametrize("case,amount", [("cash", "100"), ("empty", "0"), ("btc_zero", "0")])
async def test_recorded_packaged_api_source_qualifies_without_an_invented_config_field(
    case, amount
):
    from pathlib import Path

    from custos.engines.nautilus.native_opening_inventory import (
        NativeInventoryScope,
        NativeOpeningInventoryCollector,
        NativeRuntimeArtifactIdentity,
    )
    from tests.test_runner_fact_outbox import _authority

    recorded = json.loads(
        (Path(__file__).parent / "fixtures/native_capture_packaged_v1.json").read_text()
    )["cases"][case]
    source = recorded["source"]
    assert "allow_borrowing" not in json.loads(source["configuration_json"])["client_config"]
    assert source["account_type"] == "CASH"
    assert recorded["response"]["capture"]["snapshot"]["no_borrowing_model"] is True

    class RecordedHandle:
        def source_json(self):
            return json.dumps(source)

        async def capture_json(self, timeout_ms=1000):
            return json.dumps(recorded["response"])

    # This registry is only a pure conversion fixture, not current deployment capability.
    manifest = _manifest(True)
    manifest["runner_fact_contracts"]["risk"]["opening_inventory"]["runtime_artifact_digests"] = [
        recorded["wheel_sha256"]
    ]
    scope = NativeInventoryScope(
        _authority(), "binance", "USD", source["client_id"], source["account_id"], source["venue"]
    )
    result = await NativeOpeningInventoryCollector(
        scope,
        RecordedHandle(),
        manifest,
        NativeRuntimeArtifactIdentity(recorded["wheel_sha256"], source["runtime_version"]),
    ).collect()
    assert result.inventory["equity_binding"]["status"] == "coherent"
    assert result.inventory["equity_binding"]["amount"] == amount
    assert len(result.inventory["observations"]["balances"]["rows"]) == len(
        recorded["response"]["capture"]["snapshot"]["balances"]
    )


async def test_cash_configuration_alone_cannot_replace_the_actual_owner_no_borrow_guarantee():
    response = _response()
    response["capture"]["snapshot"]["no_borrowing_model"] = False
    result = await _collector(response, complete=True).collect()
    assert result.inventory["equity_binding"]["status"] == "unproven"
    assert result.inventory["observations"]["liabilities"]["status"] == "unavailable"


async def test_margin_enumeration_does_not_prove_cash_liabilities_or_equity():
    response = _response()
    source = response["capture"]["snapshot"]["source"]
    source["account_type"] = "MARGIN"
    configuration = json.loads(source["configuration_json"])
    configuration["client_config"]["account_type"] = "MARGIN"
    source["configuration_json"] = json.dumps(configuration)
    source["configuration_sha256"] = sha256(source["configuration_json"].encode()).hexdigest()
    response["capture"]["snapshot"]["no_borrowing_model"] = False
    result = await _collector(response, complete=True, bound_source=source).collect()
    assert result.inventory["inventory_complete"] is False
    assert result.inventory["equity_binding"]["status"] == "unproven"
    assert result.inventory["observations"]["liabilities"]["status"] == "unavailable"
    assert result.inventory["completeness"] == {
        name: name != "liabilities" for name in result.inventory["completeness"]
    }
