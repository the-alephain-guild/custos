"""Risk control component.

Holds the RiskController lifecycle: building the controller seeded with current
equity (on_start), and the per-bar risk-limit gate (drawdown / daily-loss /
peak). Injects a strategy reference and reaches ``config`` / ``log`` /
``_risk_controller`` / ``_last_risk_reason`` plus the equity getters
``_get_effective_capital`` / ``_get_risk_equity`` through it.

``on_start`` delegates ``init_risk_controls``; the ``_process_bar`` pipeline delegates
the gate to ``check_risk_limits``. The ``risk_controller`` accessor stays on the
strategy (subclass-facing public contract).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec
from custos_toolkit.risk import RiskController

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class RiskControlCoordinator:
    """RiskController lifecycle (build on_start, per-bar risk-limit gate).

    Dependencies are reached through ``self._strategy``.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def init_risk_controls(self) -> None:
        """Initialize risk controls with current equity."""
        s = self._strategy
        # Baseline seeds the RiskController: fixed_capital uses it as the drawdown
        # denominator (config constant), compound uses it as the initial peak seed.
        baseline_capital = s._get_effective_capital()

        # Initialize RiskController with actual equity (not config value)
        pos_config = s.config.position
        s._risk_controller = RiskController(
            config=msgspec.structs.asdict(s.config.risk.global_risk),
            initial_capital=baseline_capital,
            capital_mode=pos_config.capital_mode,
        )
        # Peak tracks mark-to-market risk equity, not free balance.
        s._risk_controller.update_peak_equity(s._get_risk_equity())

    def check_risk_limits(self, current_ts: int = 0) -> bool:
        """Check all risk limits. Returns True if trading allowed."""
        s = self._strategy
        equity = s._get_risk_equity()
        controller = s._risk_controller
        if controller is None:
            s.log.error("Risk controller unavailable; blocking trading")
            return False
        # Sample the drawdown baseline here, on the evaluation tempo, rather than
        # waiting for the next fill. A high the market prints while a position is
        # held is part of the peak the drawdown is measured from; sampling only on
        # fills meant a run-up followed by a give-back never registered as one.
        # update_peak_equity only raises the peak, so a new high reads as zero
        # drawdown rather than resetting the measurement.
        controller.update_peak_equity(equity)
        allowed, reason = controller.check_limits(equity, current_ts)
        if not allowed:
            if reason != s._last_risk_reason:
                s.log.warning(f"Risk limit: {reason}")
                s._last_risk_reason = reason
        else:
            s._last_risk_reason = ""
        return allowed
