"""Typed local opening observations; none of these observations authorizes trading."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Generic, TypeVar
from uuid import UUID

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.venue_config import venue_options

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class InventoryAccountScope:
    tenant_id: str
    runner_id: UUID
    authority: EngineLifecycleAuthority
    connector: str
    wallet_address: str
    account_id: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.tenant_id):
            raise ValueError("opening inventory tenant is invalid")
        if not isinstance(self.runner_id, UUID) or self.runner_id.int == 0:
            raise ValueError("opening inventory runner is invalid")
        if self.authority.trading_mode not in {"testnet", "live"}:
            raise ValueError("opening inventory requires a real venue mode")
        if self.connector not in {"sodex", "sodex_perpetual"}:
            raise ValueError("opening inventory connector is unsupported")
        if not isinstance(self.wallet_address, str) or not re.fullmatch(
            r"0x[0-9a-fA-F]{40}", self.wallet_address
        ):
            raise ValueError("opening inventory requires the main wallet address")
        if type(self.account_id) is not int or not 0 < self.account_id < 2**64:
            raise ValueError("opening inventory account id is invalid")

    @classmethod
    def from_spec(cls, spec: dict, *, tenant_id: str, runner_id: UUID):
        if type(spec.get("generation")) is not int:
            raise ValueError("opening inventory generation must be an integer")
        options = venue_options(
            spec, {"wallet_address", "sodex_account_id", "margin_mode", "settlement_currency"}
        )
        return cls(
            tenant_id,
            runner_id,
            EngineLifecycleAuthority.from_spec(spec),
            spec["connector"],
            options.get("wallet_address"),
            options.get("sodex_account_id"),
        )

    @property
    def network(self) -> str:
        return "testnet" if self.authority.trading_mode == "testnet" else "mainnet"

    @property
    def account_type(self) -> str:
        return "perps" if self.connector == "sodex_perpetual" else "spot"


@dataclass(frozen=True, slots=True)
class InventoryBalance:
    asset_id: int
    asset: str
    total: Decimal
    locked: Decimal | None
    collateral: Decimal | None
    available: Decimal | None


@dataclass(frozen=True, slots=True)
class InventoryReportedAccount:
    wallet_address: str
    account_id: int


@dataclass(frozen=True, slots=True)
class InventoryPosition:
    position_id: int
    symbol: str
    quantity: Decimal
    active: bool
    position_side: str
    average_entry_price: Decimal


@dataclass(frozen=True, slots=True)
class InventoryOrder:
    order_id: int
    symbol: str
    side: str
    order_type: str
    status: str
    original_quantity: Decimal
    executed_quantity: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class InventoryRead(Generic[T]):
    rows: tuple[T, ...] | None
    requested_at: datetime
    received_at: datetime
    block_height: int | None
    source_time: datetime | None
    unavailable_reason: str | None
    response_digest: str | None = None
    pagination_hint: str = "unknown"


@dataclass(frozen=True, slots=True)
class OpeningAccountInventory:
    scope: InventoryAccountScope
    started_at: datetime
    completed_at: datetime
    state: InventoryRead[InventoryReportedAccount]
    balances: InventoryRead[InventoryBalance]
    positions: InventoryRead[InventoryPosition]
    orders: InventoryRead[InventoryOrder]
    reasons: tuple[str, ...]

    @property
    def account_identity_matches(self) -> bool | None:
        if self.state.rows is None:
            return None
        reported = self.state.rows[0]
        if (
            reported.wallet_address.lower() != self.scope.wallet_address.lower()
            or reported.account_id != self.scope.account_id
        ):
            return False
        heights = [
            section.block_height
            for section in (self.state, self.balances, self.positions, self.orders)
            if section.block_height is not None
        ]
        if not heights or 0 in heights:
            return None
        return True

    @property
    def completeness(self) -> Mapping[str, bool | None]:
        return MappingProxyType(
            {
                "identity": False if self.account_identity_matches is False else None,
                "balances": None,
                "liabilities": None,
                "positions": None,
                "ordinary_orders": None,
                "order_kinds": None,
                "pagination": None,
                "common_boundary": None,
            }
        )

    @property
    def liabilities(self) -> None:
        return None

    @property
    def inventory_complete(self) -> bool:
        # Endpoint pagination, liabilities, hidden order categories, signer
        # ownership and common-snapshot semantics remain unproven.
        return False


@dataclass(frozen=True, slots=True)
class InventoryUnavailable:
    authority: EngineLifecycleAuthority
    connector: str
    reason: str
    scope: InventoryAccountScope | None = None
    tenant_id: str | None = None
    runner_id: UUID | None = None

    @property
    def inventory_complete(self) -> bool:
        return False
