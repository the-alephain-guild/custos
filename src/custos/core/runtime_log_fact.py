"""Strict producer for ``RunnerRuntimeLogFact.v1``.

Runtime logs are facts inside the existing signed RunnerFactBatchV1 stream,
not a second logging transport.  They therefore share exact deployment
binding, sequence allocation, deduplication, the Ed25519 signing domain, the
durable outbox, and the JetStream PubAck checkpoint with every other RunnerFact.

Only explicitly structured events enter this producer.  It never tails stdout
or forwards exception text.  Sensitive keys and recognizable secret material
are redacted before enqueue; any residual sensitive material rejects the fact
before the durable outbox can store it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from custos.core.runner_fact import (
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactContractError,
    RunnerFactEmitter,
    runner_fact_event_id,
)

RUNTIME_LOG_KIND = "RunnerRuntimeLogFact.v1"
_LEVELS = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MESSAGE_BYTES = 4 * 1024
_MAX_FIELDS_BYTES = 32 * 1024
_MAX_DEPTH = 8
_MAX_COLLECTION_ITEMS = 128
_REDACTED = "<redacted>"

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "password",
    "passphrase",
    "passwd",
    "authorization",
    "bearer",
    "private_key",
    "privatekey",
    "age_key",
    "kek",
    "seed_phrase",
    "mnemonic",
)
_TOKEN_KEY_PARTS = ("token", "credential")
_PUBLIC_IDENTIFIER_KEYS = frozenset(
    {
        "credential_id",
        "credential_version",
        "key_id",
        "machine_key_id",
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "correlation_id",
        "causation_id",
        "event_id",
        "runner_id",
        "strategy_id",
        "container_id",
        "status_id",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\brkc1\.[A-Za-z0-9._~-]+"),
    re.compile(r"\bAGE-SECRET-KEY-[A-Z0-9-]+"),
    re.compile(
        r"-----BEGIN (?:ENCRYPTED |EC |RSA |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:ENCRYPTED |EC |RSA |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r"(?i)\b(api[-_ ]?key|api[-_ ]?secret|(?:api[-_ ]?)?passphrase|secret|token|password|passwd|"
        r"authorization|credential|private[-_ ]?key|age[-_ ]?key|kek)"
        r"(\s*[:=]\s*)([^\s,;]+)"
    ),
)


class RuntimeLogFactError(RunnerFactContractError):
    """A runtime log violates the signed, redacted fact contract."""


@dataclass(frozen=True, slots=True)
class RuntimeLogRedactor:
    """Deterministic recursive redactor with optional exact secret values."""

    known_secrets: tuple[str, ...] = ()

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        cleaned = tuple(
            value for value in (str(candidate) for candidate in known_secrets) if len(value) >= 8
        )
        object.__setattr__(self, "known_secrets", cleaned)

    def message(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeLogFactError("runtime log message must be non-empty")
        redacted = self._text(value.strip(), allow_public_identifier=False)
        if len(redacted.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            raise RuntimeLogFactError("runtime log message exceeds 4 KiB")
        self._reject_residual(redacted)
        return redacted

    def fields(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RuntimeLogFactError("structured_fields must be an object")
        redacted = self._mapping(value, depth=0)
        import json

        encoded = json.dumps(
            redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if len(encoded) > _MAX_FIELDS_BYTES:
            raise RuntimeLogFactError("structured_fields exceeds 32 KiB")
        self._reject_residual(encoded.decode("utf-8"))
        return redacted

    def _mapping(self, value: Mapping[str, Any], *, depth: int) -> dict[str, Any]:
        if depth > _MAX_DEPTH:
            raise RuntimeLogFactError("structured_fields nesting exceeds limit")
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise RuntimeLogFactError("structured_fields contains too many keys")
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 128:
                raise RuntimeLogFactError("structured_fields keys must be short strings")
            key = raw_key.strip()
            normalized = key.lower().replace("-", "_").replace(" ", "_")
            if self._sensitive_key(normalized):
                anonymous_key = f"redacted_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"
                result[anonymous_key] = _REDACTED
                continue
            result[key] = self._value(
                raw_value,
                depth=depth + 1,
                allow_public_identifier=(
                    normalized in _PUBLIC_IDENTIFIER_KEYS
                    or normalized.endswith("_id")
                    or normalized.endswith("_digest")
                ),
            )
        return result

    def _value(self, value: Any, *, depth: int, allow_public_identifier: bool) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise RuntimeLogFactError("structured_fields floats must be finite")
            return value
        if isinstance(value, str):
            return self._text(value, allow_public_identifier=allow_public_identifier)
        if isinstance(value, Mapping):
            return self._mapping(value, depth=depth)
        if isinstance(value, (list, tuple)):
            if depth > _MAX_DEPTH or len(value) > _MAX_COLLECTION_ITEMS:
                raise RuntimeLogFactError("structured_fields collection exceeds limit")
            return [
                self._value(item, depth=depth + 1, allow_public_identifier=False) for item in value
            ]
        raise RuntimeLogFactError(
            "structured_fields values must be JSON primitives, arrays, or objects"
        )

    def _text(self, value: str, *, allow_public_identifier: bool) -> str:
        redacted = value
        for secret in self.known_secrets:
            redacted = redacted.replace(secret, _REDACTED)
        for pattern in _SECRET_PATTERNS:
            if pattern.groups >= 3:
                redacted = pattern.sub(_REDACTED, redacted)
            else:
                redacted = pattern.sub(_REDACTED, redacted)
        if not allow_public_identifier:
            redacted = re.sub(
                r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_~-]{48,}(?![A-Za-z0-9])",
                _REDACTED,
                redacted,
            )
        return redacted

    @staticmethod
    def _sensitive_key(normalized: str) -> bool:
        if normalized in _PUBLIC_IDENTIFIER_KEYS:
            return False
        if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
            return True
        return any(part in normalized for part in _TOKEN_KEY_PARTS)

    def _reject_residual(self, value: str) -> None:
        for secret in self.known_secrets:
            if secret in value:
                raise RuntimeLogFactError("runtime log still contains registered secret material")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise RuntimeLogFactError("runtime log still contains recognizable secret material")


class RunnerRuntimeLogEmitter:
    """Capability-bound facade over the shared RunnerFactEmitter/outbox."""

    def __init__(
        self,
        *,
        emitter: RunnerFactEmitter,
        capability: RunnerCapabilityReceipt,
        redactor: RuntimeLogRedactor,
    ) -> None:
        if capability.binding_status != "validated":
            raise RuntimeLogFactError("runtime log capability binding is not validated")
        self._emitter = emitter
        self._capability = capability
        self._redactor = redactor

    def authority_for_spec(
        self,
        spec: Mapping[str, Any],
        *,
        strategy_id: str,
    ) -> RunnerFactAuthority:
        mode = str(spec.get("trading_mode") or "")
        deployment_instance_id = _required_uuid(
            spec.get("deployment_instance_id"), "deployment_instance_id"
        )
        deployment_spec_id = _required_uuid(spec.get("spec_id"), "deployment_spec_id")
        deployment_spec_digest = str(spec.get("deployment_spec_digest") or "")
        generation = spec.get("generation")
        if type(generation) is not int or generation < 1:
            raise RuntimeLogFactError("generation must be a positive integer")
        strategy = _required_uuid(strategy_id, "strategy_id")
        if not _LOWER_SHA256.fullmatch(deployment_spec_digest):
            raise RuntimeLogFactError("deployment_spec_digest must be lowercase SHA-256")
        self._capability.require_scope_bindings(
            projectors=("health",),
            trading_mode=mode,
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=deployment_spec_id,
            deployment_spec_digest=deployment_spec_digest,
            strategy_id=strategy,
        )
        return RunnerFactAuthority(
            tenant_id=self._capability.tenant_id,
            trading_mode=mode,
            runner_id=self._capability.runner_id,
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=deployment_spec_id,
            deployment_spec_digest=deployment_spec_digest,
            generation=generation,
            strategy_id=strategy,
            capability_version_id=self._capability.capability_version_id,
            capability_version=self._capability.capability_version,
            capability_manifest_digest=self._capability.manifest_digest,
        )

    async def emit(
        self,
        authority: RunnerFactAuthority,
        *,
        level: str,
        component: str,
        message: str,
        structured_fields: Mapping[str, Any],
        correlation_id: UUID | str,
        causation_id: UUID | str | None = None,
    ) -> UUID | None:
        fact = self._fact(
            authority,
            level=level,
            component=component,
            message=message,
            structured_fields=structured_fields,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return await self._emitter.emit(authority, (fact,))

    def emit_sync(
        self,
        authority: RunnerFactAuthority,
        *,
        level: str,
        component: str,
        message: str,
        structured_fields: Mapping[str, Any],
        correlation_id: UUID | str,
        causation_id: UUID | str | None = None,
    ) -> UUID | None:
        """Commit a structured log from a synchronous engine event callback."""

        fact = self._fact(
            authority,
            level=level,
            component=component,
            message=message,
            structured_fields=structured_fields,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return self._emitter.emit_sync(authority, (fact,))

    def _fact(
        self,
        authority: RunnerFactAuthority,
        *,
        level: str,
        component: str,
        message: str,
        structured_fields: Mapping[str, Any],
        correlation_id: UUID | str,
        causation_id: UUID | str | None,
    ) -> dict[str, Any]:
        normalized_level = level.upper()
        if normalized_level not in _LEVELS:
            raise RuntimeLogFactError("runtime log level must be DEBUG, INFO, WARN, or ERROR")
        if not _COMPONENT.fullmatch(component):
            raise RuntimeLogFactError("runtime log component is invalid")
        correlation = _required_uuid(correlation_id, "correlation_id")
        causation = (
            _required_uuid(causation_id, "causation_id") if causation_id is not None else None
        )
        safe_message = self._redactor.message(message)
        safe_fields = self._redactor.fields(structured_fields)
        identity_material = json.dumps(
            {
                "level": normalized_level,
                "component": component,
                "message": safe_message,
                "structured_fields": safe_fields,
                "correlation_id": str(correlation),
                "causation_id": str(causation) if causation is not None else None,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            "kind": RUNTIME_LOG_KIND,
            "event_id": str(
                runner_fact_event_id(
                    "runtime_log",
                    authority.tenant_id,
                    authority.trading_mode,
                    authority.runner_id,
                    authority.deployment_instance_id,
                    correlation,
                    hashlib.sha256(identity_material).hexdigest(),
                )
            ),
            "occurred_at": _now_rfc3339_nanos(),
            "level": normalized_level,
            "component": component,
            "message": safe_message,
            "structured_fields": safe_fields,
            "correlation_id": str(correlation),
            "causation_id": str(causation) if causation is not None else None,
        }


def _required_uuid(value: object, field: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeLogFactError(f"{field} must be a UUID") from exc
    if parsed.int == 0:
        raise RuntimeLogFactError(f"{field} must not be nil")
    return parsed


def _now_rfc3339_nanos() -> str:
    nanoseconds = time.time_ns()
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    value = time.gmtime(seconds)
    return (
        f"{value.tm_year:04d}-{value.tm_mon:02d}-{value.tm_mday:02d}T"
        f"{value.tm_hour:02d}:{value.tm_min:02d}:{value.tm_sec:02d}."
        f"{remainder:09d}Z"
    )
