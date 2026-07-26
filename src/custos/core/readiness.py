"""Atomic readiness state tied to an active machine credential authority."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_FILE_MODE = 0o600
_DIR_MODE = 0o700


@dataclass(frozen=True, slots=True)
class ReadinessFile:
    path: Path
    tenant_id: str
    runner_id: str
    credential_id: str
    credential_version: int
    credential_valid_until: str
    machine_key_id: str

    def mark_ready(
        self,
        *,
        strategy_id: str | None,
        nats_connected: bool,
        deployment_subscription: bool,
        transport_modes: Mapping[str, bool],
        runtime_metrics: Mapping[str, object],
    ) -> None:
        if _expired(self.credential_valid_until):
            self.clear()
            raise RuntimeError("refusing readiness for an expired machine credential")
        mode_state = dict(transport_modes)
        metrics = dict(runtime_metrics)
        ready = bool(
            nats_connected
            and mode_state
            and all(mode_state.values())
            and metrics.get("sqlite_quick_check") == "ok"
        )
        state = {
            "ready": ready,
            "tenant_id": self.tenant_id,
            "runner_id": self.runner_id,
            "credential_id": self.credential_id,
            "credential_version": self.credential_version,
            "credential_valid_until": self.credential_valid_until,
            "machine_key_id": self.machine_key_id,
            "credential_state": "active",
            "credential_binding_valid": True,
            "strategy_id": strategy_id,
            "nats_connected": nats_connected,
            "deployment_subscription": deployment_subscription,
            "transport_modes": mode_state,
            "runtime_metrics": metrics,
        }
        self._atomic_write(json.dumps(state, separators=(",", ":")).encode("utf-8"))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _atomic_write(self, payload: bytes) -> None:
        self.path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.path.parent, _DIR_MODE)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.path)
            os.chmod(self.path, _FILE_MODE)
        finally:
            temporary.unlink(missing_ok=True)


def is_ready_file(path: Path) -> bool:
    state = read_health_file(path)
    return state is not None and is_ready_state(state)


def read_health_file(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or set(state) != set(_FIELDS):
        return None
    if not isinstance(state.get("ready"), bool) or not isinstance(
        state.get("nats_connected"), bool
    ):
        return None
    if not isinstance(state.get("credential_state"), str) or not isinstance(
        state.get("credential_binding_valid"), bool
    ):
        return None
    if not isinstance(state.get("credential_version"), int) or state["credential_version"] < 1:
        return None
    for field in ("tenant_id", "runner_id", "credential_id", "machine_key_id"):
        if not isinstance(state.get(field), str) or not state[field]:
            return None
    if not isinstance(state.get("credential_valid_until"), str):
        return None
    strategy_id = state.get("strategy_id")
    if strategy_id is not None and (not isinstance(strategy_id, str) or not strategy_id):
        return None
    subscription = state.get("deployment_subscription")
    if strategy_id is not None and subscription is not True:
        return None
    if not isinstance(subscription, bool):
        return None
    transport_modes = state.get("transport_modes")
    if (
        not isinstance(transport_modes, dict)
        or not transport_modes
        or any(
            mode not in {"sandbox", "testnet", "live"} or not isinstance(connected, bool)
            for mode, connected in transport_modes.items()
        )
    ):
        return None
    metrics = state.get("runtime_metrics")
    if (
        not isinstance(metrics, dict)
        or set(metrics) != set(_RUNTIME_METRIC_FIELDS)
        or metrics.get("schema_version") != "alephain.custos.runner-runtime-metrics.v1"
        or not isinstance(metrics.get("sqlite_quick_check"), str)
    ):
        return None
    return state


def is_ready_state(state: Mapping[str, Any]) -> bool:
    transport_modes = state["transport_modes"]
    metrics = state["runtime_metrics"]
    return bool(
        state["ready"] is True
        and state["nats_connected"] is True
        and state["credential_state"] == "active"
        and state["credential_binding_valid"] is True
        and not _expired(str(state["credential_valid_until"]))
        and all(transport_modes.values())
        and metrics["sqlite_quick_check"] == "ok"
    )


_FIELDS = (
    "ready",
    "tenant_id",
    "runner_id",
    "credential_id",
    "credential_version",
    "credential_valid_until",
    "machine_key_id",
    "credential_state",
    "credential_binding_valid",
    "strategy_id",
    "nats_connected",
    "deployment_subscription",
    "transport_modes",
    "runtime_metrics",
)

_RUNTIME_METRIC_FIELDS = (
    "schema_version",
    "collected_at",
    "database_bytes",
    "wal_bytes",
    "disk_free_bytes",
    "sqlite_quick_check",
    "pending_fact_batches",
    "oldest_pending_fact_age_seconds",
    "fact_publish_attempts",
    "published_fact_batches",
    "last_fact_puback_age_seconds",
    "desired_deployments",
    "desired_applied_drift",
    "oldest_desired_applied_drift_age_seconds",
    "quarantined_deployments",
    "restart_count_total",
    "in_progress_commands",
    "overdue_in_progress_commands",
    "command_outcomes",
    "terminal_command_outcomes",
    "last_command_outcome_age_seconds",
    "policy_heads",
    "expired_policy_heads",
    "next_policy_expiry_seconds",
)


def _expired(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed.tzinfo is None or parsed.astimezone(UTC) <= datetime.now(UTC)
