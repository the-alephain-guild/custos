"""Immutable position evidence for an explicitly targeted reduction."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from custos.core.execution_risk_events import _token
from custos.core.runtime_authority import RuntimeAuthorityError

if TYPE_CHECKING:
    from custos.core.local_runtime_state import RuntimeLease


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


ReductionClaimState = Literal[
    "prepared", "dispatching", "unknown", "not_sent", "filled", "proven_no_fill"
]


@dataclass(frozen=True)
class ReductionClaimSnapshot:
    runtime_id: UUID
    authorization_epoch: int
    process_generation: int
    permit_digest: str
    closing_client_order_id: str
    target: ReductionTarget
    state: ReductionClaimState

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_id, UUID) or self.runtime_id.int == 0:
            raise RuntimeAuthorityError("reduction claim requires a runtime identity")
        for value in (self.authorization_epoch, self.process_generation):
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise RuntimeAuthorityError("reduction claim revisions must be positive integers")
        _token(self.closing_client_order_id, "closing_client_order_id")
        if len(self.permit_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.permit_digest
        ):
            raise RuntimeAuthorityError("reduction claim requires its actual permit digest")
        if not isinstance(self.target, ReductionTarget) or self.state not in {
            "prepared",
            "dispatching",
            "unknown",
            "not_sent",
            "filled",
            "proven_no_fill",
        }:
            raise RuntimeAuthorityError("reduction claim target or state is invalid")


class DurableReductionPort(Protocol):
    @property
    def reservation_store(self) -> object: ...
    @property
    def runtime_lease(self) -> RuntimeLease: ...
    @property
    def runtime_id(self) -> UUID: ...
    @property
    def policy_id(self) -> UUID: ...
    def bind_runtime(self, semantics: Any) -> None: ...
    def prepare_submit(self, intent: Any) -> bool: ...
    def begin_dispatch(self, client_order_id: str) -> bool: ...
    def mark_not_sent(self, client_order_id: str) -> None: ...
    def dispatch_failed(self, client_order_id: str) -> None: ...
    def judged_risk_reducing(self, client_order_id: str) -> bool | None: ...
    def on_order_event(self, event: Any, *, stable_event_id: str) -> None: ...
