# tests/test_execution_manager.py
"""Tests for ExecutionManager."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.execution import ExecutionManager
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId

# =============================================================================
# Mock Classes
# =============================================================================


class MockInstrument:
    """Mock Nautilus instrument for testing."""

    price_precision = 2
    price_increment = Decimal("0.01")
    size_precision = 8

    def make_qty(self, qty):
        """Return quantity as-is for testing."""
        return qty


class MockBar:
    """Mock Nautilus bar for testing."""

    def __init__(self, close: float = 100.0):
        self.close = Decimal(str(close))


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_order_factory():
    """Create mock order factory."""
    factory = MagicMock()
    factory.market.return_value = MagicMock(name="MarketOrder")
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
def execution_manager(mock_order_factory, mock_cache, mock_log):
    """Create ExecutionManager instance."""
    return ExecutionManager(mock_order_factory, mock_cache, mock_log)


@pytest.fixture
def instrument_id():
    """Create test instrument ID."""
    return InstrumentId.from_str("BTCUSDT.BINANCE")


# =============================================================================
# Entry Order Tests
# =============================================================================


class TestCreateEntryOrder:
    """Tests for create_entry_order method."""

    def test_create_long_entry_market_order(
        self, execution_manager, mock_order_factory, instrument_id
    ):
        """Long entry signal should create BUY market order."""
        signal = Signal.enter_long(price=100.0)
        bar = MockBar(close=100.0)

        order = execution_manager.create_entry_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
            bar=bar,
            order_type="market",
        )

        assert order is not None
        mock_order_factory.market.assert_called_once()
        call_kwargs = mock_order_factory.market.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.BUY
        assert call_kwargs["instrument_id"] == instrument_id
        assert call_kwargs["time_in_force"] == TimeInForce.IOC

    def test_create_short_entry_market_order(
        self, execution_manager, mock_order_factory, instrument_id
    ):
        """Short entry signal should create SELL market order."""
        signal = Signal.enter_short(price=100.0)
        bar = MockBar(close=100.0)

        order = execution_manager.create_entry_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
            bar=bar,
            order_type="market",
        )

        assert order is not None
        mock_order_factory.market.assert_called_once()
        call_kwargs = mock_order_factory.market.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.SELL

    def test_create_limit_order_buy(self, execution_manager, mock_order_factory, instrument_id):
        """Limit order should be created with price offset for BUY."""
        signal = Signal.enter_long(price=100.0)
        bar = MockBar(close=100.0)
        slippage = Decimal("0.001")  # 0.1%

        order = execution_manager.create_entry_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
            bar=bar,
            order_type="limit",
            slippage_tolerance=slippage,
        )

        assert order is not None
        mock_order_factory.limit.assert_called_once()
        call_kwargs = mock_order_factory.limit.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.BUY
        # BUY limit: price * (1 - slippage) = 100 * 0.999 = 99.9
        expected_price = Decimal("100") * (Decimal("1") - slippage)
        assert float(call_kwargs["price"]) == pytest.approx(float(expected_price), rel=0.001)

    def test_create_limit_order_sell(self, execution_manager, mock_order_factory, instrument_id):
        """Limit order should be created with price offset for SELL."""
        signal = Signal.enter_short(price=100.0)
        bar = MockBar(close=100.0)
        slippage = Decimal("0.001")  # 0.1%

        order = execution_manager.create_entry_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
            bar=bar,
            order_type="limit",
            slippage_tolerance=slippage,
        )

        assert order is not None
        mock_order_factory.limit.assert_called_once()
        call_kwargs = mock_order_factory.limit.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.SELL
        # SELL limit: price * (1 + slippage) = 100 * 1.001 = 100.1
        expected_price = Decimal("100") * (Decimal("1") + slippage)
        assert float(call_kwargs["price"]) == pytest.approx(float(expected_price), rel=0.001)

    def test_neutral_signal_returns_none(
        self, execution_manager, mock_order_factory, instrument_id
    ):
        """Neutral signal should return None without creating order."""
        signal = Signal.neutral(price=100.0)
        bar = MockBar(close=100.0)

        order = execution_manager.create_entry_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
            bar=bar,
        )

        assert order is None
        mock_order_factory.market.assert_not_called()
        mock_order_factory.limit.assert_not_called()

    def test_missing_instrument_returns_none(self, mock_order_factory, mock_log, instrument_id):
        """Missing instrument should return None and log error."""
        cache = MagicMock()
        cache.instrument.return_value = None

        manager = ExecutionManager(mock_order_factory, cache, mock_log)
        signal = Signal.enter_long(price=100.0)
        bar = MockBar(close=100.0)

        order = manager.create_entry_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
            bar=bar,
        )

        assert order is None
        mock_log.error.assert_called()


# =============================================================================
# Exit Order Tests
# =============================================================================


class TestCreateExitOrder:
    """Tests for create_exit_order method."""

    def test_create_exit_order_long(self, execution_manager, mock_order_factory, instrument_id):
        """Exit long signal should create SELL market order."""
        signal = Signal.exit_long(price=110.0)

        order = execution_manager.create_exit_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
        )

        assert order is not None
        mock_order_factory.market.assert_called_once()
        call_kwargs = mock_order_factory.market.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.SELL
        assert call_kwargs["instrument_id"] == instrument_id
        assert call_kwargs["time_in_force"] == TimeInForce.IOC
        # reduce_only protects the money path: if a retry fires while the local cache
        # still lags a filled close, the venue rejects it instead of opening a reverse.
        assert call_kwargs["reduce_only"] is True

    def test_create_exit_order_short(self, execution_manager, mock_order_factory, instrument_id):
        """Exit short signal should create BUY market order."""
        signal = Signal.exit_short(price=90.0)

        order = execution_manager.create_exit_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
        )

        assert order is not None
        mock_order_factory.market.assert_called_once()
        call_kwargs = mock_order_factory.market.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.BUY

    def test_exit_neutral_signal_returns_none(
        self, execution_manager, mock_order_factory, instrument_id
    ):
        """Neutral signal on exit should return None."""
        signal = Signal.neutral(price=100.0)

        order = execution_manager.create_exit_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
        )

        assert order is None
        mock_order_factory.market.assert_not_called()

    def test_exit_missing_instrument_returns_none(
        self, mock_order_factory, mock_log, instrument_id
    ):
        """Missing instrument on exit should return None."""
        cache = MagicMock()
        cache.instrument.return_value = None

        manager = ExecutionManager(mock_order_factory, cache, mock_log)
        signal = Signal.exit_long(price=110.0)

        order = manager.create_exit_order(
            instrument_id=instrument_id,
            signal=signal,
            size=Decimal("1.0"),
        )

        assert order is None
