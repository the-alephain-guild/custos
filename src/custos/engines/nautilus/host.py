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
from custos.core.runner_fact import (
    SUPPORTED_CURRENCIES,
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactEmitter,
)
from custos.core.runner_fact_producer import (
    RunnerFactDeployment,
    RunnerFactMessageBusBridge,
    VenueLedgerEvidence,
)
from custos.engines.nautilus.portfolio_snapshot import (
    NautilusPortfolioSnapshotProvider,
)
from custos.engines.nautilus.settlement import settlement_currency_for_pairs

try:
    from nautilus_trader.adapters.binance.factories import (
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
    from nautilus_trader.config import LiveExecEngineConfig, LoggingConfig, TradingNodeConfig
    from nautilus_trader.core.rust.model import PriceType
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import TraderId
except ImportError:  # nautilus extra absent (audit / paper install) — deploy fails fast
    BinanceLiveDataClientFactory = None
    BinanceLiveExecClientFactory = None
    SandboxLiveExecClientFactory = None
    LiveExecEngineConfig = None
    LoggingConfig = None
    TradingNodeConfig = None
    TradingNode = None
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
        # deployment_instance_id -> (TradingNode, background run task). Never holds credentials.
        self._active_nodes: dict[str, tuple] = {}
        self._lifecycle_authorities: dict[str, EngineLifecycleAuthority] = {}
        # deployment_instance_id -> signed fact scope plus independent venue ledger adapter.
        self._runner_fact_contexts: dict[str, tuple[RunnerFactDeployment, object | None]] = {}
        # Decimal equity high-water mark per instance so get_engine_status can
        # report drawdown percentage over time. Never a float (red line 0.4).
        self._peak_equity: dict[str, Decimal] = {}
        # deployment_instance_id -> the currency this deployment settles in, derived from
        # its pairs at deploy. The guards below read equity, and equity only has one
        # answer once a currency is named: a funded account holds several at once, so
        # without this they go unreliable on any real account and fail closed.
        self._settlement_currencies: dict[str, str] = {}
        self._stop_timeout_secs = _STOP_TIMEOUT_SECS
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
        if TradingNode is None:
            raise RuntimeError(
                "NautilusTrader not installed — install `custos-runner[nautilus]` "
                "(needs Python 3.12+) to run NtTradingNodeHost"
            )

    def supports_trading_mode(self, mode: str) -> bool:
        return mode in {"sandbox", "testnet", "live"}

    def supports_venue(self, venue: str) -> bool:
        return venue.lower() in _SUPPORTED_VENUES

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
        if deployment_instance_id in self._active_nodes:
            # Idempotency guard: re-deploying a live spec must go through stop first
            # (structural changes are stop + re-deploy), never silently replace it.
            raise RuntimeError(
                f"deployment instance {deployment_instance_id!r} already deployed; call stop first"
            )

        if not artifact.activation_id.strip():
            raise RuntimeError("verified artifact activation identity is required")
        strategy = artifact.strategy

        # Imported lazily: venue_binance imports NautilusTrader at module top.
        from custos.engines.nautilus import venue_binance as venue

        trading_mode = str(spec.get("trading_mode") or "sandbox").lower()
        data_cfg = venue.build_data_client_config(
            spec, credential, venue.data_environment_for_mode(trading_mode)
        )
        exec_cfg, exec_factory, reconciliation = self._build_exec_plan(
            trading_mode, spec, credential, venue
        )
        exec_factory, runner_safety_boundary = await self._build_guarded_exec_plan(
            exec_factory,
            spec,
        )

        # ps runner.py._create_node_config exposes the NT startup timeouts and the
        # reconciliation lookback to strategy authors; custos accepts the same
        # knobs via a plain nautilus_config dict-key so operators can tune a slow
        # exchange without needing a code change. Every knob is optional — an
        # absent key falls through to the NT internal default.
        nautilus_cfg = spec.get("nautilus_config") or {}
        node_kwargs: dict = {
            "trader_id": TraderId(self._trader_id(deployment_instance_id)),
            "logging": LoggingConfig(log_level=str(spec.get("log_level", "INFO"))),
            "data_clients": {venue.BINANCE_VENUE: data_cfg},
            "exec_clients": {venue.BINANCE_VENUE: exec_cfg},
            # Real venues reconcile against exchange account state; the sandbox has none.
            "exec_engine": LiveExecEngineConfig(
                reconciliation=reconciliation,
                reconciliation_lookback_mins=nautilus_cfg.get("reconciliation_lookback_mins"),
            ),
        }
        for timeout_key in (
            "timeout_connection",
            "timeout_reconciliation",
            "timeout_portfolio",
            "timeout_disconnection",
        ):
            if timeout_key in nautilus_cfg:
                node_kwargs[timeout_key] = nautilus_cfg[timeout_key]

        node_config = TradingNodeConfig(**node_kwargs)

        try:
            node = TradingNode(config=node_config)
            node.add_data_client_factory(venue.BINANCE_VENUE, BinanceLiveDataClientFactory)
            node.add_exec_client_factory(venue.BINANCE_VENUE, exec_factory)
            node.build()
        except Exception as exc:  # noqa: BLE001 — reconciler maps this to degraded status
            _log.error(
                "nt_startup_failure",
                deployment_instance_id=deployment_instance_id,
                spec_id=spec_id,
                **_sanitize_exception(exc),
            )
            raise

        fact_context = self._build_runner_fact_context(spec, credential)
        try:
            self._attach_runtime_bridges(
                node,
                fact_context,
                runner_safety_boundary,
            )
        except Exception:
            node.dispose()
            raise
        if fact_context is not None:
            self._runner_fact_contexts[fact_context[0].deployment_instance_id] = fact_context

        node.trader.add_strategy(strategy)

        task = asyncio.create_task(node.run_async())
        task.add_done_callback(
            lambda task, instance_id=deployment_instance_id: self._on_node_task_done(
                instance_id, task
            )
        )
        self._active_nodes[deployment_instance_id] = (node, task)
        self._lifecycle_authorities[deployment_instance_id] = lifecycle_authority
        # Derived from the pairs rather than the open positions: at this moment there are
        # no positions, and the startup guards read equity immediately.
        self._settlement_currencies[deployment_instance_id] = settlement_currency_for_pairs(
            spec.get("pairs") or []
        )

        _log.info(
            "nt_deploy_started",
            deployment_instance_id=deployment_instance_id,
            spec_id=spec_id,
            trading_mode=trading_mode,
            connector=spec.get("connector"),
            permission_scope=credential.get("permission_scope"),
            artifact_activation_id=artifact.activation_id,
            strategy=type(strategy).__name__,
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
            return exec_cfg, SandboxLiveExecClientFactory, False
        if trading_mode == "testnet":
            exec_cfg = venue.build_exec_client_config_testnet(spec, credential)
            return exec_cfg, BinanceLiveExecClientFactory, True
        if trading_mode == "live":
            _log.warning(
                "nt_live_deploy_requested",
                spec_id=spec.get("deployment_spec_id"),
                connector=spec.get("connector"),
                promotion_id=spec.get("promotion_id"),
            )
            exec_cfg = venue.build_exec_client_config_live(spec, credential)
            return exec_cfg, BinanceLiveExecClientFactory, True
        raise RuntimeError(
            f"unsupported trading_mode {trading_mode!r} (expected sandbox / testnet / live)"
        )

    async def _build_guarded_exec_plan(self, exec_factory, spec: dict):
        if self._runner_safety_boundary_factory is None:
            return exec_factory, None
        from custos.engines.nautilus.runner_safety import guarded_exec_client_factory

        boundary = self._runner_safety_boundary_factory(spec)
        if isawaitable(boundary):
            boundary = await boundary
        if boundary is None:
            raise RuntimeError("runner safety boundary factory returned no boundary")
        return guarded_exec_client_factory(exec_factory, boundary), boundary

    def _attach_runtime_bridges(
        self,
        node,
        fact_context,
        runner_safety_boundary=None,
    ) -> None:
        msgbus = node.kernel.msgbus
        if runner_safety_boundary is not None:
            runner_safety_boundary.bootstrap(msgbus)
        if fact_context is not None and self._runner_fact_emitter is not None:
            RunnerFactMessageBusBridge(
                emitter=self._runner_fact_emitter,
                deployment=fact_context[0],
            ).bootstrap(msgbus)

    def _build_runner_fact_context(self, spec: dict, credential: dict):
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
        deployment = RunnerFactDeployment(
            authority=authority,
            deployment_instance_id=deployment_instance_id,
            deployment_spec_id=str(spec_id),
            deployment_spec_digest=deployment_spec_digest,
            venue="BINANCE",
            currency=currency,
            reconciliation_available=provider is not None,
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
        self._peak_equity.pop(deployment_instance_id, None)
        self._settlement_currencies.pop(deployment_instance_id, None)
        self._runner_fact_contexts.pop(deployment_instance_id, None)
        entry = self._active_nodes.pop(deployment_instance_id, None)
        self._lifecycle_authorities.pop(deployment_instance_id, None)
        if entry is None:
            # Idempotent: stopping an unknown / already-stopped spec is a no-op.
            _log.info(
                "nt_stop_noop_unknown_instance",
                deployment_instance_id=deployment_instance_id,
            )
            return

        node, task = entry
        try:
            await asyncio.wait_for(node.stop_async(), timeout=self._stop_timeout_secs)
        except TimeoutError:
            _log.error(
                "nt_stop_timeout",
                deployment_instance_id=deployment_instance_id,
                timeout_secs=self._stop_timeout_secs,
            )
        finally:
            node.dispose()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaping the run task
                pass
        _log.info("nt_stop_completed", deployment_instance_id=deployment_instance_id)

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
        entry = self._active_nodes.get(deployment_instance_id)
        return entry is not None and not entry[1].done()

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
            entry = self._active_nodes.get(instance_id)
            if entry is None:
                raise RuntimeError("engine task exited before readiness")
            node, task = entry
            checks = await self._readiness_checks(authority, node, task)
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
        entry = self._active_nodes.get(deployment_instance_id)
        if authority is None or entry is None:
            return False
        node, task = entry
        checks = await self._readiness_checks(authority, node, task)
        return checks.ready

    async def _readiness_checks(
        self,
        authority: EngineLifecycleAuthority,
        node: object,
        task: object,
    ) -> EngineReadinessChecks:
        """Ask the engine what it has actually finished, field by field.

        Every field here used to be derivable from the deployment's trading mode, which
        is to say from what it was asked to do rather than from what it did. Three of the
        seven were: see the tests for what each one now proves.
        """
        connectivity = await self.check_engine_connected(str(authority.deployment_instance_id))
        kernel = node.kernel
        trader = getattr(kernel, "trader", None)
        portfolio = getattr(kernel, "portfolio", None)
        exec_engine = getattr(kernel, "exec_engine", None)

        # A running trader is the closest thing to a reconciliation receipt that
        # NautilusTrader offers: there is no completion flag to read, but
        # ``NautilusKernel.start_async`` awaits reconciliation and *returns without
        # starting the trader* if it fails. So a started trader means the step was
        # passed -- either reconciled, or legitimately skipped.
        trader_running = bool(trader is not None and trader.is_running)
        strategies = tuple(trader.strategies()) if trader is not None else ()

        # Skipping is only legitimate in sandbox, which fills locally against live
        # prices and has no exchange account to reconcile against (see
        # ``_build_exec_plan``). On testnet and live it is a misconfiguration, and
        # the trader starts either way -- so passing it needs its own check.
        reconciliation_required = authority.trading_mode != "sandbox"
        reconciliation_enabled = bool(getattr(exec_engine, "reconciliation", False))

        return EngineReadinessChecks(
            node_task_alive=not task.done(),
            data_connectivity_ready=connectivity.data_connected,
            execution_connectivity_ready=connectivity.exec_connected,
            portfolio_initialized=bool(portfolio is not None and portfolio.initialized),
            reconciliation_initialized=(
                trader_running and (reconciliation_enabled or not reconciliation_required)
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
        entry = self._active_nodes.get(instance_id)
        if entry is None:
            return EngineTerminalEvent.from_authority(
                authority,
                reason_code="engine_task_missing",
                retryable=True,
            )
        task = entry[1]
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
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None or context is None:
            raise RuntimeError(
                f"RunnerFact DeploymentInstance {deployment_instance_id!r} is not active"
            )
        node, _task = entry
        snapshot = self._portfolio_snapshot_provider.snapshot(node, currency=currency)
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.equity, snapshot.runner_fact_rows()

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
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None:
            return Decimal("0")
        node, _task = entry
        snapshot = self._portfolio_snapshot_provider.snapshot(
            node, currency=self._declared_currency(deployment_instance_id)
        )
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.open_notional

    async def check_engine_connected(self, deployment_instance_id: str) -> ConnectivityState:
        """Data + execution engine connectivity for this spec's node. An unknown
        / not-yet-deployed spec is reported disconnected — a spec the reconciler
        believes is running but has no live node is exactly the zombie case."""
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None:
            return ConnectivityState(
                data_connected=False, exec_connected=False, checked_at_epoch_s=time.time()
            )
        node, _task = entry
        return ConnectivityState(
            data_connected=bool(node.kernel.data_engine.check_connected()),
            exec_connected=bool(node.kernel.exec_engine.check_connected()),
            checked_at_epoch_s=time.time(),
        )

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        """Close every open position for this spec via NT's per-instrument
        ``Strategy.close_all_positions`` — the engine-neutral ``flatten_positions``
        name maps here (NT has no ``flatten_positions``). An unknown spec is a
        logged no-op."""
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None:
            _log.warning(
                "flatten_positions_unknown_instance",
                deployment_instance_id=deployment_instance_id,
                reason=reason,
            )
            return
        node, _task = entry
        instrument_ids = {position.instrument_id for position in node.kernel.cache.positions_open()}
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
        for strategy in node.kernel.trader.strategies():
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
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None:
            return []
        node, _task = entry
        snapshot = self._portfolio_snapshot_provider.snapshot(
            node, currency=self._declared_currency(deployment_instance_id)
        )
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        return snapshot.engine_positions()

    async def get_orders(self, deployment_instance_id: str) -> list[OrderSnapshot]:
        """Materialise every open order as a Decimal-only ``OrderSnapshot``."""

        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None:
            return []
        node, _task = entry
        snapshots: list[OrderSnapshot] = []
        for order in node.kernel.cache.orders_open():
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
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is None:
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
        node, _task = entry
        try:
            orders = list(node.kernel.cache.orders_open())
        except AttributeError:
            orders = []
        snapshot = self._portfolio_snapshot_provider.snapshot(
            node, currency=self._declared_currency(deployment_instance_id)
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
        entry = self._active_nodes.get(deployment_instance_id)
        if entry is not None and entry[1] is task:
            self._active_nodes.pop(deployment_instance_id, None)
            self._runner_fact_contexts.pop(deployment_instance_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error(
                "nt_node_loop_failed",
                deployment_instance_id=deployment_instance_id,
                **_sanitize_exception(exc),
            )
