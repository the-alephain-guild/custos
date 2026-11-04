from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest


def scope_spec():
    return {
        "deployment_instance_id": str(UUID(int=1)),
        "deployment_spec_id": str(UUID(int=2)),
        "deployment_spec_digest": "a" * 64,
        "generation": 1,
        "trading_mode": "testnet",
        "connector": "sodex_perpetual",
        "pairs": ["BTC-USD"],
        "nautilus_config": {
            "venue": {
                "wallet_address": "0x" + "a" * 40,
                "sodex_account_id": 42,
                "margin_mode": "cross",
                "settlement_currency": "USDC",
            }
        },
    }


async def test_collector_keeps_nonsettlement_assets_and_unconfigured_positions(monkeypatch):
    import json

    from custos.engines.nautilus.ledger_http import decode_exact_venue_response
    from custos.engines.nautilus.opening_inventory import InventoryAccountScope
    from custos.engines.nautilus.sodex_opening_inventory import SodexOpeningInventoryCollector

    scope = InventoryAccountScope.from_spec(scope_spec(), tenant_id="acme", runner_id=UUID(int=3))
    source = SodexOpeningInventoryCollector(scope)
    payloads = {
        "state": {"user": scope.wallet_address, "aid": scope.account_id},
        "balances": {
            "blockHeight": 42,
            "blockTime": 1793760000000,
            "balances": [
                {"id": 1, "coin": "USDC", "total": "100", "collateral": "10"},
                {"id": 2, "coin": "BTC", "total": "0.123456789123456789", "collateral": "0"},
            ],
        },
        "positions": {
            "blockHeight": 42,
            "blockTime": 1793760000000,
            "positions": [
                {
                    "id": 3,
                    "symbol": "ETH-USD",
                    "size": "-0.2",
                    "positionSide": "BOTH",
                    "active": True,
                    "avgEntryPrice": "2000",
                },
            ],
        },
        "orders": {"blockHeight": 42, "blockTime": 1793760000000, "orders": []},
    }
    monkeypatch.setattr(
        source._http,
        "get_exact",
        lambda path, params: decode_exact_venue_response(
            json.dumps({"code": "0", "data": payloads[path.rsplit("/", 1)[1]]}).encode()
        ),
    )

    inventory = await source.collect()

    assert [row.asset for row in inventory.balances.rows] == ["USDC", "BTC"]
    assert inventory.balances.rows[1].total == Decimal("0.123456789123456789")
    assert inventory.positions.rows[0].symbol == "ETH-USD"
    assert inventory.positions.rows[0].quantity == Decimal("-0.2")
    assert inventory.liabilities is None
    assert inventory.inventory_complete is False


def test_exact_raw_json_rejects_duplicate_keys_and_nonfinite_constants():
    import pytest

    from custos.engines.nautilus.ledger_http import VenueLedgerError, decode_exact_venue_response

    for payload in (
        b'{"code":"0","data":{"total":"1","total":"0"}}',
        b'{"code":"0","data":{"total":NaN}}',
        b'{"code":"0","data":{"total":Infinity}}',
    ):
        with pytest.raises(VenueLedgerError):
            decode_exact_venue_response(payload)


@pytest.mark.parametrize(
    ("wallet", "account", "height", "expected"),
    [
        ("0x" + "a" * 40, 42, 42, True),
        ("0x" + "b" * 40, 42, 42, False),
        ("0x" + "a" * 40, 43, 42, False),
        ("0x" + "a" * 40, 42, 0, None),
    ],
)
async def test_empty_account_identity_requires_matching_main_wallet_and_positive_height(
    monkeypatch, wallet, account, height, expected
):
    import json

    from custos.engines.nautilus.ledger_http import decode_exact_venue_response
    from custos.engines.nautilus.opening_inventory import InventoryAccountScope
    from custos.engines.nautilus.sodex_opening_inventory import SodexOpeningInventoryCollector

    scope = InventoryAccountScope.from_spec(scope_spec(), tenant_id="acme", runner_id=UUID(int=3))
    source = SodexOpeningInventoryCollector(scope)
    payloads = {
        "state": {"user": wallet, "aid": account},
        **{
            name: {"blockHeight": height, "blockTime": 1793760000000, name: []}
            for name in ("balances", "positions", "orders")
        },
    }
    monkeypatch.setattr(
        source._http,
        "get_exact",
        lambda path, params: decode_exact_venue_response(
            json.dumps({"code": "0", "data": payloads[path.rsplit("/", 1)[1]]}).encode()
        ),
    )
    result = await source.collect()

    assert result.account_identity_matches is expected
    assert result.completeness["identity"] is (False if expected is False else None)
    assert result.state.rows[0].wallet_address == wallet
    assert result.balances.block_height == height
    assert result.balances.rows == (() if height else None)
    assert result.inventory_complete is False


def populated_payloads():
    return {
        "state": {"user": "0x" + "a" * 40, "aid": 42},
        "balances": {
            "blockHeight": 42,
            "blockTime": 1793760000000,
            "balances": [
                {"id": 1, "coin": "USDC", "total": "100", "collateral": "10"},
            ],
        },
        "positions": {
            "blockHeight": 43,
            "blockTime": 1793760000001,
            "positions": [
                {
                    "id": 3,
                    "symbol": "ETH-USD",
                    "size": "-0.2",
                    "positionSide": "BOTH",
                    "active": True,
                    "avgEntryPrice": "2000",
                },
            ],
        },
        "orders": {
            "blockHeight": 44,
            "blockTime": 1793760000002,
            "orders": [
                {
                    "orderID": 4,
                    "symbol": "ETH-USD",
                    "side": "SELL",
                    "type": "LIMIT",
                    "status": "NEW",
                    "origQty": "0.4",
                    "executedQty": "0",
                    "price": "2010",
                },
            ],
        },
    }


async def collect_payloads(monkeypatch, payloads, *, spot=False):
    import json

    from custos.engines.nautilus.ledger_http import decode_exact_venue_response
    from custos.engines.nautilus.opening_inventory import InventoryAccountScope
    from custos.engines.nautilus.sodex_opening_inventory import SodexOpeningInventoryCollector

    spec = scope_spec()
    if spot:
        spec["connector"] = "sodex"
    source = SodexOpeningInventoryCollector(
        InventoryAccountScope.from_spec(spec, tenant_id="acme", runner_id=UUID(int=3))
    )
    requested = []

    def get(path, params):
        suffix = path.rsplit("/", 1)[1]
        requested.append((path, params))
        value = payloads[suffix]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, bytes):
            return decode_exact_venue_response(value)
        return decode_exact_venue_response(json.dumps({"code": "0", "data": value}).encode())

    monkeypatch.setattr(source._http, "get_exact", get)
    return await source.collect(), requested


@pytest.mark.parametrize("leg", ["state", "balances", "positions", "orders"])
async def test_one_failed_source_keeps_every_other_observed_source(monkeypatch, leg):
    from custos.engines.nautilus.ledger_http import VenueLedgerError

    payloads = populated_payloads()
    payloads[leg] = VenueLedgerError("fixture unavailable")
    inventory, _requested = await collect_payloads(monkeypatch, payloads)
    assert getattr(inventory, leg).rows is None
    for name in {"state", "balances", "positions", "orders"} - {leg}:
        assert getattr(inventory, name).rows is not None
        assert getattr(inventory, name).response_digest is not None
    assert inventory.inventory_complete is False


async def test_different_heights_and_ordinary_orders_never_imply_common_complete_boundary(
    monkeypatch,
):
    inventory, _requested = await collect_payloads(monkeypatch, populated_payloads())
    assert [
        inventory.balances.block_height,
        inventory.positions.block_height,
        inventory.orders.block_height,
    ] == [42, 43, 44]
    assert inventory.orders.rows[0].symbol == "ETH-USD"
    assert all(value is not True for value in inventory.completeness.values())
    assert inventory.inventory_complete is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origQty", "-1"),
        ("executedQty", "-1"),
        ("executedQty", "1"),
        ("price", "-1"),
    ],
)
async def test_invalid_order_amounts_are_unknown_not_zero(monkeypatch, field, value):
    payloads = populated_payloads()
    payloads["orders"]["orders"][0][field] = value
    inventory, _requested = await collect_payloads(monkeypatch, payloads)
    assert inventory.orders.rows is None
    assert inventory.balances.rows is not None


async def test_spot_locked_is_exact_and_positions_are_explicitly_not_read(monkeypatch):
    payloads = populated_payloads()
    payloads["balances"]["balances"] = [
        {
            "coin": "vBTC",
            "id": 1,
            "total": "9007199254740993.123456789123456789",
            "locked": "0.000000000000000001",
        }
    ]
    inventory, requested = await collect_payloads(monkeypatch, payloads, spot=True)
    balance = inventory.balances.rows[0]
    assert balance.available == Decimal("9007199254740993.123456789123456788")
    assert balance.collateral is None
    assert inventory.positions.rows is None
    assert inventory.positions.unavailable_reason == "spot_positions_not_applicable"
    assert all(not path.endswith("/positions") for path, _params in requested)


@pytest.mark.parametrize("kind", ["balances", "positions", "orders"])
async def test_duplicate_rows_make_the_dimension_unknown_without_discarding_other_sources(
    monkeypatch, kind
):
    payloads = populated_payloads()
    payloads[kind][kind].append(dict(payloads[kind][kind][0]))
    inventory, _requested = await collect_payloads(monkeypatch, payloads)
    assert getattr(inventory, kind).rows is None
    assert inventory.state.rows is not None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "bad", None, True])
async def test_bad_balance_amounts_cannot_become_zero(monkeypatch, value):
    payloads = populated_payloads()
    payloads["balances"]["balances"][0]["total"] = value
    inventory, _requested = await collect_payloads(monkeypatch, payloads)
    assert inventory.balances.rows is None
    assert inventory.positions.rows[0].quantity == Decimal("-0.2")


async def test_truncated_order_page_preserves_rows_but_cannot_claim_coverage(monkeypatch):
    payloads = populated_payloads()
    payloads["orders"]["hasMore"] = True
    payloads["orders"]["nextCursor"] = "private-cursor-do-not-log"
    inventory, _requested = await collect_payloads(monkeypatch, payloads)
    assert inventory.orders.rows is not None
    assert inventory.orders.pagination_hint == "indicated_more"
    assert inventory.inventory_complete is False


async def test_duplicate_coin_identity_is_not_hidden_by_a_different_symbol(monkeypatch):
    payloads = populated_payloads()
    payloads["balances"]["balances"].append({"id": 1, "coin": "BTC", "total": "1"})
    inventory, _requested = await collect_payloads(monkeypatch, payloads)
    assert inventory.balances.rows is None
