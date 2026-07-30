"""Tests for block 1: tick full-position exit uses native close_position().

The hand-written `order_factory.market(reduce_only, IOC)` full-close
in `_execute_trailing_stop_exit_for_pair` is replaced by NautilusTrader's native
`self.close_position(position, time_in_force=IOC, reduce_only=True)`.

The three defences must be preserved:
  1. `can_submit_close` in-flight/cooldown gate (protection 1)
  2. cancel resting reduce_only orders before close (protection 2)
  3. `mark_closing` in-flight gate after submit (protection 3)
"""

import inspect

import pytest

pytest.importorskip("msgspec")


def _can_import_nautilus() -> bool:
    try:
        from nautilus_trader.trading.strategy import Strategy  # noqa: F401

        return True
    except ImportError:
        return False


requires_nautilus = pytest.mark.skipif(
    not _can_import_nautilus(), reason="nautilus_trader not installed"
)


class TestTickExitUsesClosePosition:
    """The tick full-close path must use native close_position, keeping defenses."""

    @requires_nautilus
    def test_full_close_uses_native_close_position(self):
        from custos_toolkit_nautilus.adapter.coordinators import ExecutionCoordinator

        source = inspect.getsource(ExecutionCoordinator._execute_trailing_stop_exit_for_pair)

        assert "s.close_position(" in source, (
            "_execute_trailing_stop_exit_for_pair should use native close_position()"
        )

    @requires_nautilus
    def test_full_close_no_longer_uses_manual_market_order(self):
        from custos_toolkit_nautilus.adapter.coordinators import ExecutionCoordinator

        source = inspect.getsource(ExecutionCoordinator._execute_trailing_stop_exit_for_pair)

        assert "order_factory.market" not in source, (
            "Full-close should no longer build a manual market order; "
            "close_position handles side + full quantity"
        )

    @requires_nautilus
    def test_full_close_passes_ioc_and_reduce_only(self):
        from custos_toolkit_nautilus.adapter.coordinators import ExecutionCoordinator

        source = inspect.getsource(ExecutionCoordinator._execute_trailing_stop_exit_for_pair)

        assert "TimeInForce.IOC" in source, (
            "close_position must keep IOC time-in-force (not default GTC)"
        )
        assert "reduce_only=True" in source, "close_position must keep reduce_only=True"

    @requires_nautilus
    def test_three_defenses_preserved(self):
        from custos_toolkit_nautilus.adapter.coordinators import ExecutionCoordinator

        source = inspect.getsource(ExecutionCoordinator._execute_trailing_stop_exit_for_pair)

        # Protection 1: in-flight / cooldown gate
        assert "can_submit_close" in source, "lost protection 1 (can_submit_close gate)"
        # Protection 2: cancel resting reduce_only before close
        assert "resting_reduce_only" in source, (
            "lost protection 2 (cancel resting reduce_only orders)"
        )
        assert "is_reduce_only" in source, (
            "lost protection 2 detection of resting reduce_only orders"
        )
        # Protection 3: mark in-flight after submit
        assert "mark_closing" in source, "lost protection 3 (mark_closing gate)"
