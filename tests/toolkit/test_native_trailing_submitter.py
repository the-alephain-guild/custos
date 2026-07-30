# tests/test_native_trailing_submitter.py
"""NativeTrailingStopSubmitter.

Validates the exchange-managed TrailingStopMarketOrder submitter:
- offset is BASIS_POINTS with correct dimension (trailing_pct × 10000)
- callbackRate fail-fast: trailing_pct must be in [0.001, 0.10] (else None)
- activation_price = entry × (1 ± activation_pct), tick-aligned
- no trigger_price is passed; order is reduce_only
- trigger_type maps from trigger_price_type (mark/last/default)

The assertions are on absolute values, not merely on ranges or trends.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.orders import NativeTrailingStopSubmitter
from nautilus_trader.model.enums import OrderSide, TimeInForce, TrailingOffsetType, TriggerType
from nautilus_trader.model.identifiers import InstrumentId

# =============================================================================
# Mock Classes / Fixtures
# =============================================================================


class MockInstrument:
    """Mock Nautilus instrument with configurable price precision/increment."""

    def __init__(self, price_precision: int = 2, price_increment: str = "0.01"):
        self.price_precision = price_precision
        self.price_increment = Decimal(price_increment)


class MockPosition:
    """Mock Nautilus position for testing."""

    def __init__(self, is_long: bool = True, quantity: float = 1.0):
        self.is_long = is_long
        self.quantity = Decimal(str(quantity))


def _trailing_cfg(
    trailing_pct: float, activation_pct: float = 0.02, trigger_price_type: str = "mark"
):
    """Build a lightweight trailing config, independent of the risk config module."""
    return SimpleNamespace(
        enabled=True,
        activation_pct=activation_pct,
        trailing_pct=trailing_pct,
        trigger_price_type=trigger_price_type,
    )


@pytest.fixture
def mock_order_factory():
    factory = MagicMock()
    factory.trailing_stop_market.return_value = MagicMock(name="TrailingStopMarketOrder")
    return factory


@pytest.fixture
def mock_log():
    return MagicMock()


@pytest.fixture
def instrument_id():
    return InstrumentId.from_str("BTCUSDT.BINANCE")


def _make_cache(instrument=None):
    cache = MagicMock()
    cache.instrument.return_value = instrument if instrument is not None else MockInstrument()
    return cache


def _submitter(factory, cache, log):
    return NativeTrailingStopSubmitter(order_factory=factory, cache=cache, log=log)


def _call_kwargs(factory):
    factory.trailing_stop_market.assert_called_once()
    return factory.trailing_stop_market.call_args.kwargs


# =============================================================================
# offset dimension (BASIS_POINTS)
# =============================================================================


def test_trailing_offset_basis_points_dimension(mock_order_factory, mock_log, instrument_id):
    """trailing_pct=0.015 -> trailing_offset == 150 bps, offset_type == BASIS_POINTS."""
    submitter = _submitter(mock_order_factory, _make_cache(), mock_log)
    order = submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015),
    )

    assert order is not None
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs["trailing_offset"] == Decimal("150")
    assert kwargs["trailing_offset_type"] == TrailingOffsetType.BASIS_POINTS
    assert kwargs["order_side"] == OrderSide.SELL
    assert kwargs["quantity"] == Decimal("1.0")
    assert kwargs["time_in_force"] == TimeInForce.GTC


# =============================================================================
# activation_price derivation (long/short, tick-aligned)
# =============================================================================


def test_activation_price_long_aligned(mock_order_factory, mock_log, instrument_id):
    """Long: activation = entry×(1+activation_pct), tick-aligned (round down for SELL)."""
    # tick 0.1, entry 100, activation_pct 0.0234 -> raw 102.34 -> aligned 102.3
    cache = _make_cache(MockInstrument(price_precision=1, price_increment="0.1"))
    submitter = _submitter(mock_order_factory, cache, mock_log)
    submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015, activation_pct=0.0234),
    )
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs["order_side"] == OrderSide.SELL
    assert str(kwargs["activation_price"]) == "102.3"


def test_activation_price_short_aligned(mock_order_factory, mock_log, instrument_id):
    """Short: activation = entry×(1-activation_pct), tick-aligned (round up for BUY)."""
    # tick 0.1, entry 100, activation_pct 0.0234 -> raw 97.66 -> aligned 97.7
    cache = _make_cache(MockInstrument(price_precision=1, price_increment="0.1"))
    submitter = _submitter(mock_order_factory, cache, mock_log)
    submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_short(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=False, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015, activation_pct=0.0234),
    )
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs["order_side"] == OrderSide.BUY
    assert str(kwargs["activation_price"]) == "97.7"


# =============================================================================
# fail-fast on callbackRate out of range
# =============================================================================


@pytest.mark.parametrize("bad_pct", [0.0005, 0.0, 0.2, 0.5])
def test_fail_fast_out_of_range_returns_none(mock_order_factory, mock_log, instrument_id, bad_pct):
    """trailing_pct outside [0.001, 0.10] -> None (fail-fast, no order, no clamp)."""
    submitter = _submitter(mock_order_factory, _make_cache(), mock_log)
    order = submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=bad_pct),
    )
    assert order is None
    mock_order_factory.trailing_stop_market.assert_not_called()
    mock_log.error.assert_called_once()


@pytest.mark.parametrize("good_pct,expected_bps", [(0.001, "10"), (0.10, "1000")])
def test_boundary_values_accepted(
    mock_order_factory, mock_log, instrument_id, good_pct, expected_bps
):
    """Boundary trailing_pct (0.001 / 0.10) are accepted (closed interval)."""
    submitter = _submitter(mock_order_factory, _make_cache(), mock_log)
    order = submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=good_pct),
    )
    assert order is not None
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs["trailing_offset"] == Decimal(expected_bps)


# =============================================================================
# no trigger_price + reduce_only
# =============================================================================


def test_no_trigger_price_and_reduce_only(mock_order_factory, mock_log, instrument_id):
    """Order must NOT pass trigger_price (adapter rejects it) and must be reduce_only."""
    submitter = _submitter(mock_order_factory, _make_cache(), mock_log)
    submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015),
    )
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs.get("trigger_price") is None
    assert kwargs["reduce_only"] is True


# =============================================================================
# trigger_type mapping
# =============================================================================


@pytest.mark.parametrize(
    "trigger_price_type,expected",
    [
        ("mark", TriggerType.MARK_PRICE),
        ("last", TriggerType.LAST_PRICE),
        ("default", TriggerType.DEFAULT),
        ("MARK", TriggerType.MARK_PRICE),
    ],
)
def test_trigger_type_mapping(
    mock_order_factory, mock_log, instrument_id, trigger_price_type, expected
):
    """trigger_price_type maps to the right nautilus TriggerType."""
    submitter = _submitter(mock_order_factory, _make_cache(), mock_log)
    submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015, trigger_price_type=trigger_price_type),
    )
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs["trigger_type"] == expected


def test_invalid_trigger_type_falls_back_to_mark(mock_order_factory, mock_log, instrument_id):
    """Unknown trigger_price_type falls back to MARK_PRICE with a warning."""
    submitter = _submitter(mock_order_factory, _make_cache(), mock_log)
    submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015, trigger_price_type="index"),
    )
    kwargs = _call_kwargs(mock_order_factory)
    assert kwargs["trigger_type"] == TriggerType.MARK_PRICE
    mock_log.warning.assert_called_once()


# =============================================================================
# instrument not found
# =============================================================================


def test_instrument_not_found_returns_none(mock_order_factory, mock_log, instrument_id):
    """Missing instrument -> None (no order)."""
    submitter = _submitter(mock_order_factory, _make_cache(instrument=None), mock_log)
    # cache.instrument returns None
    submitter._cache.instrument.return_value = None
    order = submitter.create_order(
        instrument_id=instrument_id,
        signal=Signal.enter_long(price=100.0),
        entry_price=Decimal("100"),
        position=MockPosition(is_long=True, quantity=1.0),
        trailing_cfg=_trailing_cfg(trailing_pct=0.015),
    )
    assert order is None
    mock_order_factory.trailing_stop_market.assert_not_called()
