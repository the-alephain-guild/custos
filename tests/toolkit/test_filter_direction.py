# tests/test_filter_direction.py
"""Golden-baseline behaviour tests for the filter direction redesign.

Two defects are fixed and locked here:
  1. Directional bias -- the momentum filter must read short thresholds for a short
     entry candidate instead of always gating against long thresholds.
  2. Exit suppression -- a failing entry filter must not swallow an exit/reversal
     signal or skip position management.

Momentum routing is asserted on absolute thresholds rather than relative
behavior. The pipeline test drives the real ``_process_bar`` via an unbound call
with a stub ``self``, because the Cython Strategy base cannot be constructed,
asserting the real coordinator methods invoked, not a test-maintained counter
so the assertions land on the methods the dispatch really invokes.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit.signals.types import SignalDirection
from custos_toolkit_nautilus.adapter.config.filters import (
    MomentumFilterConfig,
    RocConfig,
    RsiConfig,
)


def _momentum(indicator: str, **rsi_or_roc):
    from custos_toolkit_nautilus.adapter.filters.momentum import NautilusMomentumFilter

    if indicator == "rsi":
        cfg = MomentumFilterConfig(enabled=True, indicator="rsi", rsi=RsiConfig(**rsi_or_roc))
    else:
        cfg = MomentumFilterConfig(enabled=True, indicator="roc", roc=RocConfig(**rsi_or_roc))
    return NautilusMomentumFilter(cfg)


# --- 1. momentum direction routing (absolute thresholds) ---


def test_rsi_short_candidate_passes_short_band_blocked_as_long():
    """RSI 35 with defaults (long [40,70], short [30,60]): a short entry is allowed
    (in the short band) but a long entry is blocked (below long_min). Before the
    redesign both directions hit the long band and the short entry was wrongly
    rejected."""
    f = _momentum("rsi")  # defaults: long_min=40 long_max=70 short_min=30 short_max=60
    f._ready = True
    f._rsi = SimpleNamespace(value=0.35, initialized=True)  # nautilus RSI is 0..1 -> 35

    assert f.check(MagicMock(), SignalDirection.ENTER_SHORT).passed is True
    assert f.check(MagicMock(), SignalDirection.ENTER_LONG).passed is False
    # None (legacy / direct call) falls back to long band -> blocked
    assert f.check(MagicMock(), None).passed is False


def test_rsi_long_candidate_in_long_band_passes():
    f = _momentum("rsi")
    f._ready = True
    f._rsi = SimpleNamespace(value=0.55, initialized=True)  # 55, inside both bands
    assert f.check(MagicMock(), SignalDirection.ENTER_LONG).passed is True
    assert f.check(MagicMock(), SignalDirection.ENTER_SHORT).passed is True


def test_roc_direction_routing():
    """ROC long requires positive momentum (>= long_threshold); short requires
    negative momentum (<= short_threshold). Defaults are both 0.0."""
    # negative momentum -0.5%: long blocked, short allowed
    f = _momentum("roc")
    f._ready = True
    f._roc = SimpleNamespace(value=-0.005, initialized=True)
    assert f.check(MagicMock(), SignalDirection.ENTER_LONG).passed is False
    assert f.check(MagicMock(), SignalDirection.ENTER_SHORT).passed is True

    # positive momentum +0.5%: long allowed, short blocked
    g = _momentum("roc")
    g._ready = True
    g._roc = SimpleNamespace(value=0.005, initialized=True)
    assert g.check(MagicMock(), SignalDirection.ENTER_LONG).passed is True
    assert g.check(MagicMock(), SignalDirection.ENTER_SHORT).passed is False


def test_momentum_is_direction_aware_flag():
    """FilterManager dispatches direction only to filters that opt in."""
    f = _momentum("rsi")
    assert getattr(f, "direction_aware", False) is True


# --- 2. exit bypass + unconditional position management (pipeline) ---


def _exit_signal():
    return SimpleNamespace(
        direction=SignalDirection.EXIT_LONG,
        is_actionable=lambda: True,
        is_exit=lambda: True,
        metadata={},
        strength=1.0,
        price=Decimal("100"),
        pair="BTC-USDT",
    )


def _entry_signal():
    return SimpleNamespace(
        direction=SignalDirection.ENTER_LONG,
        is_actionable=lambda: True,
        is_exit=lambda: False,
        metadata={},
        strength=1.0,
        price=Decimal("100"),
        pair="BTC-USDT",
    )


def _pipeline_stub(signal):
    """Stub ``self`` for NautilusTradingStrategy._process_bar with both filter gates
    failing and risk paused -- only exits/position-management should still fire."""
    ctx = SimpleNamespace(pair="BTC-USDT")
    exec_coord = SimpleNamespace(
        execute_exit_for_pair=MagicMock(),
        execute_entry_for_pair=MagicMock(),
        manage_positions_for_pair=MagicMock(),
    )
    filter_coord = SimpleNamespace(
        handle_mtf_bar=MagicMock(return_value=False),
        update_global=MagicMock(),
        update_pair=MagicMock(),
        check_global=MagicMock(return_value=False),  # entry filters fail
        check_pair=MagicMock(return_value=False),
    )
    risk_coord = SimpleNamespace(check_risk_limits=MagicMock(return_value=False))  # risk paused
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    stub = SimpleNamespace(
        _get_context_from_instrument=MagicMock(return_value=ctx),
        _warmup_coordinator=SimpleNamespace(handle_warmup_gate=MagicMock(return_value=False)),
        on_pre_bar=MagicMock(),
        on_post_bar=MagicMock(),
        _filter_coordinator=filter_coord,
        _signal_execution_coordinator=exec_coord,
        _risk_control_coordinator=risk_coord,
        _event_publisher=SimpleNamespace(enabled=False),
        _is_direction_allowed=MagicMock(return_value=True),
        calculate_signal=MagicMock(return_value=signal),
        calculate_position_size=MagicMock(return_value=Decimal("1")),
        log=MagicMock(),
    )
    # Run the real entry-gate logic (risk + filters), not a mock, so the test proves
    # the actual gate blocks the entry.
    stub._entry_gates_pass = lambda c, b, d: NautilusTradingStrategy._entry_gates_pass(
        stub, c, b, d
    )
    return stub


def _bar():
    instrument_id = object()
    return SimpleNamespace(
        bar_type=SimpleNamespace(instrument_id=instrument_id),
        close=Decimal("100"),
        ts_event=1_000,
    )


def test_exit_signal_bypasses_failing_entry_filters():
    """An exit signal executes even though both filter gates fail and risk is paused;
    position management still runs."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    stub = _pipeline_stub(_exit_signal())
    NautilusTradingStrategy._process_bar(stub, _bar())

    stub._signal_execution_coordinator.execute_exit_for_pair.assert_called_once()
    stub._signal_execution_coordinator.execute_entry_for_pair.assert_not_called()
    stub._signal_execution_coordinator.manage_positions_for_pair.assert_called_once()


def test_failing_entry_filters_block_entry_but_keep_position_management():
    """An entry signal is NOT executed when entry filters fail, but SL/TP/trailing
    management still runs (filters gate entries only)."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    stub = _pipeline_stub(_entry_signal())
    NautilusTradingStrategy._process_bar(stub, _bar())

    stub._signal_execution_coordinator.execute_entry_for_pair.assert_not_called()
    stub._signal_execution_coordinator.manage_positions_for_pair.assert_called_once()


# --- unreliable risk equity blocks new entries, failing closed ---


def _gate_stub(*, risk_ok, reliable, filters_ok=True):
    return SimpleNamespace(
        _risk_control_coordinator=SimpleNamespace(
            check_risk_limits=MagicMock(return_value=risk_ok)
        ),
        _equity_provider=SimpleNamespace(is_risk_equity_reliable=MagicMock(return_value=reliable)),
        _filter_coordinator=SimpleNamespace(
            check_global=MagicMock(return_value=filters_ok),
            check_pair=MagicMock(return_value=filters_ok),
        ),
        log=MagicMock(),
    )


def test_entry_gate_blocks_when_risk_equity_unreliable():
    """All other gates pass, but an unreliable risk equity must block the new entry."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    stub = _gate_stub(risk_ok=True, reliable=False)
    ctx = SimpleNamespace(pair="BTC-USDT")
    assert (
        NautilusTradingStrategy._entry_gates_pass(stub, ctx, _bar(), SignalDirection.ENTER_LONG)
        is False
    )


def test_entry_gate_passes_when_risk_equity_reliable():
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    stub = _gate_stub(risk_ok=True, reliable=True)
    ctx = SimpleNamespace(pair="BTC-USDT")
    assert (
        NautilusTradingStrategy._entry_gates_pass(stub, ctx, _bar(), SignalDirection.ENTER_LONG)
        is True
    )
