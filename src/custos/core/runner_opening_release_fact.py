"""Closed opening decisions carried by the existing signed RunnerFact stream."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from custos.core.opening_inventory_contract import (
    Currency,
    Digest,
    OpeningInventory,
    Timestamp,
    UnsignedMoney,
    UuidText,
    normalize_opening_inventory,
)
from custos.core.runner_fact import (
    OPENING_RELEASE_FACT_KIND,
    RunnerFactAuthority,
    RunnerFactContractError,
    runner_fact_event_id,
)

OpeningReleaseReason = Literal["inventory_unproven", "account_mismatch", "balance_mismatch"]


class RunnerOpeningReleaseFactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["RunnerOpeningReleaseFact.v1"]
    event_id: UuidText
    release_id: UuidText
    release_command_event_id: UuidText
    basis_version_digest: Digest
    outcome: Literal["acknowledged", "refused"]
    reason: OpeningReleaseReason | None
    amount: UnsignedMoney
    currency: Currency
    observed_at: Timestamp
    account_inventory: OpeningInventory

    @model_validator(mode="after")
    def require_decision_boundary(self):
        for field in ("event_id", "release_id", "release_command_event_id"):
            if UUID(getattr(self, field)).int == 0:
                raise ValueError("opening decision identifiers must not be nil")
        expected_event = runner_fact_event_id("opening_release_" + self.outcome, self.release_id)
        if self.event_id != str(expected_event):
            raise ValueError("opening decision event differs from its stable release identity")
        if (self.outcome == "acknowledged") != (self.reason is None):
            raise ValueError("only an acknowledged opening decision has no refusal reason")
        inventory = self.account_inventory
        binding = inventory.equity_binding
        if binding.status == "coherent" and (
            binding.amount != self.amount
            or binding.currency != self.currency
            or binding.observed_at != self.observed_at
        ):
            raise ValueError("opening decision differs from its observed equity boundary")
        if self.outcome == "acknowledged" and (
            not inventory.inventory_complete
            or inventory.observations.positions.rows
            or inventory.observations.ordinary_orders.rows
        ):
            raise ValueError("opening acknowledgement requires a complete empty cash account")
        return self


def opening_release_fact(
    *,
    authority: RunnerFactAuthority,
    release_id: str,
    release_command_event_id: str,
    basis_version_digest: str,
    outcome: str,
    reason: str | None,
    amount: str,
    currency: str,
    observed_at: str,
    account_inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one observation; only the durable release transaction may enqueue it."""
    try:
        fact = RunnerOpeningReleaseFactV1.model_validate(
            {
                "kind": OPENING_RELEASE_FACT_KIND,
                "event_id": str(runner_fact_event_id("opening_release_" + outcome, release_id)),
                "release_id": release_id,
                "release_command_event_id": release_command_event_id,
                "basis_version_digest": basis_version_digest,
                "outcome": outcome,
                "reason": reason,
                "amount": amount,
                "currency": currency,
                "observed_at": observed_at,
                "account_inventory": account_inventory,
            }
        )
        result = fact.model_dump(mode="json")
        result["account_inventory"] = normalize_opening_inventory(
            result["account_inventory"],
            authority=authority,
            amount=amount,
            currency=currency,
            observed_at=observed_at,
        )
        return result
    except ValueError as error:
        raise RunnerFactContractError(f"invalid opening decision: {error}") from error
