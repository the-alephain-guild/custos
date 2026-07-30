# tests/test_base_strategy_filters.py
"""Tests for filter initialization, now hosted by FilterCoordinator.

The FilterManager wiring was extracted from NautilusTradingStrategy to
FilterCoordinator; these white-box getsource/hasattr assertions follow the code
to its new home. The orchestration call sites (on_start / _process_bar) stay on
the strategy and delegate to ``self._filter_coordinator``.
"""

import inspect

import pytest


def _can_import_nautilus():
    """Check if nautilus_trader can be imported."""
    try:
        from nautilus_trader.trading.strategy import Strategy  # noqa: F401

        return True
    except ImportError:
        return False


requires_nautilus = pytest.mark.skipif(
    not _can_import_nautilus(), reason="nautilus_trader not installed"
)


class TestBaseStrategyGlobalFilterManager:
    """Tests for the global filter manager (_global_filter_manager field stays on
    the strategy; init logic lives on FilterCoordinator)."""

    @requires_nautilus
    def test_global_filter_manager_attribute_exists_in_init(self):
        """Test _global_filter_manager is defined in __init__."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        source = inspect.getsource(NautilusTradingStrategy.__init__)
        assert "_global_filter_manager" in source, (
            "__init__ should define _global_filter_manager attribute"
        )

    @requires_nautilus
    def test_init_global_uses_scope_filter_global(self):
        """Test init_global creates global FilterManager with scope_filter='global'."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        source = inspect.getsource(FilterCoordinator.init_global)
        assert 'scope_filter="global"' in source or "scope_filter='global'" in source, (
            "init_global should create FilterManager with scope_filter='global'"
        )

    @requires_nautilus
    def test_init_global_stores_in_global_filter_manager(self):
        """Test init_global stores result in _global_filter_manager."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        source = inspect.getsource(FilterCoordinator.init_global)
        assert "_global_filter_manager" in source, (
            "init_global should store result in _global_filter_manager"
        )


class TestBaseStrategyPairFilters:
    """Tests for per-pair filter initialization."""

    @requires_nautilus
    def test_init_pair_method_exists(self):
        """Test FilterCoordinator.init_pair method exists."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        assert hasattr(FilterCoordinator, "init_pair"), (
            "FilterCoordinator should have init_pair method"
        )

    @requires_nautilus
    def test_init_pair_uses_scope_filter_per_pair(self):
        """Test init_pair creates FilterManager with scope_filter='per_pair'."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        source = inspect.getsource(FilterCoordinator.init_pair)
        assert 'scope_filter="per_pair"' in source or "scope_filter='per_pair'" in source, (
            "init_pair should create FilterManager with scope_filter='per_pair'"
        )

    @requires_nautilus
    def test_init_pair_stores_in_context(self):
        """Test init_pair stores FilterManager in ctx.filter_manager."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        source = inspect.getsource(FilterCoordinator.init_pair)
        assert "ctx.filter_manager" in source, (
            "init_pair should store FilterManager in ctx.filter_manager"
        )

    @requires_nautilus
    def test_setup_pairs_calls_init_pair(self):
        """Per-pair filter init is delegated to the coordinator inside the pair
        bootstrap (PairContextCoordinator.setup_pairs)."""
        from custos_toolkit_nautilus.adapter.coordinators import PairContextCoordinator

        source = inspect.getsource(PairContextCoordinator.setup_pairs)
        assert "_filter_coordinator.init_pair" in source, (
            "setup_pairs should call _filter_coordinator.init_pair"
        )


class TestDualLayerFilterArchitecture:
    """Tests for dual-layer filter architecture (global + per-pair)."""

    @requires_nautilus
    def test_check_pair_method_exists(self):
        """Test FilterCoordinator.check_pair method exists for per-pair filter checks."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        assert hasattr(FilterCoordinator, "check_pair"), (
            "FilterCoordinator should have check_pair method"
        )

    @requires_nautilus
    def test_check_global_uses_global_filter_manager(self):
        """Test check_global uses _global_filter_manager."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        source = inspect.getsource(FilterCoordinator.check_global)
        assert "_global_filter_manager" in source, "check_global should use _global_filter_manager"

    @requires_nautilus
    def test_update_global_uses_global_filter_manager(self):
        """Test update_global uses _global_filter_manager."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        source = inspect.getsource(FilterCoordinator.update_global)
        assert "_global_filter_manager" in source, "update_global should use _global_filter_manager"

    @requires_nautilus
    def test_entry_gate_checks_both_global_and_pair_filters(self):
        """The entry gate delegates both global and per-pair filter checks (entry-only;
        filters no longer gate the whole bar after the direction redesign)."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        source = inspect.getsource(NautilusTradingStrategy._entry_gates_pass)
        # Should check global filters
        assert "_filter_coordinator.check_global" in source, (
            "_entry_gates_pass should call _filter_coordinator.check_global for global filters"
        )
        # Should check per-pair filters
        assert "_filter_coordinator.check_pair" in source, (
            "_entry_gates_pass should call _filter_coordinator.check_pair for per-pair filters"
        )

    @requires_nautilus
    def test_check_pair_checks_only_update_split_out(self):
        """check_pair only checks; per-bar state update moved to update_pair so it
        runs every bar while gating happens at the entry."""
        from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

        check_src = inspect.getsource(FilterCoordinator.check_pair)
        assert "filter_manager.check" in check_src, "check_pair should check per-pair filters"
        assert "filter_manager.update" not in check_src, (
            "check_pair must not update -- update moved to update_pair (runs every bar)"
        )
        update_src = inspect.getsource(FilterCoordinator.update_pair)
        assert "filter_manager.update" in update_src, "update_pair should update per-pair filters"

    @requires_nautilus
    def test_old_filter_manager_removed(self):
        """Test old _filter_manager attribute is removed from __init__."""
        from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

        source = inspect.getsource(NautilusTradingStrategy.__init__)
        # Should not have the old pattern
        assert "self._filter_manager: FilterManager" not in source, (
            "__init__ should not use old _filter_manager attribute"
        )
