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
import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from inspect import isawaitable
from typing import Any
from uuid import UUID

from custos.core.engine_protocol import (
    ActivatedEngineArtifactV1,
    ConnectivityState,
    EngineDeploymentRefused,
    EngineLifecycleAuthority,
    EngineNodeHolder,
    EngineReadinessChecks,
    EngineReadyReceipt,
    EngineStatus,
    EngineStopBoundary,
    EngineTerminalEvent,
    OrderSnapshot,
    PositionSnapshot,
    StopBoundaryValuation,
)
from custos.core.log import get_logger
from custos.core.order_reservation_boundary import RunnerReservationBoundary
from custos.core.runner_fact import (
    SUPPORTED_CURRENCIES,
    RunnerCapabilityReceipt,
    RunnerFactAuthority,
    RunnerFactEmitter,
    runner_fact_event_id,
)
from custos.core.runner_fact_producer import (
    RunnerCapitalBasisSnapshot,
    RunnerFactDeployment,
    RunnerFactEventBridge,
    RunnerRuntimeLogPort,
    VenueLedgerEvidence,
    strategy_signal_metadata,
)
from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
from custos.core.venue_proxy import VenueProxy
from custos.engines.nautilus.cancel_discipline import CancelDiscipline, PartitionGate
from custos.engines.nautilus.portfolio_snapshot import (
    NautilusPortfolioSnapshot,
    NautilusPortfolioSnapshotProvider,
)
from custos.engines.nautilus.settlement import settlement_currency_for_pairs
from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder
from custos.engines.nautilus.venues import (
    venue_for_connector,
    venue_module_name_for,
    venue_module_name_for_venue,
)

try:
    from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode, NodeState
    from nautilus_trader.model import PriceType, TraderId
except ImportError:  # nautilus extra absent (audit / paper install) — deploy fails fast
    SandboxExecutionClientFactory = None
    Environment = None
    LoggerConfig = None
    LogLevel = None
    LiveExecutionEngineConfig = None
    LiveNode = None
    NodeState = None
    TraderId = None
    PriceType = None

__all__ = ["SandboxSimulationHost", "NtTradingNodeHost"]

_log = get_logger("custos.nautilus_host")

_DEFAULT_STARTING_BALANCES = ["10_000 USDT"]
_STOP_TIMEOUT_SECS = 30.0
# After the graceful stop times out the run task is cancelled; a run that does not
# end within this bound either may still be running, and is reaped in the background.
_REAP_TIMEOUT_SECS = 10.0
# Nautilus's continuous open-order check, off by default. It is what settles an
# order the venue says it does not have: after this many misses it asks for the
# order directly and, still missing, marks it rejected. Its threshold leaves an
# order alone for five seconds after any local action on it, and the cancel
# discipline never re-cancels a not-found order, so the check gets to decide.
_OPEN_CHECK_INTERVAL_SECS = 10.0
_OPEN_CHECK_MISSING_RETRIES = 3

# Venues this runner can execute, per trading mode. Kept NT-free so admission can
# query capability on a base install, and kept in sync with the venue-config modules'
# wired connectors by a drift-guard test (test_nt_venue_wiring.py).
#
# Capability declarations mirror the native venue modules. Runtime approval,
# signed deployment promotion, credentials and risk policy are checked separately
# before any live node is constructed.
_SANDBOX_VENUES = frozenset(
    {"binance", "binance_perpetual", "sodex", "sodex_perpetual", "okx", "okx_perpetual"}
)
_TESTNET_VENUES = frozenset(
    {"binance", "binance_perpetual", "sodex", "sodex_perpetual", "okx", "okx_perpetual"}
)
_LIVE_VENUES = frozenset(
    {"binance", "binance_perpetual", "sodex", "sodex_perpetual", "okx", "okx_perpetual"}
)
_VENUES_BY_MODE: dict[str, frozenset[str]] = {
    "sandbox": _SANDBOX_VENUES,
    "testnet": _TESTNET_VENUES,
    "live": _LIVE_VENUES,
}


def _venue_module_for(connector: str):
    """The venue-config module for a connector.

    Imported here rather than at module scope: each venue module imports
    NautilusTrader at its top, and this module has to stay importable on a base
    install so admission can answer capability questions without the nautilus extra.
    """
    from importlib import import_module

    return import_module(f"custos.engines.nautilus.{venue_module_name_for(connector)}")


def _venue_dust_counts_as_flat(venue: str) -> bool:
    """Whether the venue a holding is on exempts dust; a venue nothing wires exempts none."""
    module_name = venue_module_name_for_venue(venue)
    if module_name is None:
        return False
    from importlib import import_module

    return import_module(f"custos.engines.nautilus.{module_name}").dust_counts_as_flat() is True


def _require_authorized_trading_scope(strategy: object, spec: dict, venue) -> None:
    """Refuse a strategy whose trading scope differs from the deployment's.

    The exchange clients are built from the deployment's connector, pairs and
    leverage; the strategy trades what its own config declares. A strategy on
    other instruments subscribes to something the clients never loaded and sits
    idle while the deployment looks healthy, so the two must agree before the
    node is built. The deployment's scope has one source, the strategy's
    declared one; a trading section in the signed strategy config would be a
    second, and is refused as such.
    """
    strategy_config = spec.get("strategy_config") or {}
    if isinstance(strategy_config, dict) and "trading" in strategy_config:
        raise EngineDeploymentRefused(
            "signed_strategy_config_overrides_trading",
            "the signed strategy config may not carry a trading section; the "
            "strategy's declared trading scope is the only source",
        )
    config = getattr(strategy, "config", None)
    trading = getattr(config, "trading", None)
    claimed = getattr(config, "external_order_instrument_ids", None)
    if trading is None or claimed is None:
        raise EngineDeploymentRefused(
            "strategy_trading_scope_undeclared",
            "the strategy does not declare the connector, instruments and leverage it trades",
        )
    declared = (
        str(trading.connector),
        tuple(sorted(str(instrument) for instrument in claimed)),
        int(trading.leverage),
    )
    authorized = (
        str(spec["connector"]),
        tuple(sorted(venue.build_instrument_id_strings(spec))),
        int(spec["leverage"]),
    )
    if declared != authorized:
        raise EngineDeploymentRefused(
            "strategy_trading_scope_mismatch",
            "strategy trades connector={} instruments={} leverage={}; deployment "
            "authorizes connector={} instruments={} leverage={}".format(*declared, *authorized),
        )


@dataclass(frozen=True, slots=True)
class _DeploymentIdentity:
    """The spec fields this host routes on, read once.

    A DeploymentSpec arrives as a plain dict, and every reader of it used to normalise
    for itself -- six readings of the instance id, six of the mode, two derivations of
    the settlement currency. Readings that disagree are not a style problem: the venue
    a deployment trades on reaches the wire inside signed facts, so a second reader
    that answered differently would sign a claim the first reader knew to be false.

    Built after ``EngineLifecycleAuthority`` has validated the identity and the mode,
    so nothing here re-parses or re-defaults them.
    """

    instance_id: str
    spec_id: str
    trading_mode: str
    connector: str
    venue: str
    pairs: tuple[str, ...]
    settlement_currency: str


def _sodex_settlement_currency(spec: dict) -> str:
    from custos.engines.nautilus.venue_config import venue_options

    options = venue_options(
        spec, {"wallet_address", "sodex_account_id", "margin_mode", "settlement_currency"}
    )
    currency = options.get("settlement_currency")
    if not isinstance(currency, str) or currency.upper() not in SUPPORTED_CURRENCIES:
        raise ValueError(
            "SoDEX requires an explicit supported nautilus_config.venue.settlement_currency"
        )
    return currency.upper()


def _deployment_identity(spec: dict, authority: EngineLifecycleAuthority) -> _DeploymentIdentity:
    connector = str(spec["connector"])
    pairs = tuple(str(pair) for pair in (spec.get("pairs") or []))
    if connector in {"binance", "okx", "sodex"}:
        for pair in pairs:
            normalized = pair.replace("/", "-").replace("_", "-")
            parts = normalized.split("-")
            if len(parts) == 2 and parts[0].upper() not in SUPPORTED_CURRENCIES:
                raise ValueError("spot base asset is not supported by the signed ledger")
    return _DeploymentIdentity(
        instance_id=str(authority.deployment_instance_id),
        spec_id=str(authority.deployment_spec_id),
        trading_mode=authority.trading_mode,
        connector=connector,
        venue=venue_for_connector(connector),
        pairs=pairs,
        # Derived here rather than at each use: the guards read it at startup and the
        # facts carry it, and a deployment that settles in no single currency has no
        # equity figure for either of them.
        settlement_currency=(
            _sodex_settlement_currency(spec)
            if connector.startswith("sodex")
            else settlement_currency_for_pairs(pairs)
        ),
    )


# Substrings that flag an exception message as potentially carrying credential
# material (NT config repr, adapter auth errors) — such messages are redacted
# before logging so a raw key can never reach the log (non-custodial red line 0.1).
_CREDENTIAL_HINTS = (
    "api_key",
    "api_secret",
    "api_passphrase",
    "passphrase",
    "secret",
    "authorization",
)


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


def _exec_engine_config(trading_mode: str) -> object | None:
    """The execution engine config a real venue runs with; the sandbox keeps the default."""
    if trading_mode == "sandbox":
        return None
    return LiveExecutionEngineConfig(
        open_check_interval_secs=_OPEN_CHECK_INTERVAL_SECS,
        open_check_open_only=True,
        open_check_missing_retries=_OPEN_CHECK_MISSING_RETRIES,
    )


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


def _base_fill_quantity(cache, instrument_id: str, quantity: str) -> str:
    from nautilus_trader.model import InstrumentId

    instrument = cache.instrument(InstrumentId.from_str(instrument_id))
    if instrument is None or bool(instrument.is_inverse):
        raise RuntimeError("fill requires a known linear instrument")
    multiplier = Decimal(str(instrument.multiplier))
    if not multiplier.is_finite() or multiplier <= 0:
        raise RuntimeError("fill instrument multiplier is invalid")
    return str(Decimal(quantity) * multiplier)


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

    async def stop_at_boundary(self, deployment_instance_id: str) -> EngineStopBoundary:
        """Stop the simulation; it has no account to value at the boundary.

        The simulator publishes its configured starting capital rather than a
        valuation of anything, so a stop never confirms an equity.
        """
        requested_at = datetime.now(UTC)
        was_running = deployment_instance_id in self._lifecycle_authorities
        await self.stop(deployment_instance_id)
        return EngineStopBoundary(
            stop_requested_at=requested_at,
            node_was_running=was_running,
            stopped_gracefully=True,
            run_task_failed=False,
            reaped=True,
            stop_effective_at=datetime.now(UTC),
            valuation=None,
            valuation_failure="valuation_unavailable",
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

    def supports_venue(self, venue: str, mode: str) -> bool:
        return venue.lower() in _VENUES_BY_MODE.get(mode.lower(), frozenset())

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
        venue_proxy: VenueProxy | None = None,
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
        # Instances whose node is being stopped. Such a node still holds the
        # runner, but only until its stop completes.
        self._stopping: set[str] = set()
        self._stop_timeout_secs = _STOP_TIMEOUT_SECS
        self._reap_timeout_secs = _REAP_TIMEOUT_SECS
        # deployment_instance_id -> the background reap of a run that outlived its
        # cancellation. Present only while the node may still be running.
        self._pending_reaps: dict[str, asyncio.Future[datetime]] = {}
        # deployment_instance_id -> the stop under way. Every caller waits on this
        # one task, and a caller that is cancelled only stops waiting: the shutdown
        # policy it started runs to its own end instead of beginning again.
        self._stops_in_flight: dict[str, asyncio.Task[EngineStopBoundary]] = {}
        self._shutdown_poll_secs = 0.2
        self._shutdown_stable_polls = 3
        self._shutdown_close_retry_secs = 2.0
        # Containment runs inside a supervision tick (10s by default), so its
        # confirmation has to finish well inside one and never outlive it.
        self._containment_confirm_secs = 5.0
        self._containment_retry_secs = 2.0
        # One bit per instance: has containment ever been confirmed on this node.
        # It is what separates "we closed it and it stayed closed" from "we have
        # never seen anything here", which look identical in the cache. Cleared
        # with the node, because the instance id outlives it and the next
        # generation must answer that question with its own evidence.
        self._containment_confirmed: set[str] = set()
        # What each venue said about each safety-path cancel, per instance, and the
        # request allowance of each account partition. Held here rather than in the
        # stop or containment coroutine, which the guard cancels every tick.
        self._cancel_disciplines: dict[str, CancelDiscipline] = {}
        self._partition_gates: dict[tuple[str, str], PartitionGate] = {}
        self._safety_clock: Callable[[], float] = time.monotonic
        # deployment_instance_id -> the unpriced dust last signed as a runtime log, so
        # the log says when it changes rather than on every valuation it blocks.
        self._signed_unpriced_dust: dict[str, tuple[tuple[str, str, str], ...]] = {}
        # deployment_instance_id -> the dust read just before its node was released:
        # what a stop leaves behind, asked for once the node is gone.
        self._dust_at_stop: dict[str, list[dict[str, str | None]] | None] = {}
        self._runtime_logs: RunnerRuntimeLogPort | None = None
        self._tenant_id = tenant_id
        self._runner_id = runner_id
        self._runner_fact_emitter = runner_fact_emitter
        self._capability_receipt = capability_receipt
        self._runner_safety_boundary_factory = runner_safety_boundary_factory
        self._venue_proxy = venue_proxy
        # Receivers of every order and position event, given the instance they
        # belong to. Installed after the safety and fact sinks, and never a source
        # of anything this host decides.
        self._observation_sinks: (
            tuple[Callable[[str, Any], None], Callable[[str, Any], None]] | None
        ) = None
        self._portfolio_snapshot_provider = portfolio_snapshot_provider or (
            NautilusPortfolioSnapshotProvider(
                price_type_mid=PriceType.MID if PriceType else None,
                price_type_last=PriceType.LAST if PriceType else None,
                dust_counts_as_flat=_venue_dust_counts_as_flat,
            )
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

    def add_observation_sinks(
        self,
        *,
        order: Callable[[str, Any], None],
        position: Callable[[str, Any], None],
    ) -> None:
        """Hand every later deployment's order and position events to these, too.

        A sink runs inside the strategy's callback, and one that raises marks the
        deployment's status unreliable -- which fails it closed. Whoever passes
        these has to make sure they never raise.
        """

        self._observation_sinks = (order, position)

    def supports_venue(self, venue: str, mode: str) -> bool:
        return venue.lower() in _VENUES_BY_MODE.get(mode.lower(), frozenset())

    def _claim_execution_account_partition(self, spec: dict, identity: _DeploymentIdentity) -> None:
        if identity.trading_mode == "sandbox":
            return
        instance_id = identity.instance_id
        scope = spec.get("credential_scope")
        if isinstance(scope, dict):
            scope_id = str(scope.get("scope_id") or "").strip()
        else:
            scope_id = str(getattr(scope, "scope_id", "") or "").strip()
        if not scope_id:
            raise RuntimeError(
                "real-venue deployment requires instance and credential-scope identity"
            )
        partition = (identity.trading_mode, scope_id)
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

    def _partition_gate(self, deployment_instance_id: str, budget) -> PartitionGate:
        """The gate of the account this instance trades on, made on first use.

        A sandbox instance has no real account, so it is its own partition.
        """
        partition = self._execution_account_partitions.get(
            deployment_instance_id, ("sandbox", deployment_instance_id)
        )
        gate = self._partition_gates.get(partition)
        if gate is None:
            gate = self._partition_gates[partition] = PartitionGate(budget, self._safety_clock)
        return gate

    def _new_cancel_discipline(self, deployment_instance_id: str, venue) -> CancelDiscipline:
        return CancelDiscipline(
            deployment_instance_id=deployment_instance_id,
            classify=venue.cancel_rejection_kind,
            partition=self._partition_gate(deployment_instance_id, venue.safety_request_budget()),
            clock=self._safety_clock,
        )

    def _cancel_discipline(self, deployment_instance_id: str) -> CancelDiscipline:
        """The discipline deploy registered with this node.

        A node assembled outside deploy has none; it gets one that classifies no
        refusal and has no request budget, which is the most conservative there is.
        """
        discipline = self._cancel_disciplines.get(deployment_instance_id)
        if discipline is None:
            discipline = self._cancel_disciplines[deployment_instance_id] = CancelDiscipline(
                deployment_instance_id=deployment_instance_id,
                classify=lambda _reason: None,
                partition=self._partition_gate(deployment_instance_id, None),
                clock=self._safety_clock,
            )
        return discipline

    def node_holder(self) -> EngineNodeHolder | None:
        """The instance whose node holds this runner, if any (see EngineNodeCapacity).

        A node whose run has already ended no longer holds the runner, even before
        its done-callback has removed it from the registry.
        """
        for instance, runtime in self._active_nodes.items():
            if not runtime.task.done():
                return EngineNodeHolder(
                    deployment_instance_id=instance,
                    releasing=instance in self._stopping,
                )
        return None

    def _require_the_only_node(self, deployment_instance_id: str) -> None:
        """Refuse a second node in this runner process.

        Nautilus 2.0 supports one concurrent live node per process. Its message bus,
        registries and channel senders are installed thread-locally for the thread
        driving the node, while the logger, logging worker and Tokio runtime are
        process-wide, so two nodes in one process do not have isolated state. On one
        event loop they would deliver each other's events rather than fail, which is
        also why ``run_async`` refuses a second hosted node. Nautilus's guidance is
        one node per process; a runner therefore hosts one deployment instance, and
        more instances run as more runner processes. Refusing here names the
        instance that already holds the runner, before the contender's strategy is
        built or a node is built and has to be disposed again.

        The refusal is final for this command: retrying cannot free the node, so it
        must not spend the restart budget. The command coordinator normally refuses
        earlier, before the artifact is activated; this is the guard for a node that
        started in between.
        """
        holder = self.node_holder()
        if holder is not None:
            raise EngineDeploymentRefused(
                "runner_engine_occupied",
                f"deployment instance {holder.deployment_instance_id!r} already holds this "
                "runner's event loop; nautilus runs one live node per process, so stop it "
                f"or run another runner process before deploying {deployment_instance_id!r}",
            )

    async def deploy(
        self,
        spec: dict,
        credential: dict,
        artifact: ActivatedEngineArtifactV1,
    ) -> str:
        self._ensure_nt_available()
        lifecycle_authority = EngineLifecycleAuthority.from_spec(spec)
        identity = _deployment_identity(spec, lifecycle_authority)
        spec_id = identity.spec_id
        deployment_instance_id = identity.instance_id
        shutdown_policy = _shutdown_policy_from_spec(spec)
        if deployment_instance_id in self._active_nodes:
            # Idempotency guard: re-deploying a live spec must go through stop first
            # (structural changes are stop + re-deploy), never silently replace it.
            raise RuntimeError(
                f"deployment instance {deployment_instance_id!r} already deployed; call stop first"
            )

        if not artifact.activation_id.strip():
            raise RuntimeError("verified artifact activation identity is required")
        # The runner and the message bus are both thread-local in 2.0, so two hosted
        # nodes on one event loop would cross-wire each other's events rather than
        # fail. Nautilus refuses the second run for that reason; this refuses it
        # first, where the message can say which deployment already holds the loop
        # and before anything of the contender is built. Running more than one
        # deployment per runner needs a thread or a process per node.
        self._require_the_only_node(deployment_instance_id)
        create_strategy = getattr(artifact, "create_strategy", None)
        strategy = create_strategy() if callable(create_strategy) else artifact.strategy

        venue = _venue_module_for(identity.connector)
        _require_authorized_trading_scope(strategy, spec, venue)
        # Refuse before anything reaches the venue: an account check that went out
        # directly would already be the connection the proxy was named to prevent.
        data_config_for_mode = self._venue_function(venue, "build_data_client_config_for_mode")
        await venue.validate_account_configuration(spec, credential)

        trading_mode = identity.trading_mode
        data_cfg = data_config_for_mode(spec, credential, trading_mode)
        exec_cfg, exec_factory, reconciliation = self._build_exec_plan(
            trading_mode, spec, credential, venue
        )
        runner_safety_boundary = await self._build_runner_safety_boundary(spec)

        # ps runner.py._create_node_config exposes the NT startup timeouts and the
        # reconciliation lookback to strategy authors; custos accepts the same
        # knobs via a plain nautilus_config dict-key so operators can tune a slow
        # exchange without needing a code change. Every knob is optional — an
        # absent key falls through to the NT internal default.
        nautilus_cfg = spec.get("nautilus_config") or {}

        # Claim before constructing a node. A conflicting real-venue deployment must
        # fail without creating and then disposing a node.
        self._claim_execution_account_partition(spec, identity)

        try:
            builder = LiveNode.builder(
                self._trader_id(deployment_instance_id),
                TraderId(self._trader_id(deployment_instance_id)),
                _environment_for_mode(trading_mode),
            )
            builder = builder.with_logging(_logger_config(spec))
            exec_engine = _exec_engine_config(trading_mode)
            if exec_engine is not None:
                # It replaces the whole execution engine config, so it goes before
                # the reconciliation flag and lookback that set fields of it.
                builder = builder.with_exec_engine_config(exec_engine)
            # Real venues reconcile against exchange account state; the sandbox has none.
            builder = builder.with_reconciliation(reconciliation)
            builder = _apply_nautilus_knobs(builder, nautilus_cfg)
            client_name = venue.client_name(spec)
            builder = builder.add_data_client(
                client_name,
                venue.data_client_factory(),
                data_cfg,
            )
            builder = _add_exec_client(
                builder,
                client_name,
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

        try:
            # Everything between taking the account partition and registering the node
            # is one ownership scope. This validation used to sit between two cleanup
            # blocks without being in either, so any of its exits kept the partition
            # and left the node undisposed -- and with the instance not yet in
            # _active_nodes, stop() could not undo it. The corrected config was then
            # refused by a deployment that had never started.
            fact_context = self._build_runner_fact_context(
                spec,
                credential,
                identity,
                runtime_strategy=strategy,
            )
            discipline = self._new_cancel_discipline(deployment_instance_id, venue)
            self._attach_runtime_bridges(
                deployment_instance_id,
                strategy,
                node.cache,
                fact_context,
                runner_safety_boundary,
                client_order_id_len_limit=venue.client_order_id_len_limit(),
                client_order_id_validator=venue.client_order_id_is_valid,
                cancel_discipline=discipline,
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
            self._cancel_disciplines[deployment_instance_id] = discipline
            self._dust_at_stop.pop(deployment_instance_id, None)
            node.add_strategy(strategy)
            # Captured before the run: run_async moves the node into the awaitable, and
            # reaching for these through the node afterwards raises.
            handle, cache, portfolio = node.handle(), node.cache, node.portfolio
            task = asyncio.create_task(node.run_async())
        except Exception:
            self._runner_fact_contexts.pop(deployment_instance_id, None)
            self._runner_safety_boundaries.pop(deployment_instance_id, None)
            self._cancel_disciplines.pop(deployment_instance_id, None)
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
        self._settlement_currencies[deployment_instance_id] = identity.settlement_currency
        self._shutdown_policies[deployment_instance_id] = shutdown_policy

        _log.info(
            "nt_deploy_started",
            deployment_instance_id=deployment_instance_id,
            spec_id=spec_id,
            trading_mode=trading_mode,
            connector=identity.connector,
            venue=identity.venue,
            permission_scope=credential.get("permission_scope"),
            artifact_activation_id=artifact.activation_id,
            strategy=type(strategy).__name__,
            shutdown_position_policy=shutdown_policy.position_policy,
        )
        return deployment_instance_id

    def _build_exec_plan(self, trading_mode: str, spec: dict, credential: dict, venue):
        """Resolve (exec_config, exec_factory, reconciliation) for the trading mode.

        sandbox fills locally against real-time prices (no exchange contact); testnet
        and live place real orders on the venue's own endpoints. Real venues reconcile
        against exchange account state, the sandbox has none. What a mode costs is the
        venue module's to say -- a live plan on Binance requires control-plane-signed
        promotion evidence, and a venue with no live delivery refuses outright.
        """
        if trading_mode == "sandbox":
            starting_balances = (spec.get("sandbox") or {}).get(
                "starting_balances"
            ) or _DEFAULT_STARTING_BALANCES
            exec_cfg = venue.build_exec_client_config_sandbox(spec, credential, starting_balances)
            return exec_cfg, SandboxExecutionClientFactory(), False
        if trading_mode == "testnet":
            exec_cfg = self._venue_function(venue, "build_exec_client_config_testnet")(
                spec, credential
            )
            return exec_cfg, venue.exec_client_factory(), True
        if trading_mode == "live":
            _log.warning(
                "nt_live_deploy_requested",
                spec_id=spec.get("deployment_spec_id"),
                connector=spec.get("connector"),
                promotion_id=spec.get("promotion_id"),
            )
            exec_cfg = self._venue_function(venue, "build_exec_client_config_live")(
                spec, credential
            )
            return exec_cfg, venue.exec_client_factory(), True
        raise RuntimeError(
            f"unsupported trading_mode {trading_mode!r} (expected sandbox / testnet / live)"
        )

    def _venue_function(self, venue, name: str) -> Callable[..., Any]:
        """A venue module function that reaches the venue, routed through the proxy.

        A venue that cannot take the proxy refuses once one is named. Connecting
        directly would expose the egress address the operator configured the proxy
        to hide, and fail where the venue is unreachable without it.
        """
        function = getattr(venue, name)
        if getattr(venue, "SUPPORTS_VENUE_PROXY", False):
            return functools.partial(function, proxy=self._venue_proxy)
        if self._venue_proxy is not None:
            raise RuntimeError(
                f"venue module {venue.__name__.rsplit('.', 1)[-1]} does not route through a "
                f"proxy; refusing to connect directly while {self._venue_proxy} is configured"
            )
        return function

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
        *,
        client_order_id_len_limit: int | None = None,
        client_order_id_validator=None,
        cancel_discipline: CancelDiscipline | None = None,
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
                quantity_to_base=lambda instrument, quantity: _base_fill_quantity(
                    cache, instrument, quantity
                ),
                # The gate's own verdict, not a second judgement: the fact says what
                # the order was allowed to be.
                order_reduces_exposure=(
                    runner_safety_boundary.judged_risk_reducing
                    if runner_safety_boundary is not None
                    else None
                ),
                runtime_log_emitter=RunnerRuntimeLogEmitter(
                    emitter=self._runner_fact_emitter,
                    capability=self._capability_receipt,
                    redactor=RuntimeLogRedactor(),
                ),
            )
            fact_bridge.bootstrap(forwarder)
        if cancel_discipline is not None:
            # Never raises: the discipline reports its own failures, so a broken
            # classifier does not mark the deployment's observations unreliable.
            forwarder.add_order_sink("cancel_discipline", cancel_discipline.on_order_event)
        if self._observation_sinks is not None:
            order_sink, position_sink = self._observation_sinks
            forwarder.add_order_sink(
                "observation", functools.partial(order_sink, deployment_instance_id)
            )
            forwarder.add_position_sink(
                "observation", functools.partial(position_sink, deployment_instance_id)
            )
        forwarder.install(strategy)
        if runner_safety_boundary is not None:
            self._install_order_gate(
                strategy,
                cache,
                runner_safety_boundary,
                fact_bridge,
                client_order_id_len_limit,
                client_order_id_validator,
            )

    def _install_order_gate(
        self,
        strategy,
        cache,
        boundary,
        fact_bridge,
        client_order_id_len_limit: int | None,
        client_order_id_validator=None,
    ) -> None:
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
                client_order_id_len_limit=client_order_id_len_limit,
                client_order_id_validator=client_order_id_validator,
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
        identity: _DeploymentIdentity,
        *,
        runtime_strategy: object | None = None,
    ):
        if self._runner_fact_emitter is None or self._capability_receipt is None:
            return None
        strategy_id = spec.get("strategy_id")
        if not strategy_id:
            raise RuntimeError("validated DeploymentSpec lost its canonical strategy_id")
        deployment_spec_digest = str(spec.get("deployment_spec_digest") or "").strip()
        if not deployment_spec_digest:
            raise RuntimeError(
                "validated DeploymentSpec lacks explicit DeploymentInstance/spec digest authority"
            )
        required_projectors = ["settlement", "risk", "health"]
        if identity.trading_mode in {"testnet", "live"}:
            required_projectors.append("reconciliation")
        self._capability_receipt.require_scope_bindings(
            projectors=required_projectors,
            trading_mode=identity.trading_mode,
            deployment_instance_id=identity.instance_id,
            deployment_spec_id=identity.spec_id,
            deployment_spec_digest=deployment_spec_digest,
            strategy_id=strategy_id,
        )
        authority = RunnerFactAuthority(
            tenant_id=self._tenant_id or "",
            trading_mode=identity.trading_mode,
            runner_id=self._capability_receipt.runner_id,
            deployment_instance_id=UUID(identity.instance_id),
            deployment_spec_id=UUID(identity.spec_id),
            deployment_spec_digest=deployment_spec_digest,
            generation=int(spec["generation"]),
            strategy_id=strategy_id,
            capability_version_id=self._capability_receipt.capability_version_id,
            capability_version=self._capability_receipt.capability_version,
            capability_manifest_digest=self._capability_receipt.manifest_digest,
        )
        currency = identity.settlement_currency
        if currency not in SUPPORTED_CURRENCIES:
            raise RuntimeError(f"settlement currency {currency!r} is outside RunnerFact v1")
        provider = None
        if identity.trading_mode in {"testnet", "live"}:
            provider = self._venue_function(
                _venue_module_for(identity.connector), "venue_ledger_source"
            )(spec, credential)
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
            deployment_instance_id=identity.instance_id,
            deployment_spec_id=identity.spec_id,
            deployment_spec_digest=deployment_spec_digest,
            venue=identity.venue,
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
        boundary = await self.stop_at_boundary(deployment_instance_id)
        if boundary.reap is not None:
            # Callers of a plain stop need the node gone before they continue, as
            # they always have; only the signed stop command acts before the reap.
            await boundary.reap

    async def stop_at_boundary(self, deployment_instance_id: str) -> EngineStopBoundary:
        """Stop the node and report how the stop ended, valuing it at the boundary.

        The shutdown policy runs first, while the strategy still receives events.
        The node is then asked to stop; once its run task has ended, and before the
        kernel is disposed, the account is read once. Nothing can trade after the
        run has ended, and disposal empties the captured portfolio, so that read is
        the only one that values the stop itself.

        A run that ignores the graceful stop is cancelled. A run that outlasts the
        cancellation as well may still be running: it is left registered and
        undisposed, reaped in the background, and reported with ``reaped=False``.
        A second stop while that reap is pending reports the same reap and does
        not run the shutdown policy again.

        One stop runs per instance at a time. A second caller waits on the first
        stop and gets its answer, success or failure, and a caller that is
        cancelled stops waiting without stopping the stop. A failure is not
        remembered past the waiters it reached: the next stop starts afresh.
        """
        in_flight = self._stops_in_flight.get(deployment_instance_id)
        if in_flight is None:
            in_flight = asyncio.ensure_future(self._stop_at_boundary_once(deployment_instance_id))
            self._stops_in_flight[deployment_instance_id] = in_flight
            in_flight.add_done_callback(
                functools.partial(self._forget_stop_in_flight, deployment_instance_id)
            )
        return await asyncio.shield(in_flight)

    def _forget_stop_in_flight(
        self, deployment_instance_id: str, task: asyncio.Task[EngineStopBoundary]
    ) -> None:
        if self._stops_in_flight.get(deployment_instance_id) is task:
            self._stops_in_flight.pop(deployment_instance_id, None)
        # Every waiter may have been cancelled; the outcome is still observed here.
        if not task.cancelled() and task.exception() is not None:
            _log.warning(
                "nt_stop_failed",
                deployment_instance_id=deployment_instance_id,
                **_sanitize_exception(task.exception()),
            )

    async def _stop_at_boundary_once(self, deployment_instance_id: str) -> EngineStopBoundary:
        requested_at = datetime.now(UTC)
        pending = self._pending_reaps.get(deployment_instance_id)
        if pending is not None:
            return EngineStopBoundary(
                stop_requested_at=requested_at,
                node_was_running=True,
                stopped_gracefully=False,
                run_task_failed=False,
                reaped=False,
                stop_effective_at=None,
                reap=pending,
            )
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None:
            # Idempotent: stopping an unknown / already-stopped spec is a no-op.
            _log.info(
                "nt_stop_noop_unknown_instance",
                deployment_instance_id=deployment_instance_id,
            )
            return EngineStopBoundary(
                stop_requested_at=requested_at,
                node_was_running=False,
                stopped_gracefully=True,
                run_task_failed=False,
                reaped=True,
                stop_effective_at=requested_at,
            )

        task = runtime.task
        self._dust_at_stop.pop(deployment_instance_id, None)
        policy = self._shutdown_policies.get(
            deployment_instance_id,
            _ShutdownPolicy(position_policy="preserve", confirmation_timeout_secs=30.0),
        )
        currency = self._settlement_currencies.get(deployment_instance_id)
        self._stopping.add(deployment_instance_id)
        try:
            # Before the node is asked to stop, while the strategy is still Running
            # and therefore still receiving the events these bridges depend on.
            await self._apply_shutdown_policy(deployment_instance_id, runtime, policy)
        except BaseException:
            self._stopping.discard(deployment_instance_id)
            raise
        stopped_gracefully = True
        run_task_failed = False
        try:
            # The handle is how a hosted run is stopped: run_async owns the node, so
            # calling stop on the node itself would be refused. Awaiting the task is
            # what waits for the shutdown sequence to finish.
            runtime.handle.stop()
            await asyncio.wait_for(asyncio.shield(task), timeout=self._stop_timeout_secs)
        except asyncio.CancelledError:
            if task.done():
                # The run itself was cancelled by someone else: it has ended, but
                # not by the stop it was asked for.
                run_task_failed = True
            else:
                await self._reap_after_caller_cancelled(deployment_instance_id, runtime)
                raise
        except TimeoutError:
            stopped_gracefully = False
            _log.error(
                "nt_stop_timeout",
                deployment_instance_id=deployment_instance_id,
                timeout_secs=self._stop_timeout_secs,
            )
        except Exception as exc:  # noqa: BLE001 — a node that died on its own is still stopped
            run_task_failed = True
            _log.warning(
                "nt_stop_run_task_error",
                deployment_instance_id=deployment_instance_id,
                **_sanitize_exception(exc),
            )
        valuation = None
        valuation_failure = None
        if stopped_gracefully and not run_task_failed and task.done():
            valuation, valuation_failure = self._value_stop_boundary(
                deployment_instance_id, runtime, currency
            )
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._reap_timeout_secs)
            except TimeoutError:
                # The run outlived its cancellation too, so the node may still be
                # running. Nothing is disposed or forgotten and no stop is claimed;
                # the reap continues in the background and finishes the cleanup.
                _log.error(
                    "nt_stop_reap_timeout",
                    deployment_instance_id=deployment_instance_id,
                    timeout_secs=self._reap_timeout_secs,
                )
                reap = asyncio.ensure_future(
                    self._reap_in_background(deployment_instance_id, runtime)
                )
                self._pending_reaps[deployment_instance_id] = reap
                return EngineStopBoundary(
                    stop_requested_at=requested_at,
                    node_was_running=True,
                    stopped_gracefully=False,
                    run_task_failed=run_task_failed,
                    reaped=False,
                    stop_effective_at=None,
                    reap=reap,
                )
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaping the run task
                if not task.done():
                    raise
        if deployment_instance_id not in self._dust_at_stop:
            # No boundary read to take it from: judge the positions alone, which is
            # not a valuation and needs none of what the stop failed to reach.
            self._dust_at_stop[deployment_instance_id] = self._position_dust_rows(
                deployment_instance_id, runtime
            )
        self._release_stopped_node(deployment_instance_id, runtime)
        return EngineStopBoundary(
            stop_requested_at=requested_at,
            node_was_running=True,
            stopped_gracefully=stopped_gracefully,
            run_task_failed=run_task_failed,
            reaped=True,
            # Placed after the boundary read: the node was already stopped when the
            # account was read, so the stop is in effect no later than this.
            stop_effective_at=datetime.now(UTC),
            valuation=valuation,
            valuation_failure=valuation_failure,
        )

    async def _reap_after_caller_cancelled(
        self, deployment_instance_id: str, runtime: _NodeRuntime
    ) -> None:
        """Whoever awaited the stop was cancelled, not the run: still end the run."""
        runtime.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(runtime.task), timeout=self._reap_timeout_secs)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaping the run task
            pass
        if runtime.task.done():
            self._release_stopped_node(deployment_instance_id, runtime)

    def _value_stop_boundary(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        currency: str | None,
    ) -> tuple[StopBoundaryValuation | None, str | None]:
        """Read the account of a node whose run has ended, before it is disposed."""
        observed_at = datetime.now(UTC)
        snapshot = self._portfolio_snapshot_provider.snapshot(
            runtime, currency=currency, refuse_float=True
        )
        # The last look at the account before the node goes: what the stop leaves.
        self._dust_at_stop[deployment_instance_id] = [holding.row() for holding in snapshot.dust]
        if (
            not snapshot.signable
            or currency is None
            or snapshot.currency != currency
            or not snapshot.price_watermarks_complete
        ):
            _log.warning(
                "nt_stop_boundary_valuation_unreliable",
                deployment_instance_id=deployment_instance_id,
                reason=snapshot.unreliable_reason
                or ("portfolio_dust_unpriced" if snapshot.unpriced_dust else None)
                or ("price_watermark_missing" if snapshot.reliable else None),
            )
            return None, "valuation_unreliable"
        marks_oldest_at = None
        if snapshot.marks_oldest_ns is not None:
            # Truncated to microseconds, so the watermark is never later than the price.
            seconds, nanos = divmod(snapshot.marks_oldest_ns, 1_000_000_000)
            marks_oldest_at = datetime.fromtimestamp(seconds, UTC).replace(
                microsecond=nanos // 1000
            )
        return (
            StopBoundaryValuation(
                currency=currency,
                equity=snapshot.equity,
                open_positions=tuple(snapshot.runner_fact_rows()),
                observed_at=observed_at,
                marks_oldest_at=marks_oldest_at,
            ),
            None,
        )

    async def _reap_in_background(self, deployment_instance_id: str, runtime: _NodeRuntime):
        """Wait for a run that outlived its cancellation, then release it."""
        try:
            try:
                await asyncio.shield(runtime.task)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — reaping the run task
                if not runtime.task.done():
                    raise
            reaped_at = datetime.now(UTC)
            self._release_stopped_node(deployment_instance_id, runtime)
            _log.info("nt_stop_reaped", deployment_instance_id=deployment_instance_id)
            return reaped_at
        finally:
            self._pending_reaps.pop(deployment_instance_id, None)

    def _release_stopped_node(self, deployment_instance_id: str, runtime: _NodeRuntime) -> None:
        # Only once the run has ended: run_async holds the node until then, and
        # 2.0 disposal releases the kernel without touching the runner's loop.
        self._dispose_node(deployment_instance_id, runtime.node)
        self._peak_equity.pop(deployment_instance_id, None)
        self._settlement_currencies.pop(deployment_instance_id, None)
        self._runner_fact_contexts.pop(deployment_instance_id, None)
        self._runner_safety_boundaries.pop(deployment_instance_id, None)
        self._cancel_disciplines.pop(deployment_instance_id, None)
        self._signed_unpriced_dust.pop(deployment_instance_id, None)
        self._event_forwarding_failures.pop(deployment_instance_id, None)
        self._release_execution_account_partition(deployment_instance_id)
        if self._active_nodes.get(deployment_instance_id) is runtime:
            self._active_nodes.pop(deployment_instance_id, None)
        self._containment_confirmed.discard(deployment_instance_id)
        self._lifecycle_authorities.pop(deployment_instance_id, None)
        self._shutdown_policies.pop(deployment_instance_id, None)
        self._stopping.discard(deployment_instance_id)
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
    def _risk_increasing_orders(
        deployment_instance_id: str, runtime: _NodeRuntime, orders: list
    ) -> list:
        """The working orders that could add exposure, judged as the submission gate does.

        By what each order would do to its strategy's open position, not by its
        reduce-only flag: a spot venue refuses that flag, so its exits carry none, and
        judged by the flag they were withdrawn as risk-increasing. Each order is judged
        against what the orders before it already claim of the position, so two exits
        for the whole of it do not both pass. Reduce-only orders go first: their answer
        does not depend on the claims, and they are the protection to keep.

        Anything that cannot be judged is counted as risk-increasing, so the judgement
        only ever withdraws more than the flag did, never less.
        """
        from custos.engines.nautilus.runner_safety import NautilusCachedOrderSemantics

        semantics = NautilusCachedOrderSemantics(runtime.cache)
        claimed: dict[str, Decimal] = {}
        risk_increasing = []
        for order in sorted(orders, key=lambda order: not bool(order.is_reduce_only)):
            try:
                instrument_id = semantics.order_instrument_id(order)
                already = claimed.get(instrument_id, Decimal(0))
                if semantics.order_is_risk_reducing(order, already):
                    claimed[instrument_id] = already + semantics.order_quantity(order)
                    continue
            except Exception as exc:  # noqa: BLE001 - unjudged counts as risk-increasing
                _log.error(
                    "nt_order_risk_unjudged",
                    deployment_instance_id=deployment_instance_id,
                    client_order_id=str(getattr(order, "client_order_id", None)),
                    error_type=type(exc).__name__,
                )
            risk_increasing.append(order)
        return risk_increasing

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
            risk_increasing = self._risk_increasing_orders(deployment_instance_id, runtime, orders)
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
                discipline = self._cancel_discipline(deployment_instance_id)
                for order in risk_increasing:
                    discipline.cancel(canceler, order)
            await asyncio.sleep(self._shutdown_poll_secs)
        _positions, orders = self._open_venue_state(runtime)
        risk_increasing = self._risk_increasing_orders(deployment_instance_id, runtime, orders)
        _log.error(
            "nt_shutdown_preserve_unconfirmed",
            deployment_instance_id=deployment_instance_id,
            risk_increasing_order_count=len(risk_increasing),
            residual_orders=self._cancel_discipline(deployment_instance_id).residual(
                risk_increasing
            ),
            timeout_secs=timeout_secs,
        )
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
            positions = self._without_dust(deployment_instance_id, runtime, positions)
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
                canceler = self._canceler(strategies)
                if canceler is None:
                    raise RuntimeError("shutdown flatten cannot cancel venue orders")
                # One by one rather than cancel_all_orders, which would carry every
                # order past what the venue last said about it.
                discipline = self._cancel_discipline(deployment_instance_id)
                for order in orders:
                    discipline.cancel(canceler, order)
            elif not orders and positions and not close_ack_pending:
                await self.flatten_positions(deployment_instance_id, "shutdown_policy")
                last_close_request = now
            await asyncio.sleep(self._shutdown_poll_secs)
        positions, orders = self._open_venue_state(runtime)
        positions = self._without_dust(deployment_instance_id, runtime, positions)
        _log.error(
            "nt_shutdown_flatten_unconfirmed",
            deployment_instance_id=deployment_instance_id,
            position_count=len(positions),
            order_count=len(orders),
            residual_orders=self._cancel_discipline(deployment_instance_id).residual(orders),
            timeout_secs=timeout_secs,
        )
        raise RuntimeError("shutdown flatten confirmation timed out")

    def stop_residue(self, deployment_instance_id: str) -> dict[str, int] | None:
        """What a stop has yet to see withdrawn, counted by why each order is still open.

        Empty once the node is gone: a confirmed stop leaves nothing it was asked to
        withdraw, and protection a preserve stop keeps is not residue. None when the
        venue state cannot be read.
        """
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None or runtime.task.done():
            return {}
        policy = self._shutdown_policies.get(deployment_instance_id)
        try:
            _positions, orders = self._open_venue_state(runtime)
        except RuntimeError:
            return None
        if policy is None or policy.position_policy != "flatten":
            orders = self._risk_increasing_orders(deployment_instance_id, runtime, orders)
        return self._cancel_discipline(deployment_instance_id).residual(orders)

    def stop_dust(self, deployment_instance_id: str) -> list[dict[str, str | None]] | None:
        """The dust a stop leaves behind: no order could take it, so it outlives the stop.

        Read now while the node is still held; once it is released, as read just
        before. None when it could not be read.
        """
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is not None and not runtime.task.done():
            return self._dust_rows(deployment_instance_id, runtime)
        return self._dust_at_stop.get(deployment_instance_id)

    def _dust_rows(
        self, deployment_instance_id: str, runtime: _NodeRuntime
    ) -> list[dict[str, str | None]] | None:
        try:
            snapshot = self._portfolio_snapshot_provider.snapshot(
                runtime, currency=self._settlement_currencies.get(deployment_instance_id)
            )
            return [holding.row() for holding in snapshot.dust]
        except Exception as exc:  # noqa: BLE001 - a stop's summary must still be written
            _log.warning(
                "nt_stop_dust_unreadable",
                deployment_instance_id=deployment_instance_id,
                error_type=type(exc).__name__,
            )
            return None

    def _position_dust_rows(
        self, deployment_instance_id: str, runtime: _NodeRuntime
    ) -> list[dict[str, str | None]] | None:
        try:
            dust = self._portfolio_snapshot_provider.position_dust(runtime)
        except Exception as exc:  # noqa: BLE001 - a stop's summary must still be written
            _log.warning(
                "nt_stop_dust_unreadable",
                deployment_instance_id=deployment_instance_id,
                error_type=type(exc).__name__,
            )
            return None
        return [holding.row() for holding in dust.values()]

    def _without_dust(self, deployment_instance_id: str, runtime: _NodeRuntime, positions: list):
        """The positions an order could still close: dust counts as flat.

        No order can close dust, so waiting for it to go would never confirm, and
        asking for it to be closed would only be refused. A judgement that cannot be
        made counts every position as real.
        """
        if not positions:
            return positions
        try:
            dust = self._portfolio_snapshot_provider.position_dust(runtime)
        except Exception as exc:  # noqa: BLE001 - unjudged counts as real
            _log.error(
                "nt_position_dust_unjudged",
                deployment_instance_id=deployment_instance_id,
                error_type=type(exc).__name__,
            )
            return positions
        return [position for position in positions if str(position.instrument_id) not in dust]

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

        # The exposure guard asks ``get_open_notional`` the moment this says ready, and
        # that raises unless the snapshot is reliable. Leaving valuation out of the
        # boundary is what let the guard trip on data still in flight: the wait added on
        # 2026-08-01 waits for exactly this answer, so the answer has to include the
        # thing it was waiting for. Otherwise the same failure returns under a different
        # reason -- equity missing then, marks missing now, and any position at all is
        # enough to need a price that a fresh subscription has not delivered yet.
        valuation = self._portfolio_snapshot_provider.snapshot(
            runtime, currency=self._declared_currency(str(authority.deployment_instance_id))
        )

        return EngineReadinessChecks(
            node_task_alive=not runtime.task.done(),
            data_connectivity_ready=connectivity.data_connected,
            execution_connectivity_ready=connectivity.exec_connected,
            portfolio_initialized=bool(portfolio is not None and portfolio.is_initialized),
            portfolio_valuation_ready=valuation.reliable,
            reconciliation_initialized=(
                node_running and (reconciliation_enabled or not reconciliation_required)
            ),
            strategy_accepting_lifecycle=bool(strategies)
            and all(strategy.is_running for strategy in strategies),
            mandatory_capabilities_active=(
                authority.trading_mode in {"sandbox", "testnet"}
                or (
                    str(authority.deployment_instance_id) in self._runner_safety_boundaries
                    and str(authority.deployment_instance_id) in self._runner_fact_contexts
                    and str(authority.deployment_instance_id) not in self._event_forwarding_failures
                )
            ),
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
            # The shield exists to separate "the node was cancelled" from "whoever was
            # waiting was cancelled". Both raise here, and only the first is a terminal
            # event. Reporting the second restarts a healthy engine -- and swallowing
            # the cancel instead of propagating it leaves the supervision task alive,
            # so the gather that follows a shutdown never finishes.
            if not task.done():
                raise
        except Exception:  # noqa: BLE001 - reason is deliberately sanitized
            pass
        return EngineTerminalEvent.from_authority(
            authority,
            reason_code=self._terminal_reason(task),
            retryable=True,
        )

    @staticmethod
    def _terminal_reason(task: asyncio.Task[object]) -> str:
        """How the node task ended, read from the task rather than from how we woke."""
        if task.cancelled():
            return "engine_task_cancelled"
        if task.exception() is not None:
            return "engine_task_failed"
        return "engine_task_exited"

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
        self._require_signable(deployment_instance_id, snapshot)
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
        self._require_signable(deployment_instance_id, snapshot)
        return snapshot.equity, snapshot.valuation_rows()

    async def runner_fact_cash_snapshot(
        self, deployment_instance_id: str, currency: str
    ) -> tuple[Decimal, tuple[dict[str, str], ...]]:
        runtime = self._active_nodes.get(deployment_instance_id)
        if runtime is None or deployment_instance_id not in self._runner_fact_contexts:
            raise RuntimeError("cash inventory requires an active RunnerFact deployment")
        snapshot = self._portfolio_snapshot_provider.snapshot(runtime, currency=currency)
        self._require_signable(deployment_instance_id, snapshot)
        if snapshot.cash_inventory is None:
            raise RuntimeError("cash inventory is unavailable or unreliable")
        return snapshot.equity, snapshot.cash_inventory

    def _require_signable(
        self, deployment_instance_id: str, snapshot: NautilusPortfolioSnapshot
    ) -> None:
        """Refuse a signed valuation the account cannot fully back.

        Safety may leave unpriced dust out; a signed valuation may not, so it is not
        produced, and a signed runtime log says why.
        """
        if not snapshot.reliable:
            raise RuntimeError(f"portfolio snapshot unreliable: {snapshot.unreliable_reason}")
        self._sign_unpriced_dust(deployment_instance_id, snapshot)
        if not snapshot.signable:
            raise RuntimeError("portfolio snapshot unsignable: portfolio_dust_unpriced")

    def _sign_unpriced_dust(
        self, deployment_instance_id: str, snapshot: NautilusPortfolioSnapshot
    ) -> None:
        """Sign the unpriced dust that holds valuations back, once each time it changes."""
        holdings = snapshot.unpriced_dust
        identity = tuple(
            sorted(
                (holding.instrument_id, str(holding.quantity), holding.quantity_source)
                for holding in holdings
            )
        )
        if self._signed_unpriced_dust.get(deployment_instance_id, ()) == identity:
            return
        self._signed_unpriced_dust[deployment_instance_id] = identity
        if not holdings:
            return
        fields = {
            "holdings": [
                {
                    key: value
                    for key, value in holding.row().items()
                    if key not in {"reason", "price"}
                }
                for holding in holdings
            ]
        }
        _log.warning(
            "nt_portfolio_dust_unpriced",
            deployment_instance_id=deployment_instance_id,
            **fields,
        )
        logs = self._runtime_log()
        context = self._runner_fact_contexts.get(deployment_instance_id)
        if logs is None or context is None:
            return
        authority = context[0].authority
        try:
            logs.emit_sync(
                authority,
                level="WARN",
                component="custos.portfolio",
                message="portfolio_dust_unpriced",
                structured_fields=fields,
                correlation_id=runner_fact_event_id(
                    authority.stream_key,
                    "portfolio_dust_unpriced",
                    *(part for entry in identity for part in entry),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - asked again on the next valuation
            self._signed_unpriced_dust.pop(deployment_instance_id, None)
            _log.error(
                "nt_portfolio_dust_fact_failed",
                deployment_instance_id=deployment_instance_id,
                error_type=type(exc).__name__,
            )

    def _runtime_log(self) -> RunnerRuntimeLogPort | None:
        if (
            self._runtime_logs is None
            and self._runner_fact_emitter is not None
            and self._capability_receipt is not None
        ):
            self._runtime_logs = RunnerRuntimeLogEmitter(
                emitter=self._runner_fact_emitter,
                capability=self._capability_receipt,
                redactor=RuntimeLogRedactor(),
            )
        return self._runtime_logs

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
        # Before reading the positions, and regardless of what they say: the breaker's
        # freeze only stops new submissions, and an order the venue has already
        # accepted is past that gate. Whether a position is open right now says
        # nothing about it -- a resting entry fills on the next tick and reopens
        # exactly the exposure this is containing. Reduce-only orders stay: they are
        # the position's own protection, which containment wants kept.
        working = self._cancel_risk_increasing_orders(deployment_instance_id, runtime, reason)
        held = list(runtime.cache.positions_open())
        closable = self._without_dust(deployment_instance_id, runtime, held)
        instrument_ids = {position.instrument_id for position in closable}
        if not instrument_ids:
            if working or held:
                # Nothing to close, but something that could reopen, or dust no order
                # can take. The cancel above was a request like any other, and the
                # dust is what reconciliation delivered; confirm against both.
                await self._confirm_containment(deployment_instance_id, runtime, reason)
                return
            # Nothing was contained, and at startup that is not the same as nothing being
            # there: reconciliation may not yet have delivered the account's existing
            # positions, in which case they arrive seconds later untouched. Recording this
            # as a flatten would read as containment and stop anyone from asking further,
            # which is precisely what C9 asks us not to do. An order book that could not
            # be read lands here too -- ``working`` is None then, and unreadable is not
            # clear.
            if working == 0 and deployment_instance_id in self._containment_confirmed:
                _log.info(
                    "nt_containment_still_clear",
                    deployment_instance_id=deployment_instance_id,
                    reason=reason,
                )
                return
            _log.error(
                "nt_flatten_containment_unconfirmed",
                deployment_instance_id=deployment_instance_id,
                reason=reason,
            )
            return
        close_requests = self._request_close(deployment_instance_id, runtime, instrument_ids)
        _log.warning(
            "positions_flattened",
            deployment_instance_id=deployment_instance_id,
            reason=reason,
            instrument_count=len(instrument_ids),
            close_requests=close_requests,
        )
        await self._confirm_containment(deployment_instance_id, runtime, reason)

    def _request_close(
        self, deployment_instance_id: str, runtime: _NodeRuntime, instrument_ids: set
    ) -> int:
        """Ask each strategy to close each instrument, as the account allows; returns how many."""
        discipline = self._cancel_discipline(deployment_instance_id)
        sent = 0
        for strategy in runtime.strategies:
            # NT's own close_all_positions is reduce-only, and a venue that refuses that
            # form refuses it here too -- leaving containment unable to contain at the
            # one moment it must. Toolkit strategies expose a close that drops
            # reduce-only on recorded evidence of that refusal; prefer it when present.
            # Duck-typed, not isinstance: the toolkit is not required of a deployment,
            # and a strategy without it has to keep behaving exactly as before.
            close_with_fallback = getattr(strategy, "close_all_positions_with_fallback", None)
            close = (
                close_with_fallback
                if callable(close_with_fallback)
                else strategy.close_all_positions
            )
            for instrument_id in instrument_ids:
                sent += discipline.close(functools.partial(close, instrument_id))
        return sent

    async def _confirm_containment(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        reason: str,
    ) -> None:
        """Read the venue back until it agrees, re-asking for whatever is still there.

        Having sent a close is not the same as the position being gone, and the
        difference is the whole of this deployment's remaining exposure. The
        shutdown path has confirmed its own work this way since it was written;
        containment, which is the more urgent of the two, never did.

        Done means no positions *and* no risk-increasing orders. Reduce-only
        orders are the position's protection and are meant to survive
        containment, so counting them as leftovers would silently undo that.

        A deadline expiring is reported, not raised: this runs inside the
        supervisor's tripped branch, and an exception there travels up into the
        supervision task -- which the daemon treats as grounds to fail the whole
        process. One slow venue would stop every other instance from being
        watched. The breaker stays frozen either way, so new submissions remain
        blocked while this is unresolved.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._containment_confirm_secs
        last_request = loop.time()
        positions: list = []
        risk_increasing: list = []
        while True:
            try:
                positions, orders = self._open_venue_state(runtime)
            except Exception as exc:  # noqa: BLE001 - unreadable is not confirmed
                # Neither "it worked" nor a reason to sit here until the deadline:
                # the cache will not answer, and waiting for it to change its mind
                # only delays the operator hearing about it.
                _log.error(
                    "nt_containment_state_unreadable",
                    deployment_instance_id=deployment_instance_id,
                    reason=reason,
                    error_type=type(exc).__name__,
                )
                return
            positions = self._without_dust(deployment_instance_id, runtime, positions)
            risk_increasing = self._risk_increasing_orders(deployment_instance_id, runtime, orders)
            if not positions and not risk_increasing:
                self._containment_confirmed.add(deployment_instance_id)
                _log.info(
                    "nt_containment_confirmed",
                    deployment_instance_id=deployment_instance_id,
                    reason=reason,
                    protective_order_count=len(orders),
                )
                return
            now = loop.time()
            if now >= deadline:
                break
            if now - last_request >= self._containment_retry_secs:
                self._retry_containment(deployment_instance_id, runtime, positions, risk_increasing)
                last_request = now
            await asyncio.sleep(self._shutdown_poll_secs)
        _log.error(
            "nt_containment_not_confirmed",
            deployment_instance_id=deployment_instance_id,
            reason=reason,
            position_count=len(positions),
            risk_increasing_order_count=len(risk_increasing),
            residual_orders=self._cancel_discipline(deployment_instance_id).residual(
                risk_increasing
            ),
        )

    def _retry_containment(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        positions: list,
        risk_increasing: list,
    ) -> None:
        """Ask again for what is still outstanding, as far as the venue's answers allow."""
        if risk_increasing:
            canceler = self._canceler(runtime.strategies)
            if canceler is not None:
                discipline = self._cancel_discipline(deployment_instance_id)
                for order in risk_increasing:
                    discipline.cancel(canceler, order)
        if positions:
            self._request_close(
                deployment_instance_id, runtime, self._instrument_ids(positions, [])
            )

    @staticmethod
    def _canceler(strategies: tuple):
        return next(
            (
                strategy
                for strategy in strategies
                if callable(getattr(strategy, "cancel_order", None))
            ),
            None,
        )

    def _cancel_risk_increasing_orders(
        self,
        deployment_instance_id: str,
        runtime: _NodeRuntime,
        reason: str,
    ) -> int | None:
        """Withdraw this instance's working orders that could add exposure.

        Same judgement as the preserve shutdown: reduce-only is protection and stays,
        everything else is a way back into the position being contained. Cancelling is
        a request like any other -- this does not claim the venue has withdrawn them,
        only that it was asked, which is why nothing here is logged as confirmation.

        Returns how many risk-increasing orders were found, or ``None`` when the order
        book could not be read. The caller needs those apart: zero means the book is
        clear, ``None`` means nobody knows.
        """
        try:
            _positions, orders = self._open_venue_state(runtime)
        except Exception as exc:  # noqa: BLE001 - closing the position outranks this
            # Containment's first duty is to close the position. An order list we
            # cannot read must not stop that from happening -- it is reported loudly
            # and the close proceeds, rather than the whole flatten failing on the
            # part that was meant to make it more thorough.
            _log.error(
                "nt_containment_orders_unreadable",
                deployment_instance_id=deployment_instance_id,
                reason=reason,
                error_type=type(exc).__name__,
            )
            return None
        risk_increasing = self._risk_increasing_orders(deployment_instance_id, runtime, orders)
        if not risk_increasing:
            return 0
        canceler = self._canceler(runtime.strategies)
        if canceler is None:
            _log.error(
                "nt_containment_cannot_cancel_risk_increasing_orders",
                deployment_instance_id=deployment_instance_id,
                reason=reason,
                order_count=len(risk_increasing),
            )
            return len(risk_increasing)
        discipline = self._cancel_discipline(deployment_instance_id)
        cancel_requests = sum(discipline.cancel(canceler, order) for order in risk_increasing)
        _log.warning(
            "nt_containment_cancelled_risk_increasing_orders",
            deployment_instance_id=deployment_instance_id,
            reason=reason,
            order_count=len(risk_increasing),
            cancel_requests=cancel_requests,
        )
        return len(risk_increasing)

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
        # What the cache holds open, dust included, whatever the valuation made of it:
        # a status that could not be valued must not report the account as empty.
        try:
            position_count = len(runtime.cache.positions_open())
        except AttributeError:
            position_count = len(snapshot.positions)
        if forwarding_failure is not None:
            # An event never reached one of the runner's sinks, so a fact is missing or
            # a reservation is still held. Whatever the portfolio says, what this
            # deployment reports about itself no longer matches what happened.
            return EngineStatus(
                phase="degraded",
                position_count=position_count,
                order_count=len(orders),
                open_notional=snapshot.open_notional,
                peak_equity=self._peak_equity.get(deployment_instance_id, Decimal("0")),
                current_equity=snapshot.equity if snapshot.reliable else Decimal("0"),
                drawdown_pct=Decimal("0"),
                reliable=False,
                unreliable_reason=f"runner_event_forwarding_failed:{forwarding_failure}",
                dust=snapshot.dust,
            )
        if not snapshot.reliable:
            return EngineStatus(
                phase="degraded",
                position_count=position_count,
                order_count=len(orders),
                open_notional=snapshot.open_notional,
                peak_equity=self._peak_equity.get(deployment_instance_id, Decimal("0")),
                current_equity=snapshot.equity,
                drawdown_pct=Decimal("0"),
                reliable=False,
                unreliable_reason=snapshot.unreliable_reason,
                dust=snapshot.dust,
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
            position_count=position_count,
            order_count=len(orders),
            open_notional=snapshot.open_notional,
            peak_equity=peak,
            current_equity=current_equity,
            drawdown_pct=drawdown_pct,
            reliable=True,
            dust=snapshot.dust,
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
            self._containment_confirmed.discard(deployment_instance_id)
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
