"""The offline lane's own exposure ceilings.

There is no signed owner policy on an operator's own machine, so the strictest
non-live fallback is the floor. A spec may name its own ceilings: this lane
already takes the strategy, the venue and the credential from the same unsigned
spec, and the red line asks that a guard keep running while the cloud is
unreachable — not that it enforce one particular number. The default alone would
make the lane useless for its purpose, since a funded sandbox account breaches
$200 on its first position.

A ceiling that cannot be read is refused. Falling back to the default instead
would leave the operator believing a limit is in force that never was.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Final

from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.offline.mode_guard import refuse_live
from custos.offline.spec import OfflineDeploymentSpec

_NOTIONAL_KEY: Final = "max_total_notional"
_DRAWDOWN_KEY: Final = "max_drawdown_pct"
_DECLARED_KEYS: Final = (_NOTIONAL_KEY, _DRAWDOWN_KEY)
_SPEC_SOURCE: Final = "offline_spec_risk_config"


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


__all__ = ["resolve_breaker_config"]
