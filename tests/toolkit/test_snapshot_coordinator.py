"""Guards for the SnapshotCoordinator extraction.

The snapshot lifecycle (state serialization, decode + stash, layered restore, YAML
warmup) lives in SnapshotCoordinator. The framework callback shells stay on the
strategy: ``on_save`` delegates ``save_state``, ``on_load`` delegates ``load_state``.
These guards lock the component API, the delegation wiring, that the old private
methods are gone (single address), and that the snapshot hooks + framework callbacks
+ state stay on the strategy.
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
    "save_state",
    "load_state",
    "apply_loaded_snapshot",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import SnapshotCoordinator

    assert callable(getattr(SnapshotCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    assert len(_PUBLIC_API) == 3


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_apply_loaded_snapshot",
    "_warm_indicators_from_yaml",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The snapshot restore logic must be gone from the whole class hierarchy (single
    address). hasattr (not just vars) catches re-exposure via a
    parent/mixin too."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to SnapshotCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 2


@requires_nautilus
def test_hooks_callbacks_state_stay_on_strategy():
    """Framework callbacks (on_save/on_load shells), snapshot hooks (subclasses
    override them), and snapshot state stay on the strategy class."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in (
        "on_save",
        "on_load",
        "get_snapshot_indicators",
        "get_snapshot_state",
        "restore_from_snapshot",
    ):
        assert hasattr(NautilusTradingStrategy, name), f"{name} must stay on the strategy class"


# Framework callback shell -> delegated coordinator method.
_CALLBACK_DELEGATES = [("on_save", "save_state"), ("on_load", "load_state")]


@requires_nautilus
@pytest.mark.parametrize("callback,method", _CALLBACK_DELEGATES)
def test_callback_delegates_to_snapshot_coordinator(callback, method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(getattr(NautilusTradingStrategy, callback))
    assert f"_snapshot_coordinator.{method}" in source, (
        f"{callback} should delegate to self._snapshot_coordinator.{method}"
    )


@requires_nautilus
def test_constructed_in_init_not_in_callbacks():
    """Built in __init__ (no pre-callback None window); never in the shells."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    init_src = inspect.getsource(NautilusTradingStrategy.__init__)
    on_save_src = inspect.getsource(NautilusTradingStrategy.on_save)
    on_load_src = inspect.getsource(NautilusTradingStrategy.on_load)
    assert "SnapshotCoordinator(self)" in init_src, "SnapshotCoordinator must be built in __init__"
    assert "SnapshotCoordinator(self)" not in on_save_src + on_load_src, (
        "SnapshotCoordinator must not be built inside the callback shells"
    )
