"""Conservative independent SoDEX opening reads, separate from strategy ledgers."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from decimal import localcontext

from custos.engines.nautilus.ledger_http import (
    ReadOnlyVenueHttp,
    VenueLedgerError,
    decimal,
    rows,
    timestamp_ms,
)
from custos.engines.nautilus.opening_inventory import (
    InventoryAccountScope,
    InventoryBalance,
    InventoryOrder,
    InventoryPosition,
    InventoryRead,
    InventoryReportedAccount,
    InventoryUnavailable,
    OpeningAccountInventory,
)

_MAX_AMOUNT_DIGITS = 96
_MAX_AMOUNT_EXPONENT = 96


def _token(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise VenueLedgerError("opening inventory token is invalid")
    return value


def _uint(value: object, *, positive: bool = True) -> int:
    if type(value) is not int or not (0 < value < 2**64 if positive else 0 <= value < 2**64):
        raise VenueLedgerError("opening inventory integer is invalid")
    return value


def _amount(value, field):
    result = decimal(value, field)
    if (
        len(result.as_tuple().digits) > _MAX_AMOUNT_DIGITS
        or abs(result.as_tuple().exponent) > _MAX_AMOUNT_EXPONENT
    ):
        raise VenueLedgerError("opening inventory amount exceeds the observation bound")
    return result


class SodexOpeningInventoryCollector:
    def __init__(self, scope: InventoryAccountScope) -> None:
        self.scope = scope
        self._http = ReadOnlyVenueHttp(
            f"https://{scope.network}-gw.sodex.dev/api/v1/{scope.account_type}",
            minimum_interval=0.1,
        )

    def _read(self, suffix: str):
        return self._http.get_exact(
            f"/accounts/{self.scope.wallet_address}/{suffix}", {"accountID": self.scope.account_id}
        )

    async def collect(self) -> OpeningAccountInventory | InventoryUnavailable:
        return await asyncio.to_thread(self._collect)

    def _collect(self) -> OpeningAccountInventory | InventoryUnavailable:
        started = datetime.now(UTC)
        state = self._section("state", self._account)
        balances = self._section("balances", self._balance)
        positions = (
            self._section("positions", self._position)
            if self.scope.account_type == "perps"
            else InventoryRead(
                None, started, datetime.now(UTC), None, None, "spot_positions_not_applicable"
            )
        )
        orders = self._section("orders", self._order)
        if all(section.rows is None for section in (state, balances, positions, orders)):
            return InventoryUnavailable(
                self.scope.authority,
                self.scope.connector,
                "all_account_reads_unavailable",
                self.scope,
                self.scope.tenant_id,
                self.scope.runner_id,
            )
        return OpeningAccountInventory(
            self.scope,
            started,
            datetime.now(UTC),
            state,
            balances,
            positions,
            orders,
            (
                "liabilities_unknown",
                "signer_binding_unverified",
                "pagination_unverified",
                "order_kind_coverage_unverified",
                "common_boundary_unverified",
            ),
        )

    def _section(self, name, parser):
        requested = datetime.now(UTC)
        height, source_time, digest = None, None, None
        failure = "request_or_json_unavailable"
        try:
            response = self._read(name)
            digest, snapshot = response.payload_digest, response.data
            failure = "malformed_response"
            if not isinstance(snapshot, dict):
                raise VenueLedgerError("opening inventory snapshot is invalid")
            failure = "invalid_source_boundary"
            if name != "state" or "blockHeight" in snapshot:
                height = _uint(snapshot["blockHeight"], positive=False)
                if height == 0:
                    raise VenueLedgerError("opening inventory has an unproven zero chain height")
            if name != "state" or "blockTime" in snapshot:
                source_time = timestamp_ms(_uint(snapshot["blockTime"]))
            failure = "malformed_rows"
            parsed = (
                (parser(snapshot),)
                if name == "state"
                else tuple(parser(row) for row in rows(snapshot[name]))
            )
            identities = [
                row.asset.casefold()
                if name == "balances"
                else row.position_id
                if name == "positions"
                else row.order_id
                if name == "orders"
                else row.account_id
                for row in parsed
            ]
            failure = "duplicate_identities"
            if len(set(identities)) != len(identities) or (
                name == "balances" and len({row.asset_id for row in parsed}) != len(parsed)
            ):
                raise VenueLedgerError("opening inventory contains duplicate identities")
            more = snapshot.get("hasMore") is True or snapshot.get("nextCursor") not in (None, "")
            return InventoryRead(
                parsed,
                requested,
                datetime.now(UTC),
                height,
                source_time,
                None,
                digest,
                "indicated_more" if more else "unknown",
            )
        except (VenueLedgerError, ValueError, TypeError, KeyError, OSError, OverflowError):
            return InventoryRead(
                None,
                requested,
                datetime.now(UTC),
                height,
                source_time,
                f"{name}_{failure}",
                digest,
            )

    @staticmethod
    def _account(row):
        wallet = row["user"]
        if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
            raise VenueLedgerError("opening account state wallet is invalid")
        return InventoryReportedAccount(wallet, _uint(row["aid"]))

    def _balance(self, row):
        asset = _token(row["coin"])
        total = _amount(row["total"], "total")
        locked = _amount(row["locked"], "locked") if "locked" in row else None
        collateral = _amount(row["collateral"], "collateral") if "collateral" in row else None
        if (locked is not None and locked < 0) or (collateral is not None and collateral < 0):
            raise VenueLedgerError("opening balance reservation is negative")
        available = None
        if self.scope.account_type == "spot" and locked is not None:
            with localcontext() as context:
                # Exact subtraction across both bounded coefficients and scales.
                context.prec = 2 * (_MAX_AMOUNT_DIGITS + _MAX_AMOUNT_EXPONENT)
                available = total - locked
        return InventoryBalance(
            _uint(row["id"], positive=False), asset, total, locked, collateral, available
        )

    @staticmethod
    def _position(row):
        if type(row["active"]) is not bool:
            raise VenueLedgerError("opening position active flag is invalid")
        return InventoryPosition(
            _uint(row["id"]),
            _token(row["symbol"]),
            _amount(row["size"], "size"),
            row["active"],
            _token(row["positionSide"]),
            _amount(row["avgEntryPrice"], "entry price"),
        )

    @staticmethod
    def _order(row):
        original = _amount(row["origQty"], "original quantity")
        executed = _amount(row["executedQty"], "executed quantity")
        price = _amount(row["price"], "price")
        if original < 0 or executed < 0 or executed > original or price < 0:
            raise VenueLedgerError("opening order amounts are invalid")
        return InventoryOrder(
            _uint(row["orderID"]),
            _token(row["symbol"]),
            _token(row["side"]),
            _token(row["type"]),
            _token(row["status"]),
            original,
            executed,
            price,
        )
