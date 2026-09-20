"""Single-snapshot fallback-breaker supervision for one active deployment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from custos.core.engine_protocol import EngineStatus
from custos.core.fallback_breaker import BreakerVerdict, FallbackBreaker


class EngineSafetyPort(Protocol):
    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus: ...

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class EngineSafetyTick:
    deployment_instance_id: str
    status: EngineStatus | None
    verdict: BreakerVerdict
    flattened: bool


class EngineSafetySupervisor:
    """Evaluate one trustworthy status snapshot and contain a breaker trip locally."""

    def __init__(self, *, engine: EngineSafetyPort, breaker: FallbackBreaker) -> None:
        self._engine = engine
        self._breaker = breaker

    @property
    def breaker(self) -> FallbackBreaker:
        return self._breaker

    async def evaluate_once(self, deployment_instance_id: str) -> EngineSafetyTick:
        instance_id = deployment_instance_id.strip()
        if not instance_id:
            raise ValueError("deployment_instance_id is required")
        try:
            status = await self._engine.get_engine_status(instance_id)
        except Exception as exc:  # noqa: BLE001 - an unavailable snapshot must fail closed
            verdict = self._breaker.fail_closed(f"portfolio_snapshot_error:{type(exc).__name__}")
            await self._engine.flatten_positions(instance_id, verdict.reason or "fail_closed")
            return EngineSafetyTick(instance_id, None, verdict, True)

        if status.reliable:
            if status.peak_equity > status.current_equity:
                self._breaker.evaluate(
                    open_notional=status.open_notional,
                    current_equity=status.peak_equity,
                )
            verdict = self._breaker.evaluate(
                open_notional=status.open_notional,
                current_equity=status.current_equity,
            )
        else:
            verdict = self._breaker.fail_closed(
                status.unreliable_reason or "portfolio_snapshot_unreliable"
            )
        if verdict.tripped:
            await self._engine.flatten_positions(instance_id, verdict.reason or "fail_closed")
        return EngineSafetyTick(instance_id, status, verdict, verdict.tripped)


__all__ = ["EngineSafetyPort", "EngineSafetySupervisor", "EngineSafetyTick"]
