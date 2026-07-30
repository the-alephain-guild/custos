"""Guards for the ExecutionCoordinator extraction.

The tick-monitor execution path (route ticks + partial/trailing/full exits) lives
in ExecutionCoordinator. The nautilus tick callbacks (``on_core_trade_tick`` /
``on_core_quote_tick``) stay on the strategy class and delegate to
``strategy._execution_coordinator``. These guards lock the component API, the
delegation wiring, and that the old methods are gone from the class (single
address).
"""

from __future__ import annotations

import inspect

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator entry points the on_core_*tick shells delegate to.
_PUBLIC_API = [
    "handle_trade_tick",
    "handle_quote_tick",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import ExecutionCoordinator

    assert callable(getattr(ExecutionCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    # Guard against the API list silently collapsing.
    assert len(_PUBLIC_API) == 2


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_handle_trade_tick",
    "_handle_quote_tick",
    "_execute_exit_action_for_pair",
    "_execute_partial_exit_for_pair",
    "_execute_trailing_stop_exit_for_pair",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The tick execution path must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) also catches re-exposure via a
    parent/mixin, matching the intent that strategy instances cannot call the old API."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to ExecutionCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 5


# Delegation: the engine dispatches on_core_*tick by name on the strategy, the
# thin shells must forward to the component.
@requires_nautilus
@pytest.mark.parametrize(
    "shell,target",
    [
        ("on_core_trade_tick", "_execution_coordinator.handle_trade_tick"),
        ("on_core_quote_tick", "_execution_coordinator.handle_quote_tick"),
    ],
)
def test_shell_delegates_to_component(shell, target):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(getattr(NautilusTradingStrategy, shell))
    assert target in source, f"{shell} should delegate to self.{target}"
