"""Resolve durable verified runner policy into local execution guard limits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from custos.contracts.crucible_runner_safety_policy import RunnerAggregateCapPolicyV1
from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.core.local_cap import (
    LocalCapConfig,
    RunnerSafetyPolicyUnavailableError,
)
from custos.core.runner_fact import RunnerStateStore


def _utc_now_datetime() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RunnerSafetyLimits:
    local_cap: LocalCapConfig
    breaker: FallbackBreakerConfig
    policy_id: UUID | None
    policy_digest: str | None
    owner_policy: bool
    source: str

    @classmethod
    def from_verified_policy(cls, policy: RunnerAggregateCapPolicyV1) -> RunnerSafetyLimits:
        cap = LocalCapConfig.from_verified_policy(policy)
        breaker = FallbackBreakerConfig.from_verified_policy(policy)
        return cls(
            local_cap=cap,
            breaker=breaker,
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
            owner_policy=True,
            source="verified_crucible_runner_policy",
        )

    @classmethod
    def strictest_local_fallback(cls, trading_mode: str) -> RunnerSafetyLimits:
        return cls(
            local_cap=LocalCapConfig.strictest_local_fallback(trading_mode),
            breaker=FallbackBreakerConfig.strictest_local_fallback(trading_mode),
            policy_id=None,
            policy_digest=None,
            owner_policy=False,
            source="strictest_non_live_local_fallback",
        )


class RunnerSafetyPolicyResolver(Protocol):
    async def resolve(self, trading_mode: str) -> RunnerSafetyLimits: ...


@dataclass(frozen=True, slots=True)
class DurableRunnerSafetyPolicyResolver:
    """Resolve only a current owner-signed policy from RunnerFact SQLite state."""

    store: RunnerStateStore
    now: Callable[[], datetime] = _utc_now_datetime

    async def resolve(self, trading_mode: str) -> RunnerSafetyLimits:
        durable = await self.store.load_effective_runner_safety_policy(
            trading_mode,
            now=self.now(),
        )
        return RunnerSafetyLimits.from_verified_policy(durable.policy)


async def resolve_runner_safety_limits(
    resolver: RunnerSafetyPolicyResolver | None,
    trading_mode: str,
) -> RunnerSafetyLimits:
    if resolver is None:
        return RunnerSafetyLimits.strictest_local_fallback(trading_mode)
    return await resolver.resolve(trading_mode)


__all__ = [
    "DurableRunnerSafetyPolicyResolver",
    "RunnerSafetyLimits",
    "RunnerSafetyPolicyResolver",
    "RunnerSafetyPolicyUnavailableError",
    "resolve_runner_safety_limits",
]
