# tests/test_coordinator_delegation.py
"""The coordinator's methods must stay thin delegates.

The implementations moved into EquityProvider, ConfigSummaryLogger and StartupValidator,
leaving the coordinator's `_get_*` / `_log_*` / `_validate_*` as delegates. Each
component's own tests cover the behaviour; this file pins that the coordinator really

A SimpleNamespace stub and unbound calls avoid instantiating the Cython base class.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("msgspec")

from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

# (coordinator method, component attribute on the stub, component method, returns a value)
# The reads pass the component's return through; the log and validate methods return None.
DELEGATIONS = [
    ("_get_effective_capital", "_equity_provider", "get_effective_capital", True),
    ("_get_actual_balance", "_equity_provider", "get_actual_balance", True),
    ("_fallback_capital", "_equity_provider", "fallback_capital", True),
    ("_get_risk_equity", "_equity_provider", "get_risk_equity", True),
    ("_log_config_summary", "_config_logger", "log_config_summary", False),
    ("_log_active_config", "_config_logger", "log_active_config", False),
    ("_validate_startup_config", "_startup_validator", "validate_startup_config", False),
    (
        "_validate_initial_capital_vs_balance",
        "_startup_validator",
        "validate_initial_capital_vs_balance",
        False,
    ),
]


@pytest.mark.parametrize(
    "coord_method, component_attr, component_method, returns_value", DELEGATIONS
)
def test_coordinator_method_delegates_to_component(
    coord_method, component_attr, component_method, returns_value
):
    stub = SimpleNamespace(
        _equity_provider=MagicMock(),
        _config_logger=MagicMock(),
        _startup_validator=MagicMock(),
    )
    component_call = getattr(getattr(stub, component_attr), component_method)

    result = getattr(NautilusTradingStrategy, coord_method)(stub)

    component_call.assert_called_once_with()
    if returns_value:
        assert result is component_call.return_value, (
            f"{coord_method} must pass through what {component_attr}.{component_method}() returns"
        )


def test_all_delegations_target_existing_methods():
    # Sentinel: every coordinator method exists, so the table above cannot pass vacuously.
    for coord_method, _, _, _ in DELEGATIONS:
        assert hasattr(NautilusTradingStrategy, coord_method), f"missing method: {coord_method}"
