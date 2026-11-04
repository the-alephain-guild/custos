"""Closed opening observations carried by the current equity fact contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from custos.core.runner_fact import (
    RunnerFactAuthority,
    RunnerFactContractError,
    _canonical_json_bytes,
    _signed_decimal,
    _uuid,
)

INVENTORY_SCHEMA_REVISION = 1
MAX_INVENTORY_ROWS = 192
MAX_INVENTORY_REASON_COUNT = 16
NATIVE_CAPTURE_API = "execution-account-capture-v1"
NATIVE_CAPTURE_MODEL = "sandbox-account-state-v1"
NATIVE_CAPTURE_CHANNELS = (
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
# This mapping revision identifies exact native denominations, not code case.
NATIVE_CURRENCY_DEFINITIONS = {
    "USD": (2, "FIAT", 840),
    "USDT": (8, "CRYPTO", 0),
    "USDC": (8, "CRYPTO", 0),
    "BTC": (8, "CRYPTO", 0),
    "ETH": (8, "CRYPTO", 0),
}
COMPLETENESS_FIELDS = (
    "identity",
    "balances",
    "liabilities",
    "positions",
    "ordinary_orders",
    "order_kinds",
    "pagination",
    "common_boundary",
)
Reason = Literal[
    "provider_unsupported",
    "source_unavailable",
    "source_identity_unverified",
    "source_identity_mismatch",
    "liabilities_unverified",
    "pagination_unverified",
    "order_kinds_unverified",
    "common_boundary_unverified",
    "wire_dimension_unavailable",
    "wire_budget_exceeded",
    "denomination_unmapped",
    "inventory_unavailable",
    "independent_risk_observation",
    "model_not_applicable",
    "capture_unavailable",
    "observation_pending",
    "observation_retired",
    "collector_busy",
]
Connector = Literal[
    "binance",
    "binance_perpetual",
    "okx",
    "okx_perpetual",
    "sodex",
    "sodex_perpetual",
]
Currency = Literal["USD", "USDT", "USDC", "BTC", "ETH", "VUSDC", "VBTC", "VETH"]
U64 = Annotated[int, Field(ge=0, le=2**64 - 1)]
PositiveU64 = Annotated[int, Field(ge=1, le=2**64 - 1)]
Token = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
VenueId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]
AssetCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _canonical_uuid(value: str) -> str:
    if _uuid(value, "opening UUID") != value:
        raise ValueError("opening UUID must be canonical")
    return value


def _canonical_amount(value: str) -> str:
    if _signed_decimal(value, "opening amount", bounded=True) != value:
        raise ValueError("opening amount must be canonical")
    return value


def _canonical_time(value: str) -> str:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,9})?Z", value
    ):
        raise ValueError("opening timestamp must be canonical UTC")
    from custos.core.utc_time import normalize_utc_timestamp

    if normalize_utc_timestamp(value) != value:
        raise ValueError("opening timestamp must use UTC AutoSi canonical spelling")
    return value


UuidText = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
    AfterValidator(_canonical_uuid),
]
Money = Annotated[
    str,
    Field(pattern=r"^(?:0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$"),
    AfterValidator(_canonical_amount),
]
UnsignedMoney = Annotated[
    str,
    Field(pattern=r"^(?:0|(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$"),
    AfterValidator(_canonical_amount),
]
Timestamp = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,9})?Z$"),
    AfterValidator(_canonical_time),
]
T = TypeVar("T")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SodexSource(_Closed):
    kind: Literal["sodex_venue_account"]
    network: Literal["testnet", "mainnet"]
    main_wallet: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]
    account_id: PositiveU64
    account_type: Literal["spot", "perps"]


class SandboxSource(_Closed):
    kind: Literal["sandbox_engine"]
    incarnation: UuidText
    client_id: Token
    account_id: Token
    venue: Token
    account_type: Literal["CASH", "MARGIN", "BETTING", "WALLET"]
    api_revision: Literal["execution-account-capture-v1"]
    model_revision: Literal["sandbox-account-state-v1"]
    runtime_version: Token
    configuration_json: Annotated[str, Field(min_length=2, max_length=65536)]
    configuration_sha256: Digest

    @model_validator(mode="after")
    def configuration_bytes(self):
        if sha256(self.configuration_json.encode("utf-8")).hexdigest() != self.configuration_sha256:
            raise ValueError("native configuration digest differs from its original UTF-8 bytes")
        return self


Source = Annotated[SodexSource | SandboxSource, Field(discriminator="kind")]


class InventoryScope(_Closed):
    tenant_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    trading_mode: Literal["sandbox", "testnet", "live"]
    runner_id: UuidText
    deployment_instance_id: UuidText
    deployment_spec_id: UuidText
    deployment_spec_digest: Digest
    generation: PositiveU64
    connector: Connector
    source: Source | None

    @model_validator(mode="after")
    def source_mode(self):
        if isinstance(self.source, SodexSource):
            if self.trading_mode == "sandbox" or self.connector not in {"sodex", "sodex_perpetual"}:
                raise ValueError("SoDEX inventory source differs from mode or connector")
            if self.source.network != ("testnet" if self.trading_mode == "testnet" else "mainnet"):
                raise ValueError("SoDEX inventory source network differs from mode")
            if self.source.account_type != (
                "perps" if self.connector == "sodex_perpetual" else "spot"
            ):
                raise ValueError("SoDEX inventory account type differs from connector")
        if isinstance(self.source, SandboxSource) and self.trading_mode != "sandbox":
            raise ValueError("sandbox inventory source requires sandbox mode")
        return self


class CapabilityBinding(_Closed):
    capability_version_id: UuidText
    capability_version: PositiveU64
    capability_manifest_digest: Digest
    inventory_schema_revision: Annotated[int, Field(ge=1, le=1)]
    capture_api_revision: Token | None
    model_revision: Token | None
    mapping_revision: Token | None
    runtime_artifact_digest: Digest | None


class ReportedIdentity(_Closed):
    kind: Literal["sodex_account"]
    main_wallet: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]
    account_id: PositiveU64


class NativeIdentity(_Closed):
    kind: Literal["sandbox_account"]
    incarnation: UuidText
    client_id: Token
    account_id: Token
    venue: Token
    account_type: Literal["CASH", "MARGIN", "BETTING", "WALLET"]
    configuration_sha256: Digest


class NativeCurrency(_Closed):
    precision: Annotated[int, Field(ge=0, le=255)]
    currency_type: Literal["FIAT", "CRYPTO"]
    iso4217: Annotated[int, Field(ge=0, le=65535)]


class NativeFrontier(_Closed):
    channel: Literal[
        "time_events",
        "system_events",
        "system_commands",
        "execution_events",
        "execution_commands",
        "data_events",
        "data_commands",
        "external_events",
        "maintenance",
    ]
    applied: U64
    admitted: U64

    @model_validator(mode="after")
    def applied_cut(self):
        if self.applied != self.admitted:
            raise ValueError("native capture has admitted work outside its applied frontier")
        return self


class NativeCapture(_Closed):
    node_instance_id: UuidText
    environment: Literal["SANDBOX"]
    capture_sequence: PositiveU64
    frontier: Annotated[list[NativeFrontier], Field(min_length=9, max_length=9)]
    observed_at_ns: U64
    no_borrowing_model: bool

    @model_validator(mode="after")
    def closed_frontier(self):
        if tuple(entry.channel for entry in self.frontier) != NATIVE_CAPTURE_CHANNELS:
            raise ValueError("native capture requires each actual owner channel in canonical order")
        return self


AccountIdentity = Annotated[ReportedIdentity | NativeIdentity, Field(discriminator="kind")]


class Balance(_Closed):
    raw_asset_id: Token | None
    raw_asset_code: AssetCode
    native_currency: NativeCurrency | None
    namespace: Token
    denomination: Currency | None
    mapping_revision: Token | None
    total: Money
    locked: UnsignedMoney | None
    collateral: UnsignedMoney | None
    available: Money | None

    @model_validator(mode="after")
    def reservations(self):
        if any(Decimal(v) < 0 for v in (self.locked, self.collateral) if v is not None):
            raise ValueError("opening balance reservation must be nonnegative")
        if (self.denomination is None) != (self.mapping_revision is None):
            raise ValueError("opening denomination requires an explicit mapping revision")
        return self


class Position(_Closed):
    position_id: VenueId
    instrument: Token
    quantity: Money
    active: bool
    position_side: Token
    average_entry_price: UnsignedMoney | None
    settlement_currency: AssetCode | None


class Order(_Closed):
    order_id: VenueId
    instrument: Token
    side: Token
    order_type: Token
    status: Token
    original_quantity: UnsignedMoney
    executed_quantity: UnsignedMoney
    price: UnsignedMoney | None
    client_order_id: VenueId | None
    venue_order_id: VenueId | None
    trigger_price: UnsignedMoney | None
    in_matching_engine: bool | None
    pending: bool | None

    @model_validator(mode="after")
    def quantities(self):
        if not 0 <= Decimal(self.executed_quantity) <= Decimal(self.original_quantity):
            raise ValueError("opening order quantities are invalid")
        if self.price is not None and Decimal(self.price) < 0:
            raise ValueError("opening order price must be nonnegative")
        return self


class Liability(_Closed):
    raw_asset_id: Token | None
    raw_asset_code: AssetCode
    namespace: Token
    amount: UnsignedMoney


class Observation(_Closed, Generic[T]):
    status: Literal["observed", "unavailable", "not_applicable"]
    rows: Annotated[list[T], Field(max_length=MAX_INVENTORY_ROWS)] | None
    requested_at: Timestamp | None
    received_at: Timestamp | None
    source_time: Timestamp | None
    block_height: U64 | None
    capture_token: Token | None
    response_digest: Digest | None
    pagination: Literal["unknown", "indicated_more", "owner_full"]
    reason: Reason | None

    @model_validator(mode="after")
    def observed_rows(self):
        if self.status == "observed":
            if self.rows is None or self.reason is not None:
                raise ValueError("observed inventory requires rows without an unavailable reason")
        elif self.rows is not None or self.reason is None:
            raise ValueError("unavailable inventory requires null rows and a reason")
        if (self.requested_at is None) != (self.received_at is None):
            raise ValueError("opening request boundary must have both times")

        from custos.core.utc_time import utc_timestamp_key

        if self.requested_at is not None and utc_timestamp_key(
            self.received_at
        ) < utc_timestamp_key(self.requested_at):
            raise ValueError("opening response predates request")
        return self


class IdentityObservation(Observation[AccountIdentity]):
    rows: Annotated[list[AccountIdentity], Field(max_length=1)] | None


class Observations(_Closed):
    identity: IdentityObservation
    balances: Observation[Balance]
    liabilities: Observation[Liability]
    positions: Observation[Position]
    ordinary_orders: Observation[Order]


class Completeness(_Closed):
    identity: bool | None
    balances: bool | None
    liabilities: bool | None
    positions: bool | None
    ordinary_orders: bool | None
    order_kinds: bool | None
    pagination: bool | None
    common_boundary: bool | None


class EquityBinding(_Closed):
    status: Literal["coherent", "unproven", "unavailable"]
    capture_token: Token | None
    currency: Currency | None
    amount: Money | None
    observed_at: Timestamp | None
    valuation_rule: Literal["settlement_cash_total_v1"] | None
    reason: Reason | None

    @model_validator(mode="after")
    def coherent_fields(self):
        values = (
            self.capture_token,
            self.currency,
            self.amount,
            self.observed_at,
            self.valuation_rule,
        )
        if self.status == "coherent":
            if any(v is None for v in values) or self.reason is not None:
                raise ValueError("coherent equity requires its exact capture and valuation rule")
        elif any(v is not None for v in values) or self.reason is None:
            raise ValueError("unproven equity must not imply a capture or value")
        return self


class OpeningInventory(_Closed):
    status: Literal["observed", "unavailable"]
    scope: InventoryScope
    capability_binding: CapabilityBinding
    native_capture: NativeCapture | None
    observations: Observations
    completeness: Completeness
    equity_binding: EquityBinding
    inventory_complete: bool
    reasons: Annotated[list[Reason], Field(max_length=MAX_INVENTORY_REASON_COUNT)]

    @model_validator(mode="after")
    def invariants(self):
        reads = Observations.model_fields
        observed = [getattr(self.observations, name) for name in reads]
        if self.status == "unavailable":
            if any(read.status == "observed" for read in observed) or not self.reasons:
                raise ValueError("unavailable inventory must not contain observed rows")
        elif self.scope.source is None or not any(read.status == "observed" for read in observed):
            raise ValueError("observed inventory requires a source and an actual observation")
        for name in ("balances", "positions", "ordinary_orders"):
            rows = getattr(self.observations, name).rows
            if rows is None:
                continue
            keys = [
                (row.namespace, row.raw_asset_id or row.raw_asset_code)
                if name == "balances"
                else row.position_id
                if name == "positions"
                else row.order_id
                for row in rows
            ]
            if len(set(keys)) != len(keys):
                raise ValueError("opening inventory contains duplicate identities")
        for name in reads:
            read = getattr(self.observations, name)
            if read.status == "not_applicable" and not (
                name == "liabilities"
                and isinstance(self.scope.source, SandboxSource)
                and self.native_capture is not None
                and self.status == "observed"
                and self.scope.source.account_type == "CASH"
                and self.native_capture.no_borrowing_model
                and read.reason == "model_not_applicable"
            ):
                raise ValueError(
                    f"inapplicable {name} dimension lacks its native cash model guarantee"
                )
        self._require_native_capture(observed)
        coherent = self.equity_binding.status == "coherent"
        complete = all(getattr(self.completeness, name) is True for name in COMPLETENESS_FIELDS)
        if any(getattr(self.completeness, name) is True for name in COMPLETENESS_FIELDS):
            self._require_dimension_evidence(observed)
        if coherent:
            self._require_cash_capture(observed)
        if self.inventory_complete != (complete and coherent and self.status == "observed"):
            raise ValueError("opening inventory completeness differs from its evidence")
        return self

    def _require_native_capture(self, observed):
        source = self.scope.source
        capture = self.native_capture
        if not isinstance(source, SandboxSource) or self.status == "unavailable":
            if capture is not None:
                raise ValueError(
                    "non-native or unavailable inventory must not imply a native capture"
                )
            return
        if capture is None:
            raise ValueError(
                "observed sandbox inventory requires its actual native capture boundary"
            )
        capability = self.capability_binding
        if (
            capability.capture_api_revision != source.api_revision
            or capability.model_revision != source.model_revision
        ):
            raise ValueError("native capture API or model differs from capability binding")
        from custos.core.utc_time import render_utc_nanos

        at = render_utc_nanos(capture.observed_at_ns)
        token = native_capture_token(
            source.model_dump(mode="json"), capture.model_dump(mode="json")
        )
        for read in observed:
            if read.status in {"observed", "not_applicable"} and (
                read.capture_token != token
                or read.source_time != at
                or read.block_height is not None
                or read.pagination != "owner_full"
            ):
                raise ValueError("native observation differs from its recomputed capture boundary")
        if source.account_type != "CASH" and capture.no_borrowing_model:
            raise ValueError("native margin account cannot declare the cash no-borrowing model")
        for row in self.observations.balances.rows or []:
            if (
                row.native_currency is None
                or row.raw_asset_id is not None
                or row.namespace != "native.currency"
            ):
                raise ValueError("native balances require exact native currency metadata")
        for row in self.observations.positions.rows or []:
            if row.average_entry_price is not None or row.settlement_currency is None:
                raise ValueError("native position must preserve its available exact fields")
        for row in self.observations.ordinary_orders.rows or []:
            if (
                row.client_order_id != row.order_id
                or row.in_matching_engine is None
                or row.pending is None
            ):
                raise ValueError("native order must retain its client identity and owner state")

    def _require_dimension_evidence(self, observed):
        self._require_native_identity()
        for name in ("identity", "balances", "positions", "ordinary_orders"):
            if getattr(self.completeness, name) is True:
                read = getattr(self.observations, name)
                if (
                    read.status != "observed"
                    or read.capture_token is None
                    or read.source_time is None
                ):
                    raise ValueError("complete inventory dimension has no actual capture rows")
        if (
            self.completeness.order_kinds is True
            and self.observations.ordinary_orders.status != "observed"
        ):
            raise ValueError("complete order kinds require the actual native order enumeration")
        liabilities = self.observations.liabilities
        if self.completeness.liabilities is True and (
            liabilities.status != "not_applicable"
            or liabilities.reason != "model_not_applicable"
            or self.scope.source.account_type != "CASH"
            or not self.native_capture.no_borrowing_model
        ):
            raise ValueError("the cash model must prove liabilities are inapplicable")
        if self.completeness.pagination is True and any(
            read.pagination != "owner_full" for read in observed
        ):
            raise ValueError("inventory pagination lacks full owner enumeration")
        if self.completeness.common_boundary is True:
            identity = self.observations.identity
            if (
                identity.capture_token is None
                or identity.source_time is None
                or any(
                    read.capture_token != identity.capture_token
                    or read.source_time != identity.source_time
                    for read in observed
                )
            ):
                raise ValueError("inventory dimensions lack a common capture boundary")

    def _require_native_identity(self):
        capability = self.capability_binding
        if not isinstance(self.scope.source, SandboxSource) or (
            capability.capture_api_revision != NATIVE_CAPTURE_API
            or capability.model_revision != NATIVE_CAPTURE_MODEL
            or capability.mapping_revision is None
            or capability.runtime_artifact_digest is None
        ):
            raise ValueError("complete inventory requires an actual supported sandbox capture")
        identities = self.observations.identity.rows
        if (
            identities is None
            or len(identities) != 1
            or not isinstance(identities[0], NativeIdentity)
        ):
            raise ValueError("complete inventory requires the actual native account identity")
        identity = identities[0]
        for field in (
            "incarnation",
            "client_id",
            "account_id",
            "venue",
            "account_type",
            "configuration_sha256",
        ):
            if getattr(identity, field) != getattr(self.scope.source, field):
                raise ValueError("native reported identity differs from the capture source")

    def _require_cash_capture(self, observed):
        self._require_native_identity()
        capability = self.capability_binding
        binding = self.equity_binding
        required_reads = (
            self.observations.identity,
            self.observations.balances,
            self.observations.positions,
            self.observations.ordinary_orders,
        )
        if any(read.status != "observed" for read in required_reads):
            raise ValueError("coherent cash equity requires every applicable capture dimension")
        liabilities = self.observations.liabilities
        if (
            liabilities.status != "not_applicable"
            or liabilities.reason != "model_not_applicable"
            or self.scope.source.account_type != "CASH"
            or not self.native_capture.no_borrowing_model
        ):
            raise ValueError("the supported cash model must prove liabilities are inapplicable")
        for read in observed:
            if (
                read.capture_token != binding.capture_token
                or read.source_time != binding.observed_at
                or read.pagination != "owner_full"
            ):
                raise ValueError("opening dimensions differ from the complete owner capture")
        if (
            capability.mapping_revision != "native_currency_codes_v1"
            or binding.currency not in NATIVE_CURRENCY_DEFINITIONS
        ):
            raise ValueError(
                "cash capture uses an unsupported currency mapping revision or denomination"
            )
        balances = self.observations.balances.rows
        if balances is None or any(
            row.denomination is None
            or row.mapping_revision != capability.mapping_revision
            or row.namespace != "native.currency"
            or row.raw_asset_code != row.denomination
            or row.native_currency is None
            or (
                row.native_currency.precision,
                row.native_currency.currency_type,
                row.native_currency.iso4217,
            )
            != NATIVE_CURRENCY_DEFINITIONS.get(row.denomination)
            for row in balances
        ):
            raise ValueError("coherent equity requires the actual mapped native balances")
        if any(
            row.denomination != binding.currency and Decimal(row.total) != 0 for row in balances
        ):
            raise ValueError("cash basis cannot value nonzero assets in another denomination")
        if any(
            row.active or Decimal(row.quantity) != 0 for row in self.observations.positions.rows
        ):
            raise ValueError("cash basis cannot value an effective position")
        matches = [row for row in balances if row.denomination == binding.currency]
        if not matches:
            # Absence means zero only after complete owner enumeration of an
            # existing account; unknown reads must never take this branch.
            if binding.amount != "0" or not all(
                getattr(self.completeness, name) is True for name in COMPLETENESS_FIELDS
            ):
                raise ValueError("absent settlement balance requires complete known-zero evidence")
        elif len(matches) != 1 or matches[0].total != binding.amount:
            raise ValueError("opening cash equity cannot be recomputed from balances")


def native_capture_token(source: Mapping[str, Any], capture: Mapping[str, Any]) -> str:
    """Bind exact source and owner boundary; this digest does not authorize a model."""
    return sha256(_canonical_json_bytes({"source": source, "native_capture": capture})).hexdigest()


def normalize_opening_inventory(
    value: Mapping[str, Any],
    *,
    amount: str,
    currency: str,
    authority: RunnerFactAuthority | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    try:
        result = OpeningInventory.model_validate(value)
        if result.equity_binding.status == "coherent" and (
            result.equity_binding.amount != amount
            or result.equity_binding.currency != currency
            or (observed_at is not None and result.equity_binding.observed_at != observed_at)
        ):
            raise ValueError("opening equity differs from the original risk equity")
        if authority is not None:
            for field in (
                "tenant_id",
                "trading_mode",
                "runner_id",
                "deployment_instance_id",
                "deployment_spec_id",
                "deployment_spec_digest",
                "generation",
            ):
                expected = getattr(authority, field)
                actual = getattr(result.scope, field)
                if actual != (str(expected) if isinstance(expected, UUID) else expected):
                    raise ValueError("opening scope differs from the signing authority")
            for field in (
                "capability_version_id",
                "capability_version",
                "capability_manifest_digest",
            ):
                expected = getattr(authority, field)
                actual = getattr(result.capability_binding, field)
                if actual != (str(expected) if isinstance(expected, UUID) else expected):
                    raise ValueError("opening capability differs from the signing authority")
        return result.model_dump(mode="json")
    except (ValueError, ValidationError) as exc:
        raise RunnerFactContractError(f"invalid opening inventory: {exc}") from exc


def unavailable_inventory_read(reason: Reason) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "rows": None,
        "requested_at": None,
        "received_at": None,
        "source_time": None,
        "block_height": None,
        "capture_token": None,
        "response_digest": None,
        "pagination": "unknown",
        "reason": reason,
    }


def unavailable_opening_inventory(
    authority: RunnerFactAuthority, connector: str, reason: Reason = "provider_unsupported"
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "scope": {
            **{
                name: str(getattr(authority, name))
                if isinstance(getattr(authority, name), UUID)
                else getattr(authority, name)
                for name in (
                    "tenant_id",
                    "trading_mode",
                    "runner_id",
                    "deployment_instance_id",
                    "deployment_spec_id",
                    "deployment_spec_digest",
                    "generation",
                )
            },
            "connector": connector,
            "source": None,
        },
        "capability_binding": {
            "capability_version_id": str(authority.capability_version_id),
            "capability_version": authority.capability_version,
            "capability_manifest_digest": authority.capability_manifest_digest,
            "inventory_schema_revision": INVENTORY_SCHEMA_REVISION,
            "capture_api_revision": None,
            "model_revision": None,
            "mapping_revision": None,
            "runtime_artifact_digest": None,
        },
        "native_capture": None,
        "observations": {
            name: unavailable_inventory_read(reason) for name in Observations.model_fields
        },
        "completeness": dict.fromkeys(COMPLETENESS_FIELDS),
        "equity_binding": {
            "status": "unproven",
            "capture_token": None,
            "currency": None,
            "amount": None,
            "observed_at": None,
            "valuation_rule": None,
            "reason": "inventory_unavailable",
        },
        "inventory_complete": False,
        "reasons": [reason],
    }


def require_opening_inventory_support(
    inventory: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    """Admission needs the registered manifest, beyond the closed wire parser."""
    contracts = manifest.get("runner_fact_contracts")
    risk = contracts.get("risk") if isinstance(contracts, Mapping) else None
    support = risk.get("opening_inventory") if isinstance(risk, Mapping) else None
    required = {
        "schema_revision",
        "source_models",
        "capture_apis",
        "mapping_revisions",
        "runtime_artifact_digests",
        "complete_models",
    }
    if (
        not isinstance(support, Mapping)
        or set(support) != required
        or type(support.get("schema_revision")) is not int
        or support["schema_revision"] != INVENTORY_SCHEMA_REVISION
    ):
        raise RunnerFactContractError(
            "opening inventory schema is not registered by the capability"
        )
    for field in required - {"schema_revision"}:
        values = support[field]
        if (
            type(values) is not list
            or not all(type(value) is str for value in values)
            or len(set(values)) != len(values)
        ):
            raise RunnerFactContractError(
                "registered opening support must contain unique string sets"
            )
    if inventory["status"] == "unavailable":
        if "unavailable" not in support.get("source_models", ()):
            raise RunnerFactContractError(
                "unavailable inventory is not registered by the capability"
            )
        return
    binding = inventory["capability_binding"]
    for field, supported in (
        ("capture_api_revision", "capture_apis"),
        ("model_revision", "source_models"),
    ):
        if binding[field] not in support.get(supported, ()):
            raise RunnerFactContractError(
                "opening inventory source is not registered by the capability"
            )
    if (
        inventory["equity_binding"]["status"] == "coherent"
        or inventory["inventory_complete"]
        or any(value is True for value in inventory["completeness"].values())
    ):
        for field, supported in (
            ("model_revision", "complete_models"),
            ("mapping_revision", "mapping_revisions"),
            ("runtime_artifact_digest", "runtime_artifact_digests"),
        ):
            if binding[field] not in support.get(supported, ()):
                raise RunnerFactContractError(
                    "complete opening model is not registered by the capability"
                )
