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
import signal
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Final

import nats

from custos.core.log import get_logger
from custos.core.readiness import ReadinessFile
from custos.core.runner_fact import RunnerRuntimeMetricsV1
from custos.offline.mode_guard import PERMITTED_MODES, refuse_live
from custos.offline.reconciler import OfflineReconciler
from custos.offline.safety import TICK_SECS, OfflineExposureGuard
from custos.offline.spec import OfflineDeploymentSpec, now_rfc3339_nanos, offline_subject
from custos.offline.state import OfflineAppliedStore
from custos.offline.strategy_config import strategy_trading_config_for
from custos.offline.telemetry import SNAPSHOT_SECS, OfflineTelemetry

_log = get_logger("custos.offline.daemon")

# Read back by `custos.core.readiness`, which rejects a health document carrying
# anything else. That rejection is what keeps this copy honest.
RUNNER_RUNTIME_METRICS_SCHEMA_V1: Final = "alephain.custos.runner-runtime-metrics.v1"

_DISCOVERY_PATH_VARIABLE: Final = "STRATEGY_INJECT_PATH"
# How long stopping may take in all, from the stop signal to the last deployment.
# The host allows up to 30s for its shutdown policy and 30s more for the node; this
# leaves room for both, and the container's grace period has to exceed it.
SHUTDOWN_DEADLINE_SECS: Final = 75.0
_STOP_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)
_REGISTRY_MODULE: Final = "custos_toolkit_nautilus.adapter.registry"


class BindMountedStrategy:
    """The strategy the operator mounted, presented as an engine artifact.

    The signed lane hands the engine a verified, activated artifact. Here the
    operator is vouching for their own checkout, and the activation identity is
    the directory's digest — enough for the engine to tell two mounts apart, and
    honest about where the code came from.
    """

    def __init__(
        self,
        *,
        strategy_path: Path,
        registry_name: str,
        digest: str,
    ) -> None:
        self._strategy_path = strategy_path
        self._registry_name = registry_name
        self._digest = digest

    @property
    def activation_id(self) -> str:
        return f"offline-{self._digest[:16]}"

    @property
    def strategy(self) -> object:
        self.select_discovery_path()
        # Imported lazily: the toolkit registry pulls in NautilusTrader.
        from custos_toolkit.config import load_config
        from custos_toolkit_nautilus.adapter import create_strategy

        return create_strategy(
            self._registry_name,
            config_wrapper=load_config(self._strategy_path / "config.yaml"),
        )

    def select_discovery_path(self) -> None:
        """Point strategy discovery at this directory, or refuse to guess.

        The registry reads ``STRATEGY_INJECT_PATH`` once, at import. Setting it
        afterwards changes nothing, and setting it only when unset would silently
        serve a second deployment the first one's strategy. Both cases used to pass
        quietly and load the wrong code, so both now stop.
        """

        wanted = str(self._strategy_path)
        current = os.environ.get(_DISCOVERY_PATH_VARIABLE)
        if current is not None and current != wanted:
            raise RuntimeError(
                f"strategy discovery is already pointed at {current!r}; this runner cannot "
                f"also serve {wanted!r} in the same process"
            )
        if current is None and _REGISTRY_MODULE in sys.modules:
            raise RuntimeError(
                "the strategy registry was imported before a strategy directory was chosen, "
                f"so {wanted!r} would be ignored in favour of its built-in discovery paths"
            )
        os.environ[_DISCOVERY_PATH_VARIABLE] = wanted


async def run_offline_lane(
    *,
    tenant_id: str,
    runner_id: str,
    runner_label: str | None,
    strategy_id: str,
    nats_url: str,
    vault_dir: Path,
    engine: Any,
    ready_file: Path,
    state_path: Path,
    readiness: ReadinessFile | None = None,
    connect_factory: Any | None = None,
    credential_for: Any | None = None,
    strategy_config_for: Any | None = None,
    safety_interval: float = TICK_SECS,
    telemetry_interval: float = SNAPSHOT_SECS,
    shutdown_deadline: float = SHUTDOWN_DEADLINE_SECS,
    stop: asyncio.Event | None = None,
) -> int:
    """Subscribe to offline desired state and reconcile it until stopped.

    Returns non-zero when a deployment could not be confirmed stopped on the way
    out, so whoever supervises the process can tell a clean stop from one that
    may have left orders behind.
    """

    # The identity is a v1 UUID; the label is what the operator's own probe
    # subscribes by. Keeping them separate lets the consumer name the runner
    # without first reading state this command generates.
    label = runner_label or runner_id
    store = OfflineAppliedStore(state_path)
    connect = connect_factory or nats.connect
    stop_event = stop or asyncio.Event()
    guard = OfflineExposureGuard(engine=engine, interval=safety_interval)
    installed = _stop_on_signals(stop_event)

    connection = await connect(nats_url)
    stopped_cleanly = True
    try:
        jetstream = connection.jetstream()
        subject = offline_subject(tenant_id, "deployment_spec", strategy_id)
        subscription = await jetstream.subscribe(subject)
        _log.info(
            "offline_lane_subscribed",
            subject=subject,
            runner_id=runner_id,
            runner_label=label,
        )

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
            runner_label=label,
            strategy_id=strategy_id,
            engine=engine,
            publish=jetstream.publish,
            artifact_for=_artifact_for,
            credential_for=credential_for or _credential_reader(vault_dir, tenant_id, runner_id),
            strategy_config_for=strategy_config_for or strategy_trading_config_for,
            applied_store=store,
            guard=guard,
        )
        telemetry = OfflineTelemetry(
            tenant_id=tenant_id,
            runner_label=label,
            engine=engine,
            publish=jetstream.publish,
            deployments=guard.running,
            interval=telemetry_interval,
        )
        telemetry.attach(engine)
        try:
            await _run_together(
                reconciler.run(subscription, stop_event),
                guard.run(stop_event),
                stop=stop_event,
                alongside=(telemetry.run(stop_event),),
            )
        finally:
            _log.info("offline_lane_stopping")
            stopped_cleanly = await reconciler.shutdown(deadline=shutdown_deadline)
    finally:
        _restore_signals(installed)
        if readiness is not None:
            readiness.clear()
        else:
            ready_file.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            await connection.drain()
    _log.info("offline_lane_stopped", clean=stopped_cleanly)
    return 0 if stopped_cleanly else 1


def _stop_on_signals(stop: asyncio.Event) -> tuple[signal.Signals, ...]:
    """Turn SIGINT and SIGTERM into a stop, so shutdown runs instead of a kill.

    Without this the process ignores SIGTERM until the supervisor gives up and
    kills it, and nothing a strategy does when it stops ever runs.
    """

    loop = asyncio.get_running_loop()
    installed = []
    for sig in _STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError) as exc:
            # Only the main thread may own signals; a lane run elsewhere is
            # stopped through its event instead.
            _log.warning("offline_stop_signal_not_installed", signal=sig.name, error=str(exc))
            continue
        installed.append(sig)
    return tuple(installed)


def _restore_signals(installed: tuple[signal.Signals, ...]) -> None:
    loop = asyncio.get_running_loop()
    for sig in installed:
        loop.remove_signal_handler(sig)


async def _run_together(
    *coroutines: Coroutine[Any, Any, Any],
    stop: asyncio.Event,
    alongside: tuple[Coroutine[Any, Any, Any], ...] = (),
) -> None:
    """Run the lane's two loops, and let neither outlive a failure in the other.

    Reconciling and guarding are deliberately not the same clock — that is what
    keeps the guard alive while the transport is down. It also means a guard that
    dies would leave the lane trading with nothing watching it, so the first
    failure winds the other loop down and is then raised rather than logged.

    ``alongside`` runs until the lane stops but is not part of that pact: nothing
    trades on it, so its failure is logged and the lane carries on.
    """

    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    extras = [asyncio.create_task(coroutine) for coroutine in alongside]
    for extra in extras:
        extra.add_done_callback(_report_companion_failure)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        stop.set()
        await asyncio.wait(tasks + extras)
    for task in tasks:
        task.result()


def _report_companion_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _log.error(
            "offline_lane_companion_failed",
            error_type=type(task.exception()).__name__,
            error=str(task.exception()),
        )


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
