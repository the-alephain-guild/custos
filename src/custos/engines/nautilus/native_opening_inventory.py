"""Read the native owner handle and convert only its closed, exact JSON domain."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.opening_inventory_contract import (
    COMPLETENESS_FIELDS,
    MAX_INVENTORY_ROWS,
    NATIVE_CURRENCY_DEFINITIONS,
    U64,
    Money,
    NativeCapture,
    NativeFrontier,
    OpeningInventory,
    PositiveU64,
    SandboxSource,
    Token,
    UnsignedMoney,
    UuidText,
    native_capture_token,
    require_opening_inventory_support,
    unavailable_opening_inventory,
)
from custos.core.runner_fact import RunnerFactAuthority, RunnerFactContractError
from custos.core.runner_fact_producer import RunnerFactObservationRetired
from custos.core.utc_time import render_utc_nanos
from custos.engines.nautilus.opening_inventory import InventoryUnavailable

MAX_NATIVE_CAPTURE_BYTES = 768 * 1024
NATIVE_UNAVAILABLE_REASONS = {
    "not_running",
    "retired",
    "queue_full",
    "queue_closed",
    "timed_out",
    "cancelled",
    "pending_ingress",
    "counter_overflow",
    "foreign_runtime",
    "unsupported_runtime",
    "borrowed",
    "account_unavailable",
    "unsupported_configuration",
    "inconsistent_state",
}


class NativeCaptureHandle(Protocol):
    def source_json(self) -> str: ...

    async def capture_json(self, timeout_ms: int = 1000) -> str: ...


@dataclass(frozen=True, slots=True)
class NativeRuntimeArtifactIdentity:
    """Installed runtime evidence supplied by verified runtime composition."""

    artifact_digest: str
    crate_version: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_digest) or not self.crate_version:
            raise ValueError("native runtime artifact identity is invalid")


@dataclass(frozen=True, slots=True)
class NativeInventoryScope:
    authority: RunnerFactAuthority
    connector: str
    currency: str
    client_id: str
    account_id: str
    venue: str

    def __post_init__(self):
        if self.authority.trading_mode != "sandbox":
            raise ValueError("native opening scope requires sandbox mode")
        if not all((self.client_id, self.account_id, self.venue)):
            raise ValueError("native opening scope requires actual registry identities")


@dataclass(frozen=True, slots=True)
class NativeOpeningAccountInventory:
    scope: NativeInventoryScope
    inventory: Mapping[str, Any]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _CurrencyDefinition(_Closed):
    code: Token
    precision: int = Field(ge=0, le=255)
    currency_type: Literal["FIAT", "CRYPTO"]
    iso4217: int = Field(ge=0, le=65535)


class _Configuration(_Closed):
    client_config: dict[str, Any]
    starting_currency_definitions: list[_CurrencyDefinition] = Field(max_length=MAX_INVENTORY_ROWS)
    base_currency_definition: _CurrencyDefinition | None


class _Balance(_Closed):
    asset_code: Token
    asset_precision: int = Field(ge=0, le=255)
    asset_type: Literal["FIAT", "CRYPTO"]
    asset_iso4217: int = Field(ge=0, le=65535)
    total: Money
    locked: UnsignedMoney
    free: Money


class _Position(_Closed):
    position_id: Token
    instrument_id: Token
    side: Literal["LONG", "SHORT", "FLAT"]
    quantity: UnsignedMoney
    settlement_currency: Token


class _Order(_Closed):
    client_order_id: Token
    venue_order_id: Token | None
    instrument_id: Token
    order_type: Literal[
        "MARKET",
        "LIMIT",
        "STOP_MARKET",
        "STOP_LIMIT",
        "MARKET_TO_LIMIT",
        "MARKET_IF_TOUCHED",
        "LIMIT_IF_TOUCHED",
        "TRAILING_STOP_MARKET",
        "TRAILING_STOP_LIMIT",
    ]
    side: Literal["BUY", "SELL"]
    status: Literal[
        "INITIALIZED",
        "DENIED",
        "EMULATED",
        "RELEASED",
        "SUBMITTED",
        "ACCEPTED",
        "REJECTED",
        "CANCELED",
        "EXPIRED",
        "TRIGGERED",
        "PENDING_UPDATE",
        "PENDING_CANCEL",
        "PARTIALLY_FILLED",
        "FILLED",
        "VOIDED",
    ]
    quantity: UnsignedMoney
    filled_quantity: UnsignedMoney
    price: UnsignedMoney | None
    trigger_price: UnsignedMoney | None
    in_matching_engine: bool
    pending: bool


class _Snapshot(_Closed):
    source: dict[str, Any]
    observed_at: U64
    balances: list[_Balance] = Field(max_length=MAX_INVENTORY_ROWS)
    positions: list[_Position] = Field(max_length=MAX_INVENTORY_ROWS)
    orders: list[_Order] = Field(max_length=MAX_INVENTORY_ROWS)
    no_borrowing_model: bool


class _Capture(_Closed):
    node_instance_id: UuidText
    environment: Literal["SANDBOX"]
    capture_sequence: PositiveU64
    frontier: list[NativeFrontier] = Field(min_length=9, max_length=9)
    snapshot: _Snapshot


def _json_object(text: str) -> dict[str, Any]:
    if type(text) is not str or len(text.encode("utf-8")) > MAX_NATIVE_CAPTURE_BYTES:
        raise ValueError("native capture JSON exceeds its boundary")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("native capture JSON repeats an object key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("native capture JSON contains a nonfinite number")

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if type(result) is not dict:
        raise ValueError("native capture JSON must be an object")
    return result


def _source(value: dict[str, Any]) -> SandboxSource:
    if set(value) != set(SandboxSource.model_fields) - {"kind"}:
        raise ValueError("native source differs from its closed API")
    return SandboxSource.model_validate({"kind": "sandbox_engine", **value})


class NativeOpeningInventoryCollector:
    def __init__(
        self,
        scope: NativeInventoryScope,
        handle: NativeCaptureHandle,
        manifest: Mapping[str, Any],
        artifact: NativeRuntimeArtifactIdentity | None = None,
    ):
        self.scope = scope
        self._handle = handle
        self._manifest = json.loads(json.dumps(manifest, allow_nan=False))
        self._artifact = artifact
        self.source = _source(_json_object(handle.source_json()))
        if any(
            getattr(self.source, field) != getattr(scope, field)
            for field in ("client_id", "account_id", "venue")
        ):
            raise ValueError("native registry source differs from its actual host scope")
        self._configuration = _Configuration.model_validate(
            _json_object(self.source.configuration_json)
        )
        configured = self._configuration.client_config
        if any(
            configured.get(field) != getattr(self.source, field)
            for field in ("account_id", "venue", "account_type")
        ):
            raise ValueError("native source configuration differs from its registry identity")
        self._last_node: str | None = None
        self._last_sequence = 0
        self._last_observed_ns = 0

    async def collect(self) -> NativeOpeningAccountInventory | InventoryUnavailable:
        requested_at = render_utc_nanos(time.time_ns())
        try:
            result = _json_object(await self._handle.capture_json(timeout_ms=1000))
            if result.get("status") == "unavailable":
                if (
                    set(result) != {"status", "reason"}
                    or result["reason"] not in NATIVE_UNAVAILABLE_REASONS
                ):
                    raise ValueError("native unavailable result has an invalid shape")
                if result["reason"] in {"retired", "foreign_runtime"}:
                    raise RunnerFactObservationRetired("native capture owner was retired")
                return self._unavailable("opening_native_unavailable")
            if set(result) != {"status", "capture"} or result.get("status") != "captured":
                raise ValueError("native capture result has an invalid status")
            capture = _Capture.model_validate(result["capture"])
            if _source(capture.snapshot.source) != self.source:
                raise RunnerFactObservationRetired("native capture source was replaced")
            if (
                self._last_node not in {None, capture.node_instance_id}
                or capture.capture_sequence <= self._last_sequence
                or capture.snapshot.observed_at < self._last_observed_ns
            ):
                raise RunnerFactObservationRetired(
                    "native capture boundary was retired or replayed"
                )
            received_at = render_utc_nanos(time.time_ns())
            inventory = self._convert(capture, requested_at, received_at)
            self._last_node = capture.node_instance_id
            self._last_sequence = capture.capture_sequence
            self._last_observed_ns = capture.snapshot.observed_at
            return NativeOpeningAccountInventory(self.scope, inventory)
        except RunnerFactObservationRetired:
            raise
        except Exception:
            return self._unavailable("opening_native_invalid")

    def _unavailable(self, reason):
        return InventoryUnavailable(
            EngineLifecycleAuthority(
                self.scope.authority.deployment_instance_id,
                self.scope.authority.deployment_spec_id,
                self.scope.authority.deployment_spec_digest,
                self.scope.authority.generation,
                self.scope.authority.trading_mode,
            ),
            self.scope.connector,
            reason,
            tenant_id=self.scope.authority.tenant_id,
            runner_id=self.scope.authority.runner_id,
        )

    def _convert(self, capture: _Capture, requested_at: str, received_at: str) -> dict[str, Any]:
        snapshot = capture.snapshot
        source = self.source.model_dump(mode="json")
        descriptor = NativeCapture.model_validate(
            {
                "node_instance_id": capture.node_instance_id,
                "environment": capture.environment,
                "capture_sequence": capture.capture_sequence,
                "frontier": [entry.model_dump(mode="json") for entry in capture.frontier],
                "observed_at_ns": snapshot.observed_at,
                "no_borrowing_model": snapshot.no_borrowing_model,
            }
        ).model_dump(mode="json")
        token = native_capture_token(source, descriptor)
        at = render_utc_nanos(snapshot.observed_at)
        result = unavailable_opening_inventory(self.scope.authority, self.scope.connector)
        result.update(status="observed", native_capture=descriptor, reasons=["capture_unavailable"])
        result["scope"]["source"] = source
        artifact = self._artifact
        result["capability_binding"].update(
            capture_api_revision=self.source.api_revision,
            model_revision=self.source.model_revision,
            mapping_revision="native_currency_codes_v1",
            runtime_artifact_digest=(
                artifact.artifact_digest
                if artifact is not None and artifact.crate_version == self.source.runtime_version
                else None
            ),
        )

        def read(rows):
            return {
                "status": "observed",
                "rows": rows,
                "requested_at": requested_at,
                "received_at": received_at,
                "source_time": at,
                "block_height": None,
                "capture_token": token,
                "response_digest": None,
                "pagination": "owner_full",
                "reason": None,
            }

        balances = []
        for balance in snapshot.balances:
            native = {
                "precision": balance.asset_precision,
                "currency_type": balance.asset_type,
                "iso4217": balance.asset_iso4217,
            }
            mapped = NATIVE_CURRENCY_DEFINITIONS.get(balance.asset_code) == (
                balance.asset_precision,
                balance.asset_type,
                balance.asset_iso4217,
            )
            balances.append(
                {
                    "raw_asset_id": None,
                    "raw_asset_code": balance.asset_code,
                    "native_currency": native,
                    "namespace": "native.currency",
                    "denomination": balance.asset_code if mapped else None,
                    "mapping_revision": "native_currency_codes_v1" if mapped else None,
                    "total": balance.total,
                    "locked": balance.locked,
                    "available": balance.free,
                    "collateral": None,
                }
            )
        result["observations"] = {
            "identity": read(
                [
                    {
                        "kind": "sandbox_account",
                        **{
                            key: source[key]
                            for key in (
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
            ),
            "balances": read(balances),
            "liabilities": {
                **read(None),
                "status": "not_applicable" if snapshot.no_borrowing_model else "unavailable",
                "reason": "model_not_applicable"
                if snapshot.no_borrowing_model
                else "liabilities_unverified",
            },
            "positions": read(
                [
                    {
                        "position_id": row.position_id,
                        "instrument": row.instrument_id,
                        "quantity": row.quantity,
                        "active": row.side != "FLAT" or Decimal(row.quantity) != 0,
                        "position_side": row.side,
                        "average_entry_price": None,
                        "settlement_currency": row.settlement_currency,
                    }
                    for row in snapshot.positions
                ]
            ),
            "ordinary_orders": read(
                [
                    {
                        "order_id": row.client_order_id,
                        "client_order_id": row.client_order_id,
                        "venue_order_id": row.venue_order_id,
                        "instrument": row.instrument_id,
                        "side": row.side,
                        "order_type": row.order_type,
                        "status": row.status,
                        "original_quantity": row.quantity,
                        "executed_quantity": row.filled_quantity,
                        "price": row.price,
                        "trigger_price": row.trigger_price,
                        "in_matching_engine": row.in_matching_engine,
                        "pending": row.pending,
                    }
                    for row in snapshot.orders
                ]
            ),
        }
        # Full enumeration is distinct from the ability to value that inventory
        # as settlement cash. Both claims still require actual runtime admission.
        observed = json.loads(json.dumps(result))
        observed["completeness"] = dict.fromkeys(COMPLETENESS_FIELDS, True)
        observed["completeness"]["liabilities"] = (
            self.source.account_type == "CASH" and snapshot.no_borrowing_model
        )
        try:
            observed = OpeningInventory.model_validate(observed).model_dump(mode="json")
            require_opening_inventory_support(observed, self._manifest)
        except (ValueError, RunnerFactContractError):
            observed = result
        result = observed
        candidate = json.loads(json.dumps(result))
        candidate["completeness"] = dict.fromkeys(COMPLETENESS_FIELDS, True)
        candidate["inventory_complete"] = True
        total = next(
            (row.total for row in snapshot.balances if row.asset_code == self.scope.currency), "0"
        )
        candidate["equity_binding"] = {
            "status": "coherent",
            "capture_token": token,
            "currency": self.scope.currency,
            "amount": total,
            "observed_at": at,
            "valuation_rule": "settlement_cash_total_v1",
            "reason": None,
        }
        try:
            if snapshot.orders:
                raise RunnerFactContractError("native cash qualification requires no open intents")
            configured = self._configuration.client_config
            if self.source.account_type != "CASH" or not snapshot.no_borrowing_model:
                raise RunnerFactContractError(
                    "native configuration does not prove a no-borrow cash model"
                )
            definitions = list(self._configuration.starting_currency_definitions)
            base = self._configuration.base_currency_definition
            if base is not None:
                definitions.append(base)
            if (base.code if base is not None else None) != configured.get("base_currency"):
                raise RunnerFactContractError(
                    "native base currency definition differs from configuration"
                )
            for definition in definitions:
                if NATIVE_CURRENCY_DEFINITIONS.get(definition.code) != (
                    definition.precision,
                    definition.currency_type,
                    definition.iso4217,
                ):
                    raise RunnerFactContractError("native configured denomination is not admitted")
            candidate = OpeningInventory.model_validate(candidate).model_dump(mode="json")
            require_opening_inventory_support(candidate, self._manifest)
            return candidate
        except (ValueError, RunnerFactContractError):
            result["equity_binding"]["reason"] = "capture_unavailable"
            return OpeningInventory.model_validate(result).model_dump(mode="json")
