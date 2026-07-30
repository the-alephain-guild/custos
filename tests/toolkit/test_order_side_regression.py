# tests/test_order_side_regression.py
"""
Regression tests for order side determination bug fix.

Verifies the fix for:
BinanceClientError({'code': -2022, 'msg': 'ReduceOnly Order is rejected.'})

The issue was that StopLossSubmitter and TakeProfitSubmitter determined order side
using position.is_long instead of signal.direction. When SL/TP orders were submitted
immediately after entry (before fill), the position in cache might be from a PREVIOUS
trade, causing wrong order side.

The fix uses signal.direction to determine order side, consistent with
create_scaled_orders() which already did this correctly.

Bug scenario:
1. Previous LONG position exists
2. Strategy gets ENTER_SHORT signal
3. Entry SELL order submitted
4. SL/TP submitted using position.is_long=True (from old position) → side=SELL
5. Binance rejects ReduceOnly SELL because SHORT position needs BUY to close

Fix:
- ENTER_LONG signal → side=SELL (to close long)
- ENTER_SHORT signal → side=BUY (to close short)
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
from custos_toolkit.risk.orders import OrderPriceCalculator

# Use normal imports - the dependency issue has been resolved
from custos_toolkit.signals.types import Signal, SignalDirection
from custos_toolkit_nautilus.adapter.orders import StopLossSubmitter, TakeProfitSubmitter
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId

# =============================================================================
# Mock Classes
# =============================================================================


class MockInstrument:
    """Mock Nautilus instrument for testing."""

    price_precision = 2
    price_increment = Decimal("0.01")

    def make_qty(self, qty):
        """Return quantity as-is for testing."""
        return qty


class MockPosition:
    """Mock Nautilus position for testing."""

    def __init__(self, is_long: bool = True, quantity: float = 1.0):
        self.is_long = is_long
        self.quantity = Decimal(str(quantity))


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_order_factory():
    """Create mock order factory."""
    factory = MagicMock()
    factory.stop_market.return_value = MagicMock(name="StopMarketOrder")
    factory.limit.return_value = MagicMock(name="LimitOrder")
    return factory


@pytest.fixture
def mock_cache():
    """Create mock cache with instrument."""
    cache = MagicMock()
    cache.instrument.return_value = MockInstrument()
    return cache


@pytest.fixture
def mock_log():
    """Create mock logger."""
    return MagicMock()


@pytest.fixture
def order_calculator():
    """OrderPriceCalculator with fixed stop loss and take profit."""
    config = {
        "stop_loss": {
            "method": "fixed",
            "fixed": {"value": 0.02},  # 2% stop loss
        },
        "take_profit": {
            "method": "fixed",
            "fixed": {"value": 0.04},  # 4% take profit
        },
    }
    return OrderPriceCalculator(config)


@pytest.fixture
def instrument_id():
    """Create test instrument ID."""
    return InstrumentId.from_str("BTCUSDT-PERP.BINANCE")


# =============================================================================
# Regression Tests: Order Side from Signal Direction
# =============================================================================


class TestStopLossOrderSideFromSignalDirection:
    """
    Regression tests verifying StopLossSubmitter uses signal.direction
    instead of position.is_long for order side determination.
    """

    def test_enter_short_signal_with_stale_long_position_creates_buy_order(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """
        CRITICAL REGRESSION TEST: ENTER_SHORT signal with stale LONG position.

        This is the exact bug scenario:
        - Position in cache is LONG (from previous trade, not yet closed)
        - Signal is ENTER_SHORT (new trade direction)
        - Old code: used position.is_long=True → side=SELL (WRONG)
        - New code: uses signal.direction=ENTER_SHORT → side=BUY (CORRECT)
        """
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        # ENTER_SHORT signal (new trade)
        signal = Signal.enter_short(price=Decimal("100"))

        # Stale LONG position (from previous trade, still in cache)
        stale_position = MockPosition(is_long=True, quantity=0.5)

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=stale_position,
        )

        assert order is not None
        call_kwargs = mock_order_factory.stop_market.call_args[1]

        # MUST be BUY (to close SHORT), not SELL (which would be wrong for SHORT)
        assert call_kwargs["order_side"] == OrderSide.BUY, (
            "ENTER_SHORT signal must create BUY stop order, "
            "regardless of stale position.is_long value"
        )

        # Stop price for SHORT should be ABOVE entry (102.0 = 100 * 1.02)
        assert float(call_kwargs["trigger_price"]) == pytest.approx(102.0, rel=0.001)

    def test_enter_long_signal_with_stale_short_position_creates_sell_order(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """
        ENTER_LONG signal with stale SHORT position must create SELL order.

        - Position in cache is SHORT (from previous trade)
        - Signal is ENTER_LONG (new trade direction)
        - Must create SELL stop order (to close LONG)
        """
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        # ENTER_LONG signal (new trade)
        signal = Signal.enter_long(price=Decimal("100"))

        # Stale SHORT position (from previous trade, still in cache)
        stale_position = MockPosition(is_long=False, quantity=0.5)

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=stale_position,
        )

        assert order is not None
        call_kwargs = mock_order_factory.stop_market.call_args[1]

        # MUST be SELL (to close LONG), not BUY (which would be wrong for LONG)
        assert call_kwargs["order_side"] == OrderSide.SELL, (
            "ENTER_LONG signal must create SELL stop order, "
            "regardless of stale position.is_long value"
        )

        # Stop price for LONG should be BELOW entry (98.0 = 100 * 0.98)
        assert float(call_kwargs["trigger_price"]) == pytest.approx(98.0, rel=0.001)

    def test_signal_direction_determines_side_not_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """Verify signal direction is used for order side in all cases."""
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        test_cases = [
            # (signal_direction, position_is_long, expected_side)
            (SignalDirection.ENTER_LONG, True, OrderSide.SELL),  # Normal case
            (SignalDirection.ENTER_LONG, False, OrderSide.SELL),  # Stale position
            (SignalDirection.ENTER_SHORT, False, OrderSide.BUY),  # Normal case
            (SignalDirection.ENTER_SHORT, True, OrderSide.BUY),  # Stale position (bug scenario)
        ]

        for signal_dir, pos_is_long, expected_side in test_cases:
            mock_order_factory.reset_mock()

            if signal_dir == SignalDirection.ENTER_LONG:
                signal = Signal.enter_long(price=Decimal("100"))
            else:
                signal = Signal.enter_short(price=Decimal("100"))

            position = MockPosition(is_long=pos_is_long, quantity=1.0)

            order = submitter.create_order(
                instrument_id=instrument_id,
                signal=signal,
                entry_price=Decimal("100"),
                atr=None,
                position=position,
            )

            assert order is not None
            call_kwargs = mock_order_factory.stop_market.call_args[1]
            assert call_kwargs["order_side"] == expected_side, (
                f"Signal {signal_dir.name} with position.is_long={pos_is_long} "
                f"should create {expected_side.name} order"
            )


class TestTakeProfitOrderSideFromSignalDirection:
    """
    Regression tests verifying TakeProfitSubmitter uses signal.direction
    instead of position.is_long for order side determination.
    """

    def test_enter_short_signal_with_stale_long_position_creates_buy_order(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """
        CRITICAL REGRESSION TEST: ENTER_SHORT signal with stale LONG position.

        Same bug scenario as stop loss:
        - Position in cache is LONG (from previous trade)
        - Signal is ENTER_SHORT (new trade direction)
        - Must create BUY limit order (to close SHORT at profit)
        """
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        # ENTER_SHORT signal (new trade)
        signal = Signal.enter_short(price=Decimal("100"))

        # Stale LONG position (from previous trade, still in cache)
        stale_position = MockPosition(is_long=True, quantity=0.5)

        order = submitter.create_single_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=None,
            position=stale_position,
        )

        assert order is not None
        call_kwargs = mock_order_factory.limit.call_args[1]

        # MUST be BUY (to close SHORT), not SELL
        assert call_kwargs["order_side"] == OrderSide.BUY, (
            "ENTER_SHORT signal must create BUY take profit order, "
            "regardless of stale position.is_long value"
        )

        # TP price for SHORT should be BELOW entry (96.0 = 100 * 0.96)
        assert float(call_kwargs["price"]) == pytest.approx(96.0, rel=0.001)

    def test_enter_long_signal_with_stale_short_position_creates_sell_order(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """
        ENTER_LONG signal with stale SHORT position must create SELL order.
        """
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        # ENTER_LONG signal (new trade)
        signal = Signal.enter_long(price=Decimal("100"))

        # Stale SHORT position (from previous trade, still in cache)
        stale_position = MockPosition(is_long=False, quantity=0.5)

        order = submitter.create_single_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=None,
            position=stale_position,
        )

        assert order is not None
        call_kwargs = mock_order_factory.limit.call_args[1]

        # MUST be SELL (to close LONG), not BUY
        assert call_kwargs["order_side"] == OrderSide.SELL, (
            "ENTER_LONG signal must create SELL take profit order, "
            "regardless of stale position.is_long value"
        )

        # TP price for LONG should be ABOVE entry (104.0 = 100 * 1.04)
        assert float(call_kwargs["price"]) == pytest.approx(104.0, rel=0.001)

    def test_signal_direction_determines_side_not_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """Verify signal direction is used for order side in all cases."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        test_cases = [
            # (signal_direction, position_is_long, expected_side, expected_tp_price)
            (SignalDirection.ENTER_LONG, True, OrderSide.SELL, 104.0),  # Normal case
            (SignalDirection.ENTER_LONG, False, OrderSide.SELL, 104.0),  # Stale position
            (SignalDirection.ENTER_SHORT, False, OrderSide.BUY, 96.0),  # Normal case
            (SignalDirection.ENTER_SHORT, True, OrderSide.BUY, 96.0),  # Stale position (bug)
        ]

        for signal_dir, pos_is_long, expected_side, expected_price in test_cases:
            mock_order_factory.reset_mock()

            if signal_dir == SignalDirection.ENTER_LONG:
                signal = Signal.enter_long(price=Decimal("100"))
            else:
                signal = Signal.enter_short(price=Decimal("100"))

            position = MockPosition(is_long=pos_is_long, quantity=1.0)

            order = submitter.create_single_order(
                instrument_id=instrument_id,
                signal=signal,
                entry_price=Decimal("100"),
                atr=None,
                stop_loss=None,
                position=position,
            )

            assert order is not None
            call_kwargs = mock_order_factory.limit.call_args[1]
            assert call_kwargs["order_side"] == expected_side, (
                f"Signal {signal_dir.name} with position.is_long={pos_is_long} "
                f"should create {expected_side.name} order"
            )
            assert float(call_kwargs["price"]) == pytest.approx(expected_price, rel=0.001), (
                f"Signal {signal_dir.name} should have TP price {expected_price}"
            )


# =============================================================================
# Consistency Tests: Scaled Orders vs Single Orders
# =============================================================================


class TestScaledOrdersConsistency:
    """Verify scaled orders use same side determination as single orders."""

    def test_scaled_orders_use_signal_direction_for_short(
        self, mock_order_factory, mock_cache, mock_log, order_calculator, instrument_id
    ):
        """Scaled orders for SHORT should create BUY orders."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator,
        )

        signal = Signal.enter_short(price=Decimal("100"))
        position = MockPosition(is_long=True, quantity=1.0)  # Stale LONG position

        class MockScaledConfig:
            levels = 2
            level_1 = type("Level", (), {"target_pct": 0.02, "exit_pct": 0.5})()
            level_2 = type("Level", (), {"target_pct": 0.04, "exit_pct": 0.5})()
            level_3 = type(
                "Level", (), {"target_pct": 0.06, "exit_pct": 0.0}
            )()  # Not used but needed

        orders = submitter.create_scaled_orders(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            position=position,
            scaled_config=MockScaledConfig(),
        )

        assert len(orders) == 2
        for i, call in enumerate(mock_order_factory.limit.call_args_list):
            call_kwargs = call[1]
            assert call_kwargs["order_side"] == OrderSide.BUY, (
                f"Scaled order {i + 1} for ENTER_SHORT should be BUY"
            )
