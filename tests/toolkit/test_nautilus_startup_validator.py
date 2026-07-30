# tests/test_nautilus_startup_validator.py
"""StartupValidator against a mock strategy.

The startup validation component was lifted off the strategy class. The initial_capital
check needs a runtime account balance, so it stayed here while the purely config-level
checks moved onto the configs themselves.
These cover fixed_capital above 10% of the balance raising, compound above twice the
balance warning, a non-positive balance raising, and validate_startup_config dispatching

The nautilus prefix keeps this apart from the existing test_startup_validator.py, which
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators import StartupValidator
from nautilus_trader.common.enums import LogColor


def _strategy(*, capital_mode="compound", initial_capital=1000, balance="5000", warnings=()):
    return SimpleNamespace(
        config=SimpleNamespace(
            position=SimpleNamespace(capital_mode=capital_mode, initial_capital=initial_capital),
            validation_warnings=lambda: list(warnings),
        ),
        _get_actual_balance=lambda: Decimal(balance),
        log=MagicMock(),
    )


# --- validate_initial_capital_vs_balance -----------------------------------


def test_non_positive_initial_capital_is_noop():
    s = _strategy(initial_capital=0)
    StartupValidator(s).validate_initial_capital_vs_balance()
    s.log.error.assert_not_called()
    s.log.warning.assert_not_called()


def test_zero_balance_raises():
    s = _strategy(initial_capital=1000, balance="0")
    with pytest.raises(RuntimeError):
        StartupValidator(s).validate_initial_capital_vs_balance()
    s.log.error.assert_called_once()


def test_fixed_capital_over_10pct_raises():
    # initial 6000 exceeds 1.1 * 5000 = 5500, so startup is refused
    s = _strategy(capital_mode="fixed_capital", initial_capital=6000, balance="5000")
    with pytest.raises(RuntimeError):
        StartupValidator(s).validate_initial_capital_vs_balance()
    s.log.error.assert_called_once()


def test_fixed_capital_within_10pct_ok():
    # initial 5400 is within 1.1 * 5000 = 5500, so it passes
    s = _strategy(capital_mode="fixed_capital", initial_capital=5400, balance="5000")
    StartupValidator(s).validate_initial_capital_vs_balance()  # no raise
    s.log.error.assert_not_called()


def test_compound_over_2x_warns_no_raise():
    # initial 12000 exceeds 2 * 5000 = 10000, so it warns rather than raising
    s = _strategy(capital_mode="compound", initial_capital=12000, balance="5000")
    StartupValidator(s).validate_initial_capital_vs_balance()
    s.log.warning.assert_called_once()
    s.log.error.assert_not_called()


def test_compound_within_2x_ok():
    s = _strategy(capital_mode="compound", initial_capital=9000, balance="5000")
    StartupValidator(s).validate_initial_capital_vs_balance()
    s.log.warning.assert_not_called()


# --- validate_startup_config -----------------------------------------------


def test_validate_startup_config_dispatches_warnings_by_level():
    s = _strategy(
        initial_capital=1000,
        balance="5000",
        warnings=[("error", "E"), ("warning", "W"), ("info", "I")],
    )
    StartupValidator(s).validate_startup_config()
    s.log.error.assert_called_once_with("E", color=LogColor.RED)
    s.log.warning.assert_called_once_with("W", color=LogColor.YELLOW)
    s.log.info.assert_called_once_with("I")
