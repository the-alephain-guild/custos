"""Guards for the PairContextCoordinator extraction.

The per-pair context construction + on_start bootstrap (create context, wire
execution submitters / ATR / tick monitor, subscribe bars, subscribe tick stream)
live in PairContextCoordinator. on_start delegates the per-pair loop to
``setup_pairs`` and the tick stream to ``subscribe_ticks``. These guards lock the
component API, the delegation wiring, that the old private methods are gone (single
address), and that the shared context lookup/derive accessors + ``_contexts`` state
stay on the strategy (cross-coordinator + subclass API, not on the component).
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
    "create_context",
    "setup_pairs",
    "subscribe_ticks",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator

    assert callable(getattr(PairContextCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    assert len(_PUBLIC_API) == 3


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_create_pair_context",
    "_subscribe_ticks_for_pairs",
    "_init_tick_monitor_for_ctx",
    "_needs_atr_indicator",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The pair bootstrap must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) catches re-exposure via a
    parent/mixin too."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to PairContextCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 4


@requires_nautilus
def test_shared_lookups_and_state_stay_on_strategy():
    """Context lookup/derive accessors are a shared cross-coordinator + subclass
    API over the ``_contexts`` state; capital allocator init is the capital
    domain. None of these move into PairContextCoordinator."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in (
        "_get_context",
        "_get_context_from_instrument",
        "_get_pair_from_instrument",
        "_derive_instrument_id_for_pair",
        "_derive_bar_type_for_instrument",
        "_derive_bar_type_for_pair",
        "_init_capital_allocator",
    ):
        assert hasattr(NautilusTradingStrategy, name), f"{name} must stay on the strategy class"


# Orchestration call site -> delegated coordinator method.
_ON_START_DELEGATES = ["setup_pairs", "subscribe_ticks"]


@requires_nautilus
@pytest.mark.parametrize("method", _ON_START_DELEGATES)
def test_on_start_delegates_to_pair_context_coordinator(method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.on_start)
    assert f"_pair_context_coordinator.{method}" in source, (
        f"on_start should delegate to self._pair_context_coordinator.{method}"
    )


@requires_nautilus
def test_constructed_in_init_not_on_start():
    """Built in __init__ (no pre-on_start None window); never in on_start."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    init_src = inspect.getsource(NautilusTradingStrategy.__init__)
    on_start_src = inspect.getsource(NautilusTradingStrategy.on_start)
    assert "PairContextCoordinator(self)" in init_src, (
        "PairContextCoordinator must be built in __init__"
    )
    assert "PairContextCoordinator(self)" not in on_start_src, (
        "PairContextCoordinator must not be built in on_start"
    )
