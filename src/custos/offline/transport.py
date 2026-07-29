"""The offline lane's JetStream topology, created on infrastructure the operator owns.

Owning the NATS instance is not the same as it being empty, so reconciliation is
scoped by ownership metadata: a stream that already carries this profile's marks
is brought back to the desired shape, and a stream under the same name that does
not is refused rather than reshaped.

This module is mode-agnostic by design — it moves bytes and does not decide what
may run. The mode decision belongs to :mod:`custos.offline.mode_guard`.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Any, Final

import nats
from nats.js.api import StorageType, StreamConfig
from nats.js.errors import NotFoundError

from custos.core.log import get_logger

_log = get_logger("custos.offline.transport")

_SAFE_TENANT = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_OWNER_METADATA: Final = {"owner": "custos", "profile": "standalone"}
_CONNECT_RETRY_SECS: Final = 0.1

ConnectFactory = Callable[[str], Awaitable[Any]]


def standalone_stream_configs(tenant_id: str) -> tuple[StreamConfig, StreamConfig]:
    """Return the complete offline topology for one tenant.

    Desired state keeps only the newest message per subject, because a spec is a
    statement of what should be true rather than an event that happened.
    """

    tenant = _validate_tenant_id(tenant_id)
    metadata = {**_OWNER_METADATA, "tenant_hash": _tenant_hash(tenant)}
    desired = StreamConfig(
        name=f"CUSTOS_{_tenant_hash(tenant)}_DEPLOYMENT",
        description="Offline lane desired deployment state",
        subjects=[f"arx.{tenant}.deployment_spec.>"],
        storage=StorageType.FILE,
        max_msgs_per_subject=1,
        metadata=metadata,
    )
    observed = StreamConfig(
        name=f"CUSTOS_{_tenant_hash(tenant)}_OBSERVED",
        description="Offline lane observed state and telemetry",
        subjects=[
            f"arx.{tenant}.deployment_status.>",
            f"arx.{tenant}.heartbeat.>",
            f"arx.{tenant}.telemetry.>",
        ],
        storage=StorageType.FILE,
        metadata=metadata,
    )
    return desired, observed


async def ensure_standalone_streams(jetstream: Any, tenant_id: str) -> None:
    """Create what is missing and reconcile drift, but only on streams we own."""

    for desired in standalone_stream_configs(tenant_id):
        assert desired.name is not None
        try:
            current = (await jetstream.stream_info(desired.name)).config
        except NotFoundError:
            await jetstream.add_stream(config=desired)
            _log.info("offline_stream_created", stream=desired.name)
            continue

        if not _is_owned(current, desired):
            _log.error("offline_stream_not_owned", stream=desired.name)
            raise RuntimeError(
                f"stream {desired.name!r} exists but is not owned by the offline lane profile"
            )
        if _managed_shape(current) == _managed_shape(desired):
            continue
        await jetstream.update_stream(config=desired)
        _log.info("offline_stream_updated", stream=desired.name)


async def bootstrap_standalone_streams(
    *,
    nats_url: str,
    tenant_id: str,
    timeout_secs: float = 30.0,
    connect_factory: ConnectFactory | None = None,
) -> None:
    """Wait for NATS to accept a connection, reconcile the topology, then drain.

    Waiting is expected rather than exceptional: the runner and its NATS start
    together, so the first attempts routinely land before the server is listening.
    Giving up names the address, since a timeout here usually means the wrong one.
    """

    standalone_stream_configs(tenant_id)
    if timeout_secs <= 0:
        raise ValueError("timeout_secs must be greater than zero")
    connect = connect_factory or nats.connect

    async def run() -> None:
        attempt = 0
        connection = None
        while connection is None:
            attempt += 1
            try:
                connection = await connect(nats_url)
            except Exception as exc:  # noqa: BLE001 - retry until the caller's timeout
                _log.warning("offline_transport_connect_failed", attempt=attempt, error=str(exc))
                await asyncio.sleep(_CONNECT_RETRY_SECS)
        try:
            await ensure_standalone_streams(connection.jetstream(), tenant_id)
        finally:
            await connection.drain()

    try:
        await asyncio.wait_for(run(), timeout=timeout_secs)
    except TimeoutError as exc:
        raise TimeoutError(
            f"NATS at {nats_url!r} did not become ready within {timeout_secs:g} seconds"
        ) from exc


def _validate_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not _SAFE_TENANT.fullmatch(tenant_id):
        raise ValueError("tenant_id must match ^[a-zA-Z0-9_-]{1,64}$")
    return tenant_id


def _tenant_hash(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12].upper()


def _is_owned(current: StreamConfig, desired: StreamConfig) -> bool:
    marks = current.metadata or {}
    return all(marks.get(key) == value for key, value in (desired.metadata or {}).items())


def _managed_shape(config: StreamConfig) -> tuple[Any, ...]:
    metadata = config.metadata or {}
    return (
        config.description,
        tuple(config.subjects or ()),
        config.storage,
        config.max_msgs_per_subject,
        tuple(sorted((key, metadata.get(key)) for key in (*_OWNER_METADATA, "tenant_hash"))),
    )
