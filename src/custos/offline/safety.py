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

    def __init__(self, *, engine: EngineSafetyPort, interval: float = TICK_SECS) -> None:
        self._engine = engine
        self._interval = interval
        self._watched: dict[str, _Watched] = {}

    def allows_new_generations(self) -> bool:
        """False once anything has tripped, whatever else is still within limits."""

        return not any(watched.latched for watched in self._watched.values())

    def watch(self, spec: OfflineDeploymentSpec, deployment_instance_id: str) -> None:
        """Guard this deployment under the ceilings its spec asks for."""

        config = resolve_breaker_config(spec)
        watched = self._watched.get(spec.spec_id)
        if watched is None:
            self._watched[spec.spec_id] = _Watched(
                deployment_instance_id=deployment_instance_id,
                supervisor=EngineSafetySupervisor(
                    engine=self._engine, breaker=FallbackBreaker(config)
                ),
            )
            _log.info(
                "offline_exposure_guard_watching",
                spec_id=spec.spec_id,
                max_notional=str(config.max_notional),
                max_drawdown_pct=str(config.max_drawdown_pct),
                limit_source=config.source,
            )
            return
        if watched.supervisor.breaker.apply_config(config):
            _log.info(
                "offline_exposure_limits_changed",
                spec_id=spec.spec_id,
                max_notional=str(config.max_notional),
                max_drawdown_pct=str(config.max_drawdown_pct),
            )

    def release(self, spec_id: str) -> None:
        """Stop guarding a deployment that has been stopped."""

        if self._watched.pop(spec_id, None) is not None:
            _log.info("offline_exposure_guard_released", spec_id=spec_id)

    async def evaluate_once(self) -> list[EngineSafetyTick]:
        """Evaluate every deployment still worth evaluating."""

        ticks = []
        for watched in tuple(self._watched.values()):
            if watched.latched:
                continue
            ticks.append(await watched.supervisor.evaluate_once(watched.deployment_instance_id))
        return ticks

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


__all__ = ["TICK_SECS", "OfflineExposureGuard", "resolve_breaker_config"]
