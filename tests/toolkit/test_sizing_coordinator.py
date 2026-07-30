"""Guards for the SizingCoordinator extraction.

The position-sizing computation (default notional paradigm + fixed-risk path) lives
in SizingCoordinator. The ``calculate_position_size`` hook stays on the strategy
class and its default body delegates to
``strategy._sizing_coordinator.default_position_size``. These guards lock the
component API, the delegation wiring, and that the old private methods are gone from
the class (single address).
"""

from __future__ import annotations

import inspect

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator entry point the calculate_position_size hook delegates to.
_PUBLIC_API = [
    "default_position_size",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import SizingCoordinator

    assert callable(getattr(SizingCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    # Guard against the API list silently collapsing.
    assert len(_PUBLIC_API) == 1


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_default_position_size",
    "_fixed_risk_position_size",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The sizing computation must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) also catches re-exposure via a
    parent/mixin, matching the intent that strategy instances cannot call the old API."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to SizingCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 2


@requires_nautilus
def test_hook_delegates_to_component():
    """calculate_position_size stays on the strategy (subclass override point) and
    its default body must delegate to the component."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.calculate_position_size)
    assert "_sizing_coordinator.default_position_size" in source, (
        "calculate_position_size default body should delegate to "
        "self._sizing_coordinator.default_position_size"
    )
