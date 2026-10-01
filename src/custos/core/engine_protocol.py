"""Engine-agnostic execution protocol.

All engine hosts (nautilus / hummingbot / freqtrade / athanor / nt-rust) must
implement ``ExecutionEngineProtocol``. The command coordinator and lifecycle
supervisor operate exclusively through this interface so they remain
engine-agnostic.

``supports_trading_mode`` / ``supports_venue`` are synchronous capability queries the
execution-admission layer calls before any async work; a host declares up-front whether it can
handle live execution and which venues it wires.

Money contract (red line 0.4): every monetary field on every snapshot
dataclass is ``Decimal``. Dataclass frozen annotations alone cannot enforce
runtime types, so a shared ``__post_init__`` helper rejects float mixed into
money fields at construction time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


class EngineDependencyUnavailable(RuntimeError):
    """A required runtime authority is not available yet.

    The command remains valid and must be retried after the dependency arrives;
    this is not an engine crash and must not consume the restart budget.
    """


class EngineDeploymentRefused(RuntimeError):
    """The engine refuses this deployment, and would refuse it on every attempt.

    Raised for a decision about the command rather than a failure of the engine,
    such as a strategy whose trading scope differs from the one the deployment
    authorizes. Retrying cannot change the answer, so the instance is quarantined
    at once under ``reason_code`` instead of spending the restart budget.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code


# Runtime invariant: every Decimal-declared money field on the snapshot
# dataclasses must be a real ``Decimal`` — a float slipping through breaks
# money math (red line 0.4). Non-money fields (identifier strings, phase
# names, epoch timestamps, integer counters) are outside this set.
_MONEY_FIELDS_SHOULD_BE_DECIMAL = frozenset(
    {
        # PositionSnapshot
        "quantity",
        "avg_px",
        "unrealized_pnl",
        "notional",
        # OrderSnapshot shares ``quantity``; adds ``price``.
        "price",
        # EngineStatus
        "open_notional",
        "peak_equity",
        "current_equity",
        "drawdown_pct",
    }
)

# Money fields that may be absent, by owning dataclass. Keeping this narrow means a
# new nullable money field has to be declared deliberately rather than inherited.
_OPTIONAL_MONEY_FIELDS: dict[str, frozenset[str]] = {
    "OrderSnapshot": frozenset({"price"}),
}


@runtime_checkable
class ActivatedEngineArtifactV1(Protocol):
    """Verified, durably activated strategy accepted by an execution engine.

    Deployment commands never carry import paths or code hashes.  The artifact
    runtime resolves and verifies StrategyRelease bytes, activates them under an
    immutable local identity, and hands only this narrow capability to a host.
    """

    @property
    def activation_id(self) -> str: ...

    @property
    def strategy(self) -> object: ...

    def create_strategy(self) -> object: ...


def _reject_float_money(instance: Any) -> None:
    """Raise ``TypeError`` if any money field on ``instance`` is not a
    ``Decimal``. Called from every snapshot dataclass's ``__post_init__``.

    ``None`` passes only where the field is declared optional, which today means
    an order that carries no limit price. That is absence of a price, not a price
    of zero, so it is represented as absence rather than coerced into money.
    """

    optional = _OPTIONAL_MONEY_FIELDS.get(type(instance).__name__, frozenset())
    for field in fields(instance):
        if field.name not in _MONEY_FIELDS_SHOULD_BE_DECIMAL:
            continue
        value = getattr(instance, field.name)
        if value is None and field.name in optional:
            continue
        if not isinstance(value, Decimal):
            raise TypeError(
                f"{type(instance).__name__}.{field.name} must be Decimal, "
                f"got {type(value).__name__}"
            )


@dataclass(frozen=True)
class ConnectivityState:
    """Engine connectivity snapshot for the zombie watchdog. ``checked_at_epoch_s``
    is a wall-clock timestamp (not money — float is fine)."""

    data_connected: bool
    exec_connected: bool
    checked_at_epoch_s: float


@dataclass(frozen=True)
class PositionSnapshot:
    """A single open position exposed by ``get_positions``. Every money field is
    ``Decimal``. ``notional`` is gross exposure (``abs(quantity) * avg_px``).
    """

    instrument_id: str
    quantity: Decimal
    avg_px: Decimal
    unrealized_pnl: Decimal
    notional: Decimal

    def __post_init__(self) -> None:  # noqa: D401 — invariant enforcement
        _reject_float_money(self)


@dataclass(frozen=True)
class OrderSnapshot:
    """A single open order exposed by ``get_orders``. ``quantity`` is ``Decimal``;
    identifier / side / status remain strings.

    ``price`` is the order's limit price and is ``None`` for order types that have
    none, such as a market order. Zero is not used as a stand-in, since it would be
    indistinguishable from a genuine limit at zero.
    """

    client_order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal | None
    status: str

    def __post_init__(self) -> None:
        _reject_float_money(self)


@dataclass(frozen=True)
class EngineStatus:
    """Engine-side runner state snapshot: aggregate counters + gross exposure +
    equity high-water mark + drawdown percentage.

    The reconciler feeds ``current_equity`` into the fallback breaker so the
    disconnect-resilient drawdown breach is evaluated even while the cloud is
    unreachable. ``peak_equity`` + ``drawdown_pct`` are engine-tracked for
    observability; ``drawdown_pct`` is a percentage (e.g. ``Decimal("20")`` =
    20%).
    """

    phase: str
    position_count: int
    order_count: int
    open_notional: Decimal
    peak_equity: Decimal
    current_equity: Decimal
    drawdown_pct: Decimal
    reliable: bool = True
    unreliable_reason: str | None = None

    def __post_init__(self) -> None:
        _reject_float_money(self)
        if self.reliable and self.unreliable_reason is not None:
            raise ValueError("a reliable engine status cannot have an unreliable reason")
        if not self.reliable and not self.unreliable_reason:
            raise ValueError("an unreliable engine status needs an unreliable reason")


@dataclass(frozen=True, slots=True)
class EngineLifecycleAuthority:
    """Exact signed command identity accepted by an engine lifecycle adapter."""

    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str
    generation: int
    trading_mode: str

    def __post_init__(self) -> None:
        if self.deployment_instance_id.int == 0 or self.deployment_spec_id.int == 0:
            raise ValueError("engine lifecycle identity must not be nil")
        if len(self.deployment_spec_digest) != 64 or any(
            value not in "0123456789abcdef" for value in self.deployment_spec_digest
        ):
            raise ValueError("engine lifecycle spec digest must be lowercase SHA-256")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("engine lifecycle generation must be positive")
        if self.trading_mode not in {"sandbox", "testnet", "live"}:
            raise ValueError("engine lifecycle trading mode is invalid")

    @classmethod
    def from_verified_command(cls, verified: Any) -> EngineLifecycleAuthority:
        command = verified.command
        return cls(
            deployment_instance_id=UUID(str(command.deployment_instance_id)),
            deployment_spec_id=UUID(str(command.deployment_spec_id)),
            deployment_spec_digest=str(command.deployment_spec_digest),
            generation=int(command.generation),
            trading_mode=str(command.trading_mode),
        )

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> EngineLifecycleAuthority:
        return cls(
            deployment_instance_id=UUID(str(spec["deployment_instance_id"])),
            deployment_spec_id=UUID(str(spec["deployment_spec_id"])),
            deployment_spec_digest=str(spec["deployment_spec_digest"]),
            generation=int(spec["generation"]),
            trading_mode=str(spec["trading_mode"]),
        )


@dataclass(frozen=True, slots=True)
class EngineReadinessChecks:
    """Evidence that a created task has crossed every mandatory ready boundary."""

    node_task_alive: bool
    data_connectivity_ready: bool
    execution_connectivity_ready: bool
    portfolio_initialized: bool
    portfolio_valuation_ready: bool
    reconciliation_initialized: bool
    strategy_accepting_lifecycle: bool
    mandatory_capabilities_active: bool

    @property
    def ready(self) -> bool:
        return all(
            (
                self.node_task_alive,
                self.data_connectivity_ready,
                self.execution_connectivity_ready,
                self.portfolio_initialized,
                self.portfolio_valuation_ready,
                self.reconciliation_initialized,
                self.strategy_accepting_lifecycle,
                self.mandatory_capabilities_active,
            )
        )

    @classmethod
    def all_ready(cls) -> EngineReadinessChecks:
        # Named, not positional: a new boundary should fail to construct here
        # rather than silently shift every value one field to the left.
        return cls(
            node_task_alive=True,
            data_connectivity_ready=True,
            execution_connectivity_ready=True,
            portfolio_initialized=True,
            portfolio_valuation_ready=True,
            reconciliation_initialized=True,
            strategy_accepting_lifecycle=True,
            mandatory_capabilities_active=True,
        )


@dataclass(frozen=True, slots=True)
class EngineReadyReceipt:
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    deployment_spec_digest: str
    generation: int
    ready_at_ns: int
    checks: EngineReadinessChecks

    def __post_init__(self) -> None:
        if type(self.ready_at_ns) is not int or self.ready_at_ns < 1:
            raise ValueError("engine ready timestamp must be positive")
        if not self.checks.ready:
            raise ValueError("engine ready receipt requires every readiness check")

    @classmethod
    def from_authority(
        cls,
        authority: EngineLifecycleAuthority,
        *,
        checks: EngineReadinessChecks,
        ready_at_ns: int,
    ) -> EngineReadyReceipt:
        return cls(
            deployment_instance_id=authority.deployment_instance_id,
            deployment_spec_id=authority.deployment_spec_id,
            deployment_spec_digest=authority.deployment_spec_digest,
            generation=authority.generation,
            ready_at_ns=ready_at_ns,
            checks=checks,
        )


@dataclass(frozen=True, slots=True)
class EngineTerminalEvent:
    deployment_instance_id: UUID
    deployment_spec_id: UUID
    generation: int
    reason_code: str
    retryable: bool

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("engine terminal generation must be positive")
        if not self.reason_code.strip():
            raise ValueError("engine terminal reason is required")

    @classmethod
    def from_authority(
        cls,
        authority: EngineLifecycleAuthority,
        *,
        reason_code: str,
        retryable: bool,
    ) -> EngineTerminalEvent:
        return cls(
            deployment_instance_id=authority.deployment_instance_id,
            deployment_spec_id=authority.deployment_spec_id,
            generation=authority.generation,
            reason_code=reason_code,
            retryable=retryable,
        )


# The reasons a stop boundary itself can give for not confirming a valuation.
# They are a subset of the terminal valuation reason codes, which order them.
STOP_BOUNDARY_VALUATION_FAILURES = frozenset({"valuation_unavailable", "valuation_unreliable"})


def _aware(value: datetime | None, field: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StopBoundaryValuation:
    """What the account held once the node's run had ended, read before disposal.

    ``marks_oldest_at`` is the oldest source time of every price the valuation
    used, or ``None`` when it used no price at all (no position and nothing to
    convert). A price whose source time is unknown never reaches this type: the
    host reports such a read as unreliable instead.
    """

    currency: str
    equity: Decimal
    open_positions: tuple[Mapping[str, str], ...]
    observed_at: datetime
    marks_oldest_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.equity, Decimal):
            raise TypeError("stop boundary equity must be a Decimal")
        _aware(self.observed_at, "observed_at")
        _aware(self.marks_oldest_at, "marks_oldest_at")


@dataclass(frozen=True, slots=True)
class EngineStopBoundary:
    """How a stop ended, as the host observed it.

    ``reaped`` is the only claim that the node is no longer running. When the
    run ignored both the graceful stop and the cancellation within their bounds
    it is ``False``: the node may still be running, nothing may be committed as
    applied, and ``reap`` resolves with the moment the run finally ended.
    """

    stop_requested_at: datetime
    node_was_running: bool
    stopped_gracefully: bool
    run_task_failed: bool
    reaped: bool
    stop_effective_at: datetime | None
    valuation: StopBoundaryValuation | None = None
    valuation_failure: str | None = None
    reap: Awaitable[datetime] | None = None

    def __post_init__(self) -> None:
        _aware(self.stop_requested_at, "stop_requested_at")
        _aware(self.stop_effective_at, "stop_effective_at")
        if not self.reaped and self.reap is None:
            raise ValueError("an unreaped stop must carry the awaitable that reaps it")
        if self.reaped and self.reap is not None:
            raise ValueError("a reaped stop has nothing left to await")
        if self.valuation_failure is not None and (
            self.valuation_failure not in STOP_BOUNDARY_VALUATION_FAILURES
        ):
            raise ValueError("stop boundary valuation failure is not a known reason")
        if self.valuation is not None and self.valuation_failure is not None:
            raise ValueError("a stop boundary has a valuation or a reason it has none")

    def stop_causes(self) -> frozenset[str]:
        """Every reason this stop cannot confirm a same-boundary valuation."""

        if not self.node_was_running:
            return frozenset({"engine_not_running_at_stop"})
        causes: set[str] = set()
        if not self.stopped_gracefully:
            causes.add("stop_timeout")
        if self.run_task_failed:
            causes.add("engine_task_failed")
        if self.valuation is None:
            if self.valuation_failure is not None:
                causes.add(self.valuation_failure)
            elif not causes:
                causes.add("valuation_unreliable")
        return frozenset(causes)


@runtime_checkable
class ExecutionEngineProtocol(Protocol):
    """Engine contract every host must satisfy.

    Tier-1 methods (deploy / reconfigure / stop / capability queries) drive the
    command coordinator and lifecycle supervisor. Tier-2 methods expose runner-level
    risk and connectivity state so the engine-agnostic guards (notional cap,
    fallback breaker, zombie watchdog) can enforce the disconnect-resilient red
    line without knowing the concrete engine.  Every host implements the full
    surface; the ``@runtime_checkable`` isinstance check stays green because
    both shipped hosts add each Tier-2 method in lockstep with the protocol.
    """

    # -- Tier-1: lifecycle + capability ------------------------------------
    async def deploy(
        self,
        spec: dict,
        credential: dict,
        artifact: ActivatedEngineArtifactV1,
    ) -> str: ...

    async def reconfigure(self, spec: dict) -> None: ...

    async def stop(self, deployment_instance_id: str) -> None: ...

    def supports_trading_mode(self, mode: str) -> bool: ...

    def supports_venue(self, venue: str, mode: str) -> bool: ...

    # -- Tier-2: runner-level risk / connectivity state --------------------
    async def get_open_notional(self, deployment_instance_id: str) -> Decimal: ...

    async def check_engine_connected(self, deployment_instance_id: str) -> ConnectivityState: ...

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None: ...

    # -- Tier-2: observability snapshot ------------------------------------
    async def get_positions(self, deployment_instance_id: str) -> list[PositionSnapshot]: ...

    async def get_orders(self, deployment_instance_id: str) -> list[OrderSnapshot]: ...

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus: ...

    # -- Additive lifecycle supervision ---------------------------
    async def wait_ready(
        self,
        authority: EngineLifecycleAuthority,
        *,
        timeout_secs: float,
    ) -> EngineReadyReceipt: ...

    async def wait_terminal(
        self,
        authority: EngineLifecycleAuthority,
    ) -> EngineTerminalEvent: ...
