"""Strict shape and current-scope binding for an independent opening release."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from custos.contracts.crucible_runner_command import (
    CrucibleRunnerDeploymentCommandV1,
    _required_uuid,
    _strict_json_object,
)
from custos.core.opening_inventory_contract import (
    AccountIdentity,
    Digest,
    Timestamp,
    UnsignedMoney,
    UuidText,
)

_EVENT_TYPE = "RunnerOpeningReleaseCommand.v1"
_SUBJECT_PREFIX = "crucible.runner.opening-release.v1"
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "tenant_id",
        "event_plane",
        "bounded_context",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "event_type",
        "payload",
        "correlation_id",
        "actor_assertion_jti",
        "occurred_at",
    }
)


class RunnerOpeningReleaseCommandV1(BaseModel):
    """Release authority for one already authenticated deployment generation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    release_id: UuidText
    tenant_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    trading_mode: Literal["sandbox", "testnet", "live"]
    runner_id: UuidText
    deployment_instance_id: UuidText
    deployment_spec_id: UuidText
    deployment_spec_digest: Digest
    generation: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    credential_scope_id: UuidText
    credential_scope_digest: Digest
    account_identity: AccountIdentity
    basis_amount: UnsignedMoney
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,12}$")]
    basis_version_digest: Digest
    opening_valuation_event_id: UuidText
    issued_at: Timestamp

    @model_validator(mode="after")
    def validate_account_mode(self) -> Self:
        if (self.account_identity.kind == "sandbox_account") != (self.trading_mode == "sandbox"):
            raise ValueError("opening release account identity differs from its trading mode")
        return self

    @classmethod
    def from_verified_event(cls, subject: str, event_bytes: bytes) -> Self:
        event = _strict_json_object(event_bytes, "opening release event")
        payload = event.get("payload")
        if (
            frozenset(event) != _EVENT_FIELDS
            or type(event["schema_version"]) is not int
            or event["schema_version"] != 1
            or type(event["aggregate_version"]) is not int
            or event["aggregate_version"] != 1
            or event["bounded_context"] != "deployment"
            or event["aggregate_type"] != "runner_opening_release"
            or event["event_type"] != _EVENT_TYPE
            or event["actor_assertion_jti"] is not None
            or not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
        ):
            raise ValueError("opening release event differs from the V1 authority")
        _required_uuid(event["event_id"], "opening release event id")
        _required_uuid(event["correlation_id"], "opening release correlation id")
        command = cls.model_validate(payload)
        if (
            subject
            != f"{_SUBJECT_PREFIX}.{command.tenant_id}.{command.runner_id}.{command.trading_mode}"
            or event["tenant_id"] != command.tenant_id
            or event["event_plane"] != {"kind": "mode", "trading_mode": command.trading_mode}
            or event["aggregate_id"] != command.release_id
            or event["occurred_at"] != command.issued_at
        ):
            raise ValueError("opening release command differs from its signed scope")
        return command

    def require_current_desired(self, desired: CrucibleRunnerDeploymentCommandV1) -> None:
        """Bind an intake receipt to a current signed lifecycle command."""
        scope = desired.deployment_spec.get("credential_scope")
        if (
            not desired.exact_signed_event_bytes
            or not desired.command_fingerprint
            or desired.lifecycle_state != "running"
            or desired.tenant_id != self.tenant_id
            or desired.trading_mode != self.trading_mode
            or str(desired.runner_id) != self.runner_id
            or str(desired.deployment_instance_id) != self.deployment_instance_id
            or str(desired.deployment_spec_id) != self.deployment_spec_id
            or desired.deployment_spec_digest != self.deployment_spec_digest
            or desired.generation != self.generation
            or not isinstance(scope, dict)
            or scope.get("scope_id") != self.credential_scope_id
            or scope.get("scope_digest") != self.credential_scope_digest
        ):
            raise ValueError("opening release differs from the current running desired target")

    def require_current_target(
        self, desired: CrucibleRunnerDeploymentCommandV1, account_identity: AccountIdentity
    ) -> None:
        """Recheck both desired authority and a fresh account observation."""
        self.require_current_desired(desired)
        if self.account_identity != account_identity:
            raise ValueError(
                "opening release account differs from the current running desired target"
            )
