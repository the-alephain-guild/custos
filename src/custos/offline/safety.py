"""The offline lane's own exposure guard, and the ceilings it enforces.

There is no signed owner policy on an operator's own machine, so the strictest
non-live fallback is the floor. A spec may name its own ceilings: this lane
already takes the strategy, the venue and the credential from the same unsigned
spec, and the red line asks that a guard keep running while the cloud is
unreachable — not that it enforce one particular number. The default alone would
make the lane useless for its purpose, since a funded sandbox account breaches
$200 on its first position.

A ceiling that cannot be read is refused. Falling back to the default instead
would leave the operator believing a limit is in force that never was.

The guard evaluates on its own clock, touching only the engine, so a transport
that is down, wedged or gone cannot stall it. Each watched deployment carries its
own breaker: the ceiling is per deployment here rather than per runner, which
keeps one deployment's equity high-water mark out of another's drawdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol, runtime_checkable

from custos.core.engine_safety import EngineSafetyPort, EngineSafetySupervisor, EngineSafetyTick
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.log import get_logger
from custos.offline.mode_guard import refuse_live
from custos.offline.spec import OfflineDeploymentSpec

_log = get_logger("custos.offline.safety")

_NOTIONAL_KEY: Final = "max_total_notional"
_DRAWDOWN_KEY: Final = "max_drawdown_pct"
_DECLARED_KEYS: Final = (_NOTIONAL_KEY, _DRAWDOWN_KEY)
_SPEC_SOURCE: Final = "offline_spec_risk_config"
TICK_SECS: Final = 5.0
EVALUATION_DEADLINE_SECS: Final = 10.0
# How long to let an engine finish starting before guarding it regardless.
#
# NautilusTrader states its own intent up front -- ``reconciliation_startup_delay_secs``
# defaults to 10 -- and on 2026-07-31 hardware startup reconciliation completed 4.9s in.
# This is an order of magnitude above that, because the cost of waiting slightly too long
# is a short unguarded window on a deployment that is not trading yet, while the cost of
# giving up too early is the fail-closed trip this bound exists to avoid.
#
# It is a backstop, not a schedule: an engine that never reports ready must still end up
# guarded rather than silently exempt.
READINESS_TIMEOUT_SECS: Final = 120.0


@runtime_checkable
class OfflineSafetyEngine(EngineSafetyPort, Protocol):
    """Containment, plus the ability to end a deployment and to be asked whether it did.

    ``EngineSafetyPort`` covers reading exposure and flattening it, which is what
    the shared supervisor needs. That a trip should also end the deployment is this
    lane's own reading of what a trip means, so the wider requirement lives here
    rather than widening the port every engine host implements.
    """

    async def stop(self, deployment_instance_id: str) -> None: ...

    def attached(self, deployment_instance_id: str) -> bool: ...


def resolve_breaker_config(spec: OfflineDeploymentSpec) -> FallbackBreakerConfig:
    """Read the ceilings this spec asks for, starting from the strictest defaults."""

    mode = refuse_live(spec.trading_mode.value, source="deployment spec")
    default = FallbackBreakerConfig.strictest_local_fallback(mode.value)
    declared = spec.risk_config
    if not declared:
        return default

    unknown = sorted(set(declared) - set(_DECLARED_KEYS))
    if unknown:
        raise ValueError(
            f"risk_config names {', '.join(unknown)}, which this lane does not enforce; "
            f"it reads {' and '.join(_DECLARED_KEYS)}"
        )

    return replace(
        default,
        max_notional=_positive_decimal(declared, _NOTIONAL_KEY, default.max_notional),
        max_drawdown_pct=_positive_decimal(declared, _DRAWDOWN_KEY, default.max_drawdown_pct),
        source=_SPEC_SOURCE,
    )


def _positive_decimal(declared: dict[str, object], key: str, fallback: Decimal) -> Decimal:
    if key not in declared:
        return fallback
    value = declared[key]
    # bool is an int, and a float carries a binary fraction money must not inherit.
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError(
            f"{key} must be a decimal string or a whole number, not {type(value).__name__}"
        )
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{key} is not a number: {value!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{key} must be greater than zero: {value!r}")
    return amount


@dataclass(slots=True)
class _Startup:
    """How far a watched deployment has got towards being worth questioning.

    Separate from ``_Watched`` so that record can stay frozen: which deployment this is
    and which supervisor holds it are fixed, while progress through startup is not. Kept
    as a field rather than a parallel dictionary because two collections keyed by spec id
    are two chances to forget one of them in ``release``.
    """

    watched_at: float
    evaluating: bool = False
    announced_wait: bool = False
    announced_unknown: bool = False


@dataclass(frozen=True, slots=True)
class _Watched:
    deployment_instance_id: str
    supervisor: EngineSafetySupervisor
    startup: _Startup

    @property
    def latched(self) -> bool:
        return not self.supervisor.breaker.allows_new_orders()


class OfflineExposureGuard:
    """Evaluate local exposure on a clock the transport cannot stall.

    A trip latches, and latching ends the deployment: the position is flattened
    once, the engine is told to stop, and no further generation is admitted.

    Flattening and refusing generations are not containment on their own. That
    pair assumed exposure could only come back through a generation the operator
    publishes; on real hardware it came back through the strategy's own signals,
    which need no generation at all -- four positions inside three hours, each
    more than twice the ceiling that had tripped the breaker. Refusing generations
    closes the door the operator uses. Stopping closes the one the exposure
    actually came back through. Clearing it takes a restart, which is the point.
    """

    def __init__(
        self,
        *,
        engine: OfflineSafetyEngine,
        interval: float = TICK_SECS,
        deadline: float = EVALUATION_DEADLINE_SECS,
        readiness_timeout: float = READINESS_TIMEOUT_SECS,
    ) -> None:
        self._engine = engine
        self._interval = interval
        self._deadline = deadline
        self._readiness_timeout = readiness_timeout
        self._watched: dict[str, _Watched] = {}

    def allows_new_generations(self) -> bool:
        """False once anything has tripped, whatever else is still within limits."""

        return not any(watched.latched for watched in self._watched.values())

    def watch(
        self,
        spec_id: str,
        deployment_instance_id: str,
        limits: FallbackBreakerConfig,
    ) -> None:
        """Guard this deployment under ceilings the caller has already read.

        Taking them resolved rather than reading them here is deliberate: ceilings
        that cannot be read must stop a deployment before it starts, and that
        decision belongs where the deployment is decided.
        """

        watched = self._watched.get(spec_id)
        if watched is None:
            self._watched[spec_id] = _Watched(
                deployment_instance_id=deployment_instance_id,
                supervisor=EngineSafetySupervisor(
                    engine=self._engine, breaker=FallbackBreaker(limits)
                ),
                startup=_Startup(watched_at=time.monotonic()),
            )
            _log.info(
                "offline_exposure_guard_watching",
                spec_id=spec_id,
                max_notional=str(limits.max_notional),
                max_drawdown_pct=str(limits.max_drawdown_pct),
                limit_source=limits.source,
            )
            return
        if watched.supervisor.breaker.apply_config(limits):
            _log.info(
                "offline_exposure_limits_changed",
                spec_id=spec_id,
                max_notional=str(limits.max_notional),
                max_drawdown_pct=str(limits.max_drawdown_pct),
            )

    def release(self, spec_id: str) -> None:
        """Stop guarding a deployment that has been stopped."""

        if self._watched.pop(spec_id, None) is not None:
            _log.info("offline_exposure_guard_released", spec_id=spec_id)

    async def evaluate_once(self) -> list[EngineSafetyTick]:
        """Evaluate every deployment still worth evaluating, and end the latched ones."""

        ticks = []
        for spec_id, watched in tuple(self._watched.items()):
            if not watched.latched:
                if not await self._may_evaluate(spec_id, watched):
                    continue
                tick = await self._evaluate_within_deadline(spec_id, watched)
                if tick is not None:
                    ticks.append(tick)
            if watched.latched:
                await self._end_deployment(spec_id, watched)
        return ticks

    async def _end_deployment(self, spec_id: str, watched: _Watched) -> None:
        """Stop a deployment that has tripped, and keep saying so until it has stopped.

        Flattening removes the exposure that tripped the breaker. It does not stop
        the strategy, which goes on opening positions from its own signals inside
        the engine, so a guard that flattened once and stood down was watching a
        deployment that was still trading.

        Whether to keep trying is decided by asking the engine what it still holds,
        not by remembering that a stop was once requested -- a record of the request
        cannot tell a stop that took effect apart from one that did not.
        """

        if not self._engine.attached(watched.deployment_instance_id):
            return
        _log.error(
            "offline_exposure_latched_deployment_stopping",
            spec_id=spec_id,
            deployment_instance_id=watched.deployment_instance_id,
        )
        try:
            await asyncio.wait_for(
                self._engine.stop(watched.deployment_instance_id),
                timeout=self._deadline,
            )
        except TimeoutError:
            # An engine that will not answer a stop is not an engine that has
            # stopped, and nothing here can make it so. The next tick asks again,
            # because the alternative is one attempt followed by silence -- which is
            # the shape of the defect this whole path exists to remove.
            _log.error(
                "offline_exposure_stop_unconfirmed",
                spec_id=spec_id,
                deployment_instance_id=watched.deployment_instance_id,
                deadline_seconds=self._deadline,
            )

    async def _may_evaluate(self, spec_id: str, watched: _Watched) -> bool:
        """Whether the engine has finished starting enough to be asked about exposure.

        Questioning it earlier produces a fail-closed trip on data that has not arrived
        yet rather than on exposure that exists -- measured on 2026-08-01 as a trip 116ms
        before the account balance landed, while NautilusTrader was still inside the
        startup reconciliation it announces in advance.

        Waiting is not tripping: a deployment held here is untouched, and the breaker
        still allows new generations.
        """

        if watched.startup.evaluating:
            return True

        probe = getattr(self._engine, "deployment_ready", None)
        if not callable(probe):
            # Older engines are evaluated exactly as before. Said once, because a safety
            # check that is quietly not running is worse than one that is loudly not.
            if not watched.startup.announced_unknown:
                watched.startup.announced_unknown = True
                _log.warning(
                    "offline_exposure_readiness_unknown",
                    spec_id=spec_id,
                    deployment_instance_id=watched.deployment_instance_id,
                )
            watched.startup.evaluating = True
            return True

        if await probe(watched.deployment_instance_id):
            watched.startup.evaluating = True
            _log.info(
                "offline_exposure_guard_evaluating",
                spec_id=spec_id,
                deployment_instance_id=watched.deployment_instance_id,
                waited_seconds=round(time.monotonic() - watched.startup.watched_at, 3),
            )
            return True

        # Never blind: an engine stuck half-started must still end up guarded, so past
        # the bound this evaluates anyway. That lands on an unreliable snapshot and fails
        # closed -- the behaviour this whole change delays, which is the right way round.
        if time.monotonic() - watched.startup.watched_at >= self._readiness_timeout:
            watched.startup.evaluating = True
            _log.error(
                "offline_exposure_readiness_timeout",
                spec_id=spec_id,
                deployment_instance_id=watched.deployment_instance_id,
                timeout_seconds=self._readiness_timeout,
            )
            return True

        if not watched.startup.announced_wait:
            watched.startup.announced_wait = True
            _log.info(
                "offline_exposure_awaiting_readiness",
                spec_id=spec_id,
                deployment_instance_id=watched.deployment_instance_id,
                timeout_seconds=self._readiness_timeout,
            )
        return False

    async def _evaluate_within_deadline(
        self, spec_id: str, watched: _Watched
    ) -> EngineSafetyTick | None:
        """Give the engine a bounded chance to answer, and fail closed if it will not.

        An engine that raises is already handled; an engine that simply never
        returns is the one shape with no exception to catch, and waiting on it
        would stop both this tick and the lane's own shutdown. Silence is not
        evidence of safety, so it latches — but nothing was flattened, and the
        record says so rather than implying containment happened.
        """

        try:
            return await asyncio.wait_for(
                watched.supervisor.evaluate_once(watched.deployment_instance_id),
                timeout=self._deadline,
            )
        except TimeoutError:
            watched.supervisor.breaker.fail_closed("engine_unresponsive")
            _log.error(
                "offline_exposure_containment_unconfirmed",
                spec_id=spec_id,
                deployment_instance_id=watched.deployment_instance_id,
                deadline_seconds=self._deadline,
            )
            return None

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until asked to stop.

        It used to return once everything watched had latched, on the reasoning that
        nothing was left to evaluate. That held for the exposure number only: the
        deployments were still running, and a guard that has stood down cannot
        notice that a stop failed to take.
        """

        while not stop.is_set():
            await self.evaluate_once()
            await _sleep_until_stopped(stop, self._interval)


async def _sleep_until_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


__all__ = [
    "EVALUATION_DEADLINE_SECS",
    "TICK_SECS",
    "OfflineExposureGuard",
    "OfflineSafetyEngine",
    "resolve_breaker_config",
]
