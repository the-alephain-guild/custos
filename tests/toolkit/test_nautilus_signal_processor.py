"""Tests for shared.nautilus.signal_processor module.

Since SignalProcessorStrategy inherits from NautilusTradingStrategy (a Cython-based
class requiring Nautilus runtime), we use MagicMock(spec=...) to create test instances
and test the signal processing logic by binding the real methods.
"""

import pytest

pytest.importorskip("nautilus_trader")

from collections import deque
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from custos_toolkit.signals.types import Signal, SignalDirection

# =============================================================================
# Mock Setup
# =============================================================================


class MockSignalResolver:
    """Mock signal resolver for testing."""

    def resolve(self, signal):
        return signal

    def from_okx_format(self, data):
        action = data.get("action", "").upper()
        direction_map = {
            "ENTER_LONG": SignalDirection.ENTER_LONG,
            "ENTER_SHORT": SignalDirection.ENTER_SHORT,
            "EXIT_LONG": SignalDirection.EXIT_LONG,
            "EXIT_SHORT": SignalDirection.EXIT_SHORT,
        }
        direction = direction_map.get(action, SignalDirection.NEUTRAL)
        pair = data.get("instrument", "").replace("-SWAP", "")
        return Signal(direction=direction, price=Decimal("100"), pair=pair)

    def to_okx_format(self, signal):
        return {
            "action": signal.direction.name,
            "instrument": f"{signal.pair}-SWAP" if signal.pair else "UNKNOWN-SWAP",
        }


def _create_signal_processor(pairs=None):
    """Create a mock SignalProcessorStrategy with real method bindings.

    Uses MagicMock as the base and binds the actual SignalProcessorStrategy
    methods to it, avoiding Cython/Nautilus runtime dependencies.
    """
    if pairs is None:
        pairs = ["BTC-USDT"]

    from custos_toolkit_nautilus.adapter.signal_processor import SignalProcessorStrategy

    instance = MagicMock()

    # Set up the attributes that would be set by __init__
    instance._signal_resolver = MockSignalResolver()
    instance._pending_signals = {pair: deque() for pair in pairs}
    instance._last_emitted_signal = {}
    # The pairs come from config.trading.pairs, the single source; nothing injects _pairs.
    instance.config.trading.pairs = pairs
    instance.log = MagicMock()

    # Bind real methods from SignalProcessorStrategy
    import types

    for method_name in [
        "on_strategy_start",
        "on_strategy_stop",
        "on_reset",
        "_configured_pairs",
        "calculate_signal",
        "receive_external_signal",
        "receive_okx_signal",
        "emit_signal",
        "get_last_emitted_signal",
        "get_pending_signal_count",
        "get_indicator_history",
    ]:
        method = getattr(SignalProcessorStrategy, method_name)
        bound = types.MethodType(method, instance)
        setattr(instance, method_name, bound)

    return instance


# =============================================================================
# Tests
# =============================================================================


class TestSignalProcessorOnStrategyStart:
    """Tests for on_strategy_start initialization."""

    def test_on_strategy_start_initializes_queues(self):
        """Each pair should get an empty deque on start."""
        strategy = _create_signal_processor(pairs=["BTC-USDT", "ETH-USDT"])
        # Clear queues to simulate pre-start state
        strategy._pending_signals = {}
        strategy.on_strategy_start()
        assert "BTC-USDT" in strategy._pending_signals
        assert "ETH-USDT" in strategy._pending_signals
        assert len(strategy._pending_signals["BTC-USDT"]) == 0
        assert len(strategy._pending_signals["ETH-USDT"]) == 0

    def test_no_self_pairs_reference_in_source(self):
        """Guards a regression: the base class dropped its _pairs cache, so this class must
        not reference self._pairs — doing so raises AttributeError in on_strategy_start, and
        an old fixture injecting _pairs by hand hid exactly that."""
        import inspect

        from custos_toolkit_nautilus.adapter.signal_processor import SignalProcessorStrategy

        src = inspect.getsource(SignalProcessorStrategy)
        assert "self._pairs" not in src, "must not reference the removed self._pairs"
        assert "self._configured_pairs()" in src, (
            "pairs come from config-backed _configured_pairs()"
        )


class TestReceiveExternalSignal:
    """Tests for receive_external_signal method."""

    def test_receive_external_signal_queues_signal(self):
        """Signal should be queued for the correct pair."""
        strategy = _create_signal_processor()
        signal = Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")

        strategy.receive_external_signal(signal)

        assert len(strategy._pending_signals["BTC-USDT"]) == 1
        queued = strategy._pending_signals["BTC-USDT"][0]
        assert queued.direction == SignalDirection.ENTER_LONG

    def test_receive_external_signal_default_pair(self):
        """When signal has no pair, should use first configured pair."""
        strategy = _create_signal_processor(pairs=["ETH-USDT", "BTC-USDT"])
        signal = Signal(direction=SignalDirection.ENTER_LONG, price=Decimal("3000"), pair="")

        strategy.receive_external_signal(signal)

        assert len(strategy._pending_signals["ETH-USDT"]) == 1

    def test_receive_external_signal_unconfigured_pair_warns(self):
        """Signal for unconfigured pair should log warning and not queue."""
        strategy = _create_signal_processor(pairs=["BTC-USDT"])
        signal = Signal.enter_long(price=Decimal("100"), pair="SOL-USDT")

        strategy.receive_external_signal(signal)

        strategy.log.warning.assert_called_once()
        assert "SOL-USDT" not in strategy._pending_signals


class TestCalculateSignal:
    """Tests for calculate_signal method."""

    def test_calculate_signal_pops_queued_signal(self):
        """Should dequeue and return the resolved signal."""
        strategy = _create_signal_processor()
        signal = Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")
        strategy._pending_signals["BTC-USDT"].append(signal)

        bar = MagicMock()
        bar.close = Decimal("50000")

        result = strategy.calculate_signal(SimpleNamespace(pair="BTC-USDT"), bar)

        assert result.direction == SignalDirection.ENTER_LONG
        assert len(strategy._pending_signals["BTC-USDT"]) == 0

    def test_calculate_signal_neutral_when_empty(self):
        """Should return neutral signal when queue is empty."""
        strategy = _create_signal_processor()
        bar = MagicMock()
        bar.close = Decimal("50000")

        result = strategy.calculate_signal(SimpleNamespace(pair="BTC-USDT"), bar)

        assert result.direction == SignalDirection.NEUTRAL


class TestReceiveOkxSignal:
    """Tests for receive_okx_signal method."""

    def test_receive_okx_signal_parses_and_queues(self):
        """OKX format signal should be parsed and queued."""
        strategy = _create_signal_processor()
        data = {
            "action": "ENTER_LONG",
            "instrument": "BTC-USDT-SWAP",
        }

        strategy.receive_okx_signal(data)

        assert len(strategy._pending_signals["BTC-USDT"]) == 1
        queued = strategy._pending_signals["BTC-USDT"][0]
        assert queued.direction == SignalDirection.ENTER_LONG

    def test_receive_okx_signal_handles_parse_error(self):
        """Parse error should log error and not queue anything."""
        strategy = _create_signal_processor()
        # Override resolver to raise on from_okx_format
        strategy._signal_resolver = MagicMock()
        strategy._signal_resolver.from_okx_format.side_effect = ValueError("bad format")

        strategy.receive_okx_signal({"action": "INVALID"})

        strategy.log.error.assert_called_once()
        assert len(strategy._pending_signals["BTC-USDT"]) == 0


class TestEmitSignal:
    """Tests for emit_signal method."""

    def test_emit_signal_converts_to_okx_format(self):
        """Internal signal should be converted to OKX format."""
        strategy = _create_signal_processor()
        signal = Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")

        result = strategy.emit_signal(signal)

        assert result["action"] == "ENTER_LONG"
        assert "BTC-USDT" in result["instrument"]

    def test_get_last_emitted_signal(self):
        """Should store and retrieve last emitted signal."""
        strategy = _create_signal_processor()
        signal = Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")

        strategy.emit_signal(signal)
        last = strategy.get_last_emitted_signal("BTC-USDT")

        assert last is not None
        assert last["action"] == "ENTER_LONG"


class TestGetPendingSignalCount:
    """Tests for get_pending_signal_count method."""

    def test_get_pending_signal_count(self):
        """Should return correct count of pending signals."""
        strategy = _create_signal_processor()
        assert strategy.get_pending_signal_count("BTC-USDT") == 0

        signal1 = Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")
        signal2 = Signal.enter_short(price=Decimal("50000"), pair="BTC-USDT")
        strategy._pending_signals["BTC-USDT"].append(signal1)
        strategy._pending_signals["BTC-USDT"].append(signal2)

        assert strategy.get_pending_signal_count("BTC-USDT") == 2

    def test_get_pending_signal_count_default_pair(self):
        """Should use first configured pair when pair is None."""
        strategy = _create_signal_processor()
        signal = Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")
        strategy._pending_signals["BTC-USDT"].append(signal)

        assert strategy.get_pending_signal_count() == 1


class TestOnReset:
    """Tests for on_reset method."""

    def test_on_reset_clears_queues(self):
        """Reset should clear all signal queues and emitted signals."""
        strategy = _create_signal_processor(pairs=["BTC-USDT", "ETH-USDT"])
        strategy._pending_signals["BTC-USDT"].append(
            Signal.enter_long(price=Decimal("50000"), pair="BTC-USDT")
        )
        strategy._pending_signals["ETH-USDT"].append(
            Signal.enter_short(price=Decimal("3000"), pair="ETH-USDT")
        )
        strategy._last_emitted_signal["BTC-USDT"] = {"action": "ENTER_LONG"}

        strategy.on_reset()

        assert len(strategy._pending_signals["BTC-USDT"]) == 0
        assert len(strategy._pending_signals["ETH-USDT"]) == 0
        assert len(strategy._last_emitted_signal) == 0
