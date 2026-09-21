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

from collections.abc import Mapping

from typing import TYPE_CHECKING, cast

import msgspec
from nautilus_trader.common import LogColor
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

        # Restore what the last run spent. This happens here, at construction,
        # rather than in the snapshot application path: that path only runs when
        # warmup mode is "snapshot", so risk state would otherwise depend on
        # whether indicator warmup acceleration happens to be configured. It also
        # has to be in place before the first admission decision -- a restart that
        # resumed with a fresh daily budget would hand back a loss limit the
        # previous run had already spent.
        snapshot = s._loaded_snapshot
        if snapshot:
            risk_state = cast(Mapping[str, str], snapshot.get("risk") or {})
            if risk_state:
                s._risk_controller.restore_state(risk_state)
                s.log.info(
                    f"Risk state restored from snapshot: "
                    f"session_pnl={risk_state.get('session_pnl', '0')}",
                    color=LogColor.YELLOW,
                )

    def check_risk_limits(self, current_ts: int = 0) -> bool:
        """Check all risk limits. Returns True if trading allowed."""
        s = self._strategy
        equity = s._get_risk_equity()
        controller = s._risk_controller
        if controller is None:
            s.log.error("Risk controller unavailable; blocking trading")
            return False
        # No sampling here. This is the admission check, and it is reached only when
        # there is a candidate entry whose direction is allowed -- a tempo that has
        # nothing to do with when the market prints a high. The baseline is observed
        # on every bar instead, in _on_bar_risk_hygiene.
        allowed, reason = controller.check_limits(equity, current_ts)
        if not allowed:
            if reason != s._last_risk_reason:
                s.log.warning(f"Risk limit: {reason}")
                s._last_risk_reason = reason
        else:
            s._last_risk_reason = ""
        return allowed
