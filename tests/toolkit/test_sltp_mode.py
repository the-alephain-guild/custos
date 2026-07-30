"""Tests for SLTPMode enum: capability matrix + on_entry_filled dispatch.

Absolute-value assertions per (mode, capability) cell — not range/relative checks
on the cells themselves. The on_entry_filled assertions inspect the real methods the
dispatch invokes, rather than a counter this test would have to maintain.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from custos_toolkit.signals.types import SignalDirection

pytest.importorskip("custos_toolkit_nautilus")

from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402


def test_construct_from_config_string_roundtrips():
    for value in ("exchange", "tick", "hybrid", "native_trailing"):
        assert SLTPMode(value).value == value


def test_taxonomy_matches_config_constant():
    from custos_toolkit_nautilus.adapter.config.risk import SL_TP_MODES

    assert tuple(m.value for m in SLTPMode) == SL_TP_MODES


# --- capability matrix (absolute per cell) ---


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SLTPMode.EXCHANGE, False),
        (SLTPMode.TICK, False),
        (SLTPMode.HYBRID, False),
        (SLTPMode.NATIVE_TRAILING, True),
    ],
)
def test_uses_native_trailing(mode, expected):
    assert mode.uses_native_trailing is expected


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SLTPMode.EXCHANGE, False),
        (SLTPMode.TICK, True),
        (SLTPMode.HYBRID, True),
        (SLTPMode.NATIVE_TRAILING, False),
    ],
)
def test_uses_tick_monitor(mode, expected):
    assert mode.uses_tick_monitor is expected


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SLTPMode.EXCHANGE, True),
        (SLTPMode.TICK, True),
        (SLTPMode.HYBRID, True),
        (SLTPMode.NATIVE_TRAILING, False),
    ],
)
def test_subscribes_tick_stream(mode, expected):
    assert mode.subscribes_tick_stream is expected


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SLTPMode.EXCHANGE, True),
        (SLTPMode.TICK, True),
        (SLTPMode.HYBRID, True),
        (SLTPMode.NATIVE_TRAILING, False),
    ],
)
def test_allows_break_even(mode, expected):
    assert mode.allows_break_even is expected


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SLTPMode.EXCHANGE, True),
        (SLTPMode.TICK, False),
        (SLTPMode.HYBRID, True),
        (SLTPMode.NATIVE_TRAILING, False),
    ],
)
def test_uses_exchange_sl(mode, expected):
    assert mode.uses_exchange_sl is expected


# --- on_entry_filled dispatch (asserts real invoked methods, not a counter) ---


def _stub_strategy():
    return SimpleNamespace(
        _sltp_coordinator=SimpleNamespace(
            submit_stop_loss=MagicMock(),
            submit_take_profit=MagicMock(),
            submit_safety_stop_loss=MagicMock(),
            submit_native_trailing=MagicMock(),
        )
    )


def _stub_ctx(with_tick_monitor=True):
    tick_monitor = MagicMock() if with_tick_monitor else None
    return SimpleNamespace(tick_monitor=tick_monitor)


def _long_signal():
    return SimpleNamespace(direction=SignalDirection.ENTER_LONG)


def test_on_entry_filled_exchange_submits_sl_and_tp():
    s, ctx = _stub_strategy(), _stub_ctx()
    signal = _long_signal()
    SLTPMode.EXCHANGE.on_entry_filled(s, ctx, signal, object(), Decimal("100"), None)
    s._sltp_coordinator.submit_stop_loss.assert_called_once_with(ctx, signal)
    s._sltp_coordinator.submit_take_profit.assert_called_once_with(ctx, signal)
    s._sltp_coordinator.submit_safety_stop_loss.assert_not_called()
    s._sltp_coordinator.submit_native_trailing.assert_not_called()
    ctx.tick_monitor.init_position.assert_not_called()


def test_on_entry_filled_tick_inits_tick_monitor_only():
    s, ctx = _stub_strategy(), _stub_ctx()
    SLTPMode.TICK.on_entry_filled(s, ctx, _long_signal(), object(), Decimal("100"), Decimal("2"))
    s._sltp_coordinator.submit_stop_loss.assert_not_called()
    s._sltp_coordinator.submit_take_profit.assert_not_called()
    s._sltp_coordinator.submit_safety_stop_loss.assert_not_called()
    s._sltp_coordinator.submit_native_trailing.assert_not_called()
    ctx.tick_monitor.init_position.assert_called_once_with(
        entry_price=Decimal("100"), is_long=True, entry_atr=Decimal("2")
    )


def test_on_entry_filled_hybrid_safety_sl_plus_tick():
    s, ctx = _stub_strategy(), _stub_ctx()
    signal = _long_signal()
    SLTPMode.HYBRID.on_entry_filled(s, ctx, signal, object(), Decimal("100"), Decimal("2"))
    s._sltp_coordinator.submit_safety_stop_loss.assert_called_once_with(ctx, signal)
    s._sltp_coordinator.submit_stop_loss.assert_not_called()
    s._sltp_coordinator.submit_native_trailing.assert_not_called()
    ctx.tick_monitor.init_position.assert_called_once_with(
        entry_price=Decimal("100"), is_long=True, entry_atr=Decimal("2")
    )


def test_on_entry_filled_native_trailing_submits_trailing_only():
    s, ctx = _stub_strategy(), _stub_ctx()
    SLTPMode.NATIVE_TRAILING.on_entry_filled(s, ctx, _long_signal(), object(), Decimal("100"), None)
    s._sltp_coordinator.submit_native_trailing.assert_called_once()
    s._sltp_coordinator.submit_stop_loss.assert_not_called()
    s._sltp_coordinator.submit_take_profit.assert_not_called()
    s._sltp_coordinator.submit_safety_stop_loss.assert_not_called()
    ctx.tick_monitor.init_position.assert_not_called()


def test_on_entry_filled_tick_no_position_skips_init():
    s, ctx = _stub_strategy(), _stub_ctx()
    SLTPMode.TICK.on_entry_filled(s, ctx, _long_signal(), None, Decimal("100"), None)
    ctx.tick_monitor.init_position.assert_not_called()


def test_on_entry_filled_tick_no_monitor_skips_init():
    s, ctx = _stub_strategy(), _stub_ctx(with_tick_monitor=False)
    # must not raise even though tick_monitor is None
    SLTPMode.TICK.on_entry_filled(s, ctx, _long_signal(), object(), Decimal("100"), None)


def test_on_entry_filled_short_signal_is_long_false():
    s, ctx = _stub_strategy(), _stub_ctx()
    short = SimpleNamespace(direction=SignalDirection.ENTER_SHORT)
    SLTPMode.TICK.on_entry_filled(s, ctx, short, object(), Decimal("100"), None)
    ctx.tick_monitor.init_position.assert_called_once_with(
        entry_price=Decimal("100"), is_long=False, entry_atr=None
    )
