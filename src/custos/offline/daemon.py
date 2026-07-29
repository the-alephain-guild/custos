"""Compose and run the offline lane.

This is a separate composition from the signed daemon rather than a mode of it.
The signed daemon verifies a control-plane backend, loads transport authorities
and publishes RunnerFacts; none of that exists on an operator's own machine, and
pretending otherwise would mean stubbing the very checks that make the signed
lane worth having.

No credential is decrypted for a spec this lane may not run: the mode is refused
before the vault is touched, not after.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any, Final

import nats

from custos.core.log import get_logger
from custos.core.readiness import ReadinessFile
from custos.core.runner_fact import RunnerRuntimeMetricsV1
from custos.offline.mode_guard import PERMITTED_MODES, refuse_live
from custos.offline.reconciler import OfflineReconciler
from custos.offline.spec import OfflineDeploymentSpec, now_rfc3339_nanos, offline_subject
from custos.offline.state import OfflineAppliedStore

_log = get_logger("custos.offline.daemon")

# Read back by `custos.core.readiness`, which rejects a health document carrying
# anything else. That rejection is what keeps this copy honest.
RUNNER_RUNTIME_METRICS_SCHEMA_V1: Final = "alephain.custos.runner-runtime-metrics.v1"


class BindMountedStrategy:
    """The strategy the operator mounted, presented as an engine artifact.

    The signed lane hands the engine a verified, activated artifact. Here the
    operator is vouching for their own checkout, and the activation identity is
    the directory's digest — enough for the engine to tell two mounts apart, and
    honest about where the code came from.
    """

    def __init__(self, *, strategy_path: Path, registry_name: str, digest: str) -> None:
        self._strategy_path = strategy_path
        self._registry_name = registry_name
        self._digest = digest

    @property
    def activation_id(self) -> str:
        return f"offline-{self._digest[:16]}"

    @property
    def strategy(self) -> object:
        # Imported lazily: the toolkit registry pulls in NautilusTrader.
        os.environ.setdefault("STRATEGY_INJECT_PATH", str(self._strategy_path))
        from custos_toolkit_nautilus.adapter import create_strategy

        return create_strategy(
            self._registry_name,
            config_path=self._strategy_path / "config.yaml",
        )


async def run_offline_lane(
    *,
    tenant_id: str,
    runner_id: str,
    strategy_id: str,
    nats_url: str,
    vault_dir: Path,
    engine: Any,
    ready_file: Path,
    state_path: Path,
    readiness: ReadinessFile | None = None,
    connect_factory: Any | None = None,
    credential_for: Any | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    """Subscribe to offline desired state and reconcile it until stopped."""

    store = OfflineAppliedStore(state_path)
    connect = connect_factory or nats.connect
    stop_event = stop or asyncio.Event()

    connection = await connect(nats_url)
    try:
        jetstream = connection.jetstream()
        subject = offline_subject(tenant_id, "deployment_spec", strategy_id)
        subscription = await jetstream.subscribe(subject)
        _log.info("offline_lane_subscribed", subject=subject, runner_id=runner_id)

        if readiness is not None:
            readiness.mark_ready(
                strategy_id=strategy_id,
                nats_connected=True,
                deployment_subscription=True,
                transport_modes=_servable_modes(engine),
                runtime_metrics=_runtime_metrics(store).to_dict(),
            )

        reconciler = OfflineReconciler(
            tenant_id=tenant_id,
            runner_id=runner_id,
            strategy_id=strategy_id,
            engine=engine,
            publish=jetstream.publish,
            artifact_for=_artifact_for,
            credential_for=credential_for or _credential_reader(vault_dir, tenant_id, runner_id),
            applied_store=store,
        )
        await reconciler.run(subscription, stop_event)
    finally:
        if readiness is not None:
            readiness.clear()
        else:
            ready_file.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            await connection.drain()
    return 0


def _servable_modes(engine: Any) -> dict[str, bool]:
    """Report the modes this lane could actually serve, not the ones it wishes it could."""

    return {
        mode.value: True for mode in PERMITTED_MODES if engine.supports_trading_mode(mode.value)
    }


def _runtime_metrics(store: OfflineAppliedStore) -> RunnerRuntimeMetricsV1:
    """Report what this lane genuinely has, and zero for what it does not run.

    The lane publishes no facts, holds no commands, resolves no policies and
    loads no transport authorities, so those counts are zero because the things
    do not exist here — not because they went unmeasured. The SQLite verdict is
    the store's own, on a file this lane really keeps.
    """

    database_bytes = store.path.stat().st_size if store.path.exists() else 0
    return RunnerRuntimeMetricsV1(
        schema_version=RUNNER_RUNTIME_METRICS_SCHEMA_V1,
        collected_at=now_rfc3339_nanos(),
        database_bytes=database_bytes,
        wal_bytes=0,
        disk_free_bytes=shutil.disk_usage(store.path.parent).free,
        sqlite_quick_check=store.quick_check(),
        pending_fact_batches=0,
        oldest_pending_fact_age_seconds=None,
        fact_publish_attempts=0,
        published_fact_batches=0,
        last_fact_puback_age_seconds=None,
        desired_deployments=len(store.load()),
        desired_applied_drift=0,
        oldest_desired_applied_drift_age_seconds=None,
        quarantined_deployments=0,
        restart_count_total=0,
        in_progress_commands=0,
        overdue_in_progress_commands=0,
        command_outcomes=0,
        terminal_command_outcomes=0,
        last_command_outcome_age_seconds=None,
        policy_heads=0,
        expired_policy_heads=0,
        next_policy_expiry_seconds=None,
        artifact_cache_bytes=0,
        artifact_activation_bytes=0,
        active_artifacts=0,
        quarantined_artifacts=0,
        transport_authorities=0,
        invalid_transport_authorities=0,
        next_transport_expiry_seconds=None,
    )


def _artifact_for(spec: OfflineDeploymentSpec) -> BindMountedStrategy:
    if not spec.strategy_registry_name:
        raise ValueError("an offline spec must name the strategy to load from its directory")
    return BindMountedStrategy(
        strategy_path=Path(spec.strategy_path),
        registry_name=spec.strategy_registry_name,
        digest=spec.code_hash or "unpinned",
    )


def _credential_reader(vault_dir: Path, tenant_id: str, runner_id: str) -> Any:
    def read(spec: OfflineDeploymentSpec) -> dict:
        # Refuse before the vault is opened, not after it has been read.
        refuse_live(spec.trading_mode.value, source="deployment spec")
        from custos.core.per_key_vault import PerKeyVault

        vault = PerKeyVault(vault_dir=vault_dir, tenant_id=tenant_id, initiator=runner_id)
        return vault.decrypt(spec.provenance_ref.credential_id)

    return read
