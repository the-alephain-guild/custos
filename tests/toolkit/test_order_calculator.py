# tests/test_order_calculator.py
"""Tests for OrderPriceCalculator."""

from decimal import Decimal

from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit.signals.types import SignalDirection


class TestOrderPriceCalculator:
    """Tests for OrderPriceCalculator."""

    def test_fixed_stop_loss_long(self):
        """Should calculate fixed stop loss for long."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "fixed",
                    "fixed": {"value": 0.02},
                }
            }
        )
        sl = calc.calculate_stop_loss(Decimal("100"), SignalDirection.ENTER_LONG)
        assert sl == Decimal("98")  # 100 * 0.98

    def test_fixed_stop_loss_short(self):
        """Should calculate fixed stop loss for short."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "fixed",
                    "fixed": {"value": 0.02},
                }
            }
        )
        sl = calc.calculate_stop_loss(Decimal("100"), SignalDirection.ENTER_SHORT)
        assert sl == Decimal("102")  # 100 * 1.02

    def test_atr_stop_loss_long(self):
        """Should calculate ATR stop loss for long."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "atr",
                    "atr": {"multiplier": 2.0},
                }
            }
        )
        sl = calc.calculate_stop_loss(Decimal("100"), SignalDirection.ENTER_LONG, atr=Decimal("5"))
        assert sl == Decimal("90")  # 100 - (5 * 2)

    def test_atr_stop_loss_short(self):
        """Should calculate ATR stop loss for short."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "method": "atr",
                    "atr": {"multiplier": 2.0},
                }
            }
        )
        sl = calc.calculate_stop_loss(Decimal("100"), SignalDirection.ENTER_SHORT, atr=Decimal("5"))
        assert sl == Decimal("110")  # 100 + (5 * 2)

    def test_no_stop_loss_config(self):
        """Should return None without stop loss config."""
        calc = OrderPriceCalculator({})
        assert calc.calculate_stop_loss(Decimal("100"), SignalDirection.ENTER_LONG) is None

    def test_fixed_take_profit_long(self):
        """Should calculate fixed take profit for long."""
        calc = OrderPriceCalculator(
            {
                "take_profit": {
                    "method": "fixed",
                    "fixed": {"value": 0.04},
                }
            }
        )
        tp = calc.calculate_take_profit(Decimal("100"), SignalDirection.ENTER_LONG)
        assert tp == Decimal("104")  # 100 * 1.04

    def test_atr_take_profit_short(self):
        """Should calculate ATR take profit for short."""
        calc = OrderPriceCalculator(
            {
                "take_profit": {
                    "method": "atr",
                    "atr": {"multiplier": 3.0},
                }
            }
        )
        tp = calc.calculate_take_profit(
            Decimal("100"), SignalDirection.ENTER_SHORT, atr=Decimal("5")
        )
        assert tp == Decimal("85")  # 100 - (5 * 3)

    def test_risk_reward_take_profit(self):
        """Should calculate take profit from risk/reward ratio."""
        calc = OrderPriceCalculator(
            {
                "take_profit": {
                    "method": "risk_reward",
                    "risk_reward_ratio": 2.0,
                }
            }
        )
        # Entry 100, SL 95 = risk of 5
        # TP = 100 + (5 * 2) = 110
        tp = calc.calculate_take_profit(
            Decimal("100"), SignalDirection.ENTER_LONG, stop_loss=Decimal("95")
        )
        assert tp == Decimal("110")

    def test_trailing_stop_not_activated(self):
        """Should return None if trailing not activated."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "trailing": {
                        "enabled": True,
                        "activation_pct": 0.02,
                        "trailing_pct": 0.01,
                    }
                }
            }
        )
        # Entry 100, current 101 = 1% profit (need 2%)
        result = calc.calculate_trailing_stop(
            Decimal("100"), Decimal("101"), Decimal("98"), SignalDirection.ENTER_LONG
        )
        assert result is None

    def test_trailing_stop_activated_long(self):
        """Should update trailing stop when activated (long)."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "trailing": {
                        "enabled": True,
                        "activation_pct": 0.02,
                        "trailing_pct": 0.01,
                    }
                }
            }
        )
        # Entry 100, current 103 = 3% profit, current stop 98
        # New stop = 103 * 0.99 = 101.97
        result = calc.calculate_trailing_stop(
            Decimal("100"), Decimal("103"), Decimal("98"), SignalDirection.ENTER_LONG
        )
        assert result == Decimal("101.97")

    def test_trailing_stop_only_moves_up_long(self):
        """Trailing stop should only move up for long."""
        calc = OrderPriceCalculator(
            {
                "stop_loss": {
                    "trailing": {
                        "enabled": True,
                        "activation_pct": 0.02,
                        "trailing_pct": 0.01,
                    }
                }
            }
        )
        # Entry 100, current 103, current stop 102
        # New stop = 103 * 0.99 = 101.97 < 102, so keep 102
        result = calc.calculate_trailing_stop(
            Decimal("100"), Decimal("103"), Decimal("102"), SignalDirection.ENTER_LONG
        )
        assert result == Decimal("102")
