"""Immutable local run configuration, without artifact or runtime admission."""

from __future__ import annotations

import hashlib
import re
from decimal import (
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)
from typing import Annotated, Literal, Self, TypeVar
from uuid import UUID

from custos_toolkit.contracts.strategy_execution import (
    Sha256Hex,
    StrategyArtifactRefV1,
    StrategyManifestV1,
    canonical_json_bytes,
    parse_and_freeze_json_object,
)
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from custos.core.runtime_authority import RuntimeAccountIdentity


def uuid4_identity(value: UUID) -> UUID:
    if value.version != 4 or value.int == 0:
        raise ValueError("local identities require UUID4")
    return value


Uuid4 = Annotated[UUID, AfterValidator(uuid4_identity)]
Revision = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
Seconds = Annotated[StrictInt, Field(ge=1, le=86400)]


def _usd(value: str) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?", value):
        raise ValueError("USD amount must be a bounded plain decimal string")
    if ("." in value and value.endswith("0")) or Decimal(value) <= 0:
        raise ValueError("USD amount must be positive and canonical")
    return value


Usd = Annotated[StrictStr, AfterValidator(_usd)]


class LocalDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class LocalVersionedDocument(LocalDocumentModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


DocumentT = TypeVar("DocumentT", bound=LocalDocumentModel)


def parse_canonical_document(raw: bytes, model: type[DocumentT]) -> DocumentT:
    if type(raw) is not bytes or not 0 < len(raw) <= 131072:
        raise ValueError("local document must be bounded UTF-8 JSON bytes")
    value = parse_and_freeze_json_object(raw)
    parsed = model.model_validate(value)
    if parsed.canonical_bytes() != raw:
        raise ValueError("local document is not exact canonical JSON")
    return parsed


class LocalAccountV1(LocalDocumentModel):
    connector: Literal["papertrade"]
    trading_mode: Literal["live"]
    chain_id: StrictInt
    exchange: StrictStr
    wallet_address: StrictStr

    @model_validator(mode="after")
    def normalize_account(self) -> Self:
        identity = self.identity()
        object.__setattr__(self, "exchange", identity.exchange)
        object.__setattr__(self, "wallet_address", identity.wallet_address)
        return self

    def identity(self) -> RuntimeAccountIdentity:
        return RuntimeAccountIdentity(**self.model_dump())


class LocalRiskLimitsV1(LocalDocumentModel):
    max_capital_usd: Usd
    max_order_margin_usd: Usd
    max_order_notional_usd: Usd
    max_total_notional_usd: Usd
    max_loss_usd: Usd
    leverage: Annotated[StrictInt, Field(ge=1, le=1000)]

    @model_validator(mode="after")
    def check_units_and_limits(self) -> Self:
        capital = Decimal(self.max_capital_usd)
        margin = Decimal(self.max_order_margin_usd)
        order = Decimal(self.max_order_notional_usd)
        total = Decimal(self.max_total_notional_usd)
        loss = Decimal(self.max_loss_usd)
        # This context is independent of strategy code and the caller's traps/precision.
        with localcontext(
            Context(
                prec=100,
                rounding=ROUND_HALF_EVEN,
                Emin=-999999,
                Emax=999999,
                capitals=1,
                clamp=0,
                flags=[],
                traps=[
                    Clamped,
                    DivisionByZero,
                    FloatOperation,
                    Inexact,
                    InvalidOperation,
                    Overflow,
                    Rounded,
                    Subnormal,
                    Underflow,
                ],
            )
        ):
            if order != margin * self.leverage:
                raise ValueError("order notional must equal margin times leverage")
            if margin > capital or order > total or total > capital * self.leverage:
                raise ValueError("order or account exposure exceeds its declared capital")
            if loss > capital:
                raise ValueError("loss trigger exceeds declared capital")
        return self


class LocalDataPolicyV1(LocalDocumentModel):
    max_quote_age_seconds: Seconds
    max_account_age_seconds: Seconds


class LocalStopPolicyV1(LocalDocumentModel):
    position_policy: Literal["flatten"]
    confirmation_timeout_seconds: Seconds


class LocalRunManifestV1(LocalVersionedDocument):
    control_source: Literal["local"]
    run_id: Uuid4
    configuration_revision: Revision
    account: LocalAccountV1
    credential_ref: StrictStr
    artifact: StrategyArtifactRefV1
    release_bom_sha256: Sha256Hex
    toolkit_identity_sha256: Sha256Hex
    effective_config_digest: Sha256Hex
    entry_point: StrictStr
    instrument_ids: tuple[StrictStr, ...] = Field(min_length=1, max_length=64)
    limits: LocalRiskLimitsV1
    data_policy: LocalDataPolicyV1
    stop_policy: LocalStopPolicyV1

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_alias(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) or ".." in value:
            raise ValueError("credential reference must be a vault alias")
        return value

    @field_validator("entry_point")
    @classmethod
    def validate_entry_point(cls, value: str) -> str:
        return StrategyManifestV1.validate_entry_point(value)

    @field_validator("instrument_ids")
    @classmethod
    def validate_instruments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\.PAPERTRADE", item)
            for item in value
        ):
            raise ValueError("native Papertrade instrument identities must be unique")
        return value

    @property
    def account_identity(self) -> RuntimeAccountIdentity:
        return self.account.identity()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def parse_local_manifest(raw: bytes) -> LocalRunManifestV1:
    return parse_canonical_document(raw, LocalRunManifestV1)
