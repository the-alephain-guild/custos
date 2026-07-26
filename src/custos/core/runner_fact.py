"""Durable, signed RunnerFact production and JetStream delivery.

This module is the only Custos boundary allowed to construct RunnerFact batches.
Engine adapters provide raw facts; this module owns stream sequencing, canonical
digests, signatures, durable retry, and the exact NATS subject.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal
from uuid import UUID, uuid4, uuid5

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.contracts.crucible_runner_safety_policy import (
    RunnerAggregateCapPolicyV1,
    VerifiedRunnerSafetyPolicy,
)
from custos.core.log import get_logger
from custos.core.runner_command_intake import (
    CommandIdentityDecision,
    CommandTerminalOutcome,
    CommandVerificationReceipt,
    DesiredCommandRecord,
    DurableCommandOutcome,
    InboundCommandDisposition,
    VerifiedRunnerCommand,
    classify_command_identity,
    compute_command_fingerprint,
)

RUNNER_FACT_SCHEMA_VERSION: Final = 1
RUNNER_FACT_SIGNING_DOMAIN: Final = b"CRUCIBLE-RUNNER-FACT-BATCH-V1\0"
RUNNER_FACT_SIGNING_HEADER_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "batch_id",
    "tenant_id",
    "trading_mode",
    "runner_id",
    "deployment_instance_id",
    "deployment_spec_id",
    "deployment_spec_digest",
    "generation",
    "strategy_id",
    "capability_version_id",
    "capability_version",
    "capability_manifest_digest",
    "key_id",
    "emitted_at",
    "source_seq_start",
    "source_seq_end",
    "payload_digest",
)
REGISTRATION_SIGNING_DOMAIN: Final = "arx.runner_verification_key.register.v1"
PUBLICATION_SIGNING_DOMAIN: Final = "crucible.runner_capability.publish.v1"
RUNNER_FACT_EVENT_NAMESPACE: Final = UUID("834c6f30-4d2c-5f91-a2c4-5e8358fe6be4")
SUPPORTED_CURRENCIES: Final = frozenset({"USD", "USDT", "USDC", "BTC", "ETH"})
MAX_FACTS_PER_BATCH: Final = 512
MAX_BATCH_BYTES: Final = 768 * 1024
MAX_VENUE_LEDGER_CHUNKS: Final = 4096
MAX_VENUE_LEDGER_ITEMS_PER_CHUNK: Final = 512
MAX_VENUE_LEDGER_CHUNK_BYTES: Final = 262_144
RUNNER_STATE_SCHEMA_VERSION: Final = 1
RUNNER_FACT_KIND_PROJECTORS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "execution_fill": "reconciliation",
        "fill": "settlement",
        "position_closed": "settlement",
        "fee": "settlement",
        "period_closed": "settlement",
        "equity_snapshot": "risk",
        "position_snapshot": "risk",
        "heartbeat": "health",
        "RunnerRuntimeLogFact.v1": "health",
        "venue_ledger_snapshot_manifest": "reconciliation",
        "venue_ledger_snapshot_chunk": "reconciliation",
        "reconciliation_period_closed": "reconciliation",
        "RunnerDeploymentLifecycleFact.v1": "deployment_lifecycle",
    }
)
RUNNER_FACT_PROJECTOR_CONTRACTS: Final[Mapping[str, Mapping[str, object]]] = MappingProxyType(
    {
        "risk": MappingProxyType(
            {
                "schema_version": 1,
                "equity_snapshot": "v1",
                "position_snapshot": "v1",
                "heartbeat": "v1",
            }
        ),
        "settlement": MappingProxyType(
            {
                "schema_version": 1,
                "fill": "v1",
                "position_closed": "v1",
                "fee": "v1",
                "period_closed": "v1",
            }
        ),
        "reconciliation": MappingProxyType(
            {
                "schema_version": 1,
                "execution_fill": "v1",
                "venue_ledger_snapshot_manifest": "v1",
                "venue_ledger_snapshot_chunk": "v1",
                "reconciliation_period_closed": "v1",
            }
        ),
        "health": MappingProxyType(
            {
                "schema_version": 1,
                "heartbeat": "v1",
            }
        ),
        "deployment_lifecycle": MappingProxyType(
            {
                "schema_version": 1,
                "deployment_lifecycle": "v1",
            }
        ),
    }
)
_NATS_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_log = get_logger("custos.runner_fact")


class RunnerFactError(RuntimeError):
    """Base error for RunnerFact production."""


class RunnerFactContractError(RunnerFactError):
    """A fact or authority value violates the wire contract."""


def validate_runner_fact_payload(value: object, *, path: str = "$") -> None:
    """Reject values whose canonical JSON representation is language-dependent."""

    if isinstance(value, float):
        raise RunnerFactContractError(
            f"runner fact payload {path} must not contain Python float; "
            "use int or a canonical decimal string"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            validate_runner_fact_payload(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_runner_fact_payload(item, path=f"{path}[{index}]")


class RunnerFactIdentityError(RunnerFactError):
    """The local signing identity is absent, unsafe, or corrupt."""


class RunnerStateMigrationError(RunnerFactError):
    """The single SQLite state schema cannot be safely opened or migrated."""


class RunnerStateAuthorityError(RunnerFactError):
    """A durable instance is being addressed through a different authority."""


class RunnerStateDurabilityError(RunnerFactError):
    """A command state transition cannot satisfy the single-store invariant."""


@dataclass(frozen=True, slots=True)
class OrderReservationSnapshot:
    deployment_instance_id: UUID
    client_order_id: str
    policy_id: UUID
    reserved_notional: Decimal
    filled_exposure: Decimal
    state: str


@dataclass(frozen=True, slots=True)
class OrderReservationRebuildEntry:
    deployment_instance_id: UUID
    client_order_id: str
    reserved_notional: Decimal


@dataclass(frozen=True, slots=True)
class RunnerExposureSnapshot:
    policy_id: UUID
    open_exposure: Decimal
    reserved_notional: Decimal
    total_exposure: Decimal
    max_total_notional: Decimal
    within_policy: bool
    source_digest: str | None


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RunnerFactContractError(f"value is not canonical JSON: {exc}") from exc


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def runner_fact_signing_header(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact ordered and closed RunnerFact v1 signing header."""

    if not isinstance(value, Mapping):
        raise RunnerFactContractError("RunnerFact signing input must be an object")
    header_keys = frozenset(RUNNER_FACT_SIGNING_HEADER_FIELDS)
    input_keys = frozenset(value)
    batch_keys = header_keys | {"facts", "signature"}
    if input_keys not in {header_keys, batch_keys}:
        raise RunnerFactContractError("RunnerFact signing header fields differ from v1 contract")
    header = {field: value[field] for field in RUNNER_FACT_SIGNING_HEADER_FIELDS}
    if input_keys == batch_keys:
        facts = value["facts"]
        validate_runner_fact_payload(facts, path="$.facts")
        if header["payload_digest"] != _sha256_hex(_canonical_json_bytes(facts)):
            raise RunnerFactContractError("RunnerFact signing payload digest differs from facts")
    return header


def runner_fact_signing_preimage(value: Mapping[str, Any]) -> bytes:
    """Build DOMAIN plus canonical JSON header bytes for RunnerFact v1."""

    return RUNNER_FACT_SIGNING_DOMAIN + _canonical_json_bytes(runner_fact_signing_header(value))


def _utc_now() -> str:
    return _render_utc(datetime.now(UTC))


def _render_utc(value: datetime) -> str:
    """Render like chrono's RFC3339 AutoSi serializer used by Crucible."""
    value = value.astimezone(UTC)
    base = value.strftime("%Y-%m-%dT%H:%M:%S")
    micros = value.microsecond
    if micros == 0:
        return f"{base}Z"
    if micros % 1000 == 0:
        return f"{base}.{micros // 1000:03d}Z"
    return f"{base}.{micros:06d}Z"


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _uuid(value: UUID | str, field: str) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RunnerFactContractError(f"{field} must be a UUID") from exc
    if parsed.int == 0:
        raise RunnerFactContractError(f"{field} must not be nil")
    return str(parsed)


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunnerFactContractError(f"{field} must be a non-empty string")
    return value.strip()


def _settlement_period(value: Any) -> str:
    period = _non_empty(value, "period")
    try:
        parsed = datetime.strptime(period, "%Y-%m")
    except ValueError as error:
        raise RunnerFactContractError("settlement period must use YYYY-MM") from error
    if parsed.strftime("%Y-%m") != period:
        raise RunnerFactContractError("settlement period must use YYYY-MM")
    return period


def _decimal(value: Decimal | str | int, field: str, *, positive: bool = False) -> str:
    if isinstance(value, float):
        raise RunnerFactContractError(f"{field} must not be a binary float")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RunnerFactContractError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise RunnerFactContractError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise RunnerFactContractError(f"{field} must be greater than zero")
    if not positive and parsed < 0:
        raise RunnerFactContractError(f"{field} must not be negative")
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _signed_decimal(value: Decimal | str | int, field: str) -> str:
    if isinstance(value, float):
        raise RunnerFactContractError(f"{field} must not be a binary float")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RunnerFactContractError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise RunnerFactContractError(f"{field} must be finite")
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _timestamp(value: datetime | str, field: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RunnerFactContractError(f"{field} must include a timezone")
        return _render_utc(value)
    text = _non_empty(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerFactContractError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise RunnerFactContractError(f"{field} must include a timezone")
    return _render_utc(parsed)


def _currency(value: str) -> str:
    """Return the exact v1 Currency wire; object compatibility is forbidden."""
    currency = _non_empty(value, "currency")
    if currency != currency.upper() or currency not in SUPPORTED_CURRENCIES:
        raise RunnerFactContractError("currency must be one of USD, USDT, USDC, BTC, or ETH")
    return currency


def _side(value: str) -> str:
    normalized = value.lower().strip()
    if normalized not in {"buy", "sell"}:
        raise RunnerFactContractError("side must be buy or sell")
    return normalized


@dataclass(frozen=True, slots=True)
class RunnerFactAuthority:
    tenant_id: str
    trading_mode: str
    runner_id: UUID
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str
    generation: int
    strategy_id: UUID
    capability_version_id: UUID
    capability_version: int
    capability_manifest_digest: str

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        if not _NATS_TOKEN.fullmatch(self.tenant_id):
            raise RunnerFactContractError("tenant_id must be one safe NATS subject token")
        if self.trading_mode not in {"live", "sandbox", "testnet"}:
            raise RunnerFactContractError("trading_mode must be live, sandbox, or testnet")
        _uuid(self.runner_id, "runner_id")
        _uuid(self.deployment_instance_id, "deployment_instance_id")
        _uuid(self.deployment_spec_id, "deployment_spec_id")
        if type(self.generation) is not int or self.generation < 1:
            raise RunnerFactContractError("generation must be a positive integer")
        _uuid(self.strategy_id, "strategy_id")
        _uuid(self.capability_version_id, "capability_version_id")
        if self.capability_version < 1:
            raise RunnerFactContractError("capability_version must be positive")
        if not _LOWER_HEX_64.fullmatch(self.capability_manifest_digest):
            raise RunnerFactContractError(
                "capability_manifest_digest must be 64 lowercase hexadecimal characters"
            )
        if not _LOWER_HEX_64.fullmatch(self.deployment_spec_digest):
            raise RunnerFactContractError(
                "deployment_spec_digest must be 64 lowercase hexadecimal characters"
            )

    @property
    def stream_key(self) -> str:
        return (
            f"{self.tenant_id}:{self.trading_mode}:{self.runner_id}:{self.deployment_instance_id}"
        )

    @property
    def subject(self) -> str:
        return f"crucible.runner.fact.v1.{self.tenant_id}.{self.runner_id}.{self.trading_mode}"


@dataclass(frozen=True, slots=True)
class RunnerFactRegistrationRequest:
    idempotency_key: UUID
    body: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerCapabilityPublicationRequest:
    idempotency_key: UUID
    capability_version_id: UUID
    capability_version: int
    manifest_digest: str
    body: Mapping[str, Any]


class RunnerFactIdentity:
    """An Ed25519 signer loaded only from the encrypted machine vault."""

    def __init__(self, private_key: Ed25519PrivateKey, key_id: str) -> None:
        self._private_key = private_key
        self.key_id = _non_empty(key_id, "key_id")

    @classmethod
    def from_private_bytes(cls, private_bytes: bytes, key_id: str) -> RunnerFactIdentity:
        if len(private_bytes) != 32:
            raise RunnerFactIdentityError("Ed25519 private key must contain 32 bytes")
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        public_digest = _sha256_hex(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        expected_key_id = f"ed25519-{public_digest[:32]}"
        if key_id != expected_key_id:
            raise RunnerFactIdentityError("machine key id does not match Ed25519 private key")
        return cls(private_key, key_id)

    @property
    def public_key_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign_batch_payload(self, canonical_payload: bytes) -> str:
        signature = self._private_key.sign(RUNNER_FACT_SIGNING_DOMAIN + canonical_payload)
        return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    def registration_request(
        self,
        *,
        tenant_id: str,
        runner_id: UUID,
        capability_version_id: UUID,
        capability_version: int,
        manifest_digest: str,
        idempotency_key: UUID | None = None,
    ) -> RunnerFactRegistrationRequest:
        tenant = _non_empty(tenant_id, "tenant_id")
        runner = _uuid(runner_id, "runner_id")
        capability_id = _uuid(capability_version_id, "capability_version_id")
        if capability_version < 1:
            raise RunnerFactContractError("capability_version must be positive")
        if not _LOWER_HEX_64.fullmatch(manifest_digest):
            raise RunnerFactContractError("manifest_digest must be lowercase SHA-256")
        idempotency = idempotency_key or uuid4()
        public_key_digest = _sha256_hex(self.public_key_bytes)
        proof = "\n".join(
            (
                REGISTRATION_SIGNING_DOMAIN,
                f"tenant_id={tenant}",
                f"runner_id={runner}",
                f"capability_version_id={capability_id}",
                f"capability_version={capability_version}",
                f"manifest_digest={manifest_digest}",
                f"key_id={self.key_id}",
                "algorithm=ed25519",
                f"public_key_sha256={public_key_digest}",
                f"idempotency_key={idempotency}",
            )
        )
        return RunnerFactRegistrationRequest(
            idempotency_key=idempotency,
            body={
                "capability_version_id": capability_id,
                "capability_version": capability_version,
                "manifest_digest": manifest_digest,
                "key_id": self.key_id,
                "public_key_base64": base64.b64encode(self.public_key_bytes).decode("ascii"),
                "proof_signature_base64": base64.b64encode(
                    self._private_key.sign(proof.encode("utf-8"))
                ).decode("ascii"),
            },
        )

    def publication_request(
        self,
        *,
        tenant_id: str,
        runner_id: UUID,
        capability_manifest: Mapping[str, Any],
        capability_version: int,
        capability_version_id: UUID | None = None,
        idempotency_key: UUID | None = None,
    ) -> RunnerCapabilityPublicationRequest:
        """Create one exact capability-publication V1 plus initial key PoP request."""
        tenant = _non_empty(tenant_id, "tenant_id")
        runner = _uuid(runner_id, "runner_id")
        capability_id_value = capability_version_id or uuid4()
        capability_id = _uuid(capability_id_value, "capability_version_id")
        idempotency = idempotency_key or uuid4()
        if idempotency.int == 0:
            raise RunnerFactContractError("idempotency_key must not be nil")
        if type(capability_version) is not int or capability_version < 1:
            raise RunnerFactContractError("capability_version must be a positive integer")
        manifest = dict(capability_manifest)
        manifest_digest = _sha256_hex(_canonical_json_bytes(manifest))
        public_key_digest = _sha256_hex(self.public_key_bytes)
        proof = "\n".join(
            (
                PUBLICATION_SIGNING_DOMAIN,
                f"tenant_id={tenant}",
                f"runner_id={runner}",
                f"capability_version_id={capability_id}",
                f"capability_version={capability_version}",
                f"manifest_digest={manifest_digest}",
                f"key_id={self.key_id}",
                "algorithm=ed25519",
                f"public_key_sha256={public_key_digest}",
                f"idempotency_key={idempotency}",
            )
        )
        body = {
            "capability_version_id": capability_id,
            "capability_version": capability_version,
            "capability_manifest": manifest,
            "manifest_digest": manifest_digest,
            "key_id": self.key_id,
            "public_key_base64": base64.b64encode(self.public_key_bytes).decode("ascii"),
            "proof_signature_base64": base64.b64encode(
                self._private_key.sign(proof.encode("utf-8"))
            ).decode("ascii"),
        }
        return RunnerCapabilityPublicationRequest(
            idempotency_key=idempotency,
            capability_version_id=capability_id_value,
            capability_version=capability_version,
            manifest_digest=manifest_digest,
            body=body,
        )


def execution_fill(
    *,
    venue: str,
    venue_trade_id: str,
    venue_order_id: str,
    instrument: str,
    side: str,
    quantity: Decimal | str | int,
    price: Decimal | str | int,
    fee: Decimal | str | int,
    currency: str,
    occurred_at: datetime | str,
    client_order_id: str | None = None,
    event_id: UUID | str,
) -> dict[str, Any]:
    return {
        "kind": "execution_fill",
        "event_id": _uuid(event_id, "event_id"),
        "venue": _non_empty(venue, "venue"),
        "venue_trade_id": _non_empty(venue_trade_id, "venue_trade_id"),
        "client_order_id": (
            _non_empty(client_order_id, "client_order_id") if client_order_id else None
        ),
        "venue_order_id": _non_empty(venue_order_id, "venue_order_id"),
        "instrument": _non_empty(instrument, "instrument"),
        "side": _side(side),
        "quantity": _decimal(quantity, "quantity", positive=True),
        "price": _decimal(price, "price"),
        "fee": _decimal(fee, "fee"),
        "currency": _currency(currency),
        "occurred_at": _timestamp(occurred_at, "occurred_at"),
    }


def runner_fact_event_id(*parts: object) -> UUID:
    rendered = [str(part).strip() for part in parts]
    if not rendered or any(not part for part in rendered):
        raise RunnerFactContractError("deterministic event identity parts must be non-empty")
    return uuid5(RUNNER_FACT_EVENT_NAMESPACE, "\0".join(rendered))


def settlement_fill(
    *,
    event_id: UUID | str,
    fill_id: UUID | str,
    order_type: str,
    category: str,
    price: Decimal | str | int,
    avg_fill_price: Decimal | str | int,
    currency: str,
    filled_at: datetime | str,
) -> dict[str, Any]:
    return {
        "kind": "fill",
        "event_id": _uuid(event_id, "event_id"),
        "fill_id": _uuid(fill_id, "fill_id"),
        "order_type": _non_empty(order_type, "order_type"),
        "category": _non_empty(category, "category"),
        "price": _decimal(price, "price"),
        "avg_fill_price": _decimal(avg_fill_price, "avg_fill_price"),
        "currency": _currency(currency),
        "filled_at": _timestamp(filled_at, "filled_at"),
    }


def position_closed(
    *,
    event_id: UUID | str,
    position_id: UUID | str,
    realized_pnl: Decimal | str | int,
    currency: str,
    opened_at: datetime | str,
    closed_at: datetime | str,
) -> dict[str, Any]:
    opened = _timestamp(opened_at, "opened_at")
    closed = _timestamp(closed_at, "closed_at")
    if datetime.fromisoformat(closed.replace("Z", "+00:00")) < datetime.fromisoformat(
        opened.replace("Z", "+00:00")
    ):
        raise RunnerFactContractError("closed_at must not precede opened_at")
    return {
        "kind": "position_closed",
        "event_id": _uuid(event_id, "event_id"),
        "position_id": _uuid(position_id, "position_id"),
        "realized_pnl": _signed_decimal(realized_pnl, "realized_pnl"),
        "currency": _currency(currency),
        "opened_at": opened,
        "closed_at": closed,
    }


def settlement_fee(
    *,
    event_id: UUID | str,
    fill_id: UUID | str,
    amount: Decimal | str | int,
    currency: str,
    assessed_at: datetime | str,
) -> dict[str, Any]:
    return {
        "kind": "fee",
        "event_id": _uuid(event_id, "event_id"),
        "fill_id": _uuid(fill_id, "fill_id"),
        "amount": _decimal(amount, "amount"),
        "currency": _currency(currency),
        "assessed_at": _timestamp(assessed_at, "assessed_at"),
    }


def equity_snapshot(
    *,
    event_id: UUID | str,
    amount: Decimal | str | int,
    currency: str,
    observed_at: datetime | str,
) -> dict[str, Any]:
    return {
        "kind": "equity_snapshot",
        "event_id": _uuid(event_id, "event_id"),
        "amount": _signed_decimal(amount, "amount"),
        "currency": _currency(currency),
        "observed_at": _timestamp(observed_at, "observed_at"),
    }


def position_snapshot(
    *,
    event_id: UUID | str,
    positions: Sequence[Mapping[str, Any]],
    observed_at: datetime | str,
) -> dict[str, Any]:
    normalized = [
        {
            "instrument": _non_empty(row.get("instrument"), "positions.instrument"),
            "quantity": _signed_decimal(row.get("quantity"), "positions.quantity"),
            "mark_price": _decimal(row.get("mark_price"), "positions.mark_price"),
            "currency": _currency(row.get("currency")),
        }
        for row in positions
    ]
    normalized.sort(key=lambda row: str(row["instrument"]))
    _require_unique(normalized, "instrument", "positions")
    return {
        "kind": "position_snapshot",
        "event_id": _uuid(event_id, "event_id"),
        "positions": normalized,
        "observed_at": _timestamp(observed_at, "observed_at"),
    }


def heartbeat(
    *,
    event_id: UUID | str,
    status: str,
    observed_at: datetime | str,
) -> dict[str, Any]:
    normalized = _non_empty(status, "status").lower()
    if normalized not in {"online", "degraded", "offline"}:
        raise RunnerFactContractError("heartbeat status must be online, degraded, or offline")
    return {
        "kind": "heartbeat",
        "event_id": _uuid(event_id, "event_id"),
        "status": normalized,
        "observed_at": _timestamp(observed_at, "observed_at"),
    }


def settlement_period_closed(
    *,
    event_id: UUID | str,
    period: str,
    closed_at: datetime | str,
) -> dict[str, Any]:
    return {
        "kind": "period_closed",
        "event_id": _uuid(event_id, "event_id"),
        "period": _settlement_period(period),
        "closed_at": _timestamp(closed_at, "closed_at"),
    }


def venue_ledger_snapshot_facts(
    *,
    snapshot_id: UUID | str,
    venue: str,
    source: str,
    watermark: str,
    coverage_from: datetime | str,
    observed_through: datetime | str,
    completeness: Mapping[str, Any],
    balances: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    fills: Sequence[Mapping[str, Any]],
    fees: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    snapshot = _uuid(snapshot_id, "snapshot_id")
    venue_value = _non_empty(venue, "venue")
    source_value = _non_empty(source, "source")
    if source_value not in {"venue_api", "drop_copy"}:
        raise RunnerFactContractError("venue ledger source must be venue_api or drop_copy")
    watermark_value = _non_empty(watermark, "watermark")
    coverage = _timestamp(coverage_from, "coverage_from")
    observed = _timestamp(observed_through, "observed_through")
    if datetime.fromisoformat(coverage.replace("Z", "+00:00")) > datetime.fromisoformat(
        observed.replace("Z", "+00:00")
    ):
        raise RunnerFactContractError("coverage_from must not follow observed_through")
    normalized_balances = [
        {
            "asset": _non_empty(row.get("asset"), "balances.asset"),
            "currency": _currency(row.get("currency")),
            "total": _decimal(row.get("total"), "balances.total"),
            "available": _decimal(row.get("available"), "balances.available"),
        }
        for row in balances
    ]
    normalized_positions = [
        {
            "venue_position_id": _non_empty(
                row.get("venue_position_id"), "positions.venue_position_id"
            ),
            "instrument": _non_empty(row.get("instrument"), "positions.instrument"),
            "side": _side(row.get("side", "")),
            "quantity": _decimal(row.get("quantity"), "positions.quantity"),
            "avg_entry_price": (
                _decimal(row["avg_entry_price"], "positions.avg_entry_price")
                if row.get("avg_entry_price") is not None
                else None
            ),
            "currency": _currency(row.get("currency")),
        }
        for row in positions
    ]
    normalized_fills = [
        {
            "venue_trade_id": _non_empty(row.get("venue_trade_id"), "fills.venue_trade_id"),
            "venue_order_id": _non_empty(row.get("venue_order_id"), "fills.venue_order_id"),
            "instrument": _non_empty(row.get("instrument"), "fills.instrument"),
            "side": _side(row.get("side", "")),
            "quantity": _decimal(row.get("quantity"), "fills.quantity", positive=True),
            "price": _decimal(row.get("price"), "fills.price"),
            "fee": _decimal(row.get("fee"), "fills.fee"),
            "currency": _currency(row.get("currency")),
            "occurred_at": _timestamp(row.get("occurred_at"), "fills.occurred_at"),
        }
        for row in fills
    ]
    normalized_fees = [
        {
            "fee_id": _non_empty(row.get("fee_id"), "fees.fee_id"),
            "kind": _non_empty(row.get("kind"), "fees.kind"),
            "currency": _currency(row.get("currency")),
            "amount": _decimal(row.get("amount"), "fees.amount"),
            "occurred_at": _timestamp(row.get("occurred_at"), "fees.occurred_at"),
        }
        for row in fees
    ]
    normalized_balances.sort(key=lambda row: (str(row["asset"]), str(row["currency"])))
    normalized_positions.sort(
        key=lambda row: (str(row["instrument"]), str(row["side"]), str(row["venue_position_id"]))
    )
    normalized_fills.sort(
        key=lambda row: (
            str(row["occurred_at"]),
            str(row["instrument"]),
            str(row["venue_trade_id"]),
            str(row["venue_order_id"]),
        )
    )
    normalized_fees.sort(
        key=lambda row: (str(row["occurred_at"]), str(row["kind"]), str(row["fee_id"]))
    )
    balance_keys = [(str(row["asset"]), str(row["currency"])) for row in normalized_balances]
    if len(balance_keys) != len(set(balance_keys)):
        raise RunnerFactContractError("balances contains duplicate asset/currency")
    _require_unique(normalized_positions, "venue_position_id", "positions")
    fill_keys = [(str(row["instrument"]), str(row["venue_trade_id"])) for row in normalized_fills]
    if len(fill_keys) != len(set(fill_keys)):
        raise RunnerFactContractError("fills contains duplicate instrument/venue_trade_id")
    _require_unique(normalized_fees, "fee_id", "fees")
    completeness_value = {
        field: _required_bool(completeness.get(field), f"completeness.{field}")
        for field in (
            "balances_complete",
            "positions_complete",
            "fills_complete",
            "fees_complete",
        )
    }
    ordered_items: list[tuple[str, dict[str, Any]]] = []
    for label, rows in (
        ("balances", normalized_balances),
        ("positions", normalized_positions),
        ("fills", normalized_fills),
        ("fees", normalized_fees),
    ):
        ordered_items.extend((label, row) for row in rows)
    chunk_rows: list[dict[str, list[dict[str, Any]]]] = []
    current = {"balances": [], "positions": [], "fills": [], "fees": []}
    for label, row in ordered_items:
        candidate = {key: list(values) for key, values in current.items()}
        candidate[label].append(row)
        candidate_value = {
            "snapshot_id": snapshot,
            "chunk_index": len(chunk_rows),
            "chunk_count": MAX_VENUE_LEDGER_CHUNKS,
            **candidate,
        }
        item_count = sum(len(values) for values in candidate.values())
        if (
            item_count > MAX_VENUE_LEDGER_ITEMS_PER_CHUNK
            or len(_canonical_json_bytes(candidate_value)) > MAX_VENUE_LEDGER_CHUNK_BYTES
        ):
            if not any(current.values()):
                raise RunnerFactContractError("one venue ledger item exceeds the chunk byte limit")
            chunk_rows.append(current)
            current = {"balances": [], "positions": [], "fills": [], "fees": []}
            current[label].append(row)
        else:
            current = candidate
    if any(current.values()) or not chunk_rows:
        chunk_rows.append(current)
    if len(chunk_rows) > MAX_VENUE_LEDGER_CHUNKS:
        raise RunnerFactContractError("venue ledger snapshot exceeds 4096 chunks")
    chunk_count = len(chunk_rows)
    chunks: list[dict[str, Any]] = []
    chunk_digests: list[str] = []
    for index, rows in enumerate(chunk_rows):
        canonical_chunk = {
            "snapshot_id": snapshot,
            "chunk_index": index,
            "chunk_count": chunk_count,
            **rows,
        }
        canonical_bytes = _canonical_json_bytes(canonical_chunk)
        if len(canonical_bytes) > MAX_VENUE_LEDGER_CHUNK_BYTES:
            raise RunnerFactContractError("venue ledger chunk exceeds 262144 canonical bytes")
        chunk_digest = _sha256_hex(canonical_bytes)
        chunk_digests.append(chunk_digest)
        chunks.append(
            {
                "kind": "venue_ledger_snapshot_chunk",
                "event_id": str(runner_fact_event_id("venue_chunk", snapshot, index)),
                **canonical_chunk,
                "chunk_digest": chunk_digest,
            }
        )
    counts = {
        "balances": len(normalized_balances),
        "positions": len(normalized_positions),
        "fills": len(normalized_fills),
        "fees": len(normalized_fees),
    }
    digest_payload = {
        "schema_version": 1,
        "snapshot_id": snapshot,
        "venue": venue_value,
        "source": source_value,
        "watermark": watermark_value,
        "coverage_from": coverage,
        "observed_through": observed,
        "completeness": completeness_value,
        "counts": counts,
        "chunk_count": chunk_count,
        "chunk_digests": chunk_digests,
    }
    manifest = {
        "kind": "venue_ledger_snapshot_manifest",
        "event_id": str(runner_fact_event_id("venue_manifest", snapshot)),
        "snapshot_id": snapshot,
        "venue": venue_value,
        "source": source_value,
        "watermark": watermark_value,
        "coverage_from": coverage,
        "observed_through": observed,
        "completeness": completeness_value,
        "balances_count": counts["balances"],
        "positions_count": counts["positions"],
        "fills_count": counts["fills"],
        "fees_count": counts["fees"],
        "chunk_count": chunk_count,
        "content_digest": _sha256_hex(_canonical_json_bytes(digest_payload)),
    }
    return [manifest, *chunks]


def reconciliation_period_closed(
    *,
    event_id: UUID | str,
    period: str,
    period_started_at: datetime | str,
    closed_at: datetime | str,
    venue_snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    started = _timestamp(period_started_at, "period_started_at")
    closed = _timestamp(closed_at, "closed_at")
    if datetime.fromisoformat(started.replace("Z", "+00:00")) >= datetime.fromisoformat(
        closed.replace("Z", "+00:00")
    ):
        raise RunnerFactContractError("reconciliation period must have positive duration")
    references = [
        {
            "venue": _non_empty(row.get("venue"), "venue_snapshots.venue"),
            "snapshot_id": _uuid(row.get("snapshot_id"), "venue_snapshots.snapshot_id"),
        }
        for row in venue_snapshots
    ]
    references.sort(key=lambda row: (str(row["venue"]), str(row["snapshot_id"])))
    _require_unique(references, "venue", "venue_snapshots")
    _require_unique(references, "snapshot_id", "venue_snapshots")
    if not references:
        raise RunnerFactContractError("venue_snapshots must not be empty")
    return {
        "kind": "reconciliation_period_closed",
        "event_id": _uuid(event_id, "event_id"),
        "period": _non_empty(period, "period"),
        "period_started_at": started,
        "closed_at": closed,
        "venue_snapshots": references,
    }


def _required_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RunnerFactContractError(f"{field} must be a boolean")
    return value


def _require_unique(rows: Sequence[Mapping[str, Any]], field: str, label: str) -> None:
    values = [str(row[field]) for row in rows]
    if len(values) != len(set(values)):
        raise RunnerFactContractError(f"{label} contains duplicate {field}")


@dataclass(frozen=True, slots=True)
class PendingRunnerFactBatch:
    batch_id: UUID
    stream_key: str
    subject: str
    payload: bytes
    attempts: int


@dataclass(frozen=True, slots=True)
class RunnerRuntimeMetricsV1:
    """Operational projection read atomically from the sole RunnerFact store."""

    schema_version: str
    collected_at: str
    database_bytes: int
    wal_bytes: int
    disk_free_bytes: int
    sqlite_quick_check: str
    pending_fact_batches: int
    oldest_pending_fact_age_seconds: float | None
    fact_publish_attempts: int
    desired_deployments: int
    desired_applied_drift: int
    oldest_desired_applied_drift_age_seconds: float | None
    quarantined_deployments: int
    restart_count_total: int
    in_progress_commands: int
    overdue_in_progress_commands: int
    command_outcomes: int
    terminal_command_outcomes: int
    last_command_outcome_age_seconds: float | None
    policy_heads: int
    expired_policy_heads: int
    next_policy_expiry_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommandOutcomeCommitResult:
    outcome_id: str
    outcome: str
    durable_disposition: InboundCommandDisposition
    lifecycle_batch_id: UUID | None
    committed: bool


class RunnerFactOutbox:
    """SQLite-backed sequence allocator, event deduper, and durable outbox."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            schema_table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'runner_state_schema'
                """
            ).fetchone()
            if schema_table is not None:
                version_row = connection.execute(
                    "SELECT schema_version FROM runner_state_schema WHERE singleton = 1"
                ).fetchone()
                if version_row is not None and int(version_row[0]) != RUNNER_STATE_SCHEMA_VERSION:
                    raise RunnerStateMigrationError(
                        "runner state database is not the canonical first-production V1; "
                        "recreate the pre-production database"
                    )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runner_fact_stream (
                    stream_key TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL CHECK (next_sequence > 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_fact_seen_event (
                    event_id TEXT PRIMARY KEY,
                    stream_key TEXT NOT NULL,
                    source_sequence INTEGER NOT NULL CHECK (source_sequence > 0),
                    batch_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runner_fact_seen_event_stream_sequence
                    ON runner_fact_seen_event(stream_key, source_sequence);
                CREATE TABLE IF NOT EXISTS runner_fact_outbox (
                    batch_id TEXT PRIMARY KEY,
                    stream_key TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    source_seq_start INTEGER NOT NULL CHECK (source_seq_start > 0),
                    source_seq_end INTEGER NOT NULL CHECK (source_seq_end >= source_seq_start),
                    payload BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(stream_key, source_seq_start, source_seq_end)
                );
                CREATE INDEX IF NOT EXISTS runner_fact_outbox_delivery_order
                    ON runner_fact_outbox(stream_key, source_seq_start);
                CREATE TABLE IF NOT EXISTS runner_state_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS desired_deployments (
                    deployment_instance_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    trading_mode TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    deployment_spec_id TEXT NOT NULL,
                    deployment_spec_digest TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    command_event_id TEXT NOT NULL,
                    exact_subject TEXT NOT NULL,
                    command_fingerprint TEXT NOT NULL,
                    verified_event_bytes_digest TEXT NOT NULL,
                    signer_key_id TEXT NOT NULL,
                    signature_profile TEXT NOT NULL,
                    verification_receipt TEXT NOT NULL,
                    canonical_command TEXT NOT NULL,
                    exact_event_bytes BLOB NOT NULL,
                    desired_status TEXT NOT NULL,
                    quarantine_reason TEXT,
                    updated_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_activation (
                    activation_id TEXT PRIMARY KEY,
                    deployment_instance_id TEXT NOT NULL,
                    deployment_spec_id TEXT NOT NULL,
                    deployment_spec_digest TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    artifact_identity_digest TEXT NOT NULL,
                    artifact_authority_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('staged', 'active', 'quarantined')
                    ),
                    quarantine_reason TEXT,
                    activated_at_ns INTEGER,
                    updated_at_ns INTEGER NOT NULL,
                    FOREIGN KEY (deployment_instance_id)
                        REFERENCES desired_deployments(deployment_instance_id)
                );
                CREATE TABLE IF NOT EXISTS runner_cap_policy (
                    policy_id TEXT PRIMARY KEY,
                    policy_revision INTEGER NOT NULL CHECK (policy_revision > 0),
                    policy_digest TEXT NOT NULL,
                    tenant_scope TEXT NOT NULL,
                    trading_mode TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    previous_policy_id TEXT,
                    previous_policy_revision INTEGER,
                    previous_policy_digest TEXT,
                    settlement_currency TEXT NOT NULL,
                    max_order_notional TEXT NOT NULL,
                    max_notional TEXT NOT NULL,
                    effective_at_ns INTEGER NOT NULL,
                    expires_at_ns INTEGER NOT NULL,
                    policy_status TEXT NOT NULL,
                    signer_key_id TEXT NOT NULL,
                    signature_profile TEXT NOT NULL,
                    exact_subject TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    verified_event_bytes_digest TEXT NOT NULL,
                    exact_event_bytes BLOB NOT NULL,
                    signed_policy BLOB NOT NULL,
                    policy_json TEXT NOT NULL,
                    consumed_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_cap_policy_head (
                    tenant_scope TEXT NOT NULL,
                    trading_mode TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL CHECK (policy_revision > 0),
                    policy_digest TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    PRIMARY KEY (tenant_scope, trading_mode, runner_id),
                    FOREIGN KEY (policy_id) REFERENCES runner_cap_policy(policy_id)
                );
                CREATE TABLE IF NOT EXISTS applied_deployments (
                    deployment_instance_id TEXT PRIMARY KEY,
                    deployment_spec_id TEXT NOT NULL,
                    deployment_spec_digest TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    command_fingerprint TEXT NOT NULL,
                    engine_handle TEXT,
                    observed_status TEXT NOT NULL,
                    restart_count INTEGER NOT NULL DEFAULT 0 CHECK (restart_count >= 0),
                    quarantine_reason TEXT,
                    artifact_activation_id TEXT,
                    artifact_policy_id TEXT,
                    updated_at_ns INTEGER NOT NULL,
                    FOREIGN KEY (deployment_instance_id)
                        REFERENCES desired_deployments(deployment_instance_id),
                    FOREIGN KEY (artifact_activation_id)
                        REFERENCES artifact_activation(activation_id)
                );
                CREATE TABLE IF NOT EXISTS command_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL,
                    tenant_id TEXT,
                    trading_mode TEXT,
                    runner_id TEXT,
                    deployment_instance_id TEXT,
                    generation INTEGER,
                    command_fingerprint TEXT,
                    exact_subject TEXT NOT NULL,
                    raw_envelope_digest TEXT,
                    outcome TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    durable_disposition TEXT NOT NULL CHECK (
                        durable_disposition IN ('ack', 'term')
                    ),
                    lifecycle_batch_id TEXT,
                    recorded_at_ns INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS command_outcomes_instance_generation
                    ON command_outcomes(
                        deployment_instance_id, generation, command_fingerprint
                    );
                CREATE TABLE IF NOT EXISTS command_in_progress_lease (
                    deployment_instance_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    command_fingerprint TEXT NOT NULL,
                    lease_until_ns INTEGER NOT NULL,
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    last_reason_code TEXT,
                    updated_at_ns INTEGER NOT NULL,
                    FOREIGN KEY (deployment_instance_id)
                        REFERENCES desired_deployments(deployment_instance_id)
                );
                CREATE TABLE IF NOT EXISTS order_reservation (
                    deployment_instance_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    reserved_notional TEXT NOT NULL,
                    filled_exposure TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    PRIMARY KEY (deployment_instance_id, client_order_id),
                    FOREIGN KEY (deployment_instance_id)
                        REFERENCES desired_deployments(deployment_instance_id),
                    FOREIGN KEY (policy_id) REFERENCES runner_cap_policy(policy_id)
                );
                CREATE TABLE IF NOT EXISTS runner_exposure_checkpoint (
                    policy_id TEXT PRIMARY KEY,
                    open_exposure TEXT NOT NULL,
                    reconstructed_at_ns INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    FOREIGN KEY (policy_id) REFERENCES runner_cap_policy(policy_id)
                );
                CREATE TABLE IF NOT EXISTS runner_order_reservation_event (
                    event_id TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL,
                    event_fingerprint TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    deployment_instance_id TEXT,
                    client_order_id TEXT,
                    outcome_json TEXT NOT NULL,
                    recorded_at_ns INTEGER NOT NULL,
                    FOREIGN KEY (policy_id) REFERENCES runner_cap_policy(policy_id)
                );
                CREATE INDEX IF NOT EXISTS runner_order_reservation_event_scope
                    ON runner_order_reservation_event(
                        policy_id, deployment_instance_id, client_order_id, recorded_at_ns
                    );
                """
            )
            activation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(artifact_activation)")
            }
            required_activation_columns = {
                "artifact_identity_digest",
                "artifact_authority_digest",
            }
            if not required_activation_columns.issubset(activation_columns):
                raise RunnerStateMigrationError(
                    "runner state database predates the canonical V1 artifact-source shape; "
                    "recreate the pre-production database"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS runner_cap_policy_scope_revision
                ON runner_cap_policy(tenant_scope, trading_mode, runner_id, policy_revision)
                """
            )
            connection.execute(
                """
                INSERT INTO runner_state_schema (singleton, schema_version, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    updated_at = excluded.updated_at
                """,
                (RUNNER_STATE_SCHEMA_VERSION, _utc_now()),
            )
        os.chmod(self.path, 0o600)

    async def enqueue(
        self,
        authority: RunnerFactAuthority,
        identity: RunnerFactIdentity,
        facts: Sequence[Mapping[str, Any]],
    ) -> UUID | None:
        return await asyncio.to_thread(self._enqueue, authority, identity, facts)

    def enqueue_sync(
        self,
        authority: RunnerFactAuthority,
        identity: RunnerFactIdentity,
        facts: Sequence[Mapping[str, Any]],
    ) -> UUID | None:
        return self._enqueue(authority, identity, facts)

    def _enqueue(
        self,
        authority: RunnerFactAuthority,
        identity: RunnerFactIdentity,
        facts: Sequence[Mapping[str, Any]],
    ) -> UUID | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch_id = self._enqueue_in_transaction(connection, authority, identity, facts)
            connection.commit()
            return batch_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        authority: RunnerFactAuthority,
        identity: RunnerFactIdentity,
        facts: Sequence[Mapping[str, Any]],
    ) -> UUID | None:
        if not facts:
            return None
        if len(facts) > MAX_FACTS_PER_BATCH:
            raise RunnerFactContractError(f"batch exceeds {MAX_FACTS_PER_BATCH} facts")
        candidates: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        for value in facts:
            fact = dict(value)
            if "seq" in fact:
                raise RunnerFactContractError("fact seq is allocated only by RunnerFactOutbox")
            validate_runner_fact_payload(fact)
            kind = _non_empty(fact.get("kind"), "fact.kind")
            if kind not in RUNNER_FACT_KIND_PROJECTORS:
                raise RunnerFactContractError(f"unsupported runner fact kind: {kind}")
            fact["kind"] = kind
            event_id = _uuid(fact.get("event_id"), "event_id")
            if event_id in event_ids:
                raise RunnerFactContractError("batch contains duplicate event_id")
            event_ids.add(event_id)
            fact["event_id"] = event_id
            candidates.append(fact)
        placeholders = ",".join("?" for _ in event_ids)
        seen = {
            row[0]
            for row in connection.execute(
                f"SELECT event_id FROM runner_fact_seen_event WHERE event_id IN ({placeholders})",
                tuple(event_ids),
            )
        }
        candidates = [fact for fact in candidates if fact["event_id"] not in seen]
        if not candidates:
            return None
        row = connection.execute(
            "SELECT next_sequence FROM runner_fact_stream WHERE stream_key = ?",
            (authority.stream_key,),
        ).fetchone()
        source_seq_start = int(row[0]) if row else 1
        sequenced = [
            {**fact, "seq": source_seq_start + offset} for offset, fact in enumerate(candidates)
        ]
        source_seq_end = source_seq_start + len(sequenced) - 1
        batch_id = uuid4()
        emitted_at = _utc_now()
        payload_digest = _sha256_hex(_canonical_json_bytes(sequenced))
        signing_header = runner_fact_signing_header(
            {
                "schema_version": RUNNER_FACT_SCHEMA_VERSION,
                "batch_id": str(batch_id),
                "tenant_id": authority.tenant_id,
                "trading_mode": authority.trading_mode,
                "runner_id": str(authority.runner_id),
                "deployment_instance_id": str(authority.deployment_instance_id),
                "deployment_spec_id": str(authority.deployment_spec_id),
                "deployment_spec_digest": authority.deployment_spec_digest,
                "generation": authority.generation,
                "strategy_id": str(authority.strategy_id),
                "capability_version_id": str(authority.capability_version_id),
                "capability_version": authority.capability_version,
                "capability_manifest_digest": authority.capability_manifest_digest,
                "key_id": identity.key_id,
                "emitted_at": emitted_at,
                "source_seq_start": source_seq_start,
                "source_seq_end": source_seq_end,
                "payload_digest": payload_digest,
            }
        )
        batch = {
            **signing_header,
            "facts": sequenced,
            "signature": identity.sign_batch_payload(_canonical_json_bytes(signing_header)),
        }
        payload = _canonical_json_bytes(batch)
        if len(payload) > MAX_BATCH_BYTES:
            raise RunnerFactContractError(f"batch exceeds {MAX_BATCH_BYTES} bytes")
        connection.execute(
            """
            INSERT INTO runner_fact_outbox (
                batch_id, stream_key, subject, source_seq_start, source_seq_end,
                payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(batch_id),
                authority.stream_key,
                authority.subject,
                source_seq_start,
                source_seq_end,
                payload,
                emitted_at,
            ),
        )
        connection.executemany(
            """
            INSERT INTO runner_fact_seen_event (
                event_id, stream_key, source_sequence, batch_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    fact["event_id"],
                    authority.stream_key,
                    fact["seq"],
                    str(batch_id),
                    emitted_at,
                )
                for fact in sequenced
            ],
        )
        connection.execute(
            """
            INSERT INTO runner_fact_stream (stream_key, next_sequence, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(stream_key) DO UPDATE SET
                next_sequence = excluded.next_sequence,
                updated_at = excluded.updated_at
            """,
            (authority.stream_key, source_seq_end + 1, emitted_at),
        )
        return batch_id

    async def pending(self, limit: int = 64) -> list[PendingRunnerFactBatch]:
        return await asyncio.to_thread(self._pending, limit)

    def _pending(self, limit: int) -> list[PendingRunnerFactBatch]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT batch_id, stream_key, subject, payload, attempts
                FROM runner_fact_outbox
                ORDER BY stream_key, source_seq_start
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            PendingRunnerFactBatch(
                batch_id=UUID(row["batch_id"]),
                stream_key=row["stream_key"],
                subject=row["subject"],
                payload=bytes(row["payload"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    async def runtime_metrics(
        self,
        *,
        now: datetime | None = None,
    ) -> RunnerRuntimeMetricsV1:
        return await asyncio.to_thread(self._runtime_metrics, now)

    def _runtime_metrics(self, now: datetime | None) -> RunnerRuntimeMetricsV1:
        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise ValueError("runtime metrics clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        observed_at_ns = int(observed_at.timestamp() * 1_000_000_000)
        drift_predicate = """
            applied.deployment_instance_id IS NULL
            OR applied.generation != desired.generation
            OR applied.command_fingerprint != desired.command_fingerprint
        """

        with self._connect() as connection:
            connection.execute("BEGIN")
            outbox = connection.execute(
                """
                SELECT
                    COUNT(*) AS pending_count,
                    MIN(created_at) AS oldest_created_at,
                    COALESCE(SUM(attempts), 0) AS publish_attempts
                FROM runner_fact_outbox
                """
            ).fetchone()
            deployments = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS desired_count,
                    COALESCE(SUM(CASE WHEN {drift_predicate} THEN 1 ELSE 0 END), 0)
                        AS drift_count,
                    MIN(CASE WHEN {drift_predicate} THEN desired.updated_at_ns END)
                        AS oldest_drift_at_ns,
                    COALESCE(SUM(CASE
                        WHEN desired.desired_status = 'quarantined'
                          OR applied.observed_status = 'quarantined'
                        THEN 1 ELSE 0 END), 0) AS quarantined_count,
                    COALESCE(SUM(applied.restart_count), 0) AS restart_count
                FROM desired_deployments AS desired
                LEFT JOIN applied_deployments AS applied
                    ON applied.deployment_instance_id = desired.deployment_instance_id
                """
            ).fetchone()
            leases = connection.execute(
                """
                SELECT
                    COUNT(*) AS in_progress_count,
                    COALESCE(SUM(CASE WHEN lease_until_ns <= ? THEN 1 ELSE 0 END), 0)
                        AS overdue_count
                FROM command_in_progress_lease
                """,
                (observed_at_ns,),
            ).fetchone()
            outcomes = connection.execute(
                """
                SELECT
                    COUNT(*) AS outcome_count,
                    COALESCE(SUM(CASE WHEN durable_disposition = 'term' THEN 1 ELSE 0 END), 0)
                        AS terminal_count,
                    MAX(recorded_at_ns) AS latest_recorded_at_ns
                FROM command_outcomes
                """
            ).fetchone()
            policies = connection.execute(
                """
                SELECT
                    COUNT(*) AS head_count,
                    COALESCE(SUM(CASE WHEN policy.expires_at_ns <= ? THEN 1 ELSE 0 END), 0)
                        AS expired_count,
                    MIN(policy.expires_at_ns) AS next_expiry_ns
                FROM runner_cap_policy_head AS head
                JOIN runner_cap_policy AS policy ON policy.policy_id = head.policy_id
                """,
                (observed_at_ns,),
            ).fetchone()
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
            connection.rollback()

        oldest_pending = outbox["oldest_created_at"]
        oldest_pending_age = (
            max(
                0.0,
                (
                    observed_at
                    - datetime.fromisoformat(str(oldest_pending).replace("Z", "+00:00")).astimezone(
                        UTC
                    )
                ).total_seconds(),
            )
            if oldest_pending is not None
            else None
        )

        def age_seconds(value: Any) -> float | None:
            if value is None:
                return None
            return max(0.0, (observed_at_ns - int(value)) / 1_000_000_000)

        next_expiry = policies["next_expiry_ns"]
        return RunnerRuntimeMetricsV1(
            schema_version="alephain.custos.runner-runtime-metrics.v1",
            collected_at=observed_at.isoformat().replace("+00:00", "Z"),
            database_bytes=self.path.stat().st_size,
            wal_bytes=_file_size(self.path.with_name(f"{self.path.name}-wal")),
            disk_free_bytes=shutil.disk_usage(self.path.parent).free,
            sqlite_quick_check=str(quick_check[0]) if quick_check is not None else "missing",
            pending_fact_batches=int(outbox["pending_count"]),
            oldest_pending_fact_age_seconds=oldest_pending_age,
            fact_publish_attempts=int(outbox["publish_attempts"]),
            desired_deployments=int(deployments["desired_count"]),
            desired_applied_drift=int(deployments["drift_count"]),
            oldest_desired_applied_drift_age_seconds=age_seconds(deployments["oldest_drift_at_ns"]),
            quarantined_deployments=int(deployments["quarantined_count"]),
            restart_count_total=int(deployments["restart_count"]),
            in_progress_commands=int(leases["in_progress_count"]),
            overdue_in_progress_commands=int(leases["overdue_count"]),
            command_outcomes=int(outcomes["outcome_count"]),
            terminal_command_outcomes=int(outcomes["terminal_count"]),
            last_command_outcome_age_seconds=age_seconds(outcomes["latest_recorded_at_ns"]),
            policy_heads=int(policies["head_count"]),
            expired_policy_heads=int(policies["expired_count"]),
            next_policy_expiry_seconds=(
                (int(next_expiry) - observed_at_ns) / 1_000_000_000
                if next_expiry is not None
                else None
            ),
        )

    async def acknowledge(self, batch_id: UUID) -> None:
        await asyncio.to_thread(self._acknowledge, batch_id)

    def _acknowledge(self, batch_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM runner_fact_outbox WHERE batch_id = ?", (str(batch_id),)
            )

    async def record_failure(self, batch_id: UUID, error: BaseException) -> None:
        await asyncio.to_thread(self._record_failure, batch_id, type(error).__name__)

    def _record_failure(self, batch_id: UUID, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runner_fact_outbox
                SET attempts = attempts + 1, last_error = ?
                WHERE batch_id = ?
                """,
                (error[:2048], str(batch_id)),
            )


@dataclass(frozen=True, slots=True)
class DurableDesiredCommand:
    command: Any
    command_fingerprint: str
    exact_subject: str
    verification_receipt: CommandVerificationReceipt


@dataclass(frozen=True, slots=True)
class DurableDesiredCommandIdentity:
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str
    generation: int
    trading_mode: str
    strategy_id: UUID


@dataclass(frozen=True, slots=True)
class EngineLifecycleDurableState:
    desired_status: str
    applied_generation: int | None
    applied_command_fingerprint: str | None
    engine_handle: str | None
    observed_status: str | None
    restart_count: int
    quarantine_reason: str | None


class RunnerPolicyIdentityDecision(StrEnum):
    NEWER = "newer"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True, slots=True)
class RunnerPolicyCommitResult:
    decision: RunnerPolicyIdentityDecision
    committed: bool
    policy_id: UUID
    policy_digest: str


@dataclass(frozen=True, slots=True)
class DurableRunnerSafetyPolicy:
    policy: RunnerAggregateCapPolicyV1
    exact_subject: str
    exact_event_bytes: bytes
    exact_signed_envelope_bytes: bytes
    signature_key_id: str
    fingerprint: str
    verified_event_bytes_sha256: str


class RunnerStateStore:
    """Command state adapter sharing the one RunnerFact SQLite connection.

    No path or connection factory exists outside ``RunnerFactOutbox``.  Engine
    composition is intentionally deferred; callers provide the already-bound
    RunnerFact authority resolver when constructing this adapter.
    """

    def __init__(
        self,
        *,
        outbox: RunnerFactOutbox,
        identity: RunnerFactIdentity,
        tenant_id: str,
        runner_id: UUID,
        authority_resolver: Callable[[VerifiedRunnerCommand], RunnerFactAuthority],
    ) -> None:
        self._outbox = outbox
        self._identity = identity
        self._tenant_id = _non_empty(tenant_id, "tenant_id")
        self._runner_id = UUID(_uuid(runner_id, "runner_id"))
        self._authority_resolver = authority_resolver

    @property
    def database_path(self) -> Path:
        return self._outbox.path

    async def record_desired_command(
        self,
        *,
        command: Any,
        command_fingerprint: str,
        verification_receipt: CommandVerificationReceipt,
    ) -> DesiredCommandRecord:
        return await asyncio.to_thread(
            self._record_desired_command,
            command,
            command_fingerprint,
            verification_receipt,
        )

    def _record_desired_command(
        self,
        command: Any,
        command_fingerprint: str,
        verification_receipt: CommandVerificationReceipt,
    ) -> DesiredCommandRecord:
        material = self._validated_command_material(
            command,
            command_fingerprint,
            verification_receipt,
        )
        connection = self._outbox._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if row is not None:
                self._require_desired_authority(row, command)
            decision = classify_command_identity(
                current_generation=int(row["generation"]) if row else None,
                current_fingerprint=str(row["command_fingerprint"]) if row else None,
                incoming_generation=command.generation,
                incoming_fingerprint=command_fingerprint,
            )
            if decision is CommandIdentityDecision.NEWER:
                connection.execute(
                    """
                    INSERT INTO desired_deployments (
                        deployment_instance_id, tenant_id, trading_mode, runner_id,
                        deployment_spec_id, deployment_spec_digest, generation,
                        command_event_id, exact_subject, command_fingerprint,
                        verified_event_bytes_digest, signer_key_id, signature_profile,
                        verification_receipt, canonical_command, exact_event_bytes,
                        desired_status, quarantine_reason, updated_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'recorded', NULL, ?)
                    ON CONFLICT(deployment_instance_id) DO UPDATE SET
                        tenant_id = excluded.tenant_id,
                        trading_mode = excluded.trading_mode,
                        runner_id = excluded.runner_id,
                        deployment_spec_id = excluded.deployment_spec_id,
                        deployment_spec_digest = excluded.deployment_spec_digest,
                        generation = excluded.generation,
                        command_event_id = excluded.command_event_id,
                        exact_subject = excluded.exact_subject,
                        command_fingerprint = excluded.command_fingerprint,
                        verified_event_bytes_digest = excluded.verified_event_bytes_digest,
                        signer_key_id = excluded.signer_key_id,
                        signature_profile = excluded.signature_profile,
                        verification_receipt = excluded.verification_receipt,
                        canonical_command = excluded.canonical_command,
                        exact_event_bytes = excluded.exact_event_bytes,
                        desired_status = 'recorded',
                        quarantine_reason = NULL,
                        updated_at_ns = excluded.updated_at_ns
                    """,
                    (
                        str(command.deployment_instance_id),
                        command.tenant_id,
                        command.trading_mode,
                        str(command.runner_id),
                        str(command.deployment_spec_id),
                        command.deployment_spec_digest,
                        command.generation,
                        material["event_id"],
                        command.verified_subject,
                        command_fingerprint,
                        verification_receipt.verified_event_bytes_sha256,
                        verification_receipt.signature_key_id,
                        verification_receipt.signature_profile,
                        material["verification_receipt"],
                        material["canonical_command"],
                        command.exact_signed_event_bytes,
                        time.time_ns(),
                    ),
                )
            replay_disposition = InboundCommandDisposition.NONE
            if decision is CommandIdentityDecision.IDEMPOTENT:
                outcome = connection.execute(
                    """
                    SELECT durable_disposition FROM command_outcomes
                    WHERE deployment_instance_id = ? AND generation = ?
                      AND command_fingerprint = ?
                    ORDER BY recorded_at_ns DESC LIMIT 1
                    """,
                    (
                        str(command.deployment_instance_id),
                        command.generation,
                        command_fingerprint,
                    ),
                ).fetchone()
                if outcome is not None:
                    replay_disposition = InboundCommandDisposition(outcome["durable_disposition"])
            connection.commit()
            return DesiredCommandRecord(
                deployment_instance_id=command.deployment_instance_id,
                generation=command.generation,
                command_fingerprint=command_fingerprint,
                decision=decision,
                committed=True,
                replay_disposition=replay_disposition,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def commit_untrusted_command_rejection(
        self,
        *,
        delivery_id: str,
        exact_subject: str,
        raw_envelope_digest: str,
        reason_code: Literal["invalid_signature", "invalid_schema", "unsupported_version"],
    ) -> DurableCommandOutcome:
        return await asyncio.to_thread(
            self._commit_untrusted_command_rejection,
            delivery_id,
            exact_subject,
            raw_envelope_digest,
            reason_code,
        )

    def _commit_untrusted_command_rejection(
        self,
        delivery_id: str,
        exact_subject: str,
        raw_envelope_digest: str,
        reason_code: str,
    ) -> DurableCommandOutcome:
        delivery = _non_empty(delivery_id, "delivery_id")
        subject = _non_empty(exact_subject, "exact_subject")
        _state_digest(raw_envelope_digest, "raw_envelope_digest")
        if reason_code not in {
            "invalid_signature",
            "invalid_schema",
            "unsupported_version",
        }:
            raise RunnerStateDurabilityError("untrusted rejection reason code is invalid")
        outcome_id = str(
            runner_fact_event_id(
                "untrusted_command_rejection",
                self._tenant_id,
                self._runner_id,
                subject,
                raw_envelope_digest,
                reason_code,
            )
        )
        with self._outbox._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO command_outcomes (
                    outcome_id, delivery_id, tenant_id, runner_id, exact_subject,
                    raw_envelope_digest, outcome, reason_code,
                    durable_disposition, recorded_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, 'untrusted_rejection', ?, 'term', ?)
                """,
                (
                    outcome_id,
                    delivery,
                    self._tenant_id,
                    str(self._runner_id),
                    subject,
                    raw_envelope_digest,
                    reason_code,
                    time.time_ns(),
                ),
            )
        return DurableCommandOutcome(
            outcome_id=outcome_id,
            outcome=CommandTerminalOutcome.UNTRUSTED_REJECTION,
            durable_disposition=InboundCommandDisposition.TERM,
            committed=cursor.rowcount == 1,
        )

    async def commit_verified_terminal_outcome(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        outcome: Literal["conflict", "stale", "retry_exhausted"],
        reason_code: str,
    ) -> DurableCommandOutcome:
        result = await self.commit_verified_command_outcome_and_enqueue_fact(
            delivery_id=delivery_id,
            verified=verified,
            outcome=outcome,
            reason_code=reason_code,
            engine_handle=None,
            observed_status="quarantined" if outcome != "stale" else "stale_rejected",
            lifecycle_state="stopped",
        )
        return DurableCommandOutcome(
            outcome_id=result.outcome_id,
            outcome=CommandTerminalOutcome(outcome),
            durable_disposition=result.durable_disposition,
            committed=result.committed,
        )

    async def commit_applied_and_enqueue_lifecycle(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        engine_handle: str | None,
        observed_status: str,
        artifact_activation_id: str | None = None,
        artifact_policy_id: str | None = None,
    ) -> CommandOutcomeCommitResult:
        return await self.commit_verified_command_outcome_and_enqueue_fact(
            delivery_id=delivery_id,
            verified=verified,
            outcome="applied",
            reason_code="applied",
            engine_handle=engine_handle,
            observed_status=observed_status,
            lifecycle_state=verified.command.lifecycle_state,
            artifact_activation_id=artifact_activation_id,
            artifact_policy_id=artifact_policy_id,
        )

    async def commit_recovered_engine_ready(
        self,
        *,
        verified: VerifiedRunnerCommand,
        engine_handle: str,
        observed_status: str,
        artifact_activation_id: str | None = None,
        artifact_policy_id: str | None = None,
    ) -> None:
        """Persist a rebuilt process-local engine without duplicating applied facts."""

        await asyncio.to_thread(
            self._commit_recovered_engine_ready,
            verified,
            engine_handle,
            observed_status,
            artifact_activation_id,
            artifact_policy_id,
        )

    def _commit_recovered_engine_ready(
        self,
        verified: VerifiedRunnerCommand,
        engine_handle: str,
        observed_status: str,
        artifact_activation_id: str | None,
        artifact_policy_id: str | None,
    ) -> None:
        handle = _non_empty(engine_handle, "engine_handle")
        observed = _non_empty(observed_status, "observed_status")
        command = verified.command
        self._validated_command_material(
            command,
            verified.command_fingerprint,
            verified.verification_receipt,
        )
        connection = self._outbox._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            applied = connection.execute(
                "SELECT * FROM applied_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None or applied is None:
                raise RunnerStateDurabilityError(
                    "engine recovery requires durable desired and applied state"
                )
            self._require_desired_authority(desired, command)
            if (
                desired["desired_status"] != "applied"
                or int(applied["generation"]) != command.generation
                or applied["command_fingerprint"] != verified.command_fingerprint
            ):
                raise RunnerStateDurabilityError(
                    "engine recovery differs from current applied authority"
                )
            if artifact_activation_id is not None:
                activation = connection.execute(
                    """
                    SELECT 1 FROM artifact_activation
                    WHERE activation_id = ? AND deployment_instance_id = ?
                      AND generation = ? AND state = 'active'
                    """,
                    (
                        artifact_activation_id,
                        str(command.deployment_instance_id),
                        command.generation,
                    ),
                ).fetchone()
                if activation is None:
                    raise RunnerStateDurabilityError(
                        "recovered engine artifact activation is absent or quarantined"
                    )
            lease = connection.execute(
                """
                SELECT restart_count FROM command_in_progress_lease
                WHERE deployment_instance_id = ? AND generation = ?
                  AND command_fingerprint = ?
                """,
                (
                    str(command.deployment_instance_id),
                    command.generation,
                    verified.command_fingerprint,
                ),
            ).fetchone()
            restart_count = max(
                int(applied["restart_count"]),
                int(lease["restart_count"]) if lease is not None else 0,
            )
            connection.execute(
                """
                UPDATE applied_deployments
                SET engine_handle = ?, observed_status = ?, restart_count = ?,
                    quarantine_reason = NULL, artifact_activation_id = ?,
                    artifact_policy_id = ?, updated_at_ns = ?
                WHERE deployment_instance_id = ?
                """,
                (
                    handle,
                    observed,
                    restart_count,
                    artifact_activation_id,
                    artifact_policy_id,
                    time.time_ns(),
                    str(command.deployment_instance_id),
                ),
            )
            connection.execute(
                "DELETE FROM command_in_progress_lease WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def commit_verified_command_outcome_and_enqueue_fact(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        outcome: Literal["applied", "conflict", "stale", "retry_exhausted"],
        reason_code: str,
        engine_handle: str | None,
        observed_status: str,
        lifecycle_state: str,
        artifact_activation_id: str | None = None,
        artifact_policy_id: str | None = None,
    ) -> CommandOutcomeCommitResult:
        authority = self._authority_for_verified(verified)
        lifecycle_fact = _command_lifecycle_fact(
            authority=authority,
            command_fingerprint=verified.command_fingerprint,
            outcome=outcome,
            lifecycle_state=lifecycle_state,
        )
        return await asyncio.to_thread(
            self._commit_verified_command_outcome_and_enqueue_fact,
            delivery_id,
            verified,
            authority,
            outcome,
            reason_code,
            engine_handle,
            observed_status,
            lifecycle_fact,
            artifact_activation_id,
            artifact_policy_id,
        )

    def _commit_verified_command_outcome_and_enqueue_fact(
        self,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        authority: RunnerFactAuthority,
        outcome: str,
        reason_code: str,
        engine_handle: str | None,
        observed_status: str,
        lifecycle_fact: Mapping[str, Any],
        artifact_activation_id: str | None,
        artifact_policy_id: str | None,
    ) -> CommandOutcomeCommitResult:
        if outcome not in {"applied", "conflict", "stale", "retry_exhausted"}:
            raise RunnerStateDurabilityError("verified command outcome is invalid")
        delivery = _non_empty(delivery_id, "delivery_id")
        reason = _non_empty(reason_code, "reason_code")
        observed = _non_empty(observed_status, "observed_status")
        command = verified.command
        self._validated_command_material(
            command,
            verified.command_fingerprint,
            verified.verification_receipt,
        )
        expected_event_id = str(
            command_lifecycle_event_id(
                tenant_id=command.tenant_id,
                trading_mode=command.trading_mode,
                runner_id=command.runner_id,
                deployment_instance_id=command.deployment_instance_id,
                deployment_spec_id=command.deployment_spec_id,
                deployment_spec_digest=command.deployment_spec_digest,
                generation=command.generation,
                lifecycle_state=str(lifecycle_fact.get("lifecycle_state") or ""),
                command_fingerprint=verified.command_fingerprint,
                outcome=outcome,
            )
        )
        if lifecycle_fact.get("event_id") != expected_event_id:
            raise RunnerStateDurabilityError("lifecycle event id is not deterministic")
        disposition = (
            InboundCommandDisposition.ACK
            if outcome == "applied"
            else InboundCommandDisposition.TERM
        )
        outcome_id = str(
            runner_fact_event_id(
                "command_outcome",
                command.deployment_instance_id,
                command.deployment_spec_id,
                command.generation,
                verified.command_fingerprint,
                outcome,
                reason,
            )
        )
        connection = self._outbox._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM command_outcomes WHERE outcome_id = ?",
                (outcome_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return CommandOutcomeCommitResult(
                    outcome_id=outcome_id,
                    outcome=outcome,
                    durable_disposition=InboundCommandDisposition(existing["durable_disposition"]),
                    lifecycle_batch_id=(
                        UUID(existing["lifecycle_batch_id"])
                        if existing["lifecycle_batch_id"]
                        else None
                    ),
                    committed=False,
                )
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None:
                raise RunnerStateDurabilityError(
                    "verified outcome requires a durable desired command"
                )
            self._require_desired_authority(desired, command)
            if outcome in {"applied", "retry_exhausted"} and (
                int(desired["generation"]) != command.generation
                or desired["command_fingerprint"] != verified.command_fingerprint
            ):
                raise RunnerStateDurabilityError(
                    "verified outcome differs from the current desired command"
                )
            if outcome == "conflict" and (
                int(desired["generation"]) != command.generation
                or desired["command_fingerprint"] == verified.command_fingerprint
            ):
                raise RunnerStateDurabilityError("conflict outcome lacks a durable conflict")
            if outcome == "stale" and int(desired["generation"]) <= command.generation:
                raise RunnerStateDurabilityError("stale outcome is not older than desired state")
            lease = connection.execute(
                """
                SELECT restart_count FROM command_in_progress_lease
                WHERE deployment_instance_id = ? AND generation = ?
                  AND command_fingerprint = ?
                """,
                (
                    str(command.deployment_instance_id),
                    command.generation,
                    verified.command_fingerprint,
                ),
            ).fetchone()
            restart_count = int(lease["restart_count"]) if lease is not None else 0
            if artifact_activation_id is not None:
                activation = connection.execute(
                    """
                    SELECT * FROM artifact_activation
                    WHERE activation_id = ? AND deployment_instance_id = ?
                      AND generation = ? AND state = 'active'
                    """,
                    (
                        artifact_activation_id,
                        str(command.deployment_instance_id),
                        command.generation,
                    ),
                ).fetchone()
                if activation is None:
                    raise RunnerStateDurabilityError(
                        "applied command artifact activation is absent or quarantined"
                    )
            if outcome == "applied":
                connection.execute(
                    """
                    INSERT INTO applied_deployments (
                        deployment_instance_id, deployment_spec_id,
                        deployment_spec_digest, generation, command_fingerprint,
                        engine_handle, observed_status, restart_count,
                        quarantine_reason, artifact_activation_id, artifact_policy_id,
                        updated_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(deployment_instance_id) DO UPDATE SET
                        deployment_spec_id = excluded.deployment_spec_id,
                        deployment_spec_digest = excluded.deployment_spec_digest,
                        generation = excluded.generation,
                        command_fingerprint = excluded.command_fingerprint,
                        engine_handle = excluded.engine_handle,
                        observed_status = excluded.observed_status,
                        restart_count = excluded.restart_count,
                        quarantine_reason = NULL,
                        artifact_activation_id = COALESCE(
                            excluded.artifact_activation_id,
                            applied_deployments.artifact_activation_id
                        ),
                        artifact_policy_id = COALESCE(
                            excluded.artifact_policy_id,
                            applied_deployments.artifact_policy_id
                        ),
                        updated_at_ns = excluded.updated_at_ns
                    """,
                    (
                        str(command.deployment_instance_id),
                        str(command.deployment_spec_id),
                        command.deployment_spec_digest,
                        command.generation,
                        verified.command_fingerprint,
                        engine_handle,
                        observed,
                        restart_count,
                        artifact_activation_id,
                        artifact_policy_id,
                        time.time_ns(),
                    ),
                )
            elif outcome in {"conflict", "retry_exhausted"}:
                connection.execute(
                    """
                    UPDATE desired_deployments
                    SET desired_status = 'quarantined', quarantine_reason = ?,
                        updated_at_ns = ?
                    WHERE deployment_instance_id = ?
                    """,
                    (reason, time.time_ns(), str(command.deployment_instance_id)),
                )
                connection.execute(
                    """
                    UPDATE applied_deployments
                    SET observed_status = 'quarantined', quarantine_reason = ?,
                        updated_at_ns = ?
                    WHERE deployment_instance_id = ?
                    """,
                    (reason, time.time_ns(), str(command.deployment_instance_id)),
                )
            lifecycle_batch_id = self._outbox._enqueue_in_transaction(
                connection,
                authority,
                self._identity,
                (lifecycle_fact,),
            )
            connection.execute(
                """
                INSERT INTO command_outcomes (
                    outcome_id, delivery_id, tenant_id, trading_mode, runner_id,
                    deployment_instance_id, generation, command_fingerprint,
                    exact_subject, outcome, reason_code, durable_disposition,
                    lifecycle_batch_id, recorded_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    delivery,
                    command.tenant_id,
                    command.trading_mode,
                    str(command.runner_id),
                    str(command.deployment_instance_id),
                    command.generation,
                    verified.command_fingerprint,
                    command.verified_subject,
                    outcome,
                    reason,
                    disposition.value,
                    str(lifecycle_batch_id) if lifecycle_batch_id else None,
                    time.time_ns(),
                ),
            )
            if outcome == "applied":
                connection.execute(
                    """
                    UPDATE desired_deployments
                    SET desired_status = 'applied', updated_at_ns = ?
                    WHERE deployment_instance_id = ?
                    """,
                    (time.time_ns(), str(command.deployment_instance_id)),
                )
            connection.execute(
                "DELETE FROM command_in_progress_lease WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            )
            connection.commit()
            return CommandOutcomeCommitResult(
                outcome_id=outcome_id,
                outcome=outcome,
                durable_disposition=disposition,
                lifecycle_batch_id=lifecycle_batch_id,
                committed=True,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def record_in_progress_lease(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        lease_until_ns: int,
    ) -> None:
        await asyncio.to_thread(
            self._record_in_progress_lease,
            delivery_id,
            verified,
            lease_until_ns,
        )

    def _record_in_progress_lease(
        self,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        lease_until_ns: int,
    ) -> None:
        if type(lease_until_ns) is not int or lease_until_ns <= time.time_ns():
            raise RunnerStateDurabilityError("in-progress lease must expire in the future")
        with self._outbox._connect() as connection:
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(verified.command.deployment_instance_id),),
            ).fetchone()
            if desired is None or (
                int(desired["generation"]) != verified.command.generation
                or desired["command_fingerprint"] != verified.command_fingerprint
            ):
                raise RunnerStateDurabilityError(
                    "in-progress lease requires the current durable desired command"
                )
            connection.execute(
                """
                INSERT INTO command_in_progress_lease (
                    deployment_instance_id, delivery_id, generation,
                    command_fingerprint, lease_until_ns, restart_count,
                    last_reason_code, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                ON CONFLICT(deployment_instance_id) DO UPDATE SET
                    delivery_id = excluded.delivery_id,
                    restart_count = CASE
                        WHEN command_in_progress_lease.generation != excluded.generation
                          OR command_in_progress_lease.command_fingerprint
                             != excluded.command_fingerprint
                        THEN 0 ELSE command_in_progress_lease.restart_count END,
                    last_reason_code = CASE
                        WHEN command_in_progress_lease.generation != excluded.generation
                          OR command_in_progress_lease.command_fingerprint
                             != excluded.command_fingerprint
                        THEN NULL ELSE command_in_progress_lease.last_reason_code END,
                    generation = excluded.generation,
                    command_fingerprint = excluded.command_fingerprint,
                    lease_until_ns = excluded.lease_until_ns,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (
                    str(verified.command.deployment_instance_id),
                    _non_empty(delivery_id, "delivery_id"),
                    verified.command.generation,
                    verified.command_fingerprint,
                    lease_until_ns,
                    time.time_ns(),
                ),
            )

    async def load_engine_lifecycle_state(
        self, verified: VerifiedRunnerCommand
    ) -> EngineLifecycleDurableState:
        return await asyncio.to_thread(self._load_engine_lifecycle_state, verified)

    def _load_engine_lifecycle_state(
        self, verified: VerifiedRunnerCommand
    ) -> EngineLifecycleDurableState:
        self._validated_command_material(
            verified.command,
            verified.command_fingerprint,
            verified.verification_receipt,
        )
        command = verified.command
        with self._outbox._connect() as connection:
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None:
                raise RunnerStateDurabilityError(
                    "engine lifecycle requires a durable desired command"
                )
            self._require_desired_authority(desired, command)
            applied = connection.execute(
                "SELECT * FROM applied_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM command_in_progress_lease WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
        applied_matches = applied is not None and (
            int(applied["generation"]) == command.generation
            and applied["command_fingerprint"] == verified.command_fingerprint
        )
        lease_matches = lease is not None and (
            int(lease["generation"]) == command.generation
            and lease["command_fingerprint"] == verified.command_fingerprint
        )
        restart_count = 0
        if applied_matches:
            restart_count = int(applied["restart_count"])
        if lease_matches:
            restart_count = max(restart_count, int(lease["restart_count"]))
        return EngineLifecycleDurableState(
            desired_status=str(desired["desired_status"]),
            applied_generation=int(applied["generation"]) if applied_matches else None,
            applied_command_fingerprint=(
                str(applied["command_fingerprint"]) if applied_matches else None
            ),
            engine_handle=(
                str(applied["engine_handle"])
                if applied_matches and applied["engine_handle"] is not None
                else None
            ),
            observed_status=(str(applied["observed_status"]) if applied_matches else None),
            restart_count=restart_count,
            quarantine_reason=(
                str(desired["quarantine_reason"])
                if desired["quarantine_reason"] is not None
                else (
                    str(applied["quarantine_reason"])
                    if applied_matches and applied["quarantine_reason"] is not None
                    else None
                )
            ),
        )

    async def record_engine_restart(
        self,
        *,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        reason_code: str,
        lease_until_ns: int,
    ) -> int:
        return await asyncio.to_thread(
            self._record_engine_restart,
            delivery_id,
            verified,
            reason_code,
            lease_until_ns,
        )

    def _record_engine_restart(
        self,
        delivery_id: str,
        verified: VerifiedRunnerCommand,
        reason_code: str,
        lease_until_ns: int,
    ) -> int:
        if type(lease_until_ns) is not int or lease_until_ns <= time.time_ns():
            raise RunnerStateDurabilityError("engine restart lease must expire in the future")
        delivery = _non_empty(delivery_id, "delivery_id")
        reason = _non_empty(reason_code, "reason_code")
        command = verified.command
        connection = self._outbox._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None:
                raise RunnerStateDurabilityError(
                    "engine restart requires a durable desired command"
                )
            self._require_desired_authority(desired, command)
            if desired["command_fingerprint"] != verified.command_fingerprint:
                raise RunnerStateDurabilityError(
                    "engine restart differs from the durable command fingerprint"
                )
            connection.execute(
                """
                INSERT INTO command_in_progress_lease (
                    deployment_instance_id, delivery_id, generation,
                    command_fingerprint, lease_until_ns, restart_count,
                    last_reason_code, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(deployment_instance_id) DO UPDATE SET
                    delivery_id = excluded.delivery_id,
                    generation = excluded.generation,
                    command_fingerprint = excluded.command_fingerprint,
                    lease_until_ns = excluded.lease_until_ns,
                    restart_count = CASE
                        WHEN command_in_progress_lease.generation = excluded.generation
                          AND command_in_progress_lease.command_fingerprint
                              = excluded.command_fingerprint
                        THEN command_in_progress_lease.restart_count + 1 ELSE 1 END,
                    last_reason_code = excluded.last_reason_code,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (
                    str(command.deployment_instance_id),
                    delivery,
                    command.generation,
                    verified.command_fingerprint,
                    lease_until_ns,
                    reason,
                    time.time_ns(),
                ),
            )
            row = connection.execute(
                """
                SELECT restart_count FROM command_in_progress_lease
                WHERE deployment_instance_id = ?
                """,
                (str(command.deployment_instance_id),),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RunnerStateDurabilityError("engine restart counter was not persisted")
            return int(row["restart_count"])
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def record_artifact_activation(
        self,
        *,
        verified: VerifiedRunnerCommand,
        activation_id: str,
        artifact_identity_digest: str,
        artifact_authority_digest: str,
    ) -> None:
        await asyncio.to_thread(
            self._record_artifact_activation,
            verified,
            activation_id,
            artifact_identity_digest,
            artifact_authority_digest,
        )

    def _record_artifact_activation(
        self,
        verified: VerifiedRunnerCommand,
        activation_id: str,
        artifact_identity_digest: str,
        artifact_authority_digest: str,
    ) -> None:
        _state_digest(artifact_identity_digest, "artifact_identity_digest")
        _state_digest(artifact_authority_digest, "artifact_authority_digest")
        command = verified.command
        with self._outbox._connect() as connection:
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None or desired["command_fingerprint"] != verified.command_fingerprint:
                raise RunnerStateDurabilityError(
                    "artifact activation requires the current desired command"
                )
            connection.execute(
                """
                INSERT INTO artifact_activation (
                    activation_id, deployment_instance_id, deployment_spec_id,
                    deployment_spec_digest, generation, artifact_identity_digest,
                    artifact_authority_digest, state, activated_at_ns, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(activation_id) DO UPDATE SET
                    state = 'active', quarantine_reason = NULL,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (
                    _non_empty(activation_id, "activation_id"),
                    str(command.deployment_instance_id),
                    str(command.deployment_spec_id),
                    command.deployment_spec_digest,
                    command.generation,
                    artifact_identity_digest,
                    artifact_authority_digest,
                    time.time_ns(),
                    time.time_ns(),
                ),
            )

    async def record_verified_runner_safety_policy(
        self, verified: VerifiedRunnerSafetyPolicy
    ) -> RunnerPolicyCommitResult:
        """Advance one verified runner-safety policy scope head or reject the policy fail closed."""

        return await asyncio.to_thread(self._record_verified_runner_safety_policy, verified)

    def _record_verified_runner_safety_policy(
        self, verified: VerifiedRunnerSafetyPolicy
    ) -> RunnerPolicyCommitResult:
        if not isinstance(verified, VerifiedRunnerSafetyPolicy):
            raise RunnerStateAuthorityError("runner policy must be signature verified")
        policy = verified.policy
        if policy.tenant_id != self._tenant_id:
            raise RunnerStateAuthorityError("runner policy tenant differs from store authority")
        if policy.runner_id != self._runner_id:
            raise RunnerStateAuthorityError("runner policy runner differs from store authority")

        connection = self._outbox._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                """
                SELECT * FROM runner_cap_policy_head
                WHERE tenant_scope = ? AND trading_mode = ? AND runner_id = ?
                """,
                (self._tenant_id, policy.trading_mode, str(self._runner_id)),
            ).fetchone()
            if head is not None:
                head_revision = int(head["policy_revision"])
                head_digest = str(head["policy_digest"])
                if policy.revision < head_revision:
                    raise RunnerStateAuthorityError("runner policy revision is stale")
                if policy.revision == head_revision:
                    if policy.policy_digest == head_digest:
                        connection.rollback()
                        return RunnerPolicyCommitResult(
                            decision=RunnerPolicyIdentityDecision.IDEMPOTENT,
                            committed=False,
                            policy_id=policy.policy_id,
                            policy_digest=policy.policy_digest,
                        )
                    raise RunnerStateAuthorityError("runner policy revision conflict")
                previous = policy.previous
                if (
                    policy.revision != head_revision + 1
                    or previous is None
                    or str(previous.policy_id) != head["policy_id"]
                    or previous.revision != head_revision
                    or previous.policy_digest != head_digest
                ):
                    raise RunnerStateAuthorityError("runner policy prior fence differs")
            elif policy.revision != 1 or policy.previous is not None:
                raise RunnerStateAuthorityError("runner policy initial fence differs")

            existing_id = connection.execute(
                "SELECT policy_digest FROM runner_cap_policy WHERE policy_id = ?",
                (str(policy.policy_id),),
            ).fetchone()
            if existing_id is not None:
                raise RunnerStateAuthorityError("runner policy id conflict")

            previous = policy.previous
            effective_at_ns = int(policy.effective_at.timestamp() * 1_000_000_000)
            expires_at_ns = int(policy.expires_at.timestamp() * 1_000_000_000)
            consumed_at_ns = time.time_ns()
            connection.execute(
                """
                INSERT INTO runner_cap_policy (
                    policy_id, policy_revision, policy_digest,
                    tenant_scope, trading_mode, runner_id, previous_policy_id,
                    previous_policy_revision, previous_policy_digest, settlement_currency,
                    max_order_notional, max_notional, effective_at_ns,
                    expires_at_ns, policy_status, signer_key_id,
                    signature_profile, exact_subject, fingerprint,
                    verified_event_bytes_digest, exact_event_bytes, signed_policy,
                    policy_json, consumed_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(policy.policy_id),
                    policy.revision,
                    policy.policy_digest,
                    self._tenant_id,
                    policy.trading_mode,
                    str(self._runner_id),
                    str(previous.policy_id) if previous is not None else None,
                    previous.revision if previous is not None else None,
                    previous.policy_digest if previous is not None else None,
                    policy.settlement_currency,
                    policy.max_order_notional,
                    policy.max_total_notional,
                    effective_at_ns,
                    expires_at_ns,
                    policy.status,
                    verified.signature_key_id,
                    "crucible-domain-event-v1-exact-bytes",
                    verified.exact_subject,
                    verified.fingerprint,
                    verified.verified_event_bytes_sha256,
                    verified.exact_event_bytes,
                    verified.exact_signed_envelope_bytes,
                    policy.model_dump_json(),
                    consumed_at_ns,
                ),
            )
            connection.execute(
                """
                INSERT INTO runner_cap_policy_head (
                    tenant_scope, trading_mode, runner_id, policy_id,
                    policy_revision, policy_digest, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_scope, trading_mode, runner_id) DO UPDATE SET
                    policy_id = excluded.policy_id,
                    policy_revision = excluded.policy_revision,
                    policy_digest = excluded.policy_digest,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (
                    self._tenant_id,
                    policy.trading_mode,
                    str(self._runner_id),
                    str(policy.policy_id),
                    policy.revision,
                    policy.policy_digest,
                    consumed_at_ns,
                ),
            )
            connection.commit()
            return RunnerPolicyCommitResult(
                decision=RunnerPolicyIdentityDecision.NEWER,
                committed=True,
                policy_id=policy.policy_id,
                policy_digest=policy.policy_digest,
            )
        except RunnerFactError:
            connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise RunnerStateDurabilityError(
                f"verified runner policy transaction failed: {type(exc).__name__}"
            ) from exc
        finally:
            connection.close()

    async def load_effective_runner_safety_policy(
        self, trading_mode: str, *, now: datetime | None = None
    ) -> DurableRunnerSafetyPolicy:
        return await asyncio.to_thread(
            self._load_effective_runner_safety_policy,
            trading_mode,
            now or datetime.now(UTC),
        )

    def _load_effective_runner_safety_policy(
        self, trading_mode: str, now: datetime
    ) -> DurableRunnerSafetyPolicy:
        if trading_mode not in {"sandbox", "testnet", "live"}:
            raise RunnerStateAuthorityError("runner policy trading mode is invalid")
        if now.tzinfo is None:
            raise RunnerStateAuthorityError("runner policy evaluation time must be timezone aware")
        with self._outbox._connect() as connection:
            row = connection.execute(
                """
                SELECT policy.* FROM runner_cap_policy_head AS head
                JOIN runner_cap_policy AS policy ON policy.policy_id = head.policy_id
                WHERE head.tenant_scope = ? AND head.trading_mode = ? AND head.runner_id = ?
                """,
                (self._tenant_id, trading_mode, str(self._runner_id)),
            ).fetchone()
        if row is None:
            raise RunnerStateAuthorityError("verified runner policy is missing")
        policy = RunnerAggregateCapPolicyV1.model_validate_json(str(row["policy_json"]))
        if policy.status != "active":
            raise RunnerStateAuthorityError("verified runner policy is not active")
        if now < policy.effective_at:
            raise RunnerStateAuthorityError("verified runner policy is not effective")
        if now >= policy.expires_at:
            raise RunnerStateAuthorityError("verified runner policy is expired")
        exact_event_bytes = bytes(row["exact_event_bytes"])
        if _sha256_hex(exact_event_bytes) != row["verified_event_bytes_digest"]:
            raise RunnerStateDurabilityError("verified runner policy event bytes changed on disk")
        return DurableRunnerSafetyPolicy(
            policy=policy,
            exact_subject=str(row["exact_subject"]),
            exact_event_bytes=exact_event_bytes,
            exact_signed_envelope_bytes=bytes(row["signed_policy"]),
            signature_key_id=str(row["signer_key_id"]),
            fingerprint=str(row["fingerprint"]),
            verified_event_bytes_sha256=str(row["verified_event_bytes_digest"]),
        )

    async def reserve_order_notional(
        self,
        *,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        policy_id: UUID,
        requested_notional: Decimal,
    ) -> OrderReservationSnapshot:
        return await asyncio.to_thread(
            self._reserve_order_notional,
            event_id,
            deployment_instance_id,
            client_order_id,
            policy_id,
            requested_notional,
        )

    def _reserve_order_notional(
        self,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        policy_id: UUID,
        requested_notional: Decimal,
    ) -> OrderReservationSnapshot:
        instance = _uuid(deployment_instance_id, "deployment_instance_id")
        order = _non_empty(client_order_id, "client_order_id")
        policy = _uuid(policy_id, "policy_id")
        requested = Decimal(_decimal(requested_notional, "requested_notional", positive=True))
        payload = {
            "event_kind": "reserve",
            "deployment_instance_id": instance,
            "client_order_id": order,
            "policy_id": policy,
            "requested_notional": _decimal(requested, "requested_notional"),
        }
        with self._outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._reservation_event_replay(connection, event_id, payload)
            if replay is not None:
                return self._reservation_from_document(replay)
            policy_row = self._reservation_policy(
                connection,
                policy,
                deployment_instance_id=instance,
            )
            if requested > Decimal(str(policy_row["max_order_notional"])):
                raise RunnerStateAuthorityError("runner policy per-order cap exceeded")
            existing = connection.execute(
                """
                SELECT 1 FROM order_reservation
                WHERE deployment_instance_id = ? AND client_order_id = ?
                """,
                (instance, order),
            ).fetchone()
            if existing is not None:
                raise RunnerStateAuthorityError("client order reservation already exists")
            exposure = self._runner_exposure(connection, policy_row)
            if exposure.total_exposure + requested > exposure.max_total_notional:
                raise RunnerStateAuthorityError("runner aggregate cap exceeded")
            recorded_at_ns = time.time_ns()
            connection.execute(
                """
                INSERT INTO order_reservation (
                    deployment_instance_id, client_order_id, policy_id,
                    reserved_notional, filled_exposure, state, updated_at_ns
                ) VALUES (?, ?, ?, ?, '0', 'reserved', ?)
                """,
                (
                    instance,
                    order,
                    policy,
                    _decimal(requested, "requested_notional"),
                    recorded_at_ns,
                ),
            )
            snapshot = OrderReservationSnapshot(
                deployment_instance_id=UUID(instance),
                client_order_id=order,
                policy_id=UUID(policy),
                reserved_notional=requested,
                filled_exposure=Decimal("0"),
                state="reserved",
            )
            self._record_reservation_event(
                connection, event_id, payload, policy, snapshot, recorded_at_ns
            )
            return snapshot

    async def replace_order_reservation(
        self,
        *,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        new_reserved_notional: Decimal,
    ) -> OrderReservationSnapshot:
        return await asyncio.to_thread(
            self._replace_order_reservation,
            event_id,
            deployment_instance_id,
            client_order_id,
            new_reserved_notional,
        )

    def _replace_order_reservation(
        self,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        new_reserved_notional: Decimal,
    ) -> OrderReservationSnapshot:
        instance = _uuid(deployment_instance_id, "deployment_instance_id")
        order = _non_empty(client_order_id, "client_order_id")
        new_reserved = Decimal(_decimal(new_reserved_notional, "new_reserved_notional"))
        payload = {
            "event_kind": "replace",
            "deployment_instance_id": instance,
            "client_order_id": order,
            "new_reserved_notional": _decimal(new_reserved, "new_reserved_notional"),
        }
        with self._outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._reservation_event_replay(connection, event_id, payload)
            if replay is not None:
                return self._reservation_from_document(replay)
            row = self._reservation_row(connection, instance, order)
            policy_row = self._reservation_policy(
                connection,
                str(row["policy_id"]),
                deployment_instance_id=instance,
            )
            filled = Decimal(str(row["filled_exposure"]))
            if filled + new_reserved > Decimal(str(policy_row["max_order_notional"])):
                raise RunnerStateAuthorityError("runner policy per-order cap exceeded")
            exposure = self._runner_exposure(connection, policy_row)
            old_reserved = Decimal(str(row["reserved_notional"]))
            if exposure.total_exposure - old_reserved + new_reserved > exposure.max_total_notional:
                raise RunnerStateAuthorityError("runner aggregate cap exceeded")
            if new_reserved > 0:
                state = "partially_filled" if filled > 0 else "reserved"
            else:
                state = "filled" if filled > 0 else "released"
            recorded_at_ns = time.time_ns()
            connection.execute(
                """
                UPDATE order_reservation
                SET reserved_notional = ?, state = ?, updated_at_ns = ?
                WHERE deployment_instance_id = ? AND client_order_id = ?
                """,
                (
                    _decimal(new_reserved, "new_reserved_notional"),
                    state,
                    recorded_at_ns,
                    instance,
                    order,
                ),
            )
            snapshot = self._reservation_snapshot(
                row, reserved=new_reserved, filled=filled, state=state
            )
            self._record_reservation_event(
                connection,
                event_id,
                payload,
                str(row["policy_id"]),
                snapshot,
                recorded_at_ns,
            )
            return snapshot

    async def release_order_reservation(
        self,
        *,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        reason: Literal["rejected", "canceled"],
    ) -> OrderReservationSnapshot:
        return await asyncio.to_thread(
            self._release_order_reservation,
            event_id,
            deployment_instance_id,
            client_order_id,
            reason,
        )

    def _release_order_reservation(
        self,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        reason: str,
    ) -> OrderReservationSnapshot:
        if reason not in {"rejected", "canceled"}:
            raise RunnerStateAuthorityError("reservation release reason is invalid")
        instance = _uuid(deployment_instance_id, "deployment_instance_id")
        order = _non_empty(client_order_id, "client_order_id")
        payload = {
            "event_kind": "release",
            "deployment_instance_id": instance,
            "client_order_id": order,
            "reason": reason,
        }
        with self._outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._reservation_event_replay(connection, event_id, payload)
            if replay is not None:
                return self._reservation_from_document(replay)
            row = self._reservation_row(connection, instance, order)
            self._reservation_policy(
                connection,
                str(row["policy_id"]),
                deployment_instance_id=instance,
            )
            filled = Decimal(str(row["filled_exposure"]))
            state = "filled" if filled > 0 else "released"
            recorded_at_ns = time.time_ns()
            connection.execute(
                """
                UPDATE order_reservation
                SET reserved_notional = '0', state = ?, updated_at_ns = ?
                WHERE deployment_instance_id = ? AND client_order_id = ?
                """,
                (state, recorded_at_ns, instance, order),
            )
            snapshot = self._reservation_snapshot(
                row, reserved=Decimal("0"), filled=filled, state=state
            )
            self._record_reservation_event(
                connection,
                event_id,
                payload,
                str(row["policy_id"]),
                snapshot,
                recorded_at_ns,
            )
            return snapshot

    async def record_order_fill(
        self,
        *,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        fill_notional: Decimal,
    ) -> OrderReservationSnapshot:
        return await asyncio.to_thread(
            self._record_order_fill,
            event_id,
            deployment_instance_id,
            client_order_id,
            fill_notional,
        )

    def _record_order_fill(
        self,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        fill_notional: Decimal,
    ) -> OrderReservationSnapshot:
        instance = _uuid(deployment_instance_id, "deployment_instance_id")
        order = _non_empty(client_order_id, "client_order_id")
        fill = Decimal(_decimal(fill_notional, "fill_notional", positive=True))
        payload = {
            "event_kind": "fill",
            "deployment_instance_id": instance,
            "client_order_id": order,
            "fill_notional": _decimal(fill, "fill_notional"),
        }
        with self._outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._reservation_event_replay(connection, event_id, payload)
            if replay is not None:
                return self._reservation_from_document(replay)
            row = self._reservation_row(connection, instance, order)
            policy_row = self._reservation_policy(
                connection,
                str(row["policy_id"]),
                deployment_instance_id=instance,
            )
            reserved = Decimal(str(row["reserved_notional"]))
            if fill > reserved:
                raise RunnerStateAuthorityError("fill exceeds the durable order reservation")
            filled = Decimal(str(row["filled_exposure"])) + fill
            remaining = reserved - fill
            state = "partially_filled" if remaining > 0 else "filled"
            recorded_at_ns = time.time_ns()
            connection.execute(
                """
                UPDATE order_reservation
                SET reserved_notional = ?, filled_exposure = ?, state = ?, updated_at_ns = ?
                WHERE deployment_instance_id = ? AND client_order_id = ?
                """,
                (
                    _decimal(remaining, "remaining_reservation"),
                    _decimal(filled, "filled_exposure"),
                    state,
                    recorded_at_ns,
                    instance,
                    order,
                ),
            )
            self._adjust_exposure_checkpoint(
                connection,
                str(row["policy_id"]),
                fill,
                recorded_at_ns,
                source_digest=self._reservation_event_fingerprint(payload),
            )
            snapshot = self._reservation_snapshot(
                row, reserved=remaining, filled=filled, state=state
            )
            self._record_reservation_event(
                connection,
                event_id,
                payload,
                str(policy_row["policy_id"]),
                snapshot,
                recorded_at_ns,
            )
            return snapshot

    async def record_position_reduction(
        self,
        *,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        reduction_notional: Decimal,
    ) -> OrderReservationSnapshot:
        return await asyncio.to_thread(
            self._record_position_reduction,
            event_id,
            deployment_instance_id,
            client_order_id,
            reduction_notional,
        )

    def _record_position_reduction(
        self,
        event_id: str,
        deployment_instance_id: UUID,
        client_order_id: str,
        reduction_notional: Decimal,
    ) -> OrderReservationSnapshot:
        instance = _uuid(deployment_instance_id, "deployment_instance_id")
        order = _non_empty(client_order_id, "client_order_id")
        reduction = Decimal(_decimal(reduction_notional, "reduction_notional", positive=True))
        payload = {
            "event_kind": "position_reduction",
            "deployment_instance_id": instance,
            "client_order_id": order,
            "reduction_notional": _decimal(reduction, "reduction_notional"),
        }
        with self._outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._reservation_event_replay(connection, event_id, payload)
            if replay is not None:
                return self._reservation_from_document(replay)
            row = self._reservation_row(connection, instance, order)
            self._reservation_policy(
                connection,
                str(row["policy_id"]),
                deployment_instance_id=instance,
            )
            filled = Decimal(str(row["filled_exposure"]))
            if reduction > filled:
                raise RunnerStateAuthorityError(
                    "position reduction exceeds durable filled exposure"
                )
            remaining_filled = filled - reduction
            reserved = Decimal(str(row["reserved_notional"]))
            state = (
                "partially_filled"
                if reserved > 0
                else ("filled" if remaining_filled > 0 else "closed")
            )
            recorded_at_ns = time.time_ns()
            connection.execute(
                """
                UPDATE order_reservation
                SET filled_exposure = ?, state = ?, updated_at_ns = ?
                WHERE deployment_instance_id = ? AND client_order_id = ?
                """,
                (
                    _decimal(remaining_filled, "filled_exposure"),
                    state,
                    recorded_at_ns,
                    instance,
                    order,
                ),
            )
            self._adjust_exposure_checkpoint(
                connection,
                str(row["policy_id"]),
                -reduction,
                recorded_at_ns,
                source_digest=self._reservation_event_fingerprint(payload),
            )
            snapshot = self._reservation_snapshot(
                row,
                reserved=reserved,
                filled=remaining_filled,
                state=state,
            )
            self._record_reservation_event(
                connection,
                event_id,
                payload,
                str(row["policy_id"]),
                snapshot,
                recorded_at_ns,
            )
            return snapshot

    async def rebuild_runner_exposure(
        self,
        *,
        event_id: str,
        policy_id: UUID,
        open_exposure: Decimal,
        active_reservations: Sequence[OrderReservationRebuildEntry],
        source_digest: str,
    ) -> RunnerExposureSnapshot:
        return await asyncio.to_thread(
            self._rebuild_runner_exposure,
            event_id,
            policy_id,
            open_exposure,
            active_reservations,
            source_digest,
        )

    def _rebuild_runner_exposure(
        self,
        event_id: str,
        policy_id: UUID,
        open_exposure: Decimal,
        active_reservations: Sequence[OrderReservationRebuildEntry],
        source_digest: str,
    ) -> RunnerExposureSnapshot:
        policy = _uuid(policy_id, "policy_id")
        opened = Decimal(_decimal(open_exposure, "open_exposure"))
        if not _LOWER_HEX_64.fullmatch(source_digest):
            raise RunnerStateAuthorityError("exposure source digest is invalid")
        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for entry in active_reservations:
            instance = _uuid(entry.deployment_instance_id, "deployment_instance_id")
            order = _non_empty(entry.client_order_id, "client_order_id")
            reserved = Decimal(
                _decimal(entry.reserved_notional, "reserved_notional", positive=True)
            )
            key = (instance, order)
            if key in seen:
                raise RunnerStateAuthorityError("duplicate rebuild reservation")
            seen.add(key)
            entries.append(
                {
                    "deployment_instance_id": instance,
                    "client_order_id": order,
                    "reserved_notional": _decimal(reserved, "reserved_notional"),
                }
            )
        entries.sort(
            key=lambda value: (
                value["deployment_instance_id"],
                value["client_order_id"],
            )
        )
        payload = {
            "event_kind": "rebuild",
            "policy_id": policy,
            "open_exposure": _decimal(opened, "open_exposure"),
            "active_reservations": entries,
            "source_digest": source_digest,
        }
        with self._outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._reservation_event_replay(connection, event_id, payload)
            if replay is not None:
                return self._exposure_from_document(replay)
            policy_row = self._reservation_policy(connection, policy)
            max_order = Decimal(str(policy_row["max_order_notional"]))
            for entry in entries:
                self._reservation_policy(
                    connection,
                    policy,
                    deployment_instance_id=entry["deployment_instance_id"],
                )
                if Decimal(entry["reserved_notional"]) > max_order:
                    raise RunnerStateAuthorityError("rebuild reservation exceeds per-order cap")
            recorded_at_ns = time.time_ns()
            connection.execute(
                """
                UPDATE order_reservation
                SET reserved_notional = '0', filled_exposure = '0',
                    state = 'released', updated_at_ns = ?
                WHERE policy_id = ?
                """,
                (recorded_at_ns, policy),
            )
            for entry in entries:
                connection.execute(
                    """
                    INSERT INTO order_reservation (
                        deployment_instance_id, client_order_id, policy_id,
                        reserved_notional, filled_exposure, state, updated_at_ns
                    ) VALUES (?, ?, ?, ?, '0', 'reserved', ?)
                    ON CONFLICT(deployment_instance_id, client_order_id) DO UPDATE SET
                        policy_id = excluded.policy_id,
                        reserved_notional = excluded.reserved_notional,
                        filled_exposure = '0',
                        state = 'reserved',
                        updated_at_ns = excluded.updated_at_ns
                    """,
                    (
                        entry["deployment_instance_id"],
                        entry["client_order_id"],
                        policy,
                        entry["reserved_notional"],
                        recorded_at_ns,
                    ),
                )
            connection.execute(
                """
                INSERT INTO runner_exposure_checkpoint (
                    policy_id, open_exposure, reconstructed_at_ns, source_digest
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    open_exposure = excluded.open_exposure,
                    reconstructed_at_ns = excluded.reconstructed_at_ns,
                    source_digest = excluded.source_digest
                """,
                (
                    policy,
                    _decimal(opened, "open_exposure"),
                    recorded_at_ns,
                    source_digest,
                ),
            )
            exposure = self._runner_exposure(connection, policy_row)
            self._record_reservation_event(
                connection,
                event_id,
                payload,
                policy,
                exposure,
                recorded_at_ns,
            )
            return exposure

    async def load_order_reservation(
        self,
        deployment_instance_id: UUID,
        client_order_id: str,
    ) -> OrderReservationSnapshot:
        return await asyncio.to_thread(
            self._load_order_reservation,
            deployment_instance_id,
            client_order_id,
        )

    def _load_order_reservation(
        self,
        deployment_instance_id: UUID,
        client_order_id: str,
    ) -> OrderReservationSnapshot:
        with self._outbox._connect() as connection:
            row = self._reservation_row(
                connection,
                _uuid(deployment_instance_id, "deployment_instance_id"),
                _non_empty(client_order_id, "client_order_id"),
            )
            return self._reservation_snapshot(row)

    async def load_runner_exposure(
        self,
        policy_id: UUID,
    ) -> RunnerExposureSnapshot:
        return await asyncio.to_thread(self._load_runner_exposure, policy_id)

    def _load_runner_exposure(self, policy_id: UUID) -> RunnerExposureSnapshot:
        with self._outbox._connect() as connection:
            policy_row = self._reservation_policy(connection, _uuid(policy_id, "policy_id"))
            return self._runner_exposure(connection, policy_row)

    def _reservation_policy(
        self,
        connection: sqlite3.Connection,
        policy_id: str,
        *,
        deployment_instance_id: str | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT policy.* FROM runner_cap_policy AS policy
            JOIN runner_cap_policy_head AS head
              ON head.tenant_scope = policy.tenant_scope
             AND head.trading_mode = policy.trading_mode
             AND head.runner_id = policy.runner_id
             AND head.policy_id = policy.policy_id
            WHERE policy.policy_id = ?
            """,
            (policy_id,),
        ).fetchone()
        now_ns = time.time_ns()
        if (
            row is None
            or row["tenant_scope"] != self._tenant_id
            or row["runner_id"] != str(self._runner_id)
            or row["policy_status"] != "active"
            or now_ns < int(row["effective_at_ns"])
            or now_ns >= int(row["expires_at_ns"])
        ):
            raise RunnerStateAuthorityError(
                "reservation requires the current effective runner policy"
            )
        if deployment_instance_id is not None:
            desired = connection.execute(
                """
                SELECT tenant_id, trading_mode, runner_id
                FROM desired_deployments WHERE deployment_instance_id = ?
                """,
                (deployment_instance_id,),
            ).fetchone()
            if (
                desired is None
                or desired["tenant_id"] != self._tenant_id
                or desired["runner_id"] != str(self._runner_id)
                or desired["trading_mode"] != row["trading_mode"]
            ):
                raise RunnerStateAuthorityError(
                    "reservation instance is outside runner policy scope"
                )
        return row

    @staticmethod
    def _reservation_row(
        connection: sqlite3.Connection,
        deployment_instance_id: str,
        client_order_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM order_reservation
            WHERE deployment_instance_id = ? AND client_order_id = ?
            """,
            (deployment_instance_id, client_order_id),
        ).fetchone()
        if row is None:
            raise RunnerStateAuthorityError("order reservation is missing")
        return row

    @staticmethod
    def _reservation_snapshot(
        row: Mapping[str, Any],
        *,
        reserved: Decimal | None = None,
        filled: Decimal | None = None,
        state: str | None = None,
    ) -> OrderReservationSnapshot:
        return OrderReservationSnapshot(
            deployment_instance_id=UUID(str(row["deployment_instance_id"])),
            client_order_id=str(row["client_order_id"]),
            policy_id=UUID(str(row["policy_id"])),
            reserved_notional=(
                reserved if reserved is not None else Decimal(str(row["reserved_notional"]))
            ),
            filled_exposure=(
                filled if filled is not None else Decimal(str(row["filled_exposure"]))
            ),
            state=state if state is not None else str(row["state"]),
        )

    def _runner_exposure(
        self,
        connection: sqlite3.Connection,
        policy_row: Mapping[str, Any],
    ) -> RunnerExposureSnapshot:
        checkpoint = connection.execute(
            """
            SELECT open_exposure, source_digest
            FROM runner_exposure_checkpoint WHERE policy_id = ?
            """,
            (policy_row["policy_id"],),
        ).fetchone()
        open_exposure = (
            Decimal(str(checkpoint["open_exposure"])) if checkpoint is not None else Decimal("0")
        )
        rows = connection.execute(
            """
            SELECT reserved_notional FROM order_reservation
            WHERE policy_id = ? AND state IN ('reserved', 'partially_filled', 'filled')
            """,
            (policy_row["policy_id"],),
        ).fetchall()
        reserved = sum(
            (Decimal(str(row["reserved_notional"])) for row in rows),
            Decimal("0"),
        )
        maximum = Decimal(str(policy_row["max_notional"]))
        total = open_exposure + reserved
        return RunnerExposureSnapshot(
            policy_id=UUID(str(policy_row["policy_id"])),
            open_exposure=open_exposure,
            reserved_notional=reserved,
            total_exposure=total,
            max_total_notional=maximum,
            within_policy=total <= maximum,
            source_digest=(str(checkpoint["source_digest"]) if checkpoint is not None else None),
        )

    @staticmethod
    def _adjust_exposure_checkpoint(
        connection: sqlite3.Connection,
        policy_id: str,
        delta: Decimal,
        recorded_at_ns: int,
        *,
        source_digest: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT open_exposure FROM runner_exposure_checkpoint WHERE policy_id = ?
            """,
            (policy_id,),
        ).fetchone()
        current = Decimal(str(row["open_exposure"])) if row is not None else Decimal("0")
        updated = current + delta
        if updated < 0:
            raise RunnerStateAuthorityError("position reduction exceeds runner exposure checkpoint")
        connection.execute(
            """
            INSERT INTO runner_exposure_checkpoint (
                policy_id, open_exposure, reconstructed_at_ns, source_digest
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(policy_id) DO UPDATE SET
                open_exposure = excluded.open_exposure,
                reconstructed_at_ns = excluded.reconstructed_at_ns,
                source_digest = excluded.source_digest
            """,
            (
                policy_id,
                _decimal(updated, "open_exposure"),
                recorded_at_ns,
                source_digest,
            ),
        )

    @staticmethod
    def _reservation_event_fingerprint(payload: Mapping[str, Any]) -> str:
        return _sha256_hex(b"CUSTOS-RUNNER-ORDER-RESERVATION-V1\0" + _canonical_json_bytes(payload))

    def _reservation_event_replay(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        event = _non_empty(event_id, "event_id")
        fingerprint = self._reservation_event_fingerprint(payload)
        row = connection.execute(
            """
            SELECT event_fingerprint, outcome_json
            FROM runner_order_reservation_event WHERE event_id = ?
            """,
            (event,),
        ).fetchone()
        if row is None:
            return None
        if row["event_fingerprint"] != fingerprint:
            raise RunnerStateAuthorityError("order reservation event idempotency conflict")
        outcome = json.loads(str(row["outcome_json"]))
        if not isinstance(outcome, dict):
            raise RunnerStateDurabilityError("order reservation event outcome is corrupt")
        return outcome

    def _record_reservation_event(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        payload: Mapping[str, Any],
        policy_id: str,
        outcome: OrderReservationSnapshot | RunnerExposureSnapshot,
        recorded_at_ns: int,
    ) -> None:
        if isinstance(outcome, OrderReservationSnapshot):
            document: dict[str, Any] = {
                "outcome_kind": "reservation",
                "deployment_instance_id": str(outcome.deployment_instance_id),
                "client_order_id": outcome.client_order_id,
                "policy_id": str(outcome.policy_id),
                "reserved_notional": _decimal(outcome.reserved_notional, "reserved_notional"),
                "filled_exposure": _decimal(outcome.filled_exposure, "filled_exposure"),
                "state": outcome.state,
            }
            instance_id: str | None = str(outcome.deployment_instance_id)
            client_order_id: str | None = outcome.client_order_id
        else:
            document = {
                "outcome_kind": "exposure",
                "policy_id": str(outcome.policy_id),
                "open_exposure": _decimal(outcome.open_exposure, "open_exposure"),
                "reserved_notional": _decimal(outcome.reserved_notional, "reserved_notional"),
                "total_exposure": _decimal(outcome.total_exposure, "total_exposure"),
                "max_total_notional": _decimal(outcome.max_total_notional, "max_total_notional"),
                "within_policy": outcome.within_policy,
                "source_digest": outcome.source_digest,
            }
            instance_id = None
            client_order_id = None
        connection.execute(
            """
            INSERT INTO runner_order_reservation_event (
                event_id, event_kind, event_fingerprint, policy_id,
                deployment_instance_id, client_order_id, outcome_json, recorded_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _non_empty(event_id, "event_id"),
                str(payload["event_kind"]),
                self._reservation_event_fingerprint(payload),
                policy_id,
                instance_id,
                client_order_id,
                _canonical_json_bytes(document).decode("utf-8"),
                recorded_at_ns,
            ),
        )

    @staticmethod
    def _reservation_from_document(
        value: Mapping[str, Any],
    ) -> OrderReservationSnapshot:
        if value.get("outcome_kind") != "reservation":
            raise RunnerStateDurabilityError("reservation idempotency outcome has the wrong type")
        return OrderReservationSnapshot(
            deployment_instance_id=UUID(str(value["deployment_instance_id"])),
            client_order_id=str(value["client_order_id"]),
            policy_id=UUID(str(value["policy_id"])),
            reserved_notional=Decimal(str(value["reserved_notional"])),
            filled_exposure=Decimal(str(value["filled_exposure"])),
            state=str(value["state"]),
        )

    @staticmethod
    def _exposure_from_document(
        value: Mapping[str, Any],
    ) -> RunnerExposureSnapshot:
        if value.get("outcome_kind") != "exposure":
            raise RunnerStateDurabilityError("exposure idempotency outcome has the wrong type")
        return RunnerExposureSnapshot(
            policy_id=UUID(str(value["policy_id"])),
            open_exposure=Decimal(str(value["open_exposure"])),
            reserved_notional=Decimal(str(value["reserved_notional"])),
            total_exposure=Decimal(str(value["total_exposure"])),
            max_total_notional=Decimal(str(value["max_total_notional"])),
            within_policy=bool(value["within_policy"]),
            source_digest=(
                str(value["source_digest"]) if value.get("source_digest") is not None else None
            ),
        )

    async def record_exposure_checkpoint_reference(
        self,
        *,
        policy_id: str,
        open_exposure: str,
        reconstructed_at_ns: int,
        source_digest: str,
    ) -> None:
        _state_digest(source_digest, "source_digest")
        with self._outbox._connect() as connection:
            connection.execute(
                """
                INSERT INTO runner_exposure_checkpoint (
                    policy_id, open_exposure, reconstructed_at_ns, source_digest
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    open_exposure = excluded.open_exposure,
                    reconstructed_at_ns = excluded.reconstructed_at_ns,
                    source_digest = excluded.source_digest
                """,
                (
                    _non_empty(policy_id, "policy_id"),
                    _non_empty(open_exposure, "open_exposure"),
                    reconstructed_at_ns,
                    source_digest,
                ),
            )

    async def load_durable_desired_command(
        self, deployment_instance_id: UUID
    ) -> DurableDesiredCommand:
        return await asyncio.to_thread(
            self._load_durable_desired_command,
            deployment_instance_id,
        )

    async def list_recoverable_desired_command_identities(
        self,
    ) -> tuple[DurableDesiredCommandIdentity, ...]:
        return await asyncio.to_thread(self._list_recoverable_desired_command_identities)

    def _list_recoverable_desired_command_identities(
        self,
    ) -> tuple[DurableDesiredCommandIdentity, ...]:
        with self._outbox._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM desired_deployments
                WHERE desired_status IN ('recorded', 'applied')
                ORDER BY updated_at_ns, deployment_instance_id
                """
            ).fetchall()
        identities: list[DurableDesiredCommandIdentity] = []
        for row in rows:
            if row["tenant_id"] != self._tenant_id or row["runner_id"] != str(self._runner_id):
                raise RunnerStateAuthorityError(
                    "recoverable desired command differs from local tenant/runner scope"
                )
            try:
                document = json.loads(row["canonical_command"])
                deployment_spec = document["deployment_spec"]
                lifecycle_state = document["lifecycle_state"]
                strategy_id = UUID(str(deployment_spec["strategy_id"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RunnerStateDurabilityError(
                    "recoverable desired command identity is corrupt"
                ) from error
            if lifecycle_state != "running":
                continue
            if (
                str(document.get("deployment_instance_id")) != row["deployment_instance_id"]
                or str(document.get("deployment_spec_id")) != row["deployment_spec_id"]
                or document.get("deployment_spec_digest") != row["deployment_spec_digest"]
                or document.get("generation") != int(row["generation"])
            ):
                raise RunnerStateDurabilityError(
                    "recoverable desired command identity differs from normalized state"
                )
            identities.append(
                DurableDesiredCommandIdentity(
                    deployment_instance_id=UUID(row["deployment_instance_id"]),
                    deployment_spec_id=UUID(row["deployment_spec_id"]),
                    deployment_spec_digest=row["deployment_spec_digest"],
                    generation=int(row["generation"]),
                    trading_mode=row["trading_mode"],
                    strategy_id=strategy_id,
                )
            )
        return tuple(identities)

    def _load_durable_desired_command(self, deployment_instance_id: UUID) -> DurableDesiredCommand:
        from custos.contracts.crucible_runner_command import (
            CrucibleRunnerDeploymentCommandV1,
        )

        instance_id = _uuid(deployment_instance_id, "deployment_instance_id")
        with self._outbox._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise KeyError(instance_id)
        if row["tenant_id"] != self._tenant_id or row["runner_id"] != str(self._runner_id):
            raise RunnerStateAuthorityError(
                "durable desired command differs from local tenant/runner scope"
            )
        try:
            receipt = CommandVerificationReceipt(**json.loads(row["verification_receipt"]))
            command = CrucibleRunnerDeploymentCommandV1.model_validate_json(
                row["canonical_command"]
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RunnerStateDurabilityError(
                "durable desired command or verification receipt is corrupt"
            ) from error
        exact_event_bytes = bytes(row["exact_event_bytes"])
        if (
            command.deployment_instance_id != deployment_instance_id
            or receipt.command_fingerprint != row["command_fingerprint"]
            or receipt.exact_subject != row["exact_subject"]
            or receipt.verified_event_bytes_sha256 != _sha256_hex(exact_event_bytes)
        ):
            raise RunnerStateDurabilityError("durable desired command verification bindings differ")
        object.__setattr__(command, "_exact_signed_event_bytes", exact_event_bytes)
        object.__setattr__(command, "_verified_subject", row["exact_subject"])
        object.__setattr__(command, "_command_fingerprint", receipt.command_fingerprint)
        return DurableDesiredCommand(
            command=command,
            command_fingerprint=row["command_fingerprint"],
            exact_subject=row["exact_subject"],
            verification_receipt=receipt,
        )

    async def load_artifact_activation(
        self,
        *,
        command: Any,
        activation_id: str,
        artifact_identity_digest: str,
        artifact_authority_digest: str,
    ) -> Mapping[str, Any] | None:
        return await asyncio.to_thread(
            self._load_artifact_activation,
            command,
            activation_id,
            artifact_identity_digest,
            artifact_authority_digest,
        )

    def _load_artifact_activation(
        self,
        command: Any,
        activation_id: str,
        artifact_identity_digest: str,
        artifact_authority_digest: str,
    ) -> Mapping[str, Any] | None:
        activation = _non_empty(activation_id, "activation_id")
        _state_digest(artifact_identity_digest, "artifact_identity_digest")
        _state_digest(artifact_authority_digest, "artifact_authority_digest")
        with self._outbox._connect() as connection:
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None:
                raise RunnerStateDurabilityError(
                    "artifact activation lookup requires a durable desired command"
                )
            self._require_desired_authority(desired, command)
            row = connection.execute(
                "SELECT * FROM artifact_activation WHERE activation_id = ?",
                (activation,),
            ).fetchone()
            if row is None:
                return None
            expected = (
                str(command.deployment_instance_id),
                str(command.deployment_spec_id),
                command.deployment_spec_digest,
                command.generation,
                artifact_identity_digest,
                artifact_authority_digest,
            )
            actual = (
                row["deployment_instance_id"],
                row["deployment_spec_id"],
                row["deployment_spec_digest"],
                row["generation"],
                row["artifact_identity_digest"],
                row["artifact_authority_digest"],
            )
            if actual != expected:
                raise RunnerStateDurabilityError(
                    "artifact activation id is bound to different verified evidence"
                )
            return {
                "state": row["state"],
                "quarantine_reason": row["quarantine_reason"],
            }

    async def stage_artifact_activation(
        self,
        *,
        command: Any,
        activation_id: str,
        artifact_identity_digest: str,
        artifact_authority_digest: str,
    ) -> None:
        await asyncio.to_thread(
            self._stage_artifact_activation,
            command,
            activation_id,
            artifact_identity_digest,
            artifact_authority_digest,
        )

    def _stage_artifact_activation(
        self,
        command: Any,
        activation_id: str,
        artifact_identity_digest: str,
        artifact_authority_digest: str,
    ) -> None:
        activation = _non_empty(activation_id, "activation_id")
        _state_digest(artifact_identity_digest, "artifact_identity_digest")
        _state_digest(artifact_authority_digest, "artifact_authority_digest")
        with self._outbox._connect() as connection:
            desired = connection.execute(
                "SELECT * FROM desired_deployments WHERE deployment_instance_id = ?",
                (str(command.deployment_instance_id),),
            ).fetchone()
            if desired is None:
                raise RunnerStateDurabilityError(
                    "artifact activation requires a durable desired command"
                )
            self._require_desired_authority(desired, command)
            existing = connection.execute(
                "SELECT * FROM artifact_activation WHERE activation_id = ?",
                (activation,),
            ).fetchone()
            expected = (
                str(command.deployment_instance_id),
                str(command.deployment_spec_id),
                command.deployment_spec_digest,
                command.generation,
                artifact_identity_digest,
                artifact_authority_digest,
            )
            if existing is not None:
                actual = (
                    existing["deployment_instance_id"],
                    existing["deployment_spec_id"],
                    existing["deployment_spec_digest"],
                    existing["generation"],
                    existing["artifact_identity_digest"],
                    existing["artifact_authority_digest"],
                )
                if actual != expected:
                    raise RunnerStateDurabilityError(
                        "artifact activation id is bound to different verified evidence"
                    )
                return
            connection.execute(
                """
                INSERT INTO artifact_activation (
                    activation_id, deployment_instance_id, deployment_spec_id,
                    deployment_spec_digest, generation, artifact_identity_digest,
                    artifact_authority_digest, state, quarantine_reason,
                    activated_at_ns, updated_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'staged', NULL, NULL, ?)
                """,
                (activation, *expected, time.time_ns()),
            )

    async def mark_artifact_activation_active(
        self,
        *,
        command: Any,
        activation_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_artifact_activation_active,
            command,
            activation_id,
        )

    def _mark_artifact_activation_active(
        self,
        command: Any,
        activation_id: str,
    ) -> None:
        now = time.time_ns()
        with self._outbox._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE artifact_activation
                SET state = 'active', quarantine_reason = NULL,
                    activated_at_ns = ?, updated_at_ns = ?
                WHERE activation_id = ? AND deployment_instance_id = ?
                  AND deployment_spec_id = ? AND generation = ?
                  AND state = 'staged'
                """,
                (
                    now,
                    now,
                    _non_empty(activation_id, "activation_id"),
                    str(command.deployment_instance_id),
                    str(command.deployment_spec_id),
                    command.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise RunnerStateDurabilityError(
                    "artifact activation is absent, already terminal, or differs"
                )

    async def quarantine_artifact_activation(
        self,
        *,
        command: Any,
        activation_id: str,
        reason: str,
    ) -> None:
        await asyncio.to_thread(
            self._quarantine_artifact_activation,
            command,
            activation_id,
            reason,
        )

    def _quarantine_artifact_activation(
        self,
        command: Any,
        activation_id: str,
        reason: str,
    ) -> None:
        with self._outbox._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE artifact_activation
                SET state = 'quarantined', quarantine_reason = ?, updated_at_ns = ?
                WHERE activation_id = ? AND deployment_instance_id = ?
                  AND deployment_spec_id = ? AND generation = ?
                """,
                (
                    _non_empty(reason, "quarantine_reason"),
                    time.time_ns(),
                    _non_empty(activation_id, "activation_id"),
                    str(command.deployment_instance_id),
                    str(command.deployment_spec_id),
                    command.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise RunnerStateDurabilityError("artifact activation is absent")

    def _validated_command_material(
        self,
        command: Any,
        command_fingerprint: str,
        verification_receipt: CommandVerificationReceipt,
    ) -> dict[str, str]:
        if command.tenant_id != self._tenant_id or command.runner_id != self._runner_id:
            raise RunnerStateAuthorityError(
                "verified command differs from the local tenant/runner scope"
            )
        expected_fingerprint = compute_command_fingerprint(
            subject=command.verified_subject,
            verified_exact_event_bytes=command.exact_signed_event_bytes,
        )
        if (
            command_fingerprint != expected_fingerprint
            or verification_receipt.command_fingerprint != expected_fingerprint
            or verification_receipt.exact_subject != command.verified_subject
            or command.command_fingerprint != expected_fingerprint
            or verification_receipt.verified_event_bytes_sha256
            != _sha256_hex(command.exact_signed_event_bytes)
        ):
            raise RunnerStateDurabilityError(
                "verified command receipt differs from exact command bytes"
            )
        event = json.loads(command.exact_signed_event_bytes)
        event_id = _uuid(event.get("event_id"), "command_event_id")
        return {
            "event_id": event_id,
            "verification_receipt": _canonical_json_bytes(asdict(verification_receipt)).decode(
                "utf-8"
            ),
            "canonical_command": _canonical_json_bytes(command.model_dump(mode="json")).decode(
                "utf-8"
            ),
        }

    def _authority_for_verified(
        self,
        verified: VerifiedRunnerCommand,
    ) -> RunnerFactAuthority:
        authority = self._authority_resolver(verified)
        command = verified.command
        if (
            authority.tenant_id != self._tenant_id
            or authority.tenant_id != command.tenant_id
            or authority.trading_mode != command.trading_mode
            or authority.runner_id != self._runner_id
            or authority.runner_id != command.runner_id
            or authority.deployment_instance_id != command.deployment_instance_id
            or authority.deployment_spec_id != command.deployment_spec_id
            or authority.deployment_spec_digest != command.deployment_spec_digest
            or authority.generation != command.generation
        ):
            raise RunnerStateAuthorityError(
                "RunnerFact authority differs from verified command fencing"
            )
        return authority

    def _require_desired_authority(self, row: sqlite3.Row, command: Any) -> None:
        if (
            row["tenant_id"] != command.tenant_id
            or row["trading_mode"] != command.trading_mode
            or row["runner_id"] != str(command.runner_id)
        ):
            raise RunnerStateAuthorityError(
                "deployment instance desired state belongs to a different authority"
            )


def command_lifecycle_event_id(
    *,
    tenant_id: str,
    trading_mode: str,
    runner_id: UUID,
    deployment_instance_id: UUID,
    deployment_spec_id: UUID,
    deployment_spec_digest: str,
    generation: int,
    lifecycle_state: str,
    command_fingerprint: str,
    outcome: str,
) -> UUID:
    if trading_mode not in {"live", "sandbox", "testnet"}:
        raise RunnerStateDurabilityError("command lifecycle trading mode is invalid")
    if generation < 1:
        raise RunnerStateDurabilityError("command lifecycle generation must be positive")
    if lifecycle_state not in {"running", "paused", "stopped", "archived"}:
        raise RunnerStateDurabilityError("command lifecycle state is invalid")
    if outcome not in {"applied", "conflict", "stale", "retry_exhausted"}:
        raise RunnerStateDurabilityError("command lifecycle outcome is invalid")
    spec_digest = _state_digest(deployment_spec_digest, "deployment_spec_digest")
    fingerprint = _state_digest(command_fingerprint, "command_fingerprint")
    return runner_fact_event_id(
        "deployment_lifecycle",
        _non_empty(tenant_id, "tenant_id"),
        trading_mode,
        _uuid(runner_id, "runner_id"),
        _uuid(deployment_instance_id, "deployment_instance_id"),
        _uuid(deployment_spec_id, "deployment_spec_id"),
        spec_digest,
        generation,
        lifecycle_state,
        fingerprint,
        outcome,
    )


def _command_lifecycle_fact(
    *,
    authority: RunnerFactAuthority,
    command_fingerprint: str,
    outcome: str,
    lifecycle_state: str,
) -> dict[str, Any]:
    if lifecycle_state not in {"running", "paused", "stopped", "archived"}:
        raise RunnerStateDurabilityError("command lifecycle state is invalid")
    observed_at = _utc_now()
    return {
        "kind": "RunnerDeploymentLifecycleFact.v1",
        "event_id": str(
            command_lifecycle_event_id(
                tenant_id=authority.tenant_id,
                trading_mode=authority.trading_mode,
                runner_id=authority.runner_id,
                deployment_instance_id=authority.deployment_instance_id,
                deployment_spec_id=authority.deployment_spec_id,
                deployment_spec_digest=authority.deployment_spec_digest,
                generation=authority.generation,
                lifecycle_state=lifecycle_state,
                command_fingerprint=command_fingerprint,
                outcome=outcome,
            )
        ),
        "occurred_at": observed_at,
        "tenant_id": authority.tenant_id,
        "mode": authority.trading_mode,
        "runner_id": str(authority.runner_id),
        "deployment_instance_id": str(authority.deployment_instance_id),
        "deployment_spec_id": str(authority.deployment_spec_id),
        "deployment_spec_digest": authority.deployment_spec_digest,
        "generation": authority.generation,
        "lifecycle_state": lifecycle_state,
        "command_fingerprint": command_fingerprint,
        "outcome": outcome,
        "observed_at": observed_at,
    }


def _state_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not _LOWER_HEX_64.fullmatch(value):
        raise RunnerStateDurabilityError(f"{field} must be lowercase SHA-256")
    return value


class RunnerFactJetStreamPublisher:
    """Drain a RunnerFactOutbox only after a JetStream PubAck."""

    def __init__(
        self,
        *,
        connection_profiles: Mapping[str, Any],
        outbox: RunnerFactOutbox,
        runner_id: UUID,
        authority_guard: Any,
        publish_timeout: float = 5.0,
    ) -> None:
        self._connection_profiles = dict(connection_profiles)
        if not self._connection_profiles or any(
            mode not in {"sandbox", "testnet", "live"} for mode in self._connection_profiles
        ):
            raise RunnerFactError("RunnerFact publisher transport modes are invalid")
        self._outbox = outbox
        self._runner_id = runner_id
        self._authority_guard = authority_guard
        self._publish_timeout = publish_timeout
        self._nats: dict[str, Any] = {}
        self._jetstreams: dict[str, Any] = {}

    async def connect(self, trading_mode: str) -> Any:
        profile = self._connection_profiles.get(trading_mode)
        if profile is None:
            raise RunnerFactError(
                f"RunnerFact mode {trading_mode!r} has no authenticated transport session"
            )
        profile.assert_active()
        connection = self._nats.get(trading_mode)
        if connection is not None and connection.is_connected:
            return self._jetstreams[trading_mode]
        connection = await profile.connect(
            name=f"custos-runner-fact-{self._runner_id}-{trading_mode}",
        )
        self._nats[trading_mode] = connection
        self._jetstreams[trading_mode] = connection.jetstream()
        return self._jetstreams[trading_mode]

    def is_mode_connected(self, trading_mode: str) -> bool:
        connection = self._nats.get(trading_mode)
        return bool(
            connection is not None
            and getattr(connection, "is_connected", False)
            and not getattr(connection, "is_closed", False)
        )

    async def drain_once(self) -> int:
        self._authority_guard()
        delivered = 0
        blocked_streams: set[str] = set()
        for batch in await self._outbox.pending():
            if batch.stream_key in blocked_streams:
                continue
            try:
                document = json.loads(batch.payload)
                trading_mode = document.get("trading_mode") if isinstance(document, dict) else None
                if trading_mode not in {"sandbox", "testnet", "live"}:
                    raise RunnerFactError("RunnerFact batch has no valid signed trading_mode")
                profile = self._connection_profiles.get(trading_mode)
                if profile is None:
                    raise RunnerFactError(
                        f"RunnerFact mode {trading_mode!r} has no authenticated transport session"
                    )
                profile.assert_publish_subject(batch.subject)
                jetstream = await self.connect(trading_mode)
                ack = await jetstream.publish(
                    batch.subject,
                    batch.payload,
                    headers={"Nats-Msg-Id": str(batch.batch_id)},
                    timeout=self._publish_timeout,
                )
                if not getattr(ack, "stream", None):
                    raise RunnerFactError("JetStream publish returned no stream acknowledgement")
            except Exception as exc:
                await self._outbox.record_failure(batch.batch_id, exc)
                blocked_streams.add(batch.stream_key)
                continue
            await self._outbox.acknowledge(batch.batch_id)
            delivered += 1
        return delivered

    async def run(self, stop: asyncio.Event, idle_seconds: float = 0.5) -> None:
        while not stop.is_set():
            try:
                delivered = await self.drain_once()
            except Exception as exc:  # broker/connect failures must never kill durable retry
                delivered = 0
                _log.error(
                    "runner_fact_publisher_failed",
                    error_type=type(exc).__name__,
                )
            if delivered == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_seconds)
                except TimeoutError:
                    pass

    async def close(self) -> None:
        for connection in self._nats.values():
            await connection.drain()
        self._nats.clear()
        self._jetstreams.clear()


class RunnerFactEmitter:
    """Small engine-facing facade; all durable mechanics stay behind it."""

    def __init__(
        self,
        outbox: RunnerFactOutbox,
        identity: RunnerFactIdentity,
        authority_guard: Any,
    ) -> None:
        self._outbox = outbox
        self._identity = identity
        self._authority_guard = authority_guard

    async def emit(
        self,
        authority: RunnerFactAuthority,
        facts: Iterable[Mapping[str, Any]],
    ) -> UUID | None:
        self._authority_guard()
        return await self._outbox.enqueue(authority, self._identity, tuple(facts))

    def emit_sync(
        self,
        authority: RunnerFactAuthority,
        facts: Iterable[Mapping[str, Any]],
    ) -> UUID | None:
        self._authority_guard()
        return self._outbox.enqueue_sync(authority, self._identity, tuple(facts))


@dataclass(frozen=True, slots=True)
class RunnerCapabilityScopeBinding:
    projector: str
    trading_mode: str
    deployment_instance_id: str
    deployment_spec_id: str
    deployment_spec_digest: str
    strategy_id: str
    source_policy_digest: str | None
    required_venues: tuple[tuple[str, str], ...]

    def evidence_value(self) -> dict[str, Any]:
        return {
            "projector": self.projector,
            "trading_mode": self.trading_mode,
            "deployment_instance_id": self.deployment_instance_id,
            "deployment_spec_id": self.deployment_spec_id,
            "deployment_spec_digest": self.deployment_spec_digest,
            "strategy_id": self.strategy_id,
            "source_policy_digest": self.source_policy_digest,
            "required_venues": [
                {"venue": venue, "ledger_source": ledger_source}
                for venue, ledger_source in self.required_venues
            ],
        }


def _canonical_uuid_value(value: object, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RunnerFactContractError(f"{field} must be a UUID") from exc
    if parsed.int == 0:
        raise RunnerFactContractError(f"{field} must not be nil")
    return str(parsed)


def normalize_capability_scope_bindings(
    manifest: Mapping[str, Any],
) -> tuple[RunnerCapabilityScopeBinding, ...]:
    """Reproduce Crucible's declared_bindings order and normalized JSON shape."""

    base_fields = {
        "trading_mode",
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "strategy_id",
    }
    declarations: tuple[tuple[str, str, set[str]], ...] = (
        ("settlement", "settlement_scope_bindings", set()),
        ("risk", "risk_scope_bindings", {"resource_type", "resource_id"}),
        (
            "reconciliation",
            "reconciliation_scope_bindings",
            {"source_policy_digest", "required_venues"},
        ),
        (
            "health",
            "health_scope_bindings",
            {"expected_cadence_seconds", "grace_seconds"},
        ),
        ("deployment_lifecycle", "deployment_lifecycle_scope_bindings", set()),
    )
    result: list[RunnerCapabilityScopeBinding] = []
    identities: set[tuple[str, str]] = set()
    for projector, field, extra_fields in declarations:
        rows = manifest.get(field, [])
        if not isinstance(rows, list):
            raise RunnerFactContractError(f"{field} must be a list")
        for index, raw in enumerate(rows):
            label = f"{field}[{index}]"
            if not isinstance(raw, dict) or set(raw) != base_fields | extra_fields:
                raise RunnerFactContractError(f"{label} has an invalid field set")
            mode = raw["trading_mode"]
            if mode not in {"live", "sandbox", "testnet"}:
                raise RunnerFactContractError(f"{label}.trading_mode is invalid")
            instance_id = _canonical_uuid_value(
                raw["deployment_instance_id"], f"{label}.deployment_instance_id"
            )
            spec_id = _canonical_uuid_value(
                raw["deployment_spec_id"], f"{label}.deployment_spec_id"
            )
            strategy_id = _canonical_uuid_value(raw["strategy_id"], f"{label}.strategy_id")
            spec_digest = raw["deployment_spec_digest"]
            if not _is_lower_sha256(spec_digest):
                raise RunnerFactContractError(
                    f"{label}.deployment_spec_digest must be lowercase SHA-256"
                )
            identity = (projector, instance_id)
            if identity in identities:
                raise RunnerFactContractError(
                    f"{projector} repeats deployment_instance_id {instance_id}"
                )
            identities.add(identity)

            source_policy_digest: str | None = None
            required_venues: tuple[tuple[str, str], ...] = ()
            if projector == "risk":
                _non_empty(raw["resource_type"], f"{label}.resource_type")
                _canonical_uuid_value(raw["resource_id"], f"{label}.resource_id")
            elif projector == "reconciliation":
                source_policy_digest = raw["source_policy_digest"]
                if not _is_lower_sha256(source_policy_digest):
                    raise RunnerFactContractError(
                        f"{label}.source_policy_digest must be lowercase SHA-256"
                    )
                venues = raw["required_venues"]
                if not isinstance(venues, list) or not venues:
                    raise RunnerFactContractError(f"{label}.required_venues must be non-empty")
                normalized_venues: list[tuple[str, str]] = []
                for venue_index, venue_source in enumerate(venues):
                    venue_label = f"{label}.required_venues[{venue_index}]"
                    if not isinstance(venue_source, dict) or set(venue_source) != {
                        "venue",
                        "ledger_source",
                    }:
                        raise RunnerFactContractError(f"{venue_label} has an invalid field set")
                    venue = _non_empty(venue_source["venue"], f"{venue_label}.venue")
                    ledger_source = venue_source["ledger_source"]
                    if ledger_source not in {"venue_api", "drop_copy"}:
                        raise RunnerFactContractError(
                            f"{venue_label}.ledger_source is not independently authoritative"
                        )
                    normalized_venues.append((venue, ledger_source))
                if len(normalized_venues) != len(set(normalized_venues)):
                    raise RunnerFactContractError(f"{label}.required_venues contains duplicates")
                required_venues = tuple(normalized_venues)
            elif projector == "health":
                cadence = raw["expected_cadence_seconds"]
                grace = raw["grace_seconds"]
                if type(cadence) is not int or cadence <= 0 or type(grace) is not int or grace < 0:
                    raise RunnerFactContractError(f"{label} cadence/grace values are invalid")

            result.append(
                RunnerCapabilityScopeBinding(
                    projector=projector,
                    trading_mode=mode,
                    deployment_instance_id=instance_id,
                    deployment_spec_id=spec_id,
                    deployment_spec_digest=spec_digest,
                    strategy_id=strategy_id,
                    source_policy_digest=source_policy_digest,
                    required_venues=required_venues,
                )
            )
    return tuple(result)


def capability_scope_binding_values(
    bindings: Iterable[RunnerCapabilityScopeBinding],
) -> list[dict[str, Any]]:
    return [binding.evidence_value() for binding in bindings]


def capability_binding_evidence_digest(
    tenant_id: str,
    runner_id: UUID | str,
    bindings: Iterable[RunnerCapabilityScopeBinding],
) -> str:
    evidence = {
        "schema_version": 1,
        "tenant_id": _non_empty(tenant_id, "tenant_id"),
        "runner_id": _canonical_uuid_value(runner_id, "runner_id"),
        "bindings": capability_scope_binding_values(bindings),
    }
    return _sha256_hex(_canonical_json_bytes(evidence))


@dataclass(frozen=True, slots=True)
class RunnerCapabilityReceipt:
    """Public, non-secret authority returned by atomic Runner publication."""

    tenant_id: str
    runner_id: UUID
    capability_version_id: UUID
    capability_version: int
    manifest_digest: str
    key_id: str
    key_version: int
    algorithm: str
    public_key_digest: str
    binding_status: str
    binding_evidence_digest: str
    capability_manifest: Mapping[str, Any]
    scope_bindings: tuple[RunnerCapabilityScopeBinding, ...]

    def require_scope_bindings(
        self,
        *,
        projectors: Iterable[str],
        trading_mode: str,
        deployment_instance_id: UUID | str,
        deployment_spec_id: UUID | str,
        deployment_spec_digest: str,
        strategy_id: UUID | str,
    ) -> None:
        instance_id = _canonical_uuid_value(deployment_instance_id, "deployment_instance_id")
        spec_id = _canonical_uuid_value(deployment_spec_id, "deployment_spec_id")
        strategy = _canonical_uuid_value(strategy_id, "strategy_id")
        if trading_mode not in {"live", "sandbox", "testnet"} or not _is_lower_sha256(
            deployment_spec_digest
        ):
            raise RunnerFactContractError("runtime DeploymentInstance scope is invalid")
        required = tuple(projectors)
        if not required or len(required) != len(set(required)):
            raise RunnerFactContractError("required projector set is empty or duplicated")
        for projector in required:
            candidates = [
                binding
                for binding in self.scope_bindings
                if binding.projector == projector and binding.deployment_instance_id == instance_id
            ]
            if len(candidates) != 1:
                raise RunnerFactContractError(
                    f"capability has no unique {projector} binding for DeploymentInstance"
                )
            binding = candidates[0]
            if (
                binding.trading_mode != trading_mode
                or binding.deployment_spec_id != spec_id
                or binding.deployment_spec_digest != deployment_spec_digest
                or binding.strategy_id != strategy
            ):
                raise RunnerFactContractError(
                    f"capability {projector} binding does not match runtime DeploymentInstance"
                )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> RunnerCapabilityReceipt:
        from pathlib import Path

        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerFactContractError("Runner capability receipt is unreadable") from exc
        if not isinstance(document, dict):
            raise RunnerFactContractError("Runner capability receipt must be a JSON object")
        required = {
            "tenant_id",
            "schema_version",
            "runner_id",
            "capability_version_id",
            "capability_version",
            "manifest_digest",
            "key_id",
            "key_version",
            "algorithm",
            "public_key_digest",
            "binding_status",
            "binding_evidence_digest",
            "capability_manifest",
            "scope_bindings",
        }
        if not required.issubset(document):
            raise RunnerFactContractError("Runner capability receipt is incomplete")
        manifest_digest = document["manifest_digest"]
        public_key_digest = document["public_key_digest"]
        binding_status = document["binding_status"]
        evidence_digest = document.get("binding_evidence_digest")
        manifest = document["capability_manifest"]
        if not isinstance(manifest, dict):
            raise RunnerFactContractError("receipt capability_manifest must be an object")
        if (
            manifest.get("closed_fact_union") is not True
            or manifest.get("fact_kind_projectors") != dict(RUNNER_FACT_KIND_PROJECTORS)
            or manifest.get("runner_fact_contracts")
            != {
                projector: dict(contract)
                for projector, contract in RUNNER_FACT_PROJECTOR_CONTRACTS.items()
            }
            or manifest.get("unknown_fact_kind") != "terminal_unsupported_contract"
        ):
            raise RunnerFactContractError(
                "receipt capability_manifest differs from the closed fact projector contract"
            )
        bindings = normalize_capability_scope_bindings(manifest)
        binding_values = capability_scope_binding_values(bindings)
        if (
            document["schema_version"] != 1
            or not _is_lower_sha256(manifest_digest)
            or not _is_lower_sha256(public_key_digest)
            or binding_status != "validated"
            or not _is_lower_sha256(evidence_digest)
            or document["algorithm"] != "ed25519"
            or type(document["capability_version"]) is not int
            or document["capability_version"] < 1
            or type(document["key_version"]) is not int
            or document["key_version"] != 1
        ):
            raise RunnerFactContractError(
                "Runner capability receipt violates the v1 authority contract"
            )
        if _sha256_hex(_canonical_json_bytes(manifest)) != manifest_digest:
            raise RunnerFactContractError("receipt capability_manifest digest mismatch")
        if document["scope_bindings"] != binding_values:
            raise RunnerFactContractError("receipt normalized scope_bindings mismatch")
        calculated_evidence_digest = capability_binding_evidence_digest(
            document["tenant_id"], document["runner_id"], bindings
        )
        if calculated_evidence_digest != evidence_digest:
            raise RunnerFactContractError("receipt binding evidence digest mismatch")
        try:
            return cls(
                tenant_id=_non_empty(document["tenant_id"], "tenant_id"),
                runner_id=UUID(str(document["runner_id"])),
                capability_version_id=UUID(str(document["capability_version_id"])),
                capability_version=document["capability_version"],
                manifest_digest=manifest_digest,
                key_id=_non_empty(document["key_id"], "key_id"),
                key_version=document["key_version"],
                algorithm=document["algorithm"],
                public_key_digest=public_key_digest,
                binding_status=binding_status,
                binding_evidence_digest=evidence_digest,
                capability_manifest=manifest,
                scope_bindings=bindings,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise RunnerFactContractError("Runner capability receipt identity is invalid") from exc


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
