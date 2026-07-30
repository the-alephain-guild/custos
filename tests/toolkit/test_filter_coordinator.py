"""Guards for the FilterCoordinator extraction.

The FilterManager wiring (global/per-pair init, update/check, and MTF bar
short-circuiting) lives in FilterCoordinator. The orchestration call sites stay on
the strategy: ``on_start`` delegates init, ``_process_bar`` delegates MTF +
global/per-pair checks. These guards lock the component API, the delegation wiring,
and that the old private methods are gone from the class (single address).
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator entry points the strategy delegates to.
_PUBLIC_API = [
    "init_global",
    "init_pair",
    "update_global",
    "update_pair",
    "check_global",
    "check_pair",
    "handle_mtf_bar",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    assert callable(getattr(FilterCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    # Guard against the API list silently collapsing.
    assert len(_PUBLIC_API) == 7


# Component methods that are no longer on the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_init_filters",
    "_init_pair_filters",
    "_parse_bar_type_string",
    "_update_filters",
    "_check_filters",
    "_check_pair_filters",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The filter wiring must be gone from the whole class hierarchy (single address).
    hasattr (not just vars) also catches re-exposure via a
    parent/mixin, matching the intent that strategy instances cannot call the old API."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to FilterCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 6


# Orchestration call site -> delegated coordinator method.
# init_global is called directly in on_start; init_pair moved into the per-pair
# bootstrap (PairContextCoordinator.setup_pairs, guarded in test_base_strategy_filters).
_ON_START_DELEGATES = ["init_global"]
# _process_bar updates filter state every bar (no gating); the gating checks moved
# to _entry_gates_pass (entry-only) after the direction redesign.
_PROCESS_BAR_DELEGATES = ["handle_mtf_bar", "update_global", "update_pair"]
_ENTRY_GATE_DELEGATES = ["check_global", "check_pair"]


@requires_nautilus
@pytest.mark.parametrize("method", _ON_START_DELEGATES)
def test_on_start_delegates_to_filter_coordinator(method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy.on_start)
    assert f"_filter_coordinator.{method}" in source, (
        f"on_start should delegate to self._filter_coordinator.{method}"
    )


@requires_nautilus
@pytest.mark.parametrize("method", _PROCESS_BAR_DELEGATES)
def test_process_bar_delegates_to_filter_coordinator(method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy._process_bar)
    assert f"_filter_coordinator.{method}" in source, (
        f"_process_bar should delegate to self._filter_coordinator.{method}"
    )


@requires_nautilus
@pytest.mark.parametrize("method", _ENTRY_GATE_DELEGATES)
def test_entry_gate_delegates_to_filter_coordinator(method):
    """The entry gate (entry-only filter checks) delegates to the coordinator."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    source = inspect.getsource(NautilusTradingStrategy._entry_gates_pass)
    assert f"_filter_coordinator.{method}" in source, (
        f"_entry_gates_pass should delegate to self._filter_coordinator.{method}"
    )


def _stub_with_managers(global_mgr, log=None):
    """Minimal strategy stub exposing only what handle_mtf_bar reads."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    return SimpleNamespace(_global_filter_manager=global_mgr, log=log or MagicMock())


@requires_nautilus
def test_handle_mtf_bar_global_hit_short_circuits_before_per_pair():
    """Global MTF bar: update global, return True, and never touch the per-pair
    manager (precedence -- global is checked first and short-circuits)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    global_mgr = MagicMock()
    global_mgr.is_mtf_bar.return_value = True
    ctx = SimpleNamespace(pair="BTC-USDT", filter_manager=MagicMock())

    result = FilterCoordinator(_stub_with_managers(global_mgr)).handle_mtf_bar(ctx, MagicMock())

    assert result is True
    global_mgr.update.assert_called_once()
    ctx.filter_manager.update.assert_not_called()  # per-pair untouched on global hit


@requires_nautilus
def test_handle_mtf_bar_per_pair_hit():
    """Per-pair MTF bar (no global hit): update per-pair manager, return True."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    pair_mgr = MagicMock()
    pair_mgr.is_mtf_bar.return_value = True
    ctx = SimpleNamespace(pair="BTC-USDT", filter_manager=pair_mgr)

    # _global_filter_manager=None -> global branch skipped
    result = FilterCoordinator(_stub_with_managers(None)).handle_mtf_bar(ctx, MagicMock())

    assert result is True
    pair_mgr.update.assert_called_once()


@requires_nautilus
def test_handle_mtf_bar_non_mtf_returns_false():
    """Non-MTF bar: neither manager updated, return False (pipeline continues)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    global_mgr = MagicMock()
    global_mgr.is_mtf_bar.return_value = False
    pair_mgr = MagicMock()
    pair_mgr.is_mtf_bar.return_value = False
    ctx = SimpleNamespace(pair="BTC-USDT", filter_manager=pair_mgr)

    result = FilterCoordinator(_stub_with_managers(global_mgr)).handle_mtf_bar(ctx, MagicMock())

    assert result is False
    global_mgr.update.assert_not_called()
    pair_mgr.update.assert_not_called()


@requires_nautilus
def test_constructed_in_init_not_on_start():
    """Built in __init__ (no pre-on_start None window); never in on_start."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    init_src = inspect.getsource(NautilusTradingStrategy.__init__)
    on_start_src = inspect.getsource(NautilusTradingStrategy.on_start)
    assert "FilterCoordinator(self)" in init_src, "FilterCoordinator must be built in __init__"
    assert "FilterCoordinator(self)" not in on_start_src, (
        "FilterCoordinator must not be built in on_start"
    )


# --- check_global honors size_factor + delay ---


def _global_strategy_stub(global_mgr):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    return SimpleNamespace(
        _global_filter_manager=global_mgr,
        _base_size_factor=1.0,
        _global_size_factor=1.0,
        _global_filter_delay_until=0,
        log=MagicMock(),
    )


@requires_nautilus
def test_check_global_applies_size_factor():
    """A global reduce_size result (passed=True, size_factor<1) must be recorded so
    per-pair sizing combines it (was discarded)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    global_mgr = MagicMock()
    global_mgr.check.return_value = SimpleNamespace(
        passed=True, failed_filters=[], size_factor=Decimal("0.5"), delay_until=0
    )
    s = _global_strategy_stub(global_mgr)
    bar = SimpleNamespace(ts_event=1_000)
    assert FilterCoordinator(s).check_global(bar, None) is True
    assert s._global_size_factor == 0.5


@requires_nautilus
def test_check_global_honors_persistent_delay_window():
    """A global delay result opens a window that blocks subsequent global checks until
    it elapses, without re-running the filters."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    global_mgr = MagicMock()
    global_mgr.check.return_value = SimpleNamespace(
        passed=False, failed_filters=["time"], size_factor=Decimal("1"), delay_until=5_000
    )
    s = _global_strategy_stub(global_mgr)
    coord = FilterCoordinator(s)

    assert coord.check_global(SimpleNamespace(ts_event=1_000), None) is False
    assert s._global_filter_delay_until == 5_000
    # Inside the window: blocked without re-running the manager.
    assert coord.check_global(SimpleNamespace(ts_event=4_000), None) is False
    assert global_mgr.check.call_count == 1
