"""The offline lane's desired-state contract and its wire envelope.

This is deliberately not the canonical V1 ``DeploymentSpec``. The two shapes have
diverged, and the offline lane answers a different question — is the strategy
logic right — from a strategy directory the operator bind-mounts rather than from
a published artifact. Keeping it separate leaves V1 with one owner and one parser.

Live is refused during validation, so an instance of this model cannot exist in
live mode regardless of which entry point produced it.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Self
from uuid import UUID

import uuid6
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from custos.contracts import LifecycleState, TradingMode
from custos.offline.mode_guard import refuse_live

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
SafeId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Rfc3339Nanos = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z$"),
]

_HASH_EXCLUDE_DIRS = frozenset({"__pycache__"})
_HASH_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo"})
_SUBJECT_ROOT = "arx"


class ProvenanceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: SafeId


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starting_balances: list[str] = Field(min_length=1)


class OfflineDeploymentSpec(BaseModel):
    """Desired state accepted on the offline lane, in the shape PS renders."""

    model_config = ConfigDict(extra="forbid", title="OfflineDeploymentSpec v1")

    spec_id: SafeId
    generation: StrictInt = Field(ge=1)
    trading_mode: TradingMode
    lifecycle_state: LifecycleState
    strategy_path: str = Field(min_length=1)
    provenance_ref: ProvenanceRef
    connector: str = Field(min_length=1)
    pairs: list[str] = Field(min_length=1)
    leverage: StrictInt = Field(ge=1)
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    strategy_registry_name: str | None = None
    code_hash: Sha256Hex | None = None
    log_level: str = "INFO"
    sandbox: SandboxConfig | None = None
    approved_by: list[str] = Field(default_factory=list)
    risk_config: dict[str, Any] = Field(default_factory=dict)
    nautilus_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_mode_requirements(self) -> Self:
        refuse_live(self.trading_mode.value, source="deployment spec")
        if self.trading_mode is TradingMode.SANDBOX and self.sandbox is None:
            raise ValueError("sandbox deployment requires sandbox.starting_balances")
        return self


class _OfflinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: SafeId
    spec: OfflineDeploymentSpec


class _OfflineWireEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_version: Annotated[StrictInt, Field(ge=1, le=1)]
    event_id: UUID
    tenant_id: SafeId
    occurred_at: Rfc3339Nanos
    payload_schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    payload: _OfflinePayload

    @field_validator("event_id")
    @classmethod
    def require_time_ordered_id(cls, value: UUID) -> UUID:
        if value.version != 7:
            raise ValueError("offline deployment event_id must be UUIDv7")
        return value


@dataclass(frozen=True, slots=True)
class OfflineDeploymentMessage:
    """A desired-state spec bound to the one subject that may carry it."""

    subject: str
    envelope: dict[str, Any]
    spec: OfflineDeploymentSpec

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        strategy_id: str,
        spec: OfflineDeploymentSpec,
    ) -> OfflineDeploymentMessage:
        payload = _OfflinePayload.model_validate({"strategy_id": strategy_id, "spec": spec})
        envelope = {
            "envelope_version": 1,
            "event_id": str(uuid6.uuid7()),
            "tenant_id": tenant_id,
            "occurred_at": now_rfc3339_nanos(),
            "payload_schema_version": 1,
            "payload": payload.model_dump(mode="json"),
        }
        wire = _OfflineWireEnvelope.model_validate(envelope)
        return cls(
            subject=offline_subject(wire.tenant_id, "deployment_spec", payload.strategy_id),
            envelope=envelope,
            spec=payload.spec,
        )

    @classmethod
    def parse(cls, data: bytes, *, expected_tenant_id: str) -> OfflineDeploymentMessage:
        wire = _OfflineWireEnvelope.model_validate_json(data)
        if wire.tenant_id != expected_tenant_id:
            raise ValueError(
                f"offline deployment message addresses tenant {wire.tenant_id!r}, "
                f"not {expected_tenant_id!r}"
            )
        return cls(
            subject=offline_subject(wire.tenant_id, "deployment_spec", wire.payload.strategy_id),
            envelope=json.loads(data),
            spec=wire.payload.spec,
        )

    def to_bytes(self) -> bytes:
        return json.dumps(self.envelope, separators=(",", ":")).encode("utf-8")


def offline_subject(tenant: str, kind: str, *path_parts: str) -> str:
    """Build the one subject a tenant's offline traffic may use.

    Empty tokens raise rather than collapsing into a subject with a hole in it,
    which NATS would treat as a different address than the one intended.
    """

    if not tenant or not kind:
        raise ValueError("tenant and kind are required")
    parts = [tenant, kind, *path_parts]
    if any(not part for part in parts):
        raise ValueError("subject path parts must be non-empty")
    return f"{_SUBJECT_ROOT}." + ".".join(parts)


def compute_strategy_code_hash(strategy_dir: str | Path) -> str:
    """Digest a strategy directory over both content and layout.

    Files contribute their relative path as well as their bytes, in sorted order,
    so a rename changes the digest exactly as an edit does. Build artefacts are
    excluded because they are reproducible from the sources beside them.
    """

    directory = Path(strategy_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"strategy directory not found: {directory}")
    digest = hashlib.sha256()
    for file_path in sorted(directory.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(directory)
        if any(part in _HASH_EXCLUDE_DIRS for part in relative.parts):
            continue
        if file_path.suffix in _HASH_EXCLUDE_SUFFIXES:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def now_rfc3339_nanos() -> str:
    """RFC 3339 at nanosecond precision, which ``datetime`` truncates away."""

    nanos = time.time_ns()
    seconds, remainder = divmod(nanos, 1_000_000_000)
    moment = time.gmtime(seconds)
    return (
        f"{moment.tm_year:04d}-{moment.tm_mon:02d}-{moment.tm_mday:02d}"
        f"T{moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d}.{remainder:09d}Z"
    )
