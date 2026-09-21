"""fixed_risk sizing through the native FixedRiskSizer.

The wiring is verified against known absolute input and output
values, not just ranges. Critical: the native FixedRiskSizer default
``unit_batch_size=1`` floors sub-1 crypto quantities to 0 — our helper must pass
``instrument.size_increment`` (else 0.1 BTC rounds to 0).
"""

import inspect
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    SignalExecutionCoordinator,
    SizingCoordinator,  # noqa: E402
)
from custos_toolkit_nautilus.adapter.sizing import compute_fixed_risk_qty  # noqa: E402
from nautilus_trader.testkit.providers import TestInstrumentProvider  # noqa: E402


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
    """A computed size at or below zero skips the entry, rather than ordering nothing.

    This used to grep the method source for the guard expression and for its position
    relative to ``create_entry_order``. That is a test of the implementation: splitting
    the method into decide/commit without changing a single behaviour turned it red.
    It now asks the decision phase what it decided.
    """

    def test_a_zero_size_produces_no_plan_to_commit(self):
        harness = Harness()
        coordinator = SignalExecutionCoordinator(harness)

        plan = coordinator._plan_entry(
            harness.ctx,
            Signal.enter_long(price=100.0),
            Decimal("0"),
            NS(close=Decimal("100")),
        )

        assert plan is None, "a zero size must not reach the venue as make_qty(0)"
        assert harness.sent == [], "nothing may be submitted for a zero size"

    def test_a_positive_size_does_produce_a_plan(self):
        """The negative above is only evidence if the same call can succeed."""
        harness = Harness()
        coordinator = SignalExecutionCoordinator(harness)

        plan = coordinator._plan_entry(
            harness.ctx,
            Signal.enter_long(price=100.0),
            Decimal("50"),
            NS(close=Decimal("100")),
        )

        assert plan is not None
        assert plan.size == Decimal("50")
