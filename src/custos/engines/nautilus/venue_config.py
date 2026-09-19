"""Public venue account options carried inside the signed engine configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def venue_options(spec: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    config = spec.get("nautilus_config", {})
    if not isinstance(config, Mapping):
        raise ValueError("nautilus_config must be an object")
    options = config.get("venue", {})
    if not isinstance(options, Mapping) or set(options) - allowed:
        raise ValueError("nautilus_config.venue contains unsupported fields")
    return dict(options)


def require_live_owner_evidence(spec: Mapping[str, Any]) -> None:
    promotion_id = str(spec.get("promotion_id") or "")
    digest = str(spec.get("promotion_evidence_digest") or "")
    if not promotion_id or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RuntimeError("live_owner_evidence_missing")


def require_credential(credential: Mapping[str, Any], field: str) -> str:
    value = credential.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"venue credential requires {field}")
    return value
