"""Convert typed adapter observations to the closed opening fact domain."""

from __future__ import annotations

from typing import Any

from custos.core.opening_inventory_contract import (
    Balance,
    IdentityObservation,
    Observation,
    Order,
    Position,
    unavailable_inventory_read,
    unavailable_opening_inventory,
)
from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactContractError,
    _signed_decimal,
    _timestamp,
)


def opening_inventory_observation(
    authority: RunnerFactAuthority, connector: str, observation: object
) -> dict[str, Any]:
    from custos.engines.nautilus.opening_inventory import OpeningAccountInventory

    result = unavailable_opening_inventory(authority, connector, "source_unavailable")
    if not isinstance(observation, OpeningAccountInventory):
        return result
    scope = observation.scope
    expected = result["scope"]
    if (
        scope.tenant_id != authority.tenant_id
        or scope.runner_id != authority.runner_id
        or scope.connector != connector
        or scope.authority.trading_mode != authority.trading_mode
        or str(scope.authority.deployment_instance_id) != str(authority.deployment_instance_id)
        or str(scope.authority.deployment_spec_id) != str(authority.deployment_spec_id)
        or scope.authority.deployment_spec_digest != authority.deployment_spec_digest
        or scope.authority.generation != authority.generation
    ):
        result["reasons"] = ["source_identity_mismatch"]
        return result
    expected["source"] = {
        "kind": "sodex_venue_account",
        "network": scope.network,
        "main_wallet": scope.wallet_address,
        "account_id": scope.account_id,
        "account_type": scope.account_type,
    }
    result["capability_binding"].update(
        capture_api_revision="sodex_public_get_v1", model_revision="sodex_public_account_v1"
    )
    result["completeness"] = dict(observation.completeness)
    result["equity_binding"]["reason"] = "independent_risk_observation"
    result["reasons"] = [
        "source_identity_unverified",
        "liabilities_unverified",
        "pagination_unverified",
        "order_kinds_unverified",
        "common_boundary_unverified",
        "denomination_unmapped",
    ]

    def money(value):
        return None if value is None else _signed_decimal(value, "opening amount", bounded=True)

    def identity(row):
        return {
            "kind": "sodex_account",
            "main_wallet": row.wallet_address,
            "account_id": row.account_id,
        }

    def balance(row):
        return {
            "raw_asset_id": str(row.asset_id),
            "raw_asset_code": row.asset,
            "native_currency": None,
            "namespace": f"sodex.{scope.network}.{scope.account_type}",
            "denomination": None,
            "mapping_revision": None,
            **{
                name: money(getattr(row, name))
                for name in ("total", "locked", "collateral", "available")
            },
        }

    def position(row):
        return {
            "position_id": str(row.position_id),
            "instrument": row.symbol,
            "quantity": money(row.quantity),
            "active": row.active,
            "position_side": row.position_side,
            "average_entry_price": money(row.average_entry_price),
            "settlement_currency": None,
        }

    def order(row):
        return {
            "order_id": str(row.order_id),
            "client_order_id": None,
            "venue_order_id": None,
            "trigger_price": None,
            "in_matching_engine": None,
            "pending": None,
            "instrument": row.symbol,
            "side": row.side,
            "order_type": row.order_type,
            "status": row.status,
            **{
                name: money(getattr(row, name))
                for name in ("original_quantity", "executed_quantity", "price")
            },
        }

    for name, read, parser, model in (
        ("identity", observation.state, identity, IdentityObservation),
        ("balances", observation.balances, balance, Observation[Balance]),
        ("positions", observation.positions, position, Observation[Position]),
        ("ordinary_orders", observation.orders, order, Observation[Order]),
    ):
        converted = {
            **unavailable_inventory_read("source_unavailable"),
            "requested_at": _timestamp(read.requested_at, "requested_at"),
            "received_at": _timestamp(read.received_at, "received_at"),
            "source_time": None
            if read.source_time is None
            else _timestamp(read.source_time, "source_time"),
            "block_height": read.block_height,
            "response_digest": read.response_digest,
            "pagination": read.pagination_hint,
        }
        if read.rows is not None:
            try:
                converted.update(
                    status="observed", rows=[parser(row) for row in read.rows], reason=None
                )
                converted = model.model_validate(converted).model_dump(mode="json")
            except (ValueError, RunnerFactContractError):
                converted.update(
                    status="unavailable", rows=None, reason="wire_dimension_unavailable"
                )
        result["observations"][name] = converted
    result["status"] = (
        "observed"
        if any(read["status"] == "observed" for read in result["observations"].values())
        else "unavailable"
    )
    return result
