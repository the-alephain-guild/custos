"""NT process orchestration + ExecutionEngineAdapter CEX/NT implementation
(target: design for three, implement one).

Two hosts satisfy ExecutionEngineProtocol:
- SandboxSimulationHost: deterministic sandbox execution simulator.
- NtTradingNodeHost: real NautilusTrader host. deploy dispatches on
  spec.trading_mode: sandbox (real-time data + locally simulated execution),
  testnet (real Binance exec on the testnet endpoint), and live (real exchange,
  gated by verified artifact, credential, promotion and local live admission).

NautilusTrader is an optional runtime (`nautilus` extra, Python 3.12+). This
module import-guards it so the reconciler can import SandboxSimulationHost on a base install
without NT; NtTradingNodeHost.deploy fails fast if NT is missing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from inspect import isawaitable
from uuid import UUID

from custos.core.engine_protocol import (
    ActivatedEngineArtifactV1,
    ConnectivityState,
    EngineLifecycleAuthority,
    EngineReadinessChecks,
    EngineReadyReceipt,
    EngineStatus,
    EngineTerminalEvent,
    OrderSnapshot,
    PositionSnapshot,
)
from custos.core.log import get_logger
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import (
    SUPPORTED_CURRENCIES,
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactEmitter,
)
from custos.core.runner_fact_producer import (
    RunnerCapitalBasisSnapshot,
    RunnerFactDeployment,
    RunnerFactEventBridge,
    VenueLedgerEvidence,
    strategy_signal_metadata,
)
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
from custos.engines.nautilus.portfolio_snapshot import (
    NautilusPortfolioSnapshotProvider,
)
from custos.engines.nautilus.settlement import settlement_currency_for_pairs
from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder

try:
    from nautilus_trader.adapters.binance import (
        BinanceDataClientFactory,
        BinanceExecutionClientFactory,
    )
    from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveNode, NodeState
    from nautilus_trader.model import PriceType, TraderId
except ImportError:  # nautilus extra absent (audit / paper install) — deploy fails fast
    BinanceDataClientFactory = None
    BinanceExecutionClientFactory = None
    SandboxExecutionClientFactory = None
    Environment = None
    LoggerConfig = None
    LogLevel = None
    LiveNode = None
    NodeState = None
    TraderId = None
    PriceType = None

__all__ = ["SandboxSimulationHost", "NtTradingNodeHost"]

_log = get_logger("custos.nautilus_host")

_DEFAULT_STARTING_BALANCES = ["10_000 USDT"]
_STOP_TIMEOUT_SECS = 30.0

# Venues NtTradingNodeHost can execute. Declared NT-free here so admission can
# query capability on a base install, and kept in sync with the venue-config
# module's wired connectors by a drift-guard test (test_nt_binance_venue.py).
_SUPPORTED_VENUES = frozenset({"binance", "binance_perpetual"})

# Substrings that flag an exception message as potentially carrying credential
# material (NT config repr, adapter auth errors) — such messages are redacted
# before logging so a raw key can never reach the log (non-custodial red line 0.1).
_CREDENTIAL_HINTS = ("api_key", "api_secret", "secret", "authorization")


@dataclass(frozen=True, slots=True)
class _NodeRuntime:
    """What the host keeps of one running node.

    ``run_async`` takes ownership of the node: the cache, the portfolio and the
    control handle have to be captured before the run begins, and reaching for them
    through the node afterwards raises. Everything downstream reads them from here.

    ``strategies`` is what the host handed to the node rather than what the node
    reports back -- 2.0 exposes no trader to enumerate. ``reconciliation_enabled``
    is likewise what the host asked the builder for: 2.0 has no read-back for it,
    which is recorded in the plan as a narrowing of what readiness can prove.
    """

    node: object
    task: asyncio.Task
    handle: object
    cache: object
    portfolio: object
    strategies: tuple
    reconciliation_enabled: bool

    @property
    def is_running(self) -> bool:
        """Whether the node reached Running, which is where nautilus puts it once
        the trader has started -- and therefore once the connect and reconciliation
        phases have passed."""
        state = getattr(self.handle, "state", None)
        return NodeState is not None and state == NodeState.RUNNING


@dataclass(frozen=True, slots=True)
class _ShutdownPolicy:
    position_policy: str
    confirmation_timeout_secs: float


def _shutdown_policy_from_spec(spec: dict) -> _ShutdownPolicy:
    raw = spec.get("shutdown_policy")
    if raw is None:
        # Compatibility is deliberately non-liquidating. Canonical testnet input
        # must opt into flattening, while legacy/live specs preserve exposure.
        return _ShutdownPolicy(position_policy="preserve", confirmation_timeout_secs=30.0)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="python")
    if not isinstance(raw, dict):
        raise RuntimeError("shutdown policy must be an object")
    if set(raw) != {"schema_version", "position_policy", "confirmation_timeout_secs"}:
        raise RuntimeError("shutdown policy field set differs from V1")
    version = raw.get("schema_version")
    position_policy = str(raw.get("position_policy") or "")
    timeout = raw.get("confirmation_timeout_secs")
    if version != 1 or position_policy not in {"preserve", "flatten"}:
        raise RuntimeError("shutdown policy V1 is invalid")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise RuntimeError("shutdown confirmation timeout must be 1..=120 seconds")
    return _ShutdownPolicy(
        position_policy=position_policy,
        confirmation_timeout_secs=float(timeout),
    )


def _environment_for_mode(trading_mode: str) -> object:
    """The nautilus environment a custos trading mode runs in.

    2.0 offers BACKTEST / SANDBOX / LIVE, and custos has sandbox / testnet / live.
    The distinction nautilus draws is where matching happens, not whether the money
    is real: sandbox fills locally, so it is SANDBOX, while testnet places real
    orders against a test endpoint and is therefore LIVE, with the endpoint chosen
    by the adapter's own environment setting.
    """
    if trading_mode == "sandbox":
        return Environment.SANDBOX
    return Environment.LIVE


def _logger_config(spec: dict) -> object:
    """The node's logger config at the spec's requested level."""
    requested = str(spec.get("log_level", "INFO")).upper()
    level = getattr(LogLevel, requested, None)
    if level is None:
        raise RuntimeError(f"log level {requested!r} is not a nautilus level")
    return LoggerConfig(stdout_level=level)


def _apply_nautilus_knobs(builder, nautilus_cfg: dict):
    """Apply the operator-tunable startup knobs the spec carries.

    Same keys as before, so an existing spec keeps working; only the builder methods
    behind them are new. An absent key falls through to the nautilus default.
    """
    knobs = (
        ("timeout_connection", builder.with_timeout_connection),
        ("timeout_reconciliation", builder.with_timeout_reconciliation),
        ("timeout_portfolio", builder.with_timeout_portfolio),
        ("timeout_disconnection", builder.with_timeout_disconnection_secs),
        ("reconciliation_lookback_mins", builder.with_reconciliation_lookback_mins),
    )
    for key, apply in knobs:
        if key in nautilus_cfg:
            builder = apply(nautilus_cfg[key])
    return builder


def _add_exec_client(builder, name: str, factory, config, trading_mode: str):
    """Register the execution client under the shape its mode requires.

    A locally matched venue is registered as simulated; a real endpoint, testnet or
    live, goes through the ordinary path.
    """
    if trading_mode == "sandbox":
        return builder.add_simulated_exec_client(name, factory, config)
    return builder.add_exec_client(name, factory, config)


def _sanitize_exception(exc: Exception) -> dict:
    """Structured, credential-safe fields for logging an exception.

    If the message looks like it could embed credential material, drop it and
    keep only the exception type — a raw key must never be logged.
    """
    msg = str(exc)
    if any(hint in msg.lower() for hint in _CREDENTIAL_HINTS):
        return {
            "error_type": type(exc).__name__,
            "error": "<redacted: contained credential material>",
        }
    return {"error_type": type(exc).__name__, "error": msg}


class SandboxSimulationHost:
    """Deterministic sandbox execution simulator.

    It exercises artifact activation, credential resolution, lifecycle durability,
    readiness, and RunnerFact publication without connecting to a venue. Admission
    restricts it to signed ``sandbox`` commands and canonical supported connectors.
    Testnet and live always require ``NtTradingNodeHost``.

    It satisfies ExecutionEngineProtocol so the lifecycle supervisor can take it
    as a dependency and admission can reject supports_trading_mode straight away.
    It answers one method beyond that protocol — the offline lane's attachment
    query, which is declared on OfflineEngine.
    """

    def __init__(self) -> None:
        self._lifecycle_authorities: dict[str, EngineLifecycleAuthority] = {}

    async def deploy(
        self,
        spec: dict,
        credential: dict,
        artifact: ActivatedEngineArtifactV1,
    ) -> str:
        deployment_instance_id = str(spec.get("deployment_instance_id") or "")
        self._lifecycle_authorities[deployment_instance_id] = EngineLifecycleAuthority.from_spec(
            spec
        )
        _log.info(
            "sandbox_simulation_engine_deployed",
            deployment_instance_id=deployment_instance_id,
            artifact_activation_id=artifact.activation_id,
        )
        return f"container-{deployment_instance_id}"

    async def reconfigure(self, spec: dict) -> None:
        _log.info(
            "sandbox_simulation_engine_reconfigured",
            deployment_instance_id=spec.get("deployment_instance_id"),
        )

    async def stop(self, deployment_instance_id: str) -> None:
        self._lifecycle_authorities.pop(deployment_instance_id, None)
        _log.info(
            "sandbox_simulation_engine_stopped",
            deployment_instance_id=deployment_instance_id,
        )

    def attached(self, deployment_instance_id: str) -> bool:
        # The simulation lives in this process, so what it is holding is exactly
        # what this process deployed and has not stopped.
        return deployment_instance_id in self._lifecycle_authorities

    async def deployment_ready(self, deployment_instance_id: str) -> bool:
        # Nothing to wait for: the simulation is in-process and has no venue state to
        # reconcile, so a deployed instance is a ready one. It answers rather than
        # leaving the question unanswered, which would have the exposure guard record
        # the whole sandbox lane as an engine that cannot report readiness.
        return deployment_instance_id in self._lifecycle_authorities

    def supports_trading_mode(self, mode: str) -> bool:
        # This host is an explicit local simulation boundary. It may exercise the
        # full lifecycle in sandbox, but must never claim a real-venue mode.
        return mode == "sandbox"

    def supports_venue(self, venue: str) -> bool:
        return venue.lower() in _SUPPORTED_VENUES

    async def get_open_notional(self, deployment_instance_id: str) -> Decimal:
        # The simulator holds no positions, so its observed exposure is exactly zero.
        return Decimal("0")

    async def check_engine_connected(self, deployment_instance_id: str) -> ConnectivityState:
        # The in-process simulator has no external connection to lose.
        return ConnectivityState(
            data_connected=True, exec_connected=True, checked_at_epoch_s=time.time()
        )

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        # Stub holds no positions — flatten is a no-op, logged so the breaker's
        # trip is still observable on a paper/sim runner.
        _log.info(
            "sandbox_simulation_positions_flattened",
            deployment_instance_id=deployment_instance_id,
            reason=reason,
        )

    async def get_positions(self, deployment_instance_id: str) -> list[PositionSnapshot]:
        # Stub holds no positions — the snapshot publisher sees an empty list.
        return []

    async def get_orders(self, deployment_instance_id: str) -> list[OrderSnapshot]:
        return []

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        # Stub is always healthy with zero exposure; every money field is a
        # Decimal so the money-invariant guard on EngineStatus stays green.
        return EngineStatus(
            phase="running",
            position_count=0,
            order_count=0,
            open_notional=Decimal("0"),
            peak_equity=Decimal("0"),
            current_equity=Decimal("0"),
            drawdown_pct=Decimal("0"),
        )

    async def wait_ready(
        self,
        authority: EngineLifecycleAuthority,
        *,
        timeout_secs: float,
    ) -> EngineReadyReceipt:
        stored = self._lifecycle_authorities.get(str(authority.deployment_instance_id))
        if stored != authority:
            raise RuntimeError("sandbox simulation lifecycle authority is not deployed")
        return EngineReadyReceipt.from_authority(
            authority,
            checks=EngineReadinessChecks.all_ready(),
            ready_at_ns=time.time_ns(),
        )

    async def wait_terminal(
        self,
        authority: EngineLifecycleAuthority,
    ) -> EngineTerminalEvent:
        await asyncio.Event().wait()
        raise AssertionError("sandbox simulation terminal wait unexpectedly returned")


class NtTradingNodeHost:
    """Real NautilusTrader host for Binance sandbox / testnet / live deployments.

    deploy assembles a TradingNode (Binance data + an execution client chosen by
    spec.trading_mode) and runs it in a background asyncio task so the reconcile
    loop is never blocked. stop tears the node down gracefully with a bounded
    timeout.

    non-custodial red line 0.1: the decrypted credential is used only to build the
    NT data-client config and is never stored on the host, logged, or published.

    Signed observations are opt-in: pass a RunnerFact emitter and capability
    receipt to wire the message-bus bridge. Deployment commands are the only
    NATS input; execution facts leave through the signed RunnerFact outbox.
    """

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        runner_id: str | None = None,
        runner_fact_emitter: RunnerFactEmitter | None = None,
        capability_receipt: RunnerCapabilityReceipt | None = None,
        portfolio_snapshot_provider: NautilusPortfolioSnapshotProvider | None = None,
        runner_safety_boundary_factory: Callable[[dict], object] | None = None,
    ) -> None:
        # deployment_instance_id -> its running node and the state captured with it.
        # Never holds credentials.
        self._active_nodes: dict[str, _NodeRuntime] = {}
        # deployment_instance_id -> why its observations stopped being trustworthy.
        # An event sink that failed leaves a fact unrecorded or a reservation held,
        # and the rust dispatch discards what the callback raised, so this is the
        # only place that failure can still reach anyone.
        self._event_forwarding_failures: dict[str, str] = {}
        self._lifecycle_authorities: dict[str, EngineLifecycleAuthority] = {}
        # deployment_instance_id -> signed fact scope plus independent venue ledger adapter.
        self._runner_fact_contexts: dict[str, tuple[RunnerFactDeployment, object | None]] = {}
        self._runner_safety_boundaries: dict[str, RunnerReservationBoundary] = {}
        # A real venue credential scope represents one account-level order/position
        # stream. Two active deployment nodes on the same scope would both observe and
        # mutate the same net position while claiming instance-scoped facts. Sandbox
        # nodes have independent simulated accounts and intentionally do not participate.
        self._execution_account_partitions: dict[str, tuple[str, str]] = {}
        # Decimal equity high-water mark per instance so get_engine_status can
        # report drawdown percentage over time. Never a float (red line 0.4).
        self._peak_equity: dict[str, Decimal] = {}
        # deployment_instance_id -> the currency this deployment settles in, derived from
        # its pairs at deploy. The guards below read equity, and equity only has one
        # answer once a currency is named: a funded account holds several at once, so
        # without this they go unreliable on any real account and fail closed.
        self._settlement_currencies: dict[str, str] = {}
        self._shutdown_policies: dict[str, _ShutdownPolicy] = {}
        self._stop_timeout_secs = _STOP_TIMEOUT_SECS
        self._shutdown_poll_secs = 0.2
        self._shutdown_stable_polls = 3
        self._shutdown_close_retry_secs = 2.0
        self._tenant_id = tenant_id
        self._runner_id = runner_id
        self._runner_fact_emitter = runner_fact_emitter
        self._capability_receipt = capability_receipt
        self._runner_safety_boundary_factory = runner_safety_boundary_factory
        self._portfolio_snapshot_provider = portfolio_snapshot_provider or (
            NautilusPortfolioSnapshotProvider(price_type_mid=PriceType.MID if PriceType else None)
        )

    @staticmethod
    def _ensure_nt_available() -> None:
        if LiveNode is None:
            raise RuntimeError(
                "NautilusTrader not installed — install `custos-runner[nautilus]` "
                "(needs Python 3.12+) to run NtTradingNodeHost"
            )

    def supports_trading_mode(self, mode: str) -> bool:
        return mode in {"sandbox", "testnet", "live"}

    def supports_venue(self, venue: str) -> bool:
        return venue.lower() in _SUPPORTED_VENUES

    def _claim_execution_account_partition(self, spec: dict) -> None:
        mode = str(spec.get("trading_mode") or "sandbox").lower()
        if mode == "sandbox":
            return
        if mode not in {"testnet", "live"}:
            raise RuntimeError("execution account partition has an invalid trading mode")
        instance_id = str(spec.get("deployment_instance_id") or "").strip()
        scope = spec.get("credential_scope")
        if isinstance(scope, dict):
            scope_id = str(scope.get("scope_id") or "").strip()
        else:
            scope_id = str(getattr(scope, "scope_id", "") or "").strip()
        if not instance_id or not scope_id:
            raise RuntimeError(
                "real-venue deployment requires instance and credential-scope identity"
            )
        partition = (mode, scope_id)
        conflicting_instance = next(
            (
                owner
                for owner, owned_partition in self._execution_account_partitions.items()
                if owner != instance_id and owned_partition == partition
            ),
            None,
        )
        if conflicting_instance is not None:
            raise RuntimeError(
                "credential scope already has an active testnet/live deployment instance"
            )
        self._execution_account_partitions[instance_id] = partition

    def _release_execution_account_partition(self, deployment_instance_id: str) -> None:
        self._execution_account_partitions.pop(deployment_instance_id, None)

    def _require_the_only_node(self, deployment_instance_id: str) -> None:
        """Refuse a second node on this runner's event loop.

        Nautilus 2.0 binds the runner's senders and the message bus to thread-local
        storage, so two hosted nodes on one loop would deliver each other's events
        rather than fail. Nautilus refuses the second ``run_async`` for that reason;
        refusing here names the deployment that already holds the loop, and does it
        before a node is built and has to be disposed again.
        """
        held = [
            instance for instance, runtime in self._active_nodes.items() if not runtime.task.done()
        ]
        if held:
            raise RuntimeError(
                f"deployment instance {held[0]!r} already holds this runner's event loop; "
                "nautilus runs one live node per loop, so stop it before deploying "
                f"{deployment_instance_id!r}"
            )

    async def deploy(
        self,
        spec: dict,
        credential: dict,
        artifact: ActivatedEngineArtifactV1,
    ) -> str:
        self._ensure_nt_available()
        spec_id = str(spec["deployment_spec_id"])
        deployment_instance_id = str(spec["deployment_instance_id"])
        lifecycle_authority = EngineLifecycleAuthority.from_spec(spec)
        shutdown_policy = _shutdown_policy_from_spec(spec)
        if deployment_instance_id in self._active_nodes:
            # Idempotency guard: re-deploying a live spec must go through stop first
            # (structural changes are stop + re-deploy), never silently replace it.
            raise RuntimeError(
                f"deployment instance {deployment_instance_id!r} already deployed; call stop first"
            )

        if not artifact.activation_id.strip():
            raise RuntimeError("verified artifact activation identity is required")
        create_strategy = getattr(artifact, "create_strategy", None)
        strategy = create_strategy() if callable(create_strategy) else artifact.strategy

        # Imported lazily: venue_binance imports NautilusTrader at module top.
        from custos.engines.nautilus import venue_binance as venue

        trading_mode = str(spec.get("trading_mode") or "sandbox").lower()
        data_cfg = venue.build_data_client_config(
            spec, credential, venue.data_environment_for_mode(trading_mode)
        )
        exec_cfg, exec_factory, reconciliation = self._build_exec_plan(
            trading_mode, spec, credential, venue
        )
        runner_safety_boundary = await self._build_runner_safety_boundary(spec)

        # The runner and the message bus are both thread-local in 2.0, so two hosted
        # nodes on one event loop would cross-wire each other's events rather than
        # fail. Nautilus refuses the second run for that reason; this refuses it here,
        # where the message can say which deployment already holds the loop instead of
        # naming a generic nautilus constraint. Running more than one deployment per
        # runner needs a thread or a process per node, which is not this change.
        self._require_the_only_node(deployment_instance_id)

        # ps runner.py._create_node_config exposes the NT startup timeouts and the
        # reconciliation lookback to strategy authors; custos accepts the same
        # knobs via a plain nautilus_config dict-key so operators can tune a slow
        # exchange without needing a code change. Every knob is optional — an
        # absent key falls through to the NT internal default.
        nautilus_cfg = spec.get("nautilus_config") or {}

        # Claim before constructing a node. A conflicting real-venue deployment must
        # fail without creating and then disposing a node.
        self._claim_execution_account_partition(spec)

        try:
            builder = LiveNode.builder(
                self._trader_id(deployment_instance_id),
                TraderId(self._trader_id(deployment_instance_id)),
                _environment_for_mode(trading_mode),
            )
            builder = builder.with_logging(_logger_config(spec))
            # Real venues reconcile against exchange account state; the sandbox has none.
            builder = builder.with_reconciliation(reconciliation)
            builder = _apply_nautilus_knobs(builder, nautilus_cfg)
            builder = builder.add_data_client(
                venue.BINANCE_VENUE,
                BinanceDataClientFactory(),
                data_cfg,
            )
            builder = _add_exec_client(
                builder,
                venue.BINANCE_VENUE,
                exec_factory,
                exec_cfg,
                trading_mode,
            )
            node = builder.build()
        except Exception as exc:  # noqa: BLE001 — reconciler maps this to degraded status
            _log.error(
                "nt_startup_failure",
                deployment_instance_id=deployment_instance_id,
                spec_id=spec_id,
                **_sanitize_exception(exc),
            )
            self._release_execution_account_partition(deployment_instance_id)
            raise

        fact_context = self._build_runner_fact_context(
            spec,
            credential,
            runtime_strategy=strategy,
        )
        try:
            self._attach_runtime_bridges(
                deployment_instance_id,
                strategy,
                node.cache,
                fact_context,
                runner_safety_boundary,
            )
        except Exception:
            self._release_execution_account_partition(deployment_instance_id)
            self._dispose_node(deployment_instance_id, node)
            raise
        try:
            if fact_context is not None:
                self._runner_fact_contexts[fact_context[0].deployment_instance_id] = fact_context
            if runner_safety_boundary is not None:
                self._runner_safety_boundaries[deployment_instance_id] = runner_safety_boundary
            node.add_strategy(strategy)
            settlement_currency = settlement_currency_for_pairs(spec.get("pairs") or [])
            # Captured before the run: run_async moves the node into the awaitable, and
            # reaching for these through the node afterwards raises.
            handle, cache, portfolio = node.handle(), node.cache, node.portfolio
            task = asyncio.create_task(node.run_async())
        except Exception:
            self._runner_fact_contexts.pop(deployment_instance_id, None)
            self._runner_safety_boundaries.pop(deployment_instance_id, None)
            self._event_forwarding_failures.pop(deployment_instance_id, None)
            self._release_execution_account_partition(deployment_instance_id)
            self._dispose_node(deployment_instance_id, node)
            raise
        task.add_done_callback(
            lambda task, instance_id=deployment_instance_id: self._on_node_task_done(
                instance_id, task
            )
        )
        self._active_nodes[deployment_instance_id] = _NodeRuntime(
            node=node,
            task=task,
            handle=handle,
            cache=cache,
            portfolio=portfolio,
            strategies=(strategy,),
            reconciliation_enabled=reconciliation,
        )
        self._lifecycle_authorities[deployment_instance_id] = lifecycle_authority
        # Derived from the pairs rather than the open positions: at this moment there are
        # no positions, and the startup guards read equity immediately.
        self._settlement_currencies[deployment_instance_id] = settlement_currency
        self._shutdown_policies[deployment_instance_id] = shutdown_policy

        _log.info(
            "nt_deploy_started",
            deployment_instance_id=deployment_instance_id,
            spec_id=spec_id,
            trading_mode=trading_mode,
            connector=spec.get("connector"),
            permission_scope=credential.get("permission_scope"),
            artifact_activation_id=artifact.activation_id,
            strategy=type(strategy).__name__,
            shutdown_position_policy=shutdown_policy.position_policy,
        )
        return deployment_instance_id

    def _build_exec_plan(self, trading_mode: str, spec: dict, credential: dict, venue):
        """Resolve (exec_config, exec_factory, reconciliation) for the trading mode.

        sandbox fills locally against live prices (no exchange contact); testnet /
        live place real orders on the Binance testnet / live endpoints. Real venues
        reconcile against exchange account state, the sandbox has none. A live plan
        requires control-plane-signed promotion evidence inside the accepted spec.
        """
        if trading_mode == "sandbox":
            starting_balances = (spec.get("sandbox") or {}).get(
                "starting_balances"
            ) or _DEFAULT_STARTING_BALANCES
            exec_cfg = venue.build_exec_client_config_sandbox(spec, credential, starting_balances)
            return exec_cfg, SandboxExecutionClientFactory(), False
        if trading_mode == "testnet":
            exec_cfg = venue.build_exec_client_config_testnet(spec, credential)
            return exec_cfg, BinanceExecutionClientFactory(), True
        if trading_mode == "live":
            _log.warning(
                "nt_live_deploy_requested",
                spec_id=spec.get("deployment_spec_id"),
                connector=spec.get("connector"),
                promotion_id=spec.get("promotion_id"),
            )
            exec_cfg = venue.build_exec_client_config_live(spec, credential)
            return exec_cfg, BinanceExecutionClientFactory(), True
        raise RuntimeError(
            f"unsupported trading_mode {trading_mode!r} (expected sandbox / testnet / live)"
        )

    async def _build_runner_safety_boundary(self, spec: dict):
        """Build the reservation boundary this deployment's orders answer to.

        It no longer wraps the execution client: 2.0 has no seat there for python,
        so the gate goes on the strategy instead (see ``runner_safety``). What is
        built here is the boundary; ``_attach_runtime_bridges`` puts it in front of
        the strategy once the node exists and its cache can be read.
        """
        if self._runner_safety_boundary_factory is None:
            return None
        boundary = self._runner_safety_boundary_factory(spec)
        if isawaitable(boundary):
            boundary = await boundary
        if boundary is None:
            raise RuntimeError("runner safety boundary factory returned no boundary")
        return boundary

    def _attach_runtime_bridges(
        self,
        deployment_instance_id: str,
        strategy,
        cache,
        fact_context,
        runner_safety_boundary=None,
    ) -> None:
        """Wire the runner onto the strategy, or refuse the deploy.

        2.0 delivers order and position events nowhere else -- the internal message
        bus these bridges used to subscribe to has no python surface. Installing the
        forwarding is the host's job rather than the strategy's: the strategy arrives
        from a signed artifact and nothing constrains its ancestry, so a bridge that
        lived in a base class would quietly not exist for an artifact that did not
        inherit it.

        Safety first, then facts, matching the order the bus subscriptions were
        registered in.
        """
        forwarder = StrategyEventForwarder(
            deployment_instance_id=deployment_instance_id,
            on_sink_failure=lambda sink, reason: self._record_forwarding_failure(
                deployment_instance_id, sink, reason
            ),
        )
        fact_bridge: RunnerFactEventBridge | None = None
        if runner_safety_boundary is not None:
            runner_safety_boundary.bootstrap(forwarder)
        if fact_context is not None and self._runner_fact_emitter is not None:
            if self._capability_receipt is None:
                raise RuntimeError("RunnerFact bridge lacks its capability receipt")
            fact_bridge = RunnerFactEventBridge(
                emitter=self._runner_fact_emitter,
                deployment=fact_context[0],
                runtime_log_emitter=RunnerRuntimeLogEmitter(
                    emitter=self._runner_fact_emitter,
                    capability=self._capability_receipt,
                    redactor=RuntimeLogRedactor(),
                ),
            )
            fact_bridge.bootstrap(forwarder)
        forwarder.install(strategy)
        if runner_safety_boundary is not None:
            self._install_order_gate(strategy, cache, runner_safety_boundary, fact_bridge)

    def _install_order_gate(self, strategy, cache, boundary, fact_bridge) -> None:
        """Put the reservation gate in front of the strategy's outbound orders.

        A refusal produces no nautilus event -- the order is never submitted, and 2.0
        only publishes an order's initialized event inside submit -- so the refusal is
        routed to the signed fact stream here. Without a fact bridge there is nowhere
        for it to go, and the deploy is refused rather than run with a gate that
        contains silently.
        """
        from custos.engines.nautilus.runner_safety import (
            NautilusCachedOrderSemantics,
            RunnerSafetyOrderGate,
            install_order_gate,
        )

        if fact_bridge is None:
            raise RuntimeError(
                "runner safety gate requires the signed fact stream: a refusal it does "
                "not record is exposure contained without evidence"
            )
        boundary.bind_runtime(semantics=NautilusCachedOrderSemantics(cache))
        install_order_gate(
            strategy,
            RunnerSafetyOrderGate(
                boundary=boundary,
                on_refusal=lambda refusal: fact_bridge.record_local_refusal(
                    client_order_id=refusal.client_order_id,
                    instrument_id=refusal.instrument_id,
                    side=refusal.side,
                    reason_code=refusal.reason_code,
                ),
            ),
        )

    def _record_forwarding_failure(
        self,
        deployment_instance_id: str,
        sink: str,
        reason: str,
    ) -> None:
        """Remember that an event never reached one of the runner's sinks.

        A sink that failed left a fact unrecorded or a reservation held, so what this
        deployment reports about itself is no longer trustworthy. get_engine_status
        says so from here on, which is what makes the failure reach anyone at all --
        the rust dispatch discarded the exception.
        """
        self._event_forwarding_failures.setdefault(deployment_instance_id, reason)
        _log.error(
            "nt_event_forwarding_degraded",
            deployment_instance_id=deployment_instance_id,
            sink=sink,
            reason=reason,
        )

    def _build_runner_fact_context(
        self,
        spec: dict,
        credential: dict,
        *,
        runtime_strategy: object | None = None,
    ):
        if self._runner_fact_emitter is None or self._capability_receipt is None:
            return None
        strategy_id = spec.get("strategy_id")
        if not strategy_id:
            raise RuntimeError("validated DeploymentSpec lost its canonical strategy_id")
        spec_id = spec["deployment_spec_id"]
        deployment_instance_id = str(spec.get("deployment_instance_id") or "").strip()
        deployment_spec_digest = str(spec.get("deployment_spec_digest") or "").strip()
        if not deployment_instance_id or not deployment_spec_digest:
            raise RuntimeError(
                "validated DeploymentSpec lacks explicit DeploymentInstance/spec digest authority"
            )
        required_projectors = ["settlement", "risk", "health"]
        if spec["trading_mode"] in {"testnet", "live"}:
            required_projectors.append("reconciliation")
        self._capability_receipt.require_scope_bindings(
            projectors=required_projectors,
            trading_mode=str(spec["trading_mode"]),
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=spec_id,
            deployment_spec_digest=deployment_spec_digest,
            strategy_id=strategy_id,
        )
        authority = RunnerFactAuthority(
            tenant_id=self._tenant_id or "",
            trading_mode=str(spec["trading_mode"]),
            runner_id=self._capability_receipt.runner_id,
            deployment_instance_id=UUID(deployment_instance_id),
            deployment_spec_id=spec_id,
            deployment_spec_digest=deployment_spec_digest,
            generation=int(spec["generation"]),
            strategy_id=strategy_id,
            capability_version_id=self._capability_receipt.capability_version_id,
            capability_version=self._capability_receipt.capability_version,
            capability_manifest_digest=self._capability_receipt.manifest_digest,
        )
        pairs = spec.get("pairs") or []
        currencies = {str(pair).upper().replace("/", "-").split("-")[-1] for pair in pairs}
        if len(currencies) != 1:
            raise RuntimeError("RunnerFact v1 requires one settlement currency per deployment")
        currency = next(iter(currencies))
        if currency not in SUPPORTED_CURRENCIES:
            raise RuntimeError(f"settlement currency {currency!r} is outside RunnerFact v1")
        provider = None
        if spec["trading_mode"] in {"testnet", "live"}:
            from custos.engines.nautilus.binance_ledger import BinanceVenueLedgerSource

            provider = BinanceVenueLedgerSource(spec=spec, credential=credential)
        strategy_version, timeframe = strategy_signal_metadata(
            spec,
            runtime_strategy=runtime_strategy,
        )
        coverage_started_at = None
        if spec.get("reconciliation_coverage_started_at") is not None:
            try:
                parsed_coverage_start = datetime.fromisoformat(
                    str(spec["reconciliation_coverage_started_at"]).replace("Z", "+00:00")
                )
                if (
                    parsed_coverage_start.tzinfo is None
                    or parsed_coverage_start.utcoffset() is None
                ):
                    raise ValueError("timestamp has no UTC offset")
                coverage_started_at = parsed_coverage_start.astimezone(UTC)
            except ValueError as exc:
                raise RuntimeError(
                    "reconciliation coverage start is not an ISO-8601 timestamp"
                ) from exc
        deployment = RunnerFactDeployment(
            authority=authority,
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=str(spec_id),
            deployment_spec_digest=deployment_spec_digest,
            venue="BINANCE",
            currency=currency,
            reconciliation_available=provider is not None,
            strategy_version=strategy_version,
            timeframe=timeframe,
            reconciliation_coverage_started_at=coverage_started_at,
            valuation_checkpoint_available=(
                self._capability_receipt.capability_manifest.get("runner_fact_contracts", {})
                .get("reconciliation", {})
                .get("valuation_checkpoint")
                == "v1"
            ),
        )
        return deployment, provider

    def _declared_currency(self, deployment_instance_id: str) -> str | None:
        """The currency this instance settles in, or None with a record of why not.

        deploy registers it beside the node and stop drops both, so an active instance
        without one means those two fell out of step. Returning None degrades to the
        pre-fix behaviour -- equity goes unreliable on any multi-currency account and the
        guards fail closed -- which is safe but must never happen quietly.
        """
        currency = self._settlement_currencies.get(deployment_instance_id)
        if currency is None:
            _log.warning(
                "settlement_currency_unregistered",
                deployment_instance_id=deployment_instance_id,
            )
        return currency

    async def stop(self, deployment_instance_id: str) -> None:
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            # Idempotent: stopping an unknown / already-stopped spec is a no-op.
            _log.info(
                "nt_stop_noop_unknown_instance",
                deployment_instance_id=deployment_instance_id,
            )
            return

        task = runtime.task
        policy = self._shutdown_policies.get(
            deployment_instance_id,
            _ShutdownPolicy(position_policy="preserve", confirmation_timeout_secs=30.0),
        )
        # Before the node is asked to stop, while the strategy is still Running and
        # therefore still receiving the events these bridges depend on.
        await self._apply_shutdown_policy(deployment_instance_id, runtime, policy)
        try:
            # The handle is how a hosted run is stopped: run_async owns the node, so
            # calling stop on the node itself would be refused. Awaiting the task is
            # what waits for the shutdown sequence to finish.
            runtime.handle.stop()
            await asyncio.wait_for(asyncio.shield(task), timeout=self._stop_timeout_secs)
        except TimeoutError:
            _log.error(
                "nt_stop_timeout",
                deployment_instance_id=deployment_instance_id,
                timeout_secs=self._stop_timeout_secs,
            )
        except Exception as exc:  # noqa: BLE001 — a node that died on its own is still stopped
            _log.warning(
                "nt_stop_run_task_error",
                deployment_instance_id=deployment_instance_id,
                **_sanitize_exception(exc),
            )
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaping the run task
                pass
            # Only once the run has ended: run_async holds the node until then, and
            # 2.0 disposal releases the kernel without touching the runner's loop.
            self._dispose_node(deployment_instance_id, runtime.node)
            self._peak_equity.pop(deployment_instance_id, None)
            self._settlement_currencies.pop(deployment_instance_id, None)
            self._runner_fact_contexts.pop(deployment_instance_id, None)
            self._runner_safety_boundaries.pop(deployment_instance_id, None)
            self._event_forwarding_failures.pop(deployment_instance_id, None)
            self._release_execution_account_partition(deployment_instance_id)
            self._active_nodes.pop(deployment_instance_id, None)
            self._lifecycle_authorities.pop(deployment_instance_id, None)
            self._shutdown_policies.pop(deployment_instance_id, None)
        _log.info("nt_stop_completed", deployment_instance_id=deployment_instance_id)

    def _dispose_node(self, deployment_instance_id: str, node: object) -> None:
        """Release the node's kernel once its run has ended.

        The 1.x version of this took care to avoid ``TradingNode.dispose()``, which
        assumed the node owned its event loop and would cancel every task on it. 2.0
        disposal closes external ingress, disposes the kernel and marks the handle
        stopped -- it does not reach into the host's loop -- so the care is no longer
        warranted and the plain call is the whole of it.

        Disposal failing does not leave the deployment running, and stop must finish
        cleaning up regardless, so this reports rather than propagates.
        """
        try:
            node.dispose()
        except Exception as exc:  # noqa: BLE001 — the run has already ended; report and continue
            _log.error(
                "nt_node_disposal_failed",
                deployment_instance_id=deployment_instance_id,
                **_sanitize_exception(exc),
            )

    async def _apply_shutdown_policy(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        policy: _ShutdownPolicy,
    ) -> None:
        strategies = runtime.strategies
        for strategy in strategies:
            prepare = getattr(strategy, "prepare_shutdown", None)
            if callable(prepare):
                prepare(policy.position_policy)
            else:
                pause = getattr(strategy, "pause", None)
                if callable(pause):
                    pause()

        if policy.position_policy == "flatten":
            await self._flatten_and_confirm_shutdown(
                deployment_instance_id,
                runtime,
                strategies,
                timeout_secs=policy.confirmation_timeout_secs,
            )
        else:
            await self._preserve_and_confirm_shutdown(
                deployment_instance_id,
                runtime,
                strategies,
                timeout_secs=policy.confirmation_timeout_secs,
            )

    @staticmethod
    def _open_venue_state(runtime: _NodeRuntime) -> tuple[list, list]:
        try:
            positions = list(runtime.cache.positions_open())
            orders = list(runtime.cache.orders_open())
        except Exception as exc:  # noqa: BLE001 - an unreadable venue cache is not confirmation
            raise RuntimeError("shutdown venue state could not be confirmed") from exc
        return positions, orders

    @staticmethod
    def _instrument_ids(positions: list, orders: list) -> set:
        return {
            instrument_id
            for item in (*positions, *orders)
            if (instrument_id := getattr(item, "instrument_id", None)) is not None
        }

    async def _preserve_and_confirm_shutdown(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        strategies: tuple,
        *,
        timeout_secs: float,
    ) -> None:
        """Preserve positions/protection while removing risk-increasing orders."""

        deadline = asyncio.get_running_loop().time() + timeout_secs
        while asyncio.get_running_loop().time() < deadline:
            _positions, orders = self._open_venue_state(runtime)
            risk_increasing = [
                order for order in orders if not bool(getattr(order, "is_reduce_only", False))
            ]
            if not risk_increasing:
                _log.info(
                    "nt_shutdown_preserve_confirmed",
                    deployment_instance_id=deployment_instance_id,
                    protective_order_count=len(orders),
                )
                return
            else:
                canceler = next(
                    (
                        strategy
                        for strategy in strategies
                        if callable(getattr(strategy, "cancel_order", None))
                    ),
                    None,
                )
                if canceler is None:
                    raise RuntimeError(
                        "shutdown preserve cannot cancel risk-increasing venue orders"
                    )
                for order in risk_increasing:
                    canceler.cancel_order(order)
            await asyncio.sleep(self._shutdown_poll_secs)
        raise RuntimeError("shutdown preserve confirmation timed out")

    async def _flatten_and_confirm_shutdown(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        strategies: tuple,
        *,
        timeout_secs: float,
    ) -> None:
        """Cancel, flatten and require a stable zero venue cache before disposal."""

        deadline = asyncio.get_running_loop().time() + timeout_secs
        stable_polls = 0
        last_close_request: float | None = None
        while asyncio.get_running_loop().time() < deadline:
            now = asyncio.get_running_loop().time()
            positions, orders = self._open_venue_state(runtime)
            if not positions and not orders:
                stable_polls += 1
                if stable_polls >= self._shutdown_stable_polls:
                    _log.info(
                        "nt_shutdown_flatten_confirmed",
                        deployment_instance_id=deployment_instance_id,
                    )
                    return
                await asyncio.sleep(self._shutdown_poll_secs)
                continue

            stable_polls = 0
            instrument_ids = self._instrument_ids(positions, orders)
            if not instrument_ids:
                raise RuntimeError("shutdown venue state has no instrument identity")

            # Resting protection is canceled before the close so Binance does not
            # reject the exact reduce-only market close with -2022. A just-submitted
            # close gets a short acknowledgement window before any cancellation retry.
            close_ack_pending = (
                last_close_request is not None
                and now - last_close_request < self._shutdown_close_retry_secs
            )
            if orders and not close_ack_pending:
                canceler = next(
                    (
                        strategy
                        for strategy in strategies
                        if callable(getattr(strategy, "cancel_all_orders", None))
                    ),
                    None,
                )
                if canceler is None:
                    raise RuntimeError("shutdown flatten cannot cancel venue orders")
                for instrument_id in instrument_ids:
                    canceler.cancel_all_orders(instrument_id)
            elif not orders and positions and not close_ack_pending:
                await self.flatten_positions(deployment_instance_id, "shutdown_policy")
                last_close_request = now
            await asyncio.sleep(self._shutdown_poll_secs)
        positions, orders = self._open_venue_state(runtime)
        _log.error(
            "nt_shutdown_flatten_unconfirmed",
            deployment_instance_id=deployment_instance_id,
            position_count=len(positions),
            order_count=len(orders),
            timeout_secs=timeout_secs,
        )
        raise RuntimeError("shutdown flatten confirmation timed out")

    def attached(self, deployment_instance_id: str) -> bool:
        # Answered from the live node registry rather than the authority record:
        # only the former holds a running node, and it is dropped both by stop and
        # by a node loop that ended on its own. Neither survives this process, and
        # neither should — an instance nobody is holding must be deployed, not
        # reconfigured.
        #
        # The run task is checked too, because the registry is cleaned by a done
        # callback and a callback is scheduled rather than immediate. Between a
        # node loop finishing and that cleanup there is an entry with nothing
        # running behind it, and reporting it as held is the same false success
        # this query exists to prevent.
        runtime = self._active_nodes.get(deployment_instance_id)
        return runtime is not None and not runtime.task.done()

    async def wait_ready(
        self,
        authority: EngineLifecycleAuthority,
        *,
        timeout_secs: float,
    ) -> EngineReadyReceipt:
        if authority.trading_mode == "live":
            raise RuntimeError(
                "live readiness remains fail closed until engine supervision is complete"
            )
        deadline = asyncio.get_running_loop().time() + timeout_secs
        instance_id = str(authority.deployment_instance_id)
        while asyncio.get_running_loop().time() < deadline:
            if self._lifecycle_authorities.get(instance_id) != authority:
                raise RuntimeError("engine readiness authority differs from deployed instance")
            runtime = self._active_nodes.get(instance_id)
            if runtime is None:
                raise RuntimeError("engine task exited before readiness")
            checks = await self._readiness_checks(authority, runtime)
            if checks.ready:
                return EngineReadyReceipt.from_authority(
                    authority,
                    checks=checks,
                    ready_at_ns=time.time_ns(),
                )
            await asyncio.sleep(min(0.05, max(0.001, timeout_secs / 10)))
        raise TimeoutError("engine did not satisfy readiness before the deadline")

    async def deployment_ready(self, deployment_instance_id: str) -> bool:
        """Whether this deployment has crossed every ready boundary, asked without waiting.

        The offline lane's exposure guard runs on its own clock and cannot block on
        ``wait_ready``, but it must not question a deployment mid-startup: on 2026-08-01
        it tripped on ``portfolio_equity_missing`` 116ms before the account balance
        arrived, while NautilusTrader was still inside the startup reconciliation it
        announces in advance.

        Deliberately the same computation ``wait_ready`` loops on. A guard that decided
        readiness for itself would be a second opinion about one fact, and the two would
        drift apart the first time either changed.

        An unknown deployment is not ready -- fail closed on this side too.
        """
        authority = self._lifecycle_authorities.get(deployment_instance_id)
        runtime = self._active_nodes.get(deployment_instance_id)
        if authority is None or runtime is None:
            return False
        checks = await self._readiness_checks(authority, runtime)
        return checks.ready

    async def _readiness_checks(
        self,
        authority: EngineLifecycleAuthority,
        runtime: _NodeRuntime,
    ) -> EngineReadinessChecks:
        """Ask the engine what it has actually finished, field by field.

        Every field here used to be derivable from the deployment's trading mode, which
        is to say from what it was asked to do rather than from what it did. Three of the
        seven were: see the tests for what each one now proves.

        One of them is now weaker than it was. 2.0 exposes no execution engine to ask
        whether reconciliation is on, so this reports what the builder was told rather
        than what the engine confirms. That is the deployment's configuration, not its
        behaviour -- the plan records the narrowing.
        """
        connectivity = await self.check_engine_connected(str(authority.deployment_instance_id))
        portfolio = runtime.portfolio

        # Reaching Running is the closest thing to a reconciliation receipt 2.0 offers:
        # the node enters that state in ``finish_startup_trader``, after the connect
        # phase and after the trader has started. A failed reconciliation aborts
        # startup instead, so a Running node means the step was passed -- either
        # reconciled, or legitimately skipped.
        node_running = runtime.is_running
        strategies = runtime.strategies

        # Skipping is only legitimate in sandbox, which fills locally against live
        # prices and has no exchange account to reconcile against (see
        # ``_build_exec_plan``). On testnet and live it is a misconfiguration, and
        # the node starts either way -- so passing it needs its own check.
        reconciliation_required = authority.trading_mode != "sandbox"
        reconciliation_enabled = runtime.reconciliation_enabled

        return EngineReadinessChecks(
            node_task_alive=not runtime.task.done(),
            data_connectivity_ready=connectivity.data_connected,
            execution_connectivity_ready=connectivity.exec_connected,
            portfolio_initialized=bool(portfolio is not None and portfolio.initialized),
            reconciliation_initialized=(
                node_running and (reconciliation_enabled or not reconciliation_required)
            ),
            strategy_accepting_lifecycle=bool(strategies)
            and all(strategy.is_running for strategy in strategies),
            mandatory_capabilities_active=authority.trading_mode in {"sandbox", "testnet"},
        )

    async def wait_terminal(
        self,
        authority: EngineLifecycleAuthority,
    ) -> EngineTerminalEvent:
        instance_id = str(authority.deployment_instance_id)
        if self._lifecycle_authorities.get(instance_id) != authority:
            raise RuntimeError("engine terminal authority differs from deployed instance")
        runtime = self._active_nodes.get(instance_id)
        if runtime is None:
            return EngineTerminalEvent.from_authority(
                authority,
                reason_code="engine_task_missing",
                retryable=True,
            )
        task = runtime.task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            reason_code = "engine_task_cancelled"
        except Exception:  # noqa: BLE001 - reason is deliberately sanitized
            reason_code = "engine_task_failed"
        else:
            reason_code = "engine_task_exited"
        return EngineTerminalEvent.from_authority(
            authority,
            reason_code=reason_code,
            retryable=True,
        )

    async def close(self) -> None:
        for deployment_instance_id in tuple(self._active_nodes):
            await self.stop(deployment_instance_id)

    def runner_fact_deployments(self) -> tuple[RunnerFactDeployment, ...]:
        return tuple(context[0] for context in self._runner_fact_contexts.values())

    async def runner_fact_risk_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> tuple[Decimal, list[dict]]:
        context = self._runner_fact_contexts.get(deployment_instance_id)
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None or context is None:
            raise RuntimeError(
                f"RunnerFact DeploymentInstance {deployment_instance_id!r} is not active"
            )
        snapshot = self._portfolio_snapshot_provider.snapshot(runtime, currency=currency)
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.equity, snapshot.runner_fact_rows()

    async def runner_fact_valuation_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> tuple[Decimal, list[dict]]:
        context = self._runner_fact_contexts.get(deployment_instance_id)
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None or context is None:
            raise RuntimeError(
                f"RunnerFact DeploymentInstance {deployment_instance_id!r} is not active"
            )
        snapshot = self._portfolio_snapshot_provider.snapshot(runtime, currency=currency)
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.equity, snapshot.valuation_rows()

    async def runner_fact_capital_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> RunnerCapitalBasisSnapshot:
        runtime = self._active_nodes.get(deployment_instance_id)
        boundary = self._runner_safety_boundaries.get(deployment_instance_id)
        if runtime is None or boundary is None:
            raise RuntimeError("capital basis requires an active, guarded deployment")
        strategies = runtime.strategies
        if len(strategies) != 1:
            raise RuntimeError("capital basis requires exactly one strategy per deployment")
        strategy = strategies[0]
        if self._declared_currency(deployment_instance_id) != currency:
            raise RuntimeError("capital basis currency differs from deployment settlement")
        position = getattr(getattr(strategy, "config", None), "position", None)
        effective = getattr(strategy, "_get_effective_capital", None)
        available = getattr(strategy, "_get_actual_balance", None)
        if position is None or not callable(effective) or not callable(available):
            raise RuntimeError("strategy does not expose the canonical capital basis interface")
        exposure = await boundary.exposure_snapshot()
        return RunnerCapitalBasisSnapshot(
            currency=currency,
            venue_available=str(available()),
            strategy_sizing_basis=str(effective()),
            configured_initial_capital=str(position.initial_capital),
            capital_mode=str(position.capital_mode),
            reserved_notional=str(exposure.reserved_notional),
            open_exposure=str(exposure.open_exposure),
            total_exposure=str(exposure.total_exposure),
            max_total_notional=str(exposure.max_total_notional),
            within_policy=bool(exposure.within_policy),
        )

    async def runner_fact_venue_ledger(
        self, deployment_instance_id: str, coverage_from, closed_at
    ) -> VenueLedgerEvidence:
        context = self._runner_fact_contexts.get(deployment_instance_id)
        if context is None or context[1] is None:
            raise RuntimeError("independent venue ledger is unavailable for this deployment")
        return await context[1].collect(coverage_from, closed_at)

    async def reconfigure(self, spec: dict) -> None:
        """v1 reconfigure: apply runtime-tunable params in place, reject structural.

        A running TradingNode cannot hot-swap its strategy class, venue, or traded
        symbols, so any structural change must go through stop + re-deploy (the
        reconciler owns the credential ref for that). Only changes the caller
        explicitly flags as runtime-tunable (leverage / notional cap) are accepted
        here; today they are logged as intent (live application is a follow-up).
        """
        deployment_instance_id = str(spec.get("deployment_instance_id") or "")
        reconfigure_spec = spec.get("reconfigure") or {}
        if reconfigure_spec.get("runtime_tunable_only"):
            _log.info(
                "nt_reconfigure_runtime_tunable",
                deployment_instance_id=deployment_instance_id,
                params=reconfigure_spec.get("params"),
            )
            return
        raise NotImplementedError(
            f"structural reconfigure of instance {deployment_instance_id!r} "
            "requires stop + re-deploy "
            "(v1 NtTradingNodeHost does not hot-swap strategy / venue / symbol)"
        )

    async def get_open_notional(self, deployment_instance_id: str) -> Decimal:
        """Return current marked notional from the canonical portfolio snapshot."""
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            return Decimal("0")
        snapshot = self._portfolio_snapshot_provider.snapshot(
            runtime, currency=self._declared_currency(deployment_instance_id)
        )
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.open_notional

    async def check_engine_connected(self, deployment_instance_id: str) -> ConnectivityState:
        """Data + execution engine connectivity for this spec's node. An unknown
        / not-yet-deployed spec is reported disconnected — a spec the reconciler
        believes is running but has no live node is exactly the zombie case."""
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            return ConnectivityState(
                data_connected=False, exec_connected=False, checked_at_epoch_s=time.time()
            )
        # Both answers come from the node's own state, which is narrower than what
        # 1.x could say. 2.0 exposes no per-client connection query to python at all
        # -- the rust engines keep one but only consult it at startup and shutdown --
        # so what is observable here is that the node reached Running, which it does
        # only after the connect phase passed. A venue that drops mid-run no longer
        # shows up as disconnected; the plan records that narrowing against red line
        # 0.3, and the zombie watchdog reads this as "the node is still up".
        connected = runtime.is_running
        return ConnectivityState(
            data_connected=connected,
            exec_connected=connected,
            checked_at_epoch_s=time.time(),
        )

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        """Close every open position for this spec via NT's per-instrument
        ``Strategy.close_all_positions`` — the engine-neutral ``flatten_positions``
        name maps here (NT has no ``flatten_positions``). An unknown spec is a
        logged no-op."""
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            _log.warning(
                "flatten_positions_unknown_instance",
                deployment_instance_id=deployment_instance_id,
                reason=reason,
            )
            return
        instrument_ids = {position.instrument_id for position in runtime.cache.positions_open()}
        if not instrument_ids:
            # Nothing was contained, and at startup that is not the same as nothing being
            # there: reconciliation may not yet have delivered the account's existing
            # positions, in which case they arrive seconds later untouched. Recording this
            # as a flatten would read as containment and stop anyone from asking further,
            # which is precisely what C9 asks us not to do.
            _log.error(
                "nt_flatten_containment_unconfirmed",
                deployment_instance_id=deployment_instance_id,
                reason=reason,
            )
            return
        for strategy in runtime.strategies:
            # NT's own close_all_positions is reduce-only, and a venue that refuses that
            # form refuses it here too -- leaving containment unable to contain at the
            # one moment it must. Toolkit strategies expose a close that drops
            # reduce-only on recorded evidence of that refusal; prefer it when present.
            # Duck-typed, not isinstance: the toolkit is not required of a deployment,
            # and a strategy without it has to keep behaving exactly as before.
            close_with_fallback = getattr(strategy, "close_all_positions_with_fallback", None)
            for instrument_id in instrument_ids:
                if callable(close_with_fallback):
                    close_with_fallback(instrument_id)
                else:
                    strategy.close_all_positions(instrument_id)
        _log.warning(
            "positions_flattened",
            deployment_instance_id=deployment_instance_id,
            reason=reason,
            instrument_count=len(instrument_ids),
        )

    async def get_positions(self, deployment_instance_id: str) -> list[PositionSnapshot]:
        """Return positions valued by the canonical portfolio snapshot."""
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            return []
        snapshot = self._portfolio_snapshot_provider.snapshot(
            runtime, currency=self._declared_currency(deployment_instance_id)
        )
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.engine_positions()

    async def get_orders(self, deployment_instance_id: str) -> list[OrderSnapshot]:
        """Materialise every open order as a Decimal-only ``OrderSnapshot``."""

        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            return []
        snapshots: list[OrderSnapshot] = []
        for order in runtime.cache.orders_open():
            # A market order has no ``price`` attribute at all, so reaching for it
            # raises rather than yielding None. Report the absence instead of
            # inventing a number a reader could mistake for a limit.
            raw_price = getattr(order, "price", None)
            snapshots.append(
                OrderSnapshot(
                    client_order_id=str(order.client_order_id),
                    instrument_id=str(order.instrument_id),
                    side=str(order.side),
                    quantity=Decimal(str(order.quantity)),
                    price=None if raw_price is None else Decimal(str(raw_price)),
                    status=str(order.status),
                )
            )
        return snapshots

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        """Return reliable portfolio equity or an explicit degraded snapshot."""
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            return EngineStatus(
                phase="unknown",
                position_count=0,
                order_count=0,
                open_notional=Decimal("0"),
                peak_equity=Decimal("0"),
                current_equity=Decimal("0"),
                drawdown_pct=Decimal("0"),
                reliable=False,
                unreliable_reason="deployment_not_active",
            )
        try:
            orders = list(runtime.cache.orders_open())
        except AttributeError:
            orders = []
        forwarding_failure = self._event_forwarding_failures.get(deployment_instance_id)
        snapshot = self._portfolio_snapshot_provider.snapshot(
            runtime, currency=self._declared_currency(deployment_instance_id)
        )
        if forwarding_failure is not None:
            # An event never reached one of the runner's sinks, so a fact is missing or
            # a reservation is still held. Whatever the portfolio says, what this
            # deployment reports about itself no longer matches what happened.
            return EngineStatus(
                phase="degraded",
                position_count=len(snapshot.positions),
                order_count=len(orders),
                open_notional=snapshot.open_notional,
                peak_equity=self._peak_equity.get(deployment_instance_id, Decimal("0")),
                current_equity=snapshot.equity if snapshot.reliable else Decimal("0"),
                drawdown_pct=Decimal("0"),
                reliable=False,
                unreliable_reason=f"runner_event_forwarding_failed:{forwarding_failure}",
            )
        if not snapshot.reliable:
            return EngineStatus(
                phase="degraded",
                position_count=len(snapshot.positions),
                order_count=len(orders),
                open_notional=snapshot.open_notional,
                peak_equity=self._peak_equity.get(deployment_instance_id, Decimal("0")),
                current_equity=snapshot.equity,
                drawdown_pct=Decimal("0"),
                reliable=False,
                unreliable_reason=snapshot.unreliable_reason,
            )

        current_equity = snapshot.equity
        peak = self._peak_equity.get(deployment_instance_id, Decimal("0"))
        if current_equity > peak:
            peak = current_equity
            self._peak_equity[deployment_instance_id] = peak
        if peak > 0 and current_equity < peak:
            drawdown_pct = (peak - current_equity) / peak * Decimal("100")
        else:
            drawdown_pct = Decimal("0")
        return EngineStatus(
            phase="running",
            position_count=len(snapshot.positions),
            order_count=len(orders),
            open_notional=snapshot.open_notional,
            peak_equity=peak,
            current_equity=current_equity,
            drawdown_pct=drawdown_pct,
            reliable=True,
        )

    @staticmethod
    def _trader_id(deployment_instance_id: str) -> str:
        tag = "".join(ch for ch in deployment_instance_id if ch.isalnum())[:20] or "000"
        return f"CUSTOS-{tag}"

    def _on_node_task_done(self, deployment_instance_id: str, task) -> None:
        # A background node loop dying must never be silent — surface the error.
        # Also drop the registry entry so a self-terminated node doesn't linger;
        # guard on task identity so a re-deployed spec_id (new task) isn't cleared
        # by a stale callback.
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is not None and runtime.task is task:
            self._active_nodes.pop(deployment_instance_id, None)
            self._runner_fact_contexts.pop(deployment_instance_id, None)
            self._runner_safety_boundaries.pop(deployment_instance_id, None)
            self._event_forwarding_failures.pop(deployment_instance_id, None)
            self._release_execution_account_partition(deployment_instance_id)
            self._shutdown_policies.pop(deployment_instance_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error(
                "nt_node_loop_failed",
                deployment_instance_id=deployment_instance_id,
                **_sanitize_exception(exc),
            )
