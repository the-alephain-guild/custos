"""Where the offline lane's exposure ceilings come from, and what refuses them.

The lane has no signed owner policy and never will, so the strictest non-live
fallback is the floor it starts from. A spec may name its own ceilings because the
lane already takes the strategy, the venue and the credential from that same
unsigned spec — but a ceiling that cannot be read is refused rather than quietly
replaced by the default, which would leave an operator believing a limit is in
force that never was.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from custos.contracts import TradingMode
from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.core.local_cap import RunnerSafetyPolicyUnavailableError
from custos.offline.mode_guard import OfflineModeRefused
from custos.offline.safety import resolve_breaker_config
from custos.offline.spec import OfflineDeploymentSpec

STRICTEST_NOTIONAL = Decimal("200")
STRICTEST_DRAWDOWN_PCT = Decimal("10")


def _spec(**overrides: Any) -> OfflineDeploymentSpec:
    document: dict[str, Any] = {
        "spec_id": "supertrend-sandbox",
        "generation": 1,
        "trading_mode": "sandbox",
        "lifecycle_state": "running",
        "strategy_path": "/opt/ps/trend/supertrend",
        "provenance_ref": {"credential_id": "binance-supertrend"},
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }
    document.update(overrides)
    return OfflineDeploymentSpec.model_validate(document)


def test_a_spec_that_names_no_limits_gets_the_strictest_non_live_ceilings() -> None:
    config = resolve_breaker_config(_spec())

    assert config.max_notional == STRICTEST_NOTIONAL
    assert config.max_drawdown_pct == STRICTEST_DRAWDOWN_PCT
    assert config.owner_policy is False


def test_the_default_ceilings_are_the_shared_strictest_fallback_not_a_local_copy() -> None:
    """Two sources of truth for the same number drift; this asserts there is one."""

    assert resolve_breaker_config(_spec()) == FallbackBreakerConfig.strictest_local_fallback(
        "sandbox"
    )


def test_a_spec_may_raise_the_ceilings_above_the_default() -> None:
    """The consumer funds sandbox runs with 10,000 USDT; $200 would trip on entry."""

    config = resolve_breaker_config(
        _spec(risk_config={"max_total_notional": "25000", "max_drawdown_pct": "35"})
    )

    assert config.max_notional == Decimal("25000")
    assert config.max_drawdown_pct == Decimal("35")
    assert config.source != FallbackBreakerConfig.strictest_local_fallback("sandbox").source


def test_a_spec_may_also_lower_the_ceilings() -> None:
    """A tighter limit is strictly safer, so refusing it would be perverse."""

    config = resolve_breaker_config(_spec(risk_config={"max_total_notional": "25"}))

    assert config.max_notional == Decimal("25")
    assert config.max_drawdown_pct == STRICTEST_DRAWDOWN_PCT


def test_naming_one_ceiling_leaves_the_other_at_the_default() -> None:
    config = resolve_breaker_config(_spec(risk_config={"max_drawdown_pct": "40"}))

    assert config.max_notional == STRICTEST_NOTIONAL
    assert config.max_drawdown_pct == Decimal("40")


@pytest.mark.parametrize("value", ["0", "-1", "-0.5"])
def test_a_ceiling_that_is_not_positive_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": value}))


@pytest.mark.parametrize("value", ["", "lots", "1,000", None, True, ["200"]])
def test_a_ceiling_that_cannot_be_read_is_refused_rather_than_defaulted(value: object) -> None:
    """Falling back silently would report a limit the operator never set."""

    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": value}))


def test_a_float_ceiling_is_refused_because_money_is_not_binary_fractions() -> None:
    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": 25000.0}))


def test_an_integer_ceiling_is_read_exactly() -> None:
    config = resolve_breaker_config(_spec(risk_config={"max_total_notional": 25000}))

    assert config.max_notional == Decimal("25000")


def test_a_misspelled_ceiling_is_refused_rather_than_ignored() -> None:
    """An ignored typo reads, from the operator's side, exactly like a raised limit."""

    with pytest.raises(ValueError, match="max_notional"):
        resolve_breaker_config(_spec(risk_config={"max_notional": "25000"}))


def test_live_is_refused_by_the_lane_before_any_ceiling_is_resolved() -> None:
    live = _spec().model_copy(update={"trading_mode": TradingMode.LIVE})

    with pytest.raises(OfflineModeRefused, match="live"):
        resolve_breaker_config(live)


def test_the_shared_fallback_refuses_live_on_its_own_account() -> None:
    """Proves the inner layer is a live guard, not a branch the lane's guard shadows."""

    with pytest.raises(RunnerSafetyPolicyUnavailableError):
        FallbackBreakerConfig.strictest_local_fallback("live")
