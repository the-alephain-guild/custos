# tests/test_pair_context.py
"""Tests for PairContext."""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")


class TestPairContext:
    """Tests for PairContext."""

    def test_pair_context_creation(self):
        """Test creating a PairContext."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        assert ctx.pair == "BTC-USDT"
        assert ctx.instrument_id == instrument_id
        assert ctx.bar_type == bar_type
        assert ctx.warmed_up is False

    def test_pair_context_has_position_tracker(self):
        """Test that PairContext has position tracker."""
        from custos_toolkit.position import PositionTracker
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        assert ctx.position_tracker is not None
        assert isinstance(ctx.position_tracker, PositionTracker)

    def test_pair_context_has_order_tracker(self):
        """Test that PairContext has order tracker."""
        from custos_toolkit_nautilus.adapter.orders import OrderTracker
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        assert ctx.order_tracker is not None
        assert isinstance(ctx.order_tracker, OrderTracker)

    def test_pair_context_reset(self):
        """Test resetting PairContext."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )
        ctx.warmed_up = True
        ctx.indicators["test"] = "value"

        ctx.reset()

        assert ctx.warmed_up is False
        assert len(ctx.indicators) == 0

    def test_pair_context_indicators_dict(self):
        """Test that indicators dict works."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        mock_indicator = MagicMock()
        ctx.indicators["supertrend"] = mock_indicator

        assert ctx.indicators["supertrend"] == mock_indicator

    def test_pair_context_tick_monitor_default_none(self):
        """Test that tick_monitor defaults to None."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        assert ctx.tick_monitor is None

    def test_multiple_pair_contexts(self):
        """Test creating multiple PairContexts."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        btc_ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        )

        eth_ctx = PairContext(
            pair="ETH-USDT",
            instrument_id=InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
            bar_type=BarType.from_str("ETHUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        )

        # Each context should be independent
        btc_ctx.warmed_up = True
        assert eth_ctx.warmed_up is False

        btc_ctx.indicators["supertrend"] = "btc_indicator"
        assert "supertrend" not in eth_ctx.indicators

    def test_execution_components_default_none(self):
        """Test that execution components default to None."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        assert ctx.execution_manager is None
        assert ctx.sl_submitter is None
        assert ctx.tp_submitter is None

    def test_reset_calls_tick_monitor_reset(self):
        """Test that reset calls tick_monitor.reset() when tick_monitor is present."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        mock_tick_monitor = MagicMock()
        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
            tick_monitor=mock_tick_monitor,
        )

        ctx.reset()

        # tick_monitor.reset() should be called
        mock_tick_monitor.reset.assert_called_once()
        # tick_monitor reference should be preserved
        assert ctx.tick_monitor is mock_tick_monitor

    def test_execution_components_can_be_set(self):
        """Test that execution components can be assigned."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        # Assign mock execution components
        mock_execution_manager = MagicMock()
        mock_sl_submitter = MagicMock()
        mock_tp_submitter = MagicMock()

        ctx.execution_manager = mock_execution_manager
        ctx.sl_submitter = mock_sl_submitter
        ctx.tp_submitter = mock_tp_submitter

        assert ctx.execution_manager is mock_execution_manager
        assert ctx.sl_submitter is mock_sl_submitter
        assert ctx.tp_submitter is mock_tp_submitter

    def test_filter_manager_default_none(self):
        """Test that filter_manager defaults to None."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        assert ctx.filter_manager is None

    def test_filter_manager_can_be_set(self):
        """Test that filter_manager can be assigned."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        bar_type = BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=instrument_id,
            bar_type=bar_type,
        )

        # Assign mock filter manager
        mock_filter_manager = MagicMock()
        ctx.filter_manager = mock_filter_manager

        assert ctx.filter_manager is mock_filter_manager

    def test_sl_tp_submitted_for_reversal_defaults_false(self):
        """SL/TP reversal guard starts False."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        )
        assert ctx.sl_tp_submitted_for_reversal is False

    def test_sl_tp_submitted_for_reversal_reset(self):
        """reset() clears the SL/TP reversal guard flag."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        )
        ctx.sl_tp_submitted_for_reversal = True
        ctx.reset()
        assert ctx.sl_tp_submitted_for_reversal is False

    def test_active_signal_id_defaults_none(self):
        """active_signal_id — the entry signal id of the open position — defaults to None."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        )
        assert ctx.active_signal_id is None

    def test_active_signal_id_reset(self):
        """reset() clears active_signal_id, so nothing survives a close or a reversal."""
        from custos_toolkit_nautilus.adapter.pair_context import PairContext
        from nautilus_trader.model.data import BarType
        from nautilus_trader.model.identifiers import InstrumentId

        ctx = PairContext(
            pair="BTC-USDT",
            instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
        )
        ctx.active_signal_id = "sig-abc-123"
        ctx.reset()
        assert ctx.active_signal_id is None
