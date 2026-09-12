"""Startup validation component.

Holds the startup checks that need the runtime account balance (initial_capital vs
actual balance). Pure config validation already sank into
NautilusTradingStrategyConfig (``__post_init__`` fail-fast + ``validation_warnings``);
only balance-dependent checks that can't run at config construction stay here.
Injects a strategy reference and reaches ``config``, the balance
(``_get_actual_balance``) and ``log`` through it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.common import LogColor

if TYPE_CHECKING:
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy


class StartupValidator:
    """Startup configuration sanity checks.

    ``validate_startup_config`` runs the balance-dependent initial_capital check, then
    dispatches the config's own ``validation_warnings`` by level.
    """

    def __init__(self, strategy: NautilusTradingStrategy) -> None:
        self._strategy = strategy

    def validate_startup_config(self) -> None:
        """Run startup configuration sanity checks.

        Balance-dependent checks stay here (initial_capital); pure config validation
        already sank into NautilusTradingStrategyConfig (__post_init__ fail-fast +
        validation_warnings).
        """
        s = self._strategy
        self.validate_initial_capital_vs_balance()
        for level, message in s.config.validation_warnings():
            if level == "error":
                s.log.error(message, color=LogColor.RED)
            elif level == "warning":
                s.log.warning(message, color=LogColor.YELLOW)
            else:
                s.log.info(message)

    def validate_initial_capital_vs_balance(self) -> None:
        """Check initial_capital vs actual account balance at startup.

        - fixed_capital mode: initial_capital drives position sizing directly →
          abort (raise RuntimeError) if >10% over actual balance — orders will fail.
        - compound mode: initial_capital is fallback only →
          warn if >2x actual balance (likely stale config).
        """
        s = self._strategy
        pos_config = s.config.position
        initial_capital = pos_config.initial_capital
        if initial_capital <= 0:
            return

        actual_balance = s._get_actual_balance()
        if actual_balance <= 0:
            msg = (
                "[STARTUP] Unable to read the actual account balance (balance is 0 or "
                "account info unavailable). The strategy cannot confirm sufficient capital, "
                "so it refuses to start to prevent failed orders. Check the API connection "
                "and account permissions, then restart the strategy."
            )
            s.log.error(msg, color=LogColor.RED)
            raise RuntimeError(msg)

        capital_mode = pos_config.capital_mode

        if capital_mode == "fixed_capital":
            threshold = float(actual_balance) * 1.1
            if initial_capital > threshold:
                msg = (
                    f"[STARTUP] initial_capital ({initial_capital}) exceeds the actual account "
                    f"balance ({float(actual_balance):.2f}) by more than 10%. In fixed_capital "
                    f"mode this value is used directly for position sizing, so orders will fail "
                    f"for insufficient funds. Set initial_capital <= {float(actual_balance):.2f} "
                    f"and restart the strategy."
                )
                s.log.error(msg, color=LogColor.RED)
                raise RuntimeError(msg)
        else:  # compound or other
            threshold = float(actual_balance) * 2.0
            if initial_capital > threshold:
                s.log.warning(
                    f"[STARTUP] initial_capital ({initial_capital}) is more than 2x the actual "
                    f"account balance ({float(actual_balance):.2f}). In compound mode this value "
                    f"is only a fallback, but such a large gap may mean the config is stale; "
                    f"check that initial_capital in config.yaml is correct.",
                    color=LogColor.YELLOW,
                )
