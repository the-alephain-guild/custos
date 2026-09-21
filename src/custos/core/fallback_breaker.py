"""Runner fallback breaker — the disconnect-resilient hard limit.

Where the notional cap softly refuses *new* orders, the breaker is the last
line: once total open notional or peak-to-current drawdown breaches its ceiling
it trips, which flattens positions and freezes further orders until an operator
intervenes. It keeps evaluating while the cloud is unreachable so a runaway
runner is contained locally (the disconnect-resilient red line).

All money math is ``Decimal`` (red line 0.4). Peak equity is tracked as Decimal
— never copy a float high-water mark. The drawdown breach needs an equity feed;
when equity is not supplied the breaker still enforces the notional ceiling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from custos.contracts.crucible_runner_safety_policy import RunnerAggregateCapPolicyV1
from custos.core.local_cap import (
    STRICTEST_NON_LIVE_MAX_TOTAL_USD,
    RunnerSafetyPolicyUnavailableError,
)
from custos.core.log import get_logger

_log = get_logger("custos.fallback_breaker")

STRICTEST_LOCAL_MAX_DRAWDOWN_PCT = Decimal("10")


@dataclass(frozen=True)
class FallbackBreakerConfig:
    """Hard-limit ceilings. ``max_drawdown_pct`` is a percentage (e.g. 20 = 20%)."""

    max_notional: Decimal
    max_drawdown_pct: Decimal
    policy_id: UUID | None = None
    policy_digest: str | None = None
    owner_policy: bool = False
    source: str = "explicit_local_config"

    @classmethod
    def from_verified_policy(cls, policy: RunnerAggregateCapPolicyV1) -> FallbackBreakerConfig:
        if not isinstance(policy, RunnerAggregateCapPolicyV1):
            raise TypeError("fallback breaker requires a verified runner-safety policy model")
        return cls(
            max_notional=policy.max_total_notional_decimal,
            max_drawdown_pct=STRICTEST_LOCAL_MAX_DRAWDOWN_PCT,
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
            owner_policy=True,
            source="verified_crucible_runner_policy",
        )

    @classmethod
    def strictest_local_fallback(cls, trading_mode: str) -> FallbackBreakerConfig:
        if trading_mode not in {"sandbox", "testnet"}:
            raise RunnerSafetyPolicyUnavailableError(
                "live fallback breaker has no local policy fallback"
            )
        return cls(
            max_notional=STRICTEST_NON_LIVE_MAX_TOTAL_USD,
            max_drawdown_pct=STRICTEST_LOCAL_MAX_DRAWDOWN_PCT,
            owner_policy=False,
            source="strictest_non_live_local_fallback",
        )


@dataclass(frozen=True)
class BreakerVerdict:
    """Outcome of one breaker evaluation. ``reason`` is the breach that tripped
    it (``notional_breach`` / ``drawdown_breach``) or None when within limits."""

    tripped: bool
    reason: str | None
    drawdown_pct: Decimal


class FallbackBreaker:
    """Stateful runner hard limit. ``evaluate`` checks the current exposure and
    trips (and stays frozen) on the first breach; ``allows_new_orders`` reflects
    the freeze."""

    def __init__(
        self,
        config: FallbackBreakerConfig,
        *,
        on_state_change: Callable[[Decimal, bool, str | None], None] | None = None,
    ) -> None:
        self._config = config
        # Decimal high-water mark — never a float (red line 0.4).
        self._peak_equity: Decimal = Decimal("0")
        self._frozen = False
        self._on_state_change = on_state_change

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def peak_equity(self) -> Decimal:
        """The drawdown high-water mark. Losing it hides the fall it measures."""
        return self._peak_equity

    def restore(self, *, peak_equity: Decimal, frozen: bool) -> None:
        """Adopt durable state read back after a restart.

        This is loading, not tripping, so it does not notify the state sink --
        writing back what was just read would only add a round trip.
        """
        self._peak_equity = peak_equity
        self._frozen = frozen

    def _publish_state(self, reason_code: str | None) -> None:
        """Hand the new state to whoever is writing it down.

        Containment must not depend on the database answering: a sink that raises
        leaves the in-memory freeze standing, which is the conservative side. It is
        loud rather than silent, because a freeze nobody wrote down is a freeze the
        next restart will not honour.
        """
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(self._peak_equity, self._frozen, reason_code)
        except Exception as exc:  # noqa: BLE001 - the freeze outranks its bookkeeping
            _log.error(
                "fallback_breaker_state_persist_failed",
                error_type=type(exc).__name__,
                frozen=self._frozen,
                reason_code=reason_code,
            )

    @property
    def config(self) -> FallbackBreakerConfig:
        return self._config

    def apply_config(self, new_config: FallbackBreakerConfig) -> bool:
        """Swap the enforced ceilings. Returns True when the value actually
        changed. Peak equity + frozen state are preserved so a refresh does
        not silently reset the drawdown high-water mark or clear an existing
        trip — cloud-side edits raise / lower the limits, they don't reset
        the breaker."""
        if new_config == self._config:
            return False
        self._config = new_config
        return True

    def allows_new_orders(self) -> bool:
        return not self._frozen

    def fail_closed(self, reason: str = "unreliable_portfolio") -> BreakerVerdict:
        """Freeze immediately when trustworthy financial inputs are unavailable."""

        already_frozen = self._frozen
        self._frozen = True
        _log.error("fallback_breaker_fail_closed", reason=reason)
        if not already_frozen:
            self._publish_state(reason)
        return BreakerVerdict(
            tripped=True,
            reason=reason,
            drawdown_pct=Decimal("0"),
        )

    def evaluate(
        self,
        *,
        open_notional: Decimal,
        current_equity: Decimal | None = None,
    ) -> BreakerVerdict:
        drawdown_pct = Decimal("0")
        peak_advanced = False
        if current_equity is not None:
            if current_equity > self._peak_equity:
                self._peak_equity = current_equity
                peak_advanced = True
            drawdown_pct = self._drawdown_pct(current_equity)

        reason: str | None = None
        if open_notional > self._config.max_notional:
            reason = "notional_breach"
        elif current_equity is not None and drawdown_pct > self._config.max_drawdown_pct:
            reason = "drawdown_breach"

        newly_frozen = reason is not None and not self._frozen
        if newly_frozen:
            self._frozen = True
            _log.warning(
                "fallback_breaker_tripped",
                reason=reason,
                open_notional=str(open_notional),
                drawdown_pct=str(drawdown_pct),
                max_notional=str(self._config.max_notional),
                max_drawdown_pct=str(self._config.max_drawdown_pct),
            )
        if newly_frozen or peak_advanced:
            self._publish_state(reason if self._frozen else None)
        return BreakerVerdict(tripped=reason is not None, reason=reason, drawdown_pct=drawdown_pct)

    def _drawdown_pct(self, current_equity: Decimal) -> Decimal:
        if self._peak_equity <= 0:
            return Decimal("0")
        return (self._peak_equity - current_equity) / self._peak_equity * Decimal("100")
