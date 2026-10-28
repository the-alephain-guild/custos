"""Immutable original dispatch semantics and already verified non-execution observations."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from custos.core.execution_risk_events import _token
from custos.core.runtime_authority import RuntimeAuthorityError


def _digest(value: str, *, prefix: bool) -> None:
    pattern = r"0x[0-9a-f]{64}" if prefix else r"[0-9a-f]{64}"
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        raise RuntimeAuthorityError("native evidence requires a canonical digest")


def _revision(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise RuntimeAuthorityError("native journal revisions must be positive bounded integers")


def _journal(value: UUID) -> None:
    if not isinstance(value, UUID) or value.version != 4:
        raise RuntimeAuthorityError("native dispatch requires the registered journal UUID")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeAuthorityError("native dispatch binding has duplicate fields")
        result[key] = value
    return result


@dataclass(frozen=True)
class NativeDispatchBinding:
    client_order_id: str
    native_account_id: str
    client_id: str
    instrument_id: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    quote_quantity: bool
    reduce_only: bool
    target: str | None
    submission_params_keccak256: str

    def __post_init__(self) -> None:
        for name in ("client_order_id", "native_account_id", "client_id", "instrument_id"):
            value = getattr(self, name)
            if type(value) is not str:
                raise RuntimeAuthorityError("native dispatch identifiers must be exact strings")
            _token(value, name)
        if self.side not in {"BUY", "SELL"}:
            raise RuntimeAuthorityError("native dispatch side is invalid")
        if (
            type(self.quantity) is not Decimal
            or not self.quantity.is_finite()
            or self.quantity <= 0
            or self.quantity.adjusted() > 17
            or cast(int, self.quantity.as_tuple().exponent) < -18
        ):
            raise RuntimeAuthorityError("native dispatch quantity must be a positive exact Decimal")
        if type(self.reduce_only) is not bool or type(self.quote_quantity) is not bool:
            raise RuntimeAuthorityError("native dispatch flags must be exact booleans")
        if self.reduce_only:
            if (
                self.quote_quantity
                or type(self.target) is not str
                or re.fullmatch(r"0|[1-9][0-9]{0,77}", self.target) is None
                or int(self.target) >= 2**256
            ):
                raise RuntimeAuthorityError(
                    "native reduction requires its exact base position target"
                )
        elif not self.quote_quantity or self.target is not None:
            raise RuntimeAuthorityError("native opening requires quote quantity without a target")
        _digest(self.submission_params_keccak256, prefix=True)

    def document(self) -> str:
        value = asdict(self)
        quantity = format(self.quantity, "f")
        value["quantity"] = quantity.rstrip("0").rstrip(".") if "." in quantity else quantity
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_document(cls, raw: str) -> NativeDispatchBinding:
        value = json.loads(raw, object_pairs_hook=_unique)
        if type(value) is not dict or set(value) != {field.name for field in fields(cls)}:
            raise RuntimeAuthorityError("native dispatch binding fields differ")
        if type(value["quantity"]) is not str:
            raise RuntimeAuthorityError("stored native quantity must be a decimal string")
        value["quantity"] = Decimal(value["quantity"])
        return cls(**value)


@dataclass(frozen=True)
class NativeNoFillSettlement:
    binding: NativeDispatchBinding
    journal_id: UUID
    journal_revision: int
    sealed_at_revision: int
    intent_id: str
    command_keccak256: str
    proof_digest: str
    source: Literal["never_submitted", "expired_with_coverage"]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, NativeDispatchBinding):
            raise RuntimeAuthorityError(
                "native settlement requires the original dispatch semantics"
            )
        _journal(self.journal_id)
        _revision(self.journal_revision)
        _revision(self.sealed_at_revision)
        if self.sealed_at_revision > self.journal_revision:
            raise RuntimeAuthorityError("native proof exceeds its observed journal revision")
        _digest(self.intent_id, prefix=True)
        _digest(self.command_keccak256, prefix=True)
        _digest(self.proof_digest, prefix=False)
        if self.source not in {"never_submitted", "expired_with_coverage"}:
            raise RuntimeAuthorityError("native proof source is unsupported")

    def identity_document(self) -> str:
        return json.dumps(
            {
                "binding": self.binding.document(),
                "journal_id": str(self.journal_id),
                "sealed_at_revision": self.sealed_at_revision,
                "intent_id": self.intent_id,
                "command_keccak256": self.command_keccak256,
                "proof_digest": self.proof_digest,
                "source": self.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class NativeDispatchSnapshot:
    binding: NativeDispatchBinding
    runtime_id: UUID
    authorization_epoch: int
    process_generation: int
    permit_digest: str
    journal_id: UUID
    missing_revision: int
    state: Literal["prepared", "dispatched", "not_sent", "proven_no_fill"]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, NativeDispatchBinding)
            or not isinstance(self.runtime_id, UUID)
            or self.runtime_id.int == 0
        ):
            raise RuntimeAuthorityError("stored Native dispatch identity is invalid")
        _journal(self.journal_id)
        for value in (self.authorization_epoch, self.process_generation, self.missing_revision):
            _revision(value)
        _digest(self.permit_digest, prefix=False)
        if self.state not in {"prepared", "dispatched", "not_sent", "proven_no_fill"}:
            raise RuntimeAuthorityError("stored Native dispatch state is invalid")
