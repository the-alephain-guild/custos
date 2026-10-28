"""Immutable position evidence for an explicitly targeted reduction."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from custos.core.execution_risk_events import _token
from custos.core.runtime_authority import RuntimeAuthorityError


@dataclass(frozen=True)
class ReductionTarget:
    account_id: str
    client_id: str
    position_id: str
    instrument_id: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    params_digest: str

    def __post_init__(self) -> None:
        for name in ("account_id", "client_id", "position_id", "instrument_id"):
            _token(getattr(self, name), name)
        if self.side not in {"BUY", "SELL"}:
            raise RuntimeAuthorityError("reduction direction is invalid")
        if (
            type(self.quantity) is not Decimal
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise RuntimeAuthorityError("reduction quantity requires a positive exact Decimal")
        if len(self.params_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.params_digest
        ):
            raise RuntimeAuthorityError("reduction parameters require a SHA-256 digest")
