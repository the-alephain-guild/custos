"""Close-guard on the decoupled reversal-EXIT path.

When a trend reversal is decoupled into an EXIT (bypassing the entry gate) followed by
a gated entry next bar, the strategy emits an exit every bar while the position is open
and opposite the trend. The exit is a market IOC order, but its fill event lags back to
the local cache -- so until the cache shows the position closed, the next bar would
re-submit the same close. ``execute_exit_for_pair`` must consult the same OrderTracker
close gate the tick path uses (can_submit_close / mark_closing) so only one close is in
flight per position; reduce_only on the order itself is the second line of defense.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator
from custos_toolkit_nautilus.adapter.orders import OrderTracker

_ONE_SEC_NS = 1_000_000_000


def _make_strategy(now_ns: int) -> SimpleNamespace:
    submitted: list = []
    position = SimpleNamespace(is_closed=False, quantity=Decimal("0.01"))
    strat = SimpleNamespace(
        now_ns=now_ns,
        submitted=submitted,
        _position=position,
        _order_signal_map={},
    )
    strat.cache = SimpleNamespace(
        positions_open=lambda instrument_id=None: [] if position.is_closed else [position]
    )
    strat.clock = SimpleNamespace(timestamp_ns=lambda: strat.now_ns)
    strat.submit_order = lambda order: submitted.append(order)
    strat.log = SimpleNamespace(info=lambda *a, **k: None)
    return strat


def _make_ctx() -> SimpleNamespace:
    order = SimpleNamespace(client_order_id="O-1")
    return SimpleNamespace(
        instrument_id="BTCUSDT.BINANCE",
        pair="BTC/USDT",
        order_tracker=OrderTracker(),
        execution_manager=SimpleNamespace(
            create_exit_order=lambda instrument_id, signal, size: order
        ),
    )


def _exit_signal() -> SimpleNamespace:
    return SimpleNamespace(direction=SimpleNamespace(name="EXIT_LONG"), metadata={})


def test_exit_not_resubmitted_within_inflight_window():
    """Position still shows open next bar within the in-flight window (fill event lag)
    -> the exit is not resubmitted."""
    strat = _make_strategy(now_ns=1_000)
    ctx = _make_ctx()
    coord = SignalExecutionCoordinator(strat)

    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert len(strat.submitted) == 1  # first exit submitted + gate armed

    strat.now_ns = 1_000 + _ONE_SEC_NS  # +1s, well within the 5s in-flight window
    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert len(strat.submitted) == 1  # gate blocks the re-submit


def test_exit_resubmits_after_window_expires():
    """After the in-flight window times out (lost-fill safety net), a re-submit is allowed."""
    strat = _make_strategy(now_ns=1_000)
    ctx = _make_ctx()
    coord = SignalExecutionCoordinator(strat)

    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert len(strat.submitted) == 1

    strat.now_ns = 1_000 + 6 * _ONE_SEC_NS  # +6s, past the 5s window
    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert len(strat.submitted) == 2


def test_exit_single_submission_once_cache_shows_closed():
    """Once the fill event reaches the cache (position closed) -> early return, single submit."""
    strat = _make_strategy(now_ns=1_000)
    ctx = _make_ctx()
    coord = SignalExecutionCoordinator(strat)

    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert len(strat.submitted) == 1

    strat._position.is_closed = True  # market exit filled between bars
    strat.now_ns = 1_000 + _ONE_SEC_NS
    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert len(strat.submitted) == 1  # closed position -> no second submit
