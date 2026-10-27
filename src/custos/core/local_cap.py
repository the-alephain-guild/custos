"""Runner aggregate-cap enforcement from verified runner-safety policy only.

The guard rejects risk-increasing orders beyond the signed per-order or total
ceiling and always permits explicitly risk-reducing orders.  DeploymentSpec is
not an authority input.  Sandbox/testnet may use an explicit conservative local
fallback; live has no fallback and fails closed without a verified owner policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from custos.contracts.crucible_runner_safety_policy import RunnerAggregateCapPolicyV1
from custos.core.log import get_logger
from custos.core.reservation_math import notional_limit_breach

_log = get_logger("custos.local_cap")

STRICTEST_NON_LIVE_MAX_ORDER_USD = Decimal("50")
STRICTEST_NON_LIVE_MAX_TOTAL_USD = Decimal("200")

RejectPublisher = Callable[[str, Decimal, Decimal, Decimal], Awaitable[None]]


class RunnerSafetyPolicyUnavailableError(RuntimeError):
    """No valid owner policy or explicitly permitted non-live fallback exists."""


@dataclass(frozen=True, slots=True)
class LocalCapConfig:
    max_order_notional: Decimal
    max_total_notional: Decimal
    settlement_currency: str
    policy_id: UUID | None
    policy_digest: str | None
    owner_policy: bool
    source: str

    @classmethod
    def from_verified_policy(cls, policy: RunnerAggregateCapPolicyV1) -> LocalCapConfig:
        if not isinstance(policy, RunnerAggregateCapPolicyV1):
            raise TypeError("local cap requires a verified runner-safety policy model")
        return cls(
            max_order_notional=policy.max_order_notional_decimal,
            max_total_notional=policy.max_total_notional_decimal,
            settlement_currency=policy.settlement_currency,
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
            owner_policy=True,
            source="verified_crucible_runner_policy",
        )

    @classmethod
    def strictest_local_fallback(cls, trading_mode: str) -> LocalCapConfig:
        if trading_mode not in {"sandbox", "testnet"}:
            raise RunnerSafetyPolicyUnavailableError(
                "live runner safety policy has no local fallback"
            )
        return cls(
            max_order_notional=STRICTEST_NON_LIVE_MAX_ORDER_USD,
            max_total_notional=STRICTEST_NON_LIVE_MAX_TOTAL_USD,
            settlement_currency="USD",
            policy_id=None,
            policy_digest=None,
            owner_policy=False,
            source="strictest_non_live_local_fallback",
        )


def check_cap(
    current_open: Decimal,
    new_order_notional: Decimal,
    config: LocalCapConfig,
    *,
    risk_reducing: bool = False,
) -> bool:
    if risk_reducing:
        return True
    return (
        notional_limit_breach(
            new_order_notional,
            current_open + new_order_notional,
            config.max_order_notional,
            config.max_total_notional,
        )
        is None
    )


class RunnerNotionalCap:
    """Local enforcement point for one verified or explicit fallback config."""

    def __init__(
        self,
        config: LocalCapConfig,
        *,
        reject_publisher: RejectPublisher | None = None,
    ) -> None:
        self._config = config
        self._reject_publisher = reject_publisher

    @property
    def config(self) -> LocalCapConfig:
        return self._config

    def apply_config(self, new_config: LocalCapConfig) -> bool:
        if new_config == self._config:
            return False
        self._config = new_config
        return True

    async def allows(
        self,
        *,
        symbol: str,
        current_open: Decimal,
        new_order_notional: Decimal,
        risk_reducing: bool = False,
    ) -> bool:
        if check_cap(
            current_open,
            new_order_notional,
            self._config,
            risk_reducing=risk_reducing,
        ):
            return True
        _log.warning(
            "runner_cap_exceeded",
            symbol=symbol,
            current_open=str(current_open),
            requested_notional=str(new_order_notional),
            max_order_notional=str(self._config.max_order_notional),
            max_total_notional=str(self._config.max_total_notional),
            policy_source=self._config.source,
        )
        if self._reject_publisher is not None:
            try:
                await self._reject_publisher(
                    symbol,
                    current_open,
                    new_order_notional,
                    self._config.max_total_notional,
                )
            except Exception as exc:  # noqa: BLE001 - local rejection remains authoritative
                _log.warning(
                    "runner_cap_reject_publish_failed",
                    symbol=symbol,
                    error_type=type(exc).__name__,
                )
        return False
