"""Run the operator-authorized Papertrade kernel against a loopback venue."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from custos.artifacts.local_context import LocalStrategyMaterialV1, bind_local_strategy_material
from custos.artifacts.policy import ArchiveLimitsV1
from custos.core.execution_events import ExecutionEventStore, LocalExecutionRecorder
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.local_runtime_state import RuntimeAuthorityRegistry, RuntimeLease
from custos.core.log import get_logger
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runtime_authority import RuntimeAccountIdentity, RuntimeStorageUnavailable
from custos.core.toolkit_identity import RuntimeToolkitSnapshot
from custos.engines.nautilus.account_observation import NativeAccountObservation
from custos.engines.nautilus.local_papertrade_orders import PapertradeOrderRouting
from custos.engines.nautilus.local_reductions import LocalPapertradeReductions
from custos.engines.nautilus.local_risk_observation import PapertradeRiskSource
from custos.engines.nautilus.local_risk_supervisor import PapertradeRiskSupervisor
from custos.engines.nautilus.order_evidence import decode_document
from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
    install_order_gate,
)
from custos.engines.nautilus.runtime_loader import (
    LocalStrategyActivation,
    NautilusRuntimeEntryPointLoaderV1,
)
from custos.engines.nautilus.runtime_storage import PapertradeRuntimeStorageProbe
from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder
from custos.engines.nautilus.strategy_hooks import install_hook
from custos.local.authorization import LocalPermitAuthenticator, local_permit_digest
from custos.local.manifest import parse_local_manifest
from custos.local.reservations import LocalReservationStore

_log = get_logger("custos.local_host")
_POLL = 0.1
_NODE_START_TIMEOUT = 35
_NODE_STOP_TIMEOUT = 20
_NATIVE_SIGNATURE_SECONDS = 3600


@dataclass(frozen=True)
class LoopbackPapertrade:
    origin: str

    def __post_init__(self) -> None:
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/", self.origin)
        if match is None or int(match[1]) > 65535:
            raise ValueError("Papertrade simulation requires a canonical numeric loopback origin")

    def check_environment(self) -> None:
        # A system proxy must never carry a fixture session signature off-host.
        if any(
            value and key.lower() in {"http_proxy", "https_proxy", "all_proxy"}
            for key, value in os.environ.items()
        ):
            raise ValueError("Papertrade loopback simulation requires proxy-free execution")


def _private_state_root(path: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("local simulation state root must be canonical")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("local simulation state root must be private")
    return path


def open_simulation_registry(root: Path) -> RuntimeAuthorityRegistry:
    return RuntimeAuthorityRegistry.open(
        _private_state_root(root) / "control",
        identity_probe=lambda path: ExecutionEventStore.open(path).identity(),
    )


def _account_from_factory(factory: Any, account: Any) -> RuntimeAccountIdentity:
    evidence = decode_document(factory.deployment_identity_json(), maximum_bytes=4096)
    if (
        set(evidence) != {"schema_version", "connector", "chain_id", "exchange"}
        or type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 1
        or evidence["connector"] != "papertrade"
    ):
        raise ValueError("Papertrade Native deployment identity differs")
    return RuntimeAccountIdentity.from_papertrade_adapter(
        wallet_address=account.wallet_address,
        adapter_chain_id=evidence["chain_id"],
        adapter_exchange=evidence["exchange"],
        declared_chain_id=account.chain_id,
        declared_exchange=account.exchange,
        trading_mode=account.trading_mode,
    )


def _fresh_account(
    storage: PapertradeRuntimeStorageProbe, max_age: int
) -> NativeAccountObservation:
    value = storage.account_observation()
    age = time.time_ns() - value.source_timestamp * 10**9
    if (
        not value.account_source_healthy
        or not value.reconciliation_complete
        or not 0 <= age <= max_age * 10**9
    ):
        raise RuntimeStorageUnavailable("stop requires fresh reconciled venue evidence")
    return value


def papertrade_stop_cutoff(increase_until: int, exit_until: int, confirm_seconds: int) -> int:
    """Reserve the Native signature lifetime, confirmation time and strict deadline."""
    return min(increase_until, exit_until - _NATIVE_SIGNATURE_SECONDS - confirm_seconds - 1)


def _configure_position_routing(config: object) -> object:
    from custos_toolkit.config import ConfigWrapper

    if type(config) is not ConfigWrapper or config.trading.get("connector") != "papertrade":
        raise ValueError("local strategy must supply a Papertrade toolkit configuration")
    raw = dict(config.raw)
    trading = raw.get("trading")
    if not isinstance(trading, dict):
        raise ValueError("local Papertrade strategy has no execution configuration")
    raw["trading"] = {**trading, "position_mode": "HEDGE"}
    return ConfigWrapper(raw)


def _same_position_quantity(truth: Any, position: Any, instrument: Any) -> bool:
    if instrument is None or truth.use_debt or truth.entry_price_raw <= 0:
        return False
    scale = 10**instrument.size_precision
    exact = Fraction(
        truth.margin_raw * truth.leverage * truth.price_scale_raw * scale,
        10**18 * truth.entry_price_raw,
    )
    expected = Fraction(exact.numerator // exact.denominator, scale)
    return Fraction(position.quantity.as_decimal()) == expected


async def _flatten(
    *,
    node_task: asyncio.Task,
    storage: PapertradeRuntimeStorageProbe,
    store: LocalReservationStore,
    cache: Any,
    account_id: Any,
    strategy: Any,
    submit: Callable[..., bool],
    emit: Callable[[str], None],
) -> bool:
    from nautilus_trader.model import ClientId, OrderSide, TimeInForce

    manifest = store.authorization.manifest
    deadline = min(
        time.monotonic() + manifest.stop_policy.confirmation_timeout_seconds,
        time.monotonic() + (store.authorization.execution_window.exit_until - time.time()),
    )
    requested: set[str] = set()
    cancelled: set[str] = set()
    while not node_task.done() and time.monotonic() < deadline:
        try:
            account = _fresh_account(storage, manifest.data_policy.max_account_age_seconds)
        except RuntimeStorageUnavailable:
            await asyncio.sleep(_POLL)
            continue
        cohort = store.risk_cohort()
        cache_positions = cache.positions_open(account_id=account_id)
        venue_positions = {position.position_id: position for position in account.positions}
        positions = {str(position.id): position for position in cache_positions}
        orders = cache.orders_open(account_id=account_id)
        if (
            not venue_positions
            and not positions
            and not orders
            and not account.pending_client_order_ids
            and not account.venue_operation_intent_ids
            and not cohort.orders
            and not cohort.closings
            and account.locked_raw == 0
        ):
            return True
        for order in orders:
            key = str(order.client_order_id)
            if not order.is_reduce_only and key not in cancelled:
                cancelled.add(key)
                strategy.cancel_order(order, client_id=ClientId("PAPERTRADE"))
        if (
            orders
            or account.pending_client_order_ids
            or account.venue_operation_intent_ids
            or set(positions) != set(venue_positions)
            or len(positions) != len(cache_positions)
        ):
            await asyncio.sleep(_POLL)
            continue
        for key, position in positions.items():
            truth = venue_positions[key]
            if (
                key in requested
                or truth.is_long != position.is_long
                or truth.instrument_id != str(position.instrument_id)
                or not _same_position_quantity(
                    truth, position, cache.instrument(position.instrument_id)
                )
            ):
                continue
            close = strategy.order_factory.market(
                instrument_id=position.instrument_id,
                order_side=OrderSide.SELL if position.is_long else OrderSide.BUY,
                quantity=position.quantity,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
            )
            if submit(close, position_id=position.id):
                requested.add(key)
                emit("closing")
        await asyncio.sleep(_POLL)
    return False


async def wait_for_native_stop(node_task: asyncio.Task, emit: Callable[[str], None]) -> None:
    """Drain the Native task before releasing the lease, even when the host is cancelled."""
    deadline = time.monotonic() + _NODE_STOP_TIMEOUT
    overdue = False
    interrupted = False
    while not node_task.done():
        try:
            await asyncio.wait(
                (node_task,), timeout=None if overdue else max(0.0, deadline - time.monotonic())
            )
        except asyncio.CancelledError:
            interrupted = True
        if not node_task.done() and not overdue and time.monotonic() >= deadline:
            overdue = True
            emit("stop_unconfirmed")
    if interrupted:
        if not node_task.cancelled() and (error := node_task.exception()) is not None:
            _log.error("local_native_stop_failed", error_type=type(error).__name__)
        raise asyncio.CancelledError
    node_task.result()


async def _drive(
    *,
    node: Any,
    strategy: Any,
    cache: Any,
    account_id: Any,
    store: LocalReservationStore,
    storage: PapertradeRuntimeStorageProbe,
    supervisor: PapertradeRiskSupervisor,
    submit: Callable[..., bool],
    stop_requested: asyncio.Event,
    fault: threading.Event,
    emit: Callable[[str], None],
    recovering: bool,
    stop_by: int,
) -> bool:
    handle = node.handle()
    task = asyncio.create_task(node.run_async())
    try:
        cutoff = time.monotonic() + _NODE_START_TIMEOUT
        was_ready = False
        was_running = False
        reason = "startup_timeout"
        while not task.done() and (was_ready or time.monotonic() < cutoff):
            running = strategy.is_running()
            if was_running and not running:
                reason = "strategy_stopped"
                break
            was_running = was_running or running
            if time.time() >= stop_by:
                reason = "permit_exit_window"
                break
            if stop_requested.is_set() or fault.is_set() or recovering:
                reason = (
                    "recovery"
                    if recovering
                    else "risk_recording_failed"
                    if fault.is_set()
                    else "stop_requested"
                )
                if not running:
                    await asyncio.sleep(_POLL)
                    continue
                break
            if was_ready or (running and strategy.is_ready()):
                try:
                    snapshot = supervisor.refresh()
                except Exception as error:
                    _log.error("local_risk_refresh_failed", error_type=type(error).__name__)
                    reason = "risk_observation_failed"
                    break
                if snapshot.state == "latched":
                    reason = snapshot.reason_code or "risk_latched"
                    break
                if snapshot.state == "ready" and not was_ready:
                    was_ready = True
                    emit("running")
            await asyncio.sleep(_POLL)
        supervisor.close()
        emit(reason)
        return await _flatten(
            node_task=task,
            storage=storage,
            store=store,
            cache=cache,
            account_id=account_id,
            strategy=strategy,
            submit=submit,
            emit=emit,
        )
    finally:
        try:
            supervisor.close()
        finally:
            handle.stop()
            await wait_for_native_stop(task, emit)


async def run_simulated_papertrade(
    *,
    state_root: Path,
    venue: LoopbackPapertrade,
    manifest_bytes: bytes,
    permit_bytes: bytes,
    authenticator: LocalPermitAuthenticator,
    session_key: SecretStr,
    material: LocalStrategyMaterialV1,
    effective_config: bytes,
    toolkit: RuntimeToolkitSnapshot,
    stop_requested: asyncio.Event,
    emit: Callable[[str], None],
) -> bool:
    """Hold the original stores through authenticated startup, risk control and shutdown."""
    from nautilus_trader.adapters.papertrade import (
        PapertradeDataClientConfig,
        PapertradeDataClientFactory,
        PapertradeExecutionClientConfig,
        PapertradeExecutionClientFactory,
        PapertradeRiskBlocker,
    )
    from nautilus_trader.common import Environment
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import AccountId, ClientId, OmsType, TraderId

    sink = emit

    def emit(status: str) -> None:
        try:
            sink(status)
        except Exception as error:
            _log.warning("local_host_status_sink_failed", error_type=type(error).__name__)

    venue.check_environment()
    root = _private_state_root(state_root)
    manifest = parse_local_manifest(manifest_bytes)
    bind_local_strategy_material(
        material=material,
        run=manifest,
        effective_config_bytes=effective_config,
        toolkit=toolkit,
    )
    factory = PapertradeExecutionClientFactory()
    identity = _account_from_factory(factory, manifest.account)
    registry = open_simulation_registry(root)
    account_id = AccountId("PAPERTRADE-001")
    key = session_key.get_secret_value()
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", key) is None:
        raise ValueError("invalid simulation session credential")
    blocker = PapertradeRiskBlocker()
    blocker.require_account_observation()
    blocker.set_observation_valid_until_ns(0)
    with ExitStack() as lifetime:
        with registry.account_guard(identity) as guard:
            existing = registry.read(identity)
            checkpoint = guard.prepare_claim(
                run_id=manifest.run_id,
                permit_digest=local_permit_digest(permit_bytes),
                expected_authorization_epoch=existing.authorization_epoch if existing else 0,
            )
            authorization = authenticator.verify(
                manifest_bytes=manifest_bytes,
                signed_permit_bytes=permit_bytes,
                account=identity,
                control_registry_id=registry.registry_id,
                checkpoint=checkpoint,
                now=datetime.now(UTC),
            )
            recovering = existing is not None and existing.phase == "recovery_required"
            window = authorization.execution_window
            stop_by = papertrade_stop_cutoff(
                window.risk_increase_until,
                window.exit_until,
                manifest.stop_policy.confirmation_timeout_seconds,
            )
            if stop_by <= time.time() and not recovering:
                raise ValueError("local permit cannot reserve the Papertrade exit window")
            if recovering:
                blocker.freeze()
            state_path, journal = root / "state.sqlite3", root / "papertrade.redb"
            if checkpoint.state is None and checkpoint.journal is None:
                if state_path.exists() or journal.exists():
                    raise RuntimeStorageUnavailable("unregistered local stores already exist")
                state = ExecutionEventStore.initialize(state_path)
                journal_args = {"journal_mode": "initialize_new"}
            elif (
                checkpoint.state is not None
                and checkpoint.journal is not None
                and checkpoint.state.path == state_path
                and checkpoint.journal.path == journal
            ):
                state = ExecutionEventStore.open(state_path)
                journal_args = {
                    "journal_mode": "recover_existing",
                    "expected_journal_id": str(checkpoint.journal.store_id),
                    "minimum_journal_revision": checkpoint.journal.sequence,
                }
            else:
                raise RuntimeStorageUnavailable("registered local store paths differ")
            cfg = PapertradeExecutionClientConfig(
                account_id,
                identity.wallet_address,
                str(journal),
                "custos-local-simulation",
                session_private_key=key,
                leverage=manifest.limits.leverage,
                origin=venue.origin,
                rpc_url=venue.origin + "rpc",
                risk_increase_until=window.risk_increase_until if recovering else stop_by,
                exit_until=window.exit_until,
                risk_blocker=blocker,
                **journal_args,
            )
            node = (
                LiveNode.builder("CUSTOS-LOCAL", TraderId("CUSTOS-001"), Environment.LIVE)
                .with_timeout_connection(10)
                .with_timeout_reconciliation(10)
                .with_timeout_portfolio(5)
                .with_timeout_disconnection_secs(3)
                .with_delay_post_stop_secs(0)
                .with_delay_shutdown_secs(0)
                .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=True))
                .add_data_client(
                    None,
                    PapertradeDataClientFactory(),
                    PapertradeDataClientConfig(
                        origin=venue.origin,
                        rpc_url=venue.origin + "rpc",
                        reference_info_url=venue.origin + "reference",
                    ),
                )
                .add_exec_client(None, factory, cfg)
                .build()
            )
            with ExitStack() as initializing:
                initializing.callback(node.dispose)
                storage = PapertradeRuntimeStorageProbe(
                    state=state,
                    journal_path=journal,
                    account=identity,
                    adapter_account_id=str(account_id),
                    adapter_client_id="PAPERTRADE",
                    native_probe=node.journal_identity_probe(
                        ClientId("PAPERTRADE"), require_sole_execution_client=True
                    ),
                )
                lease: RuntimeLease = guard.claim(
                    checkpoint, stores=storage.bindings, identity_probe=storage
                )
                initializing.pop_all()
            # Dispose native before closing the underlying account lease, including on exceptions.
            lifetime.callback(lease.close)
        activation: LocalStrategyActivation | None = None

        def dispose_runtime() -> None:
            try:
                node.dispose()
            finally:
                if activation is not None:
                    activation.quarantine.destroy()

        lifetime.callback(dispose_runtime)
        activation = NautilusRuntimeEntryPointLoaderV1().load_local(
            material=material,
            authorization=authorization,
            lease=lease,
            toolkit=toolkit,
            effective_config_bytes=effective_config,
            activation_parent=root / "activated",
            limits=ArchiveLimitsV1(),
            configure=_configure_position_routing,
        )
        strategy = activation.strategy
        if strategy.config.oms_type != OmsType.HEDGING:
            raise ValueError("local strategy did not retain venue position routing")
        cache = node.cache
        store = LocalReservationStore(
            store=state, lease=lease, authorization=authorization, risk_blocker=blocker
        )
        port = LocalPapertradeReductions(
            store=store,
            account_id=str(account_id),
            client_id="PAPERTRADE",
            storage_probe=storage,
            native_factory=factory,
        )
        semantics = NautilusCachedOrderSemantics(cache)
        limits = manifest.limits
        breaker = FallbackBreaker(
            FallbackBreakerConfig(
                Decimal(limits.max_total_notional_usd),
                Decimal("100"),
            )
        )
        boundary = RunnerReservationBoundary(
            store=store,
            runtime_id=manifest.run_id,
            policy_id=authorization.permit.permit_id,
            fallback_breaker=breaker,
            trading_mode="live",
            semantics=semantics,
            control_guard=lease,
            reduction_claims=port,
        )
        fault = threading.Event()

        def freeze_on_fault(*_: str) -> None:
            blocker.freeze()
            fault.set()

        forwarder = StrategyEventForwarder(
            local_run_id=str(manifest.run_id),
            on_sink_failure=freeze_on_fault,
        )
        boundary.bootstrap(forwarder)
        forwarder.install(strategy)
        recorder = LocalExecutionRecorder(
            state, lease, configuration_revision=manifest.configuration_revision
        )
        gate = RunnerSafetyOrderGate(
            boundary=boundary,
            client_order_id_len_limit=None,
            on_refusal=lambda refusal: recorder.record_local_refusal(
                client_order_id=refusal.client_order_id,
                instrument_id=refusal.instrument_id,
                side=refusal.side,
                reason_code=refusal.reason_code,
            ),
            on_recording_failure=freeze_on_fault,
        )
        install_order_gate(strategy, gate)
        submit = strategy.submit_order
        routing = PapertradeOrderRouting(
            strategy,
            cache,
            account_id,
            manifest.instrument_ids,
            limits.max_order_notional_usd,
        )
        install_hook(strategy, "submit_order", routing.wrap)
        node.add_strategy(strategy)
        source = PapertradeRiskSource(
            port=port,
            storage_probe=storage,
            semantics=semantics,
            minimum_quote_init_ns=time.time_ns(),
        )
        supervisor = PapertradeRiskSupervisor(source=source, poll_interval_ms=100)
        port.bind_risk_supervisor(supervisor)
        try:
            flat = await _drive(
                node=node,
                strategy=strategy,
                cache=cache,
                account_id=account_id,
                store=store,
                storage=storage,
                supervisor=supervisor,
                submit=submit,
                stop_requested=stop_requested,
                fault=fault,
                emit=emit,
                recovering=recovering,
                stop_by=window.exit_until - _NATIVE_SIGNATURE_SECONDS - 1
                if recovering
                else stop_by,
            )
        except BaseException:
            emit("recovery_required")
            raise
        if flat:
            lease.retire()
        emit("stopped" if flat else "recovery_required")
        return flat
