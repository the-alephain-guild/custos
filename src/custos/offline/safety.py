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
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Final

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


@dataclass(frozen=True, slots=True)
class _Watched:
    deployment_instance_id: str
    supervisor: EngineSafetySupervisor

    @property
    def latched(self) -> bool:
        return not self.supervisor.breaker.allows_new_orders()


class OfflineExposureGuard:
    """Evaluate local exposure on a clock the transport cannot stall.

    A trip latches: the position is flattened once and no further generation is
    admitted, because flattening alone would be undone by the next generation the
    operator publishes. Clearing it takes a restart, which is the point.
    """

    def __init__(
        self,
        *,
        engine: EngineSafetyPort,
        interval: float = TICK_SECS,
        deadline: float = EVALUATION_DEADLINE_SECS,
    ) -> None:
        self._engine = engine
        self._interval = interval
        self._deadline = deadline
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
        """Evaluate every deployment still worth evaluating."""

        ticks = []
        for spec_id, watched in tuple(self._watched.items()):
            if watched.latched:
                continue
            tick = await self._evaluate_within_deadline(spec_id, watched)
            if tick is not None:
                ticks.append(tick)
        return ticks

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
        """Tick until asked to stop, or until nothing is left to evaluate."""

        while not stop.is_set():
            await self.evaluate_once()
            if self._watched and all(watched.latched for watched in self._watched.values()):
                _log.error(
                    "offline_exposure_guard_latched",
                    spec_ids=sorted(self._watched),
                )
                return
            await _sleep_until_stopped(stop, self._interval)


async def _sleep_until_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


__all__ = [
    "EVALUATION_DEADLINE_SECS",
    "TICK_SECS",
    "OfflineExposureGuard",
    "resolve_breaker_config",
]
