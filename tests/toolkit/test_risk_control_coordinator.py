"""Guards for the RiskControlCoordinator extraction.

The RiskController lifecycle (build the controller with current equity, and the
per-bar risk-limit gate) lives in RiskControlCoordinator. ``on_start`` delegates
``init_risk_controls`` and the ``_process_bar`` pipeline delegates the gate to
``check_risk_limits``. These guards lock the component API, the delegation wiring,
that the old private methods are gone (single address), and that the risk state +
subclass accessor stay on the strategy.
"""

from __future__ import annotations

import inspect

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator entry points the strategy delegates to.
_PUBLIC_API = [
    "init_risk_controls",
    "check_risk_limits",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import RiskControlCoordinator

    assert callable(getattr(RiskControlCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    assert len(_PUBLIC_API) == 2


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_init_risk_controls",
    "_check_risk_limits",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The risk-control logic must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) catches re-exposure via a
    parent/mixin too."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to RiskControlCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 2


@requires_nautilus
def test_accessor_stays_on_strategy():
    """The subclass-facing risk_controller property stays on the strategy class
    (public contract)."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    assert isinstance(
        inspect.getattr_static(NautilusTradingStrategy, "risk_controller"), property
    ), "risk_controller property must stay on the strategy class"


@requires_nautilus
def test_on_start_delegates_init_risk_controls():
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.on_start)
    assert "_risk_control_coordinator.init_risk_controls" in source, (
        "on_start should delegate to self._risk_control_coordinator.init_risk_controls"
    )


@requires_nautilus
def test_entry_gate_delegates_check_risk_limits():
    """The risk gate is checked in the entry gate (entry-only) after the direction
    redesign -- it no longer short-circuits the whole bar."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy._entry_gates_pass)
    assert "_risk_control_coordinator.check_risk_limits" in source, (
        "_entry_gates_pass should delegate the risk gate to "
        "self._risk_control_coordinator.check_risk_limits"
    )


@requires_nautilus
def test_constructed_in_init():
    """Built in __init__ (no pre-on_start None window)."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    init_src = inspect.getsource(NautilusTradingStrategy.__init__)
    assert "RiskControlCoordinator(self)" in init_src, (
        "RiskControlCoordinator must be built in __init__"
    )
