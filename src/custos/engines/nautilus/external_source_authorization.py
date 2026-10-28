"""Bind a private operator subscription to an active signed deployment."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from custos.core.external_source_transport import (
    ExternalSourceTransportError,
    SignalSubscriptionSpec,
    signal_subject,
    subscription_spec,
)
from custos.engines.nautilus.external_source_delivery import SourceSubscriptionBinding
from custos.engines.nautilus.host import ExternalSourceRuntimeScope

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,39}\Z")
_FIELDS = frozenset(
    {
        "schemaVersion",
        "tenantId",
        "runnerId",
        "deploymentInstanceId",
        "deploymentSpecId",
        "deploymentSpecDigest",
        "generation",
        "tradingMode",
        "sourceId",
        "sourceSymbol",
        "targetPair",
        "natsCredentialsSha256",
    }
)


class ExternalSourceAuthorizationError(ValueError):
    """The local grant, credential or active host no longer has one exact scope."""


@dataclass(frozen=True, slots=True)
class AuthorizedSourceSubscription:
    spec: SignalSubscriptionSpec
    binding: SourceSubscriptionBinding
    credentials_path: Path


def _private_file(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ExternalSourceAuthorizationError(
            "source authorization file must be private"
        ) from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size < 1
            or info.st_size > max_bytes
        ):
            raise ExternalSourceAuthorizationError("source authorization file must be private")
        return os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalSourceAuthorizationError("source authorization fields are duplicated")
        result[key] = value
    return result


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ExternalSourceAuthorizationError("source authorization identity is invalid")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ExternalSourceAuthorizationError(
            "source authorization identity is invalid"
        ) from error
    if str(parsed) != value:
        raise ExternalSourceAuthorizationError("source authorization identity is invalid")
    return parsed


def load_source_subscription(
    path: Path,
    *,
    credentials_path: Path,
    expected_tenant_id: str,
    expected_runner_id: UUID,
    active_scope: ExternalSourceRuntimeScope,
) -> AuthorizedSourceSubscription:
    """Use active host authority, never a self-reported deployment in the file."""
    try:
        payload = json.loads(
            _private_file(path, max_bytes=4096).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExternalSourceAuthorizationError("source authorization JSON is invalid") from error
    if not isinstance(payload, dict) or payload.keys() != _FIELDS:
        raise ExternalSourceAuthorizationError("source authorization fields are invalid")
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
        raise ExternalSourceAuthorizationError("source authorization schema is invalid")
    if (
        payload["tenantId"] != expected_tenant_id
        or _uuid(payload["runnerId"]) != expected_runner_id
    ):
        raise ExternalSourceAuthorizationError("source authorization runner scope differs")
    authority = active_scope.authority
    if (
        authority.trading_mode not in {"sandbox", "testnet"}
        or _uuid(payload["deploymentInstanceId"]) != authority.deployment_instance_id
        or _uuid(payload["deploymentSpecId"]) != authority.deployment_spec_id
        or payload["deploymentSpecDigest"] != authority.deployment_spec_digest
        or type(payload["generation"]) is not int
        or payload["generation"] != authority.generation
        or payload["tradingMode"] != authority.trading_mode
    ):
        raise ExternalSourceAuthorizationError(
            "source authorization differs from active deployment"
        )
    digest = payload["natsCredentialsSha256"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ExternalSourceAuthorizationError("source credential digest is invalid")
    credentials = _private_file(credentials_path, max_bytes=65536)
    if not hmac.compare_digest(hashlib.sha256(credentials).hexdigest(), digest):
        raise ExternalSourceAuthorizationError("source credential differs from authorization")
    source_id = payload["sourceId"]
    symbol = payload["sourceSymbol"]
    pair = payload["targetPair"]
    try:
        signal_subject(expected_tenant_id, source_id)
    except ExternalSourceTransportError as error:
        raise ExternalSourceAuthorizationError(
            "source authorization identity is invalid"
        ) from error
    if (
        not isinstance(symbol, str)
        or not _SYMBOL.fullmatch(symbol)
        or not isinstance(pair, str)
        or pair not in active_scope.allowed_pairs
        or active_scope.connector not in {"okx_perpetual", "binance_perpetual"}
    ):
        raise ExternalSourceAuthorizationError("source authorization pair differs from active host")
    spec = subscription_spec(
        tenant_id=expected_tenant_id,
        runner_id=expected_runner_id,
        authority=authority,
        source_id=source_id,
    )
    return AuthorizedSourceSubscription(
        spec=spec,
        binding=SourceSubscriptionBinding(
            tenant_id=expected_tenant_id,
            source_id=source_id,
            source_symbol=symbol,
            target_pair=pair,
            authority=authority,
        ),
        credentials_path=credentials_path,
    )
