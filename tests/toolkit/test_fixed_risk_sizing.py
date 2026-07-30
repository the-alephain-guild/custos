"""fixed_risk sizing through the native FixedRiskSizer.

The wiring is verified against known absolute input and output
values, not just ranges. Critical: the native FixedRiskSizer default
``unit_batch_size=1`` floors sub-1 crypto quantities to 0 — our helper must pass
``instrument.size_increment`` (else 0.1 BTC rounds to 0).
"""

import inspect
from decimal import Decimal

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    SignalExecutionCoordinator,
    SizingCoordinator,  # noqa: E402
)
from custos_toolkit_nautilus.adapter.sizing import compute_fixed_risk_qty  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402


class TestComputeFixedRiskQty:
    """Absolute-value assertions against hand-computed expectations."""

    def _instrument(self):
        return TestInstrumentProvider.btcusdt_binance()

    def test_one_percent_risk_thousand_distance(self):
        # equity 10000, risk 1% => 100 USDT; SL distance 1000 => qty = 100/1000 = 0.1 BTC
        inst = self._instrument()
        qty = compute_fixed_risk_qty(
            inst, Decimal("50000"), Decimal("49000"), Decimal("10000"), 0.01
        )
        assert abs(Decimal(str(qty)) - Decimal("0.1")) < Decimal("0.0001")

    def test_two_percent_risk_five_hundred_distance(self):
        # equity 10000, risk 2% => 200 USDT; SL distance 500 => qty = 200/500 = 0.4 BTC
        inst = self._instrument()
        qty = compute_fixed_risk_qty(
            inst, Decimal("50000"), Decimal("49500"), Decimal("10000"), 0.02
        )
        assert abs(Decimal(str(qty)) - Decimal("0.4")) < Decimal("0.0001")

    def test_short_side_distance_uses_abs(self):
        # SL above entry (short): distance still 1000 => 0.1 BTC
        inst = self._instrument()
        qty = compute_fixed_risk_qty(
            inst, Decimal("50000"), Decimal("51000"), Decimal("10000"), 0.01
        )
        assert abs(Decimal(str(qty)) - Decimal("0.1")) < Decimal("0.0001")

    def test_zero_sl_distance_returns_zero(self):
        inst = self._instrument()
        qty = compute_fixed_risk_qty(
            inst, Decimal("50000"), Decimal("50000"), Decimal("10000"), 0.01
        )
        assert Decimal(str(qty)) == Decimal("0")

    def test_default_unit_batch_would_floor_to_zero_guard(self):
        """A regression guard: the helper must not floor a sub-1 quantity to zero."""
        inst = self._instrument()
        qty = compute_fixed_risk_qty(
            inst, Decimal("50000"), Decimal("49000"), Decimal("10000"), 0.01
        )
        assert Decimal(str(qty)) > Decimal("0"), "sub-1 crypto qty must not be floored to 0"


class TestDefaultPositionSizeBranch:
    def test_default_position_size_branches_to_fixed_risk(self):
        src = inspect.getsource(SizingCoordinator.default_position_size)
        assert "fixed_risk" in src, "default_position_size must branch on size_type=='fixed_risk'"
        assert "_fixed_risk_position_size" in src

    def test_fixed_risk_method_uses_native_sizer_and_notional(self):
        src = inspect.getsource(SizingCoordinator._fixed_risk_position_size)
        assert "compute_fixed_risk_qty" in src, "must use the native FixedRiskSizer helper"
        assert "calculate_stop_loss" in src, "must derive the stop-loss price"
        # result is expressed as notional (qty * entry) to fit the existing pipeline
        assert "entry_price" in src

    def test_fixed_risk_applies_position_limits(self):
        """Self-reflect R1: fixed_risk notional must pass through check_limits."""
        src = inspect.getsource(SizingCoordinator._fixed_risk_position_size)
        assert "check_limits" in src, (
            "fixed_risk must apply position-limit safety caps (max_position_pct etc.)"
        )


class TestEntryZeroSizeGuard:
    """A computed size at or below zero skips the entry, rather than ordering nothing."""

    def test_execute_entry_guards_zero_size(self):
        src = inspect.getsource(SignalExecutionCoordinator.execute_entry_for_pair)
        assert "final_size <= 0" in src, (
            "execute_entry_for_pair must skip when final_size <= 0 (avoid make_qty(0) order)"
        )
        # the guard must return before building the order
        guard_idx = src.index("final_size <= 0")
        order_idx = src.index("create_entry_order")
        assert guard_idx < order_idx, "0-size guard must come before create_entry_order"
