# tests/test_order_submitters.py
"""Tests for StopLossSubmitter and TakeProfitSubmitter classes."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
from custos_toolkit.risk.orders import OrderPriceCalculator
from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.orders import StopLossSubmitter, TakeProfitSubmitter
from nautilus_trader.model.enums import OrderSide, TimeInForce
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


class MockScaledLevel:
    """Mock scaled exit level."""

    def __init__(self, target_pct: float, exit_pct: float):
        self.target_pct = target_pct
        self.exit_pct = exit_pct


class MockScaledConfig:
    """Mock scaled take profit configuration."""

    def __init__(self, levels: int = 3):
        self.levels = levels
        self.level_1 = MockScaledLevel(0.02, 0.33)  # 2% target, 33% exit
        self.level_2 = MockScaledLevel(0.04, 0.33)  # 4% target, 33% exit
        self.level_3 = MockScaledLevel(0.06, 0.34)  # 6% target, 34% exit


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
def order_calculator_fixed():
    """OrderPriceCalculator with fixed 2% stop loss."""
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
def order_calculator_atr():
    """OrderPriceCalculator with ATR-based stop loss."""
    config = {
        "stop_loss": {"method": "atr", "atr": {"multiplier": 2.0}},
        "take_profit": {"method": "atr", "atr": {"multiplier": 3.0}},
    }
    return OrderPriceCalculator(config)


@pytest.fixture
def order_calculator_none():
    """OrderPriceCalculator that returns None (no stop/tp configured)."""
    config = {"stop_loss": {"method": "none"}, "take_profit": {}}
    return OrderPriceCalculator(config)


@pytest.fixture
def instrument_id():
    """Create test instrument ID."""
    return InstrumentId.from_str("BTCUSDT.BINANCE")


# =============================================================================
# StopLossSubmitter Tests
# =============================================================================


class TestStopLossSubmitter:
    """Tests for StopLossSubmitter class."""

    def test_create_stop_order_long_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Long position should create SELL stop order."""
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=position,
        )

        assert order is not None
        mock_order_factory.stop_market.assert_called_once()
        call_kwargs = mock_order_factory.stop_market.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.SELL
        assert call_kwargs["instrument_id"] == instrument_id
        assert call_kwargs["time_in_force"] == TimeInForce.GTC
        assert call_kwargs["reduce_only"] is True
        # Stop price should be 100 * (1 - 0.02) = 98.0
        expected_price = 98.0
        assert float(call_kwargs["trigger_price"]) == pytest.approx(expected_price, rel=0.001)

    def test_create_stop_order_short_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Short position should create BUY stop order."""
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_short(price=100.0)
        position = MockPosition(is_long=False, quantity=1.0)

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=position,
        )

        assert order is not None
        mock_order_factory.stop_market.assert_called_once()
        call_kwargs = mock_order_factory.stop_market.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.BUY
        # Stop price should be 100 * (1 + 0.02) = 102.0
        expected_price = 102.0
        assert float(call_kwargs["trigger_price"]) == pytest.approx(expected_price, rel=0.001)

    def test_atr_stop_loss_with_atr_value(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_atr, instrument_id
    ):
        """ATR-based stop loss should use ATR multiplier."""
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_atr,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)
        atr_value = Decimal("2.5")  # ATR = 2.5

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=atr_value,
            position=position,
        )

        assert order is not None
        mock_order_factory.stop_market.assert_called_once()
        call_kwargs = mock_order_factory.stop_market.call_args[1]
        # Stop price should be 100 - (2.5 * 2.0) = 95.0
        expected_price = 95.0
        assert float(call_kwargs["trigger_price"]) == pytest.approx(expected_price, rel=0.001)

    def test_no_stop_when_calculator_returns_none(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_none, instrument_id
    ):
        """No stop order if price calculator returns None."""
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_none,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=position,
        )

        assert order is None
        mock_order_factory.stop_market.assert_not_called()

    def test_no_stop_when_instrument_not_found(
        self, mock_order_factory, mock_log, order_calculator_fixed, instrument_id
    ):
        """No stop order if instrument is not found in cache."""
        cache = MagicMock()
        cache.instrument.return_value = None

        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)

        order = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=position,
        )

        assert order is None
        mock_order_factory.stop_market.assert_not_called()


class TestCreateOrderFromPrice:
    """create_order_from_price is the shared build primitive: side-aware tick alignment

    MockInstrument has price_increment=0.01 and price_precision=2. stop_price=85840.067 is
    off that grid, so SELL aligns down to 85840.06 and BUY aligns up to 85840.07.
    """

    def test_from_price_sell_rounds_down(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        order = submitter.create_order_from_price(
            instrument_id=instrument_id,
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            stop_price=Decimal("85840.067"),
            tags=["signal_id:x"],
        )
        assert order is not None
        mock_order_factory.stop_market.assert_called_once()
        kw = mock_order_factory.stop_market.call_args[1]
        assert kw["order_side"] == OrderSide.SELL
        assert kw["reduce_only"] is True
        assert kw["time_in_force"] == TimeInForce.GTC
        assert kw["tags"] == ["signal_id:x"]
        assert float(kw["trigger_price"]) == pytest.approx(85840.06, abs=1e-9)  # SELL, down

    def test_from_price_buy_rounds_up(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        submitter.create_order_from_price(
            instrument_id=instrument_id,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            stop_price=Decimal("85840.067"),
        )
        kw = mock_order_factory.stop_market.call_args[1]
        assert kw["order_side"] == OrderSide.BUY
        assert float(kw["trigger_price"]) == pytest.approx(85840.07, abs=1e-9)  # BUY, up

    def test_from_price_no_instrument_returns_none(
        self, mock_order_factory, mock_log, order_calculator_fixed, instrument_id
    ):
        cache = MagicMock()
        cache.instrument.return_value = None
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        result = submitter.create_order_from_price(
            instrument_id=instrument_id,
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            stop_price=Decimal("100"),
        )
        assert result is None
        mock_order_factory.stop_market.assert_not_called()

    def test_create_order_delegates_to_from_price(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Zero behaviour change: create_order builds via create_order_from_price."""
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        submitter.create_order_from_price = MagicMock(return_value="SENTINEL")
        signal = Signal.enter_long(price=100.0)
        result = submitter.create_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            position=MockPosition(is_long=True, quantity=1.0),
        )
        assert result == "SENTINEL"
        submitter.create_order_from_price.assert_called_once()
        kw = submitter.create_order_from_price.call_args[1]
        assert kw["side"] == OrderSide.SELL  # long → SELL stop
        assert kw["stop_price"] == Decimal("98.00")  # 100 * (1 - 0.02)


class TestSafetySLEndToEndAlignment:
    """End-to-end absolute values: the safety stop price through the real submitter's
    side-aware alignment to the final trigger_price — no mocked submitter anywhere.

    The entry times (1 minus max_loss_pct) is deliberately off-tick so side-aware
    alignment differs from nearest. At tick 0.01, long: 100.01 * 0.95 = 95.0095, SELL
    aligns down to 95.00 where nearest would give 95.01. Short: 105.0105, BUY up to 105.02.
    """

    def _run_safety_sl(self, submitter, *, is_long, entry, max_loss_pct):
        from types import SimpleNamespace

        from custos_toolkit_nautilus.adapter.coordinators import SLTPCoordinator

        position = MagicMock(is_long=is_long, avg_px_open=entry, quantity=Decimal("1"))
        stub_cache = MagicMock()
        stub_cache.positions_open.return_value = [position]
        ctx = SimpleNamespace(
            instrument_id=InstrumentId.from_str("BTCUSDT.BINANCE"),
            pair="BTC-USDT",
            order_tracker=MagicMock(),
            sl_submitter=submitter,
            active_signal_id="s",
        )
        config = SimpleNamespace(
            risk=SimpleNamespace(trade=SimpleNamespace(max_loss_pct=max_loss_pct))
        )
        stub = SimpleNamespace(
            cache=stub_cache,
            config=config,
            log=MagicMock(),
            submit_order=MagicMock(),
            _order_signal_map={},
        )
        SLTPCoordinator(stub).submit_safety_stop_loss(ctx, MagicMock())

    def test_long_safety_sl_sell_rounds_down(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed
    ):
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        self._run_safety_sl(submitter, is_long=True, entry=100.01, max_loss_pct=0.05)
        kw = mock_order_factory.stop_market.call_args[1]
        assert kw["order_side"] == OrderSide.SELL
        assert kw["reduce_only"] is True
        # 95.0095, SELL aligns down to 95.00 — nearest would be 95.01
        assert float(kw["trigger_price"]) == pytest.approx(95.00, abs=1e-9)

    def test_short_safety_sl_buy_rounds_up(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed
    ):
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        self._run_safety_sl(submitter, is_long=False, entry=100.01, max_loss_pct=0.05)
        kw = mock_order_factory.stop_market.call_args[1]
        assert kw["order_side"] == OrderSide.BUY
        # 105.0105, BUY aligns up to 105.02 — nearest would be 105.01
        assert float(kw["trigger_price"]) == pytest.approx(105.02, abs=1e-9)


# =============================================================================
# TakeProfitSubmitter Tests
# =============================================================================


class TestTakeProfitSubmitter:
    """Tests for TakeProfitSubmitter class."""

    def test_create_tp_order_long_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Long position should create SELL limit order."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)

        order = submitter.create_single_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=None,
            position=position,
        )

        assert order is not None
        mock_order_factory.limit.assert_called_once()
        call_kwargs = mock_order_factory.limit.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.SELL
        assert call_kwargs["instrument_id"] == instrument_id
        assert call_kwargs["time_in_force"] == TimeInForce.GTC
        assert call_kwargs["reduce_only"] is True
        # TP price should be 100 * (1 + 0.04) = 104.0
        expected_price = 104.0
        assert float(call_kwargs["price"]) == pytest.approx(expected_price, rel=0.001)

    def test_create_tp_order_short_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Short position should create BUY limit order."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_short(price=100.0)
        position = MockPosition(is_long=False, quantity=1.0)

        order = submitter.create_single_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=None,
            position=position,
        )

        assert order is not None
        mock_order_factory.limit.assert_called_once()
        call_kwargs = mock_order_factory.limit.call_args[1]
        assert call_kwargs["order_side"] == OrderSide.BUY
        # TP price should be 100 * (1 - 0.04) = 96.0
        expected_price = 96.0
        assert float(call_kwargs["price"]) == pytest.approx(expected_price, rel=0.001)

    def test_no_tp_when_calculator_returns_none(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_none, instrument_id
    ):
        """No TP order if price calculator returns None."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_none,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)

        order = submitter.create_single_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=None,
            position=position,
        )

        assert order is None
        mock_order_factory.limit.assert_not_called()

    def test_create_scaled_tp_orders(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Scaled TP should create multiple limit orders at different levels."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)
        scaled_config = MockScaledConfig(levels=3)

        orders = submitter.create_scaled_orders(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            position=position,
            scaled_config=scaled_config,
        )

        assert len(orders) == 3
        assert mock_order_factory.limit.call_count == 3

        # Verify first level: 2% target, 33% exit
        call1_kwargs = mock_order_factory.limit.call_args_list[0][1]
        assert call1_kwargs["order_side"] == OrderSide.SELL
        assert float(call1_kwargs["price"]) == pytest.approx(102.0, rel=0.001)  # 100 * 1.02
        assert float(call1_kwargs["quantity"]) == pytest.approx(0.33, rel=0.001)  # 1.0 * 0.33

        # Verify second level: 4% target, 33% exit
        call2_kwargs = mock_order_factory.limit.call_args_list[1][1]
        assert float(call2_kwargs["price"]) == pytest.approx(104.0, rel=0.001)  # 100 * 1.04

        # Verify third level: 6% target, 34% exit
        call3_kwargs = mock_order_factory.limit.call_args_list[2][1]
        assert float(call3_kwargs["price"]) == pytest.approx(106.0, rel=0.001)  # 100 * 1.06

    def test_create_scaled_tp_orders_short_position(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        """Scaled TP for short position should calculate prices correctly."""
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_short(price=100.0)
        position = MockPosition(is_long=False, quantity=1.0)
        scaled_config = MockScaledConfig(levels=2)

        orders = submitter.create_scaled_orders(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            position=position,
            scaled_config=scaled_config,
        )

        assert len(orders) == 2
        assert mock_order_factory.limit.call_count == 2

        # Verify first level for short: price * (1 - target_pct)
        call1_kwargs = mock_order_factory.limit.call_args_list[0][1]
        assert call1_kwargs["order_side"] == OrderSide.BUY  # Short position closes with BUY
        assert float(call1_kwargs["price"]) == pytest.approx(98.0, rel=0.001)  # 100 * 0.98

    def test_no_tp_when_instrument_not_found(
        self, mock_order_factory, mock_log, order_calculator_fixed, instrument_id
    ):
        """No TP order if instrument is not found in cache."""
        cache = MagicMock()
        cache.instrument.return_value = None

        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )

        signal = Signal.enter_long(price=100.0)
        position = MockPosition(is_long=True, quantity=1.0)

        order = submitter.create_single_order(
            instrument_id=instrument_id,
            signal=signal,
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=None,
            position=position,
        )

        assert order is None
        mock_order_factory.limit.assert_not_called()


class TestSubmitterTagsPassthrough:
    """The stop and take-profit submitters pass tags through to order_factory, so those
    orders carry the signal id like the entry does and the push path can relate them."""

    def test_stop_loss_create_order_passes_tags(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        submitter = StopLossSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        tags = ["signal_id:sig-abc-123"]
        submitter.create_order(
            instrument_id=instrument_id,
            signal=Signal.enter_long(price=100.0),
            entry_price=Decimal("100"),
            atr=None,
            position=MockPosition(is_long=True, quantity=1.0),
            tags=tags,
        )
        mock_order_factory.stop_market.assert_called_once()
        assert mock_order_factory.stop_market.call_args[1].get("tags") == tags

    def test_take_profit_single_order_passes_tags(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        tags = ["signal_id:sig-tp-456"]
        submitter.create_single_order(
            instrument_id=instrument_id,
            signal=Signal.enter_long(price=100.0),
            entry_price=Decimal("100"),
            atr=None,
            stop_loss=Decimal("98"),
            position=MockPosition(is_long=True, quantity=1.0),
            tags=tags,
        )
        mock_order_factory.limit.assert_called_once()
        assert mock_order_factory.limit.call_args[1].get("tags") == tags

    def test_take_profit_scaled_orders_passes_tags(
        self, mock_order_factory, mock_cache, mock_log, order_calculator_fixed, instrument_id
    ):
        submitter = TakeProfitSubmitter(
            order_factory=mock_order_factory,
            cache=mock_cache,
            log=mock_log,
            order_calculator=order_calculator_fixed,
        )
        tags = ["signal_id:sig-scaled-789"]
        submitter.create_scaled_orders(
            instrument_id=instrument_id,
            signal=Signal.enter_long(price=100.0),
            entry_price=Decimal("100"),
            position=MockPosition(is_long=True, quantity=3.0),
            scaled_config=MockScaledConfig(levels=3),
            tags=tags,
        )
        assert mock_order_factory.limit.call_count >= 1
        for call in mock_order_factory.limit.call_args_list:
            assert call[1].get("tags") == tags
