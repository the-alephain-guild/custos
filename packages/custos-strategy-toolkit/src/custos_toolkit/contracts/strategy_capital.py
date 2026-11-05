"""Immutable capital-view values; parsing never proves funding or runtime support."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

# These are the existing RunnerFact/rust_decimal value boundaries, not trading limits.
_DECIMAL_MANTISSA = 2**96 - 1
_DECIMAL_SCALE = 28
_AMOUNT = re.compile(r"(?:0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))")
_TIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{3}|[0-9]{6}|[0-9]{9}))?Z"
)
CONTRACT_ID = "alephain.custos.strategy_capital_view.v1"


def _amount(value: str) -> str:
    if len(value) > 31 or _AMOUNT.fullmatch(value) is None:
        raise ValueError("capital amount must be canonical decimal text")
    parsed = Decimal(value)
    _, digits, exponent = parsed.as_tuple()
    if not parsed.is_finite() or not isinstance(exponent, int):
        raise ValueError("capital amount must be finite")
    mantissa = int("".join(map(str, digits)))
    if -min(exponent, 0) > _DECIMAL_SCALE or mantissa > _DECIMAL_MANTISSA:
        raise ValueError("capital amount is outside the existing decimal range")
    return value


def _nonnegative(value: str) -> str:
    if value.startswith("-"):
        raise ValueError("allocated capital must not be negative")
    return value


def _uuid(value: str) -> str:
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("capital UUID must be canonical and non-nil")
    return value


def _time(value: str) -> str:
    match = _TIME.fullmatch(value)
    if match is None:
        raise ValueError("capital time must be canonical UTC AutoSi text")
    base, fraction = match.groups()
    datetime.fromisoformat(base)
    if fraction is not None:
        nanos = int(fraction.ljust(9, "0"))
        width = 3 if nanos % 1_000_000 == 0 else 6 if nanos % 1_000 == 0 else 9
        if nanos == 0 or len(fraction) != width:
            raise ValueError("capital time must retain canonical nanosecond precision")
    return value


def _time_key(value: str) -> tuple[str, int]:
    base, _, fraction = value[:-1].partition(".")
    return base, int((fraction or "0").ljust(9, "0"))


def _entries(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)) or type(value) not in (list, tuple):
        raise ValueError("capital changes must be an explicit sequence")
    return tuple(value)


Currency: TypeAlias = Literal["USD", "USDT", "USDC", "BTC", "ETH", "VUSDC", "VBTC", "VETH"]
Mode: TypeAlias = Literal["sandbox", "testnet", "live"]
ChangeKind: TypeAlias = Literal[
    "contribution", "redemption", "internal_transfer", "profit_distribution"
]
Reason: TypeAlias = Literal[
    "capital_source_unavailable",
    "capital_lineage_unproven",
    "period_boundary_missing",
    "journal_prefix_incomplete",
    "period_mismatch",
    "observation_unavailable",
    "observation_retired",
    "model_not_applicable",
    "wire_budget_exceeded",
]
Revision = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
Frontier = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
Digest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
Uuid = Annotated[
    str,
    Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        json_schema_extra={"not": {"const": "00000000-0000-0000-0000-000000000000"}},
    ),
    AfterValidator(_uuid),
]
Timestamp = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.(?:[0-9]{3}|[0-9]{6}|[0-9]{9}))?Z$",
        json_schema_extra={"not": {"pattern": r"[^0-9T:.Z-]"}},
    ),
    AfterValidator(_time),
]
Amount = Annotated[
    str,
    Field(
        max_length=31,
        pattern=r"^(?:0|-?(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$",
        json_schema_extra={
            "x-decimal-mantissa-bits": 96,
            "x-decimal-max-scale": 28,
            "not": {"pattern": r"[^0-9.\-]"},
        },
    ),
    AfterValidator(_amount),
]
AllocatedAmount = Annotated[
    str,
    Field(
        max_length=31,
        pattern=r"^(?:0|(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$",
        json_schema_extra={
            "x-decimal-mantissa-bits": 96,
            "x-decimal-max-scale": 28,
            "not": {"pattern": r"[^0-9.\-]"},
        },
    ),
    AfterValidator(_amount),
    AfterValidator(_nonnegative),
]


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class CapitalScopeV1(_Closed):
    tenant_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9_-]{1,64}$",
            json_schema_extra={"not": {"pattern": r"[^A-Za-z0-9_-]"}},
        ),
    ]
    trading_mode: Mode
    runner_id: Uuid
    strategy_id: Uuid
    deployment_instance_id: Uuid
    deployment_spec_id: Uuid
    deployment_spec_digest: Digest
    generation: Revision


class CapitalPeriodV1(_Closed):
    code: Annotated[str, Field(min_length=7, max_length=7, pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")]
    start_utc: Timestamp
    end_exclusive_utc: Timestamp
    trading_mode: Mode

    @model_validator(mode="after")
    def month_boundaries(self) -> Self:
        year, month = map(int, self.code.split("-"))
        start = datetime(year, month, 1)
        end = datetime(year + (month == 12), month % 12 + 1, 1)

        def render(value: datetime) -> str:
            return f"{value.year:04d}-{value.month:02d}-01T00:00:00Z"

        if self.start_utc != render(start) or self.end_exclusive_utc != render(end):
            raise ValueError("capital period boundaries must match its UTC month")
        return self


class CapitalStateSourceV1(_Closed):
    kind: Literal["capital_state"]
    command_event_id: Uuid
    command_digest: Digest
    capital_revision: Revision
    capital_state_digest: Digest
    source_frontier: Frontier
    source_ledger_digest: Digest
    principal_lineage_digest: Digest
    phase: Literal["committed", "preparing"]
    operation_id: Uuid | None

    @model_validator(mode="after")
    def preparing_operation(self) -> Self:
        if self.phase == "preparing" and self.operation_id is None:
            raise ValueError("preparing capital requires its operation identity")
        return self


class CapitalObservationSourceV1(_Closed):
    kind: Literal["runner_observation"]
    batch_id: Uuid
    seq_start: Revision
    seq_end: Revision
    observation_id: Digest
    pnl_basis_digest: Digest
    source_prefix_digest: Digest
    capital_revision: Revision
    capital_state_digest: Digest

    @model_validator(mode="after")
    def ordered_sequence(self) -> Self:
        if self.seq_start > self.seq_end:
            raise ValueError("capital observation sequence is reversed")
        return self


class _Provenance(_Closed):
    scope: CapitalScopeV1
    period: CapitalPeriodV1
    as_of: Timestamp

    @model_validator(mode="after")
    def current_period(self) -> Self:
        if self.scope.trading_mode != self.period.trading_mode:
            raise ValueError("capital period mode differs from its scope")
        if (
            not _time_key(self.period.start_utc)
            <= _time_key(self.as_of)
            < _time_key(self.period.end_exclusive_utc)
        ):
            raise ValueError("capital observation is outside its current period")
        return self


class CapitalStateProvenanceV1(_Provenance):
    source: CapitalStateSourceV1


class CapitalObservationProvenanceV1(_Provenance):
    source: CapitalObservationSourceV1


class UnavailableCapitalMoneyV1(_Closed):
    status: Literal["unavailable"]
    amount: None
    currency: Currency | None
    reason: Reason
    provenance: CapitalStateProvenanceV1 | CapitalObservationProvenanceV1 | None


class KnownAllocatedCapitalV1(_Closed):
    status: Literal["known"]
    amount: AllocatedAmount
    currency: Currency
    reason: None
    provenance: CapitalStateProvenanceV1


class KnownCapitalPnlV1(_Closed):
    status: Literal["known"]
    amount: Amount
    currency: Currency
    reason: None
    provenance: CapitalObservationProvenanceV1


class CapitalChangeSourceV1(_Closed):
    kind: ChangeKind
    request_id: Uuid
    effect_id: Uuid
    version: Revision
    digest: Digest
    allocation_id: Uuid | None
    allocation_version: Revision | None
    allocation_target_id: Uuid | None

    @model_validator(mode="after")
    def allocation_binding(self) -> Self:
        bound = (self.allocation_id, self.allocation_version, self.allocation_target_id)
        required = self.kind in {"contribution", "redemption"}
        if any((value is not None) != required for value in bound):
            raise ValueError("capital source allocation fields differ from its owner kind")
        return self


class CapitalChangeV1(_Closed):
    change_id: Uuid
    kind: ChangeKind
    amount: Amount
    currency: Currency
    effective_at: Timestamp
    operation_id: Uuid | None
    reversal_of_change_id: Uuid | None
    source: CapitalChangeSourceV1

    @model_validator(mode="after")
    def signed_effect(self) -> Self:
        if self.source.kind != self.kind or self.amount == "0":
            raise ValueError("capital change must have its exact nonzero source kind")
        reversal = self.reversal_of_change_id is not None
        if reversal and self.kind not in {"contribution", "redemption"}:
            raise ValueError("only existing contribution and redemption allocation reversals exist")
        if self.reversal_of_change_id == self.change_id:
            raise ValueError("capital change cannot reverse itself")
        if self.kind != "internal_transfer":
            negative = (self.kind != "contribution") != reversal
            if self.amount.startswith("-") != negative:
                raise ValueError("capital change sign differs from its source effect")
        return self


class KnownCapitalChangesV1(_Closed):
    status: Literal["known"]
    entries: Annotated[tuple[CapitalChangeV1, ...], BeforeValidator(_entries)]
    reason: None
    provenance: CapitalStateProvenanceV1

    @model_validator(mode="after")
    def period_entries(self) -> Self:
        if len({entry.change_id for entry in self.entries}) != len(self.entries):
            raise ValueError("capital change identity is duplicated")
        for entry in self.entries:
            if (
                not _time_key(self.provenance.period.start_utc)
                <= _time_key(entry.effective_at)
                <= _time_key(self.provenance.as_of)
            ):
                raise ValueError("capital change is outside its current observed period")
        return self


class UnavailableCapitalChangesV1(_Closed):
    status: Literal["unavailable"]
    entries: None
    reason: Reason
    provenance: CapitalStateProvenanceV1 | None


class StrategyCapitalViewV1(_Closed):
    """Four read-only fields, without financial authority or execution admission."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        json_schema_extra={
            "$id": "https://custos.the-alephain-guild/contracts/v1/strategy-capital-view-v1.schema.json",
            "x-contract-id": CONTRACT_ID,
            "x-contract-revision": 1,
            "x-runtime-supported": False,
        },
    )
    allocated_capital: Annotated[
        KnownAllocatedCapitalV1 | UnavailableCapitalMoneyV1, Field(discriminator="status")
    ]
    realized_pnl: Annotated[
        KnownCapitalPnlV1 | UnavailableCapitalMoneyV1, Field(discriminator="status")
    ]
    unrealized_pnl: Annotated[
        KnownCapitalPnlV1 | UnavailableCapitalMoneyV1, Field(discriminator="status")
    ]
    capital_changes: Annotated[
        KnownCapitalChangesV1 | UnavailableCapitalChangesV1, Field(discriminator="status")
    ]

    @model_validator(mode="after")
    def coherent_known_fields(self) -> Self:
        fields = (
            self.allocated_capital,
            self.realized_pnl,
            self.unrealized_pnl,
            self.capital_changes,
        )
        provenances = [field.provenance for field in fields if field.provenance is not None]
        if provenances and any(p.scope != provenances[0].scope for p in provenances):
            raise ValueError("capital fields belong to different execution scopes")
        known = [field.provenance for field in fields if field.status == "known"]
        if known and any(p.period != known[0].period for p in known):
            raise ValueError("known capital fields belong to different current periods")
        states = [p for p in known if isinstance(p, CapitalStateProvenanceV1)]
        if states and any(
            (p.source, p.as_of) != (states[0].source, states[0].as_of) for p in states
        ):
            raise ValueError("known capital fields belong to different control states")
        observations = [p for p in known if isinstance(p, CapitalObservationProvenanceV1)]
        if observations and any(p != observations[0] for p in observations):
            raise ValueError("known PnL fields must share one signed observation")
        if states and any(
            (p.source.capital_revision, p.source.capital_state_digest)
            != (states[0].source.capital_revision, states[0].source.capital_state_digest)
            for p in observations
        ):
            raise ValueError("PnL observation belongs to a different capital state")
        if states and any(_time_key(p.as_of) < _time_key(states[0].as_of) for p in observations):
            raise ValueError("PnL observation precedes its capital state")
        currencies = {field.currency for field in fields[:3] if field.currency is not None}
        if isinstance(self.capital_changes, KnownCapitalChangesV1):
            currencies.update(entry.currency for entry in self.capital_changes.entries)
        if len(currencies) > 1:
            raise ValueError("capital view mixes settlement currencies")
        return self


class StrategyCapitalContractSupportV1(_Closed):
    """A schema-only declaration which cannot assert runtime capabilities."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        json_schema_extra={
            "$id": "https://custos.the-alephain-guild/contracts/v1/strategy-capital-contract-support-v1.schema.json",
            "x-contract-id": "alephain.custos.strategy_capital_contract_support.v1",
            "x-contract-revision": 1,
        },
    )
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    contract_id: Literal["alephain.custos.strategy_capital_view.v1"]
    runtime_supported: Literal[False]
    capital_change_supported: Literal[False]

    @field_validator("runtime_supported", "capital_change_supported", mode="before")
    @classmethod
    def exactly_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("capital contract has no runtime support")
        return value
