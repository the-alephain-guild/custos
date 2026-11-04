from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from custos.engines.nautilus.ledger_http import (
    VenueLedgerError,
    decode_exact_venue_response,
)
from custos.engines.nautilus.opening_inventory import InventoryAccountScope
from custos.engines.nautilus.sodex_opening_inventory import SodexOpeningInventoryCollector
from tests.test_sodex_opening_inventory import collect_payloads, populated_payloads, scope_spec


@pytest.mark.parametrize("wallet", ["SODEX_PERPS-001", "test-key", "0x123", None])
def test_routing_labels_and_key_identifiers_are_not_main_wallets(wallet):
    spec = scope_spec()
    spec["nautilus_config"]["venue"]["wallet_address"] = wallet
    with pytest.raises((TypeError, ValueError)):
        InventoryAccountScope.from_spec(spec, tenant_id="acme", runner_id=UUID(int=3))


async def test_callers_cannot_set_complete_or_mutate_the_derived_coverage(monkeypatch):
    result, _requested = await collect_payloads(monkeypatch, populated_payloads())
    with pytest.raises(TypeError):
        replace(result, inventory_complete=True)
    with pytest.raises(TypeError):
        result.completeness["balances"] = True
    assert result.inventory_complete is False


def test_raw_decimal_numbers_keep_their_exact_digits():
    response = decode_exact_venue_response(
        b'{"code":"0","data":{"total":9007199254740993.123456789123456789}}'
    )
    assert response.data["total"] == Decimal("9007199254740993.123456789123456789")


@pytest.mark.parametrize(
    ("connector", "mode", "network", "market", "count"),
    [
        ("sodex_perpetual", "testnet", "testnet", "perps", 4),
        ("sodex", "live", "mainnet", "spot", 3),
    ],
)
async def test_actual_transport_is_get_only_and_addresses_the_bound_public_account(
    monkeypatch, connector, mode, network, market, count
):
    spec = scope_spec()
    spec.update(connector=connector, trading_mode=mode)
    scope = InventoryAccountScope.from_spec(spec, tenant_id="acme", runner_id=UUID(int=3))
    source = SodexOpeningInventoryCollector(scope)
    calls = []
    payloads = populated_payloads()

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            assert limit > 0
            return self.payload

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.get_method() == "GET"
            assert request.data is None
            assert request.full_url.startswith(
                f"https://{network}-gw.sodex.dev/api/v1/{market}/accounts/{scope.wallet_address}/"
            )
            assert request.full_url.endswith("accountID=42")
            suffix = request.full_url.split("?", 1)[0].rsplit("/", 1)[1]
            return Response(json.dumps({"code": "0", "data": payloads[suffix]}).encode())

    monkeypatch.setattr(source._http, "_opener", Opener())
    result = await source.collect()
    assert len(calls) == count
    assert result.inventory_complete is False


async def test_raw_corrupt_json_only_invalidates_its_own_leg(monkeypatch):
    payloads = populated_payloads()
    payloads["balances"] = b'{"code":"0","data":{"balances":[],"balances":[]}}'
    result, _requested = await collect_payloads(monkeypatch, payloads)
    assert result.balances.rows is None
    assert result.orders.rows is not None


def test_oversized_raw_json_is_refused_even_without_a_network_request():
    with pytest.raises(VenueLedgerError):
        decode_exact_venue_response(
            b'{"code":"0","data":{"padding":"' + b"x" * (16 * 1024 * 1024) + b'"}}'
        )
