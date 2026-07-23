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

from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

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


def _reject_float_money(instance: Any) -> None:
    """Raise ``TypeError`` if any money field on ``instance`` is not a
    ``Decimal``. Called from every snapshot dataclass's ``__post_init__``."""

    for field in fields(instance):
        if field.name not in _MONEY_FIELDS_SHOULD_BE_DECIMAL:
            continue
        value = getattr(instance, field.name)
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
    """A single open order exposed by ``get_orders``. ``quantity`` + ``price``
    are ``Decimal``; identifier / side / status remain strings."""

    client_order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
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
                self.reconciliation_initialized,
                self.strategy_accepting_lifecycle,
                self.mandatory_capabilities_active,
            )
        )

    @classmethod
    def all_ready(cls) -> EngineReadinessChecks:
        return cls(True, True, True, True, True, True, True)


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

    def supports_venue(self, venue: str) -> bool: ...

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
