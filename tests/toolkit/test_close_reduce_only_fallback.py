"""After the venue refuses a reduce-only close, the next attempt drops reduce_only.

Binance's demo matching engine refuses reduce-only closes in states where the position
is genuinely there (recorded in the ecosystem's lesson #14: a real short, zero open
orders, refused from both the strategy and the venue's own UI). Retrying reduce-only
against that is retrying the thing that cannot work.

The owner's instruction (2026-07-30) is therefore: try reduce_only once; if that is
refused, place one plain opposite-direction order of the same size -- not a reduce-only
retry loop.

The arming signal is the tracker's consecutive logical-reject count, which
``handle_order_rejected`` increments only on the logic tier (so a 5xx / rate-limit
rejection does not arm it) and ``reset_close_rejects`` clears on a confirmed close.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator
from custos_toolkit_nautilus.adapter.orders import OrderTracker

_ONE_SEC_NS = 1_000_000_000
_POSITION_QTY = Decimal("0.0070")


def _make_strategy(now_ns: int) -> SimpleNamespace:
    submitted: list = []
    position = SimpleNamespace(is_closed=False, quantity=_POSITION_QTY)
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
    strat.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return strat


def _make_ctx(calls: list) -> SimpleNamespace:
    """Context whose execution manager records every create_exit_order call."""

    def _create_exit_order(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(client_order_id=f"O-{len(calls)}")

    return SimpleNamespace(
        instrument_id="BTCUSDT.BINANCE",
        pair="BTC/USDT",
        order_tracker=OrderTracker(),
        execution_manager=SimpleNamespace(create_exit_order=_create_exit_order),
    )


def _exit_signal() -> SimpleNamespace:
    return SimpleNamespace(direction=SimpleNamespace(name="EXIT_SHORT"), metadata={})


def test_first_close_attempt_is_reduce_only():
    """Nothing has been refused yet, so the protective form is used."""
    strat = _make_strategy(now_ns=1_000)
    calls: list = []
    ctx = _make_ctx(calls)
    coord = SignalExecutionCoordinator(strat)

    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())

    assert len(calls) == 1
    assert calls[0]["reduce_only"] is True


def test_second_attempt_drops_reduce_only_after_a_logical_refusal():
    """A refused reduce-only close makes the next attempt a plain order of the same size.

    This is the whole point: the plain order is what actually closes the position when
    the venue will not honour reduce-only.
    """
    strat = _make_strategy(now_ns=1_000)
    calls: list = []
    ctx = _make_ctx(calls)
    coord = SignalExecutionCoordinator(strat)

    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())
    assert calls[0]["reduce_only"] is True

    # What handle_order_rejected does on the logic tier (-2022 and friends).
    ctx.order_tracker.record_close_reject()

    strat.now_ns = 1_000 + 6 * _ONE_SEC_NS  # past the in-flight window
    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())

    assert len(calls) == 2
    assert calls[1]["reduce_only"] is False
    assert calls[1]["size"] == _POSITION_QTY, "the plain order must match the position size"


def test_reduce_only_is_not_retried_after_the_fallback_is_armed():
    """Once refused, later attempts stay plain -- no going back to the form that failed."""
    strat = _make_strategy(now_ns=1_000)
    calls: list = []
    ctx = _make_ctx(calls)
    coord = SignalExecutionCoordinator(strat)

    ctx.order_tracker.record_close_reject()
    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())

    strat.now_ns = 1_000 + 6 * _ONE_SEC_NS
    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())

    assert [c["reduce_only"] for c in calls] == [False, False]


def test_a_closed_position_never_gets_a_plain_order():
    """The dangerous case: the fallback is armed but the position is already gone.

    A plain opposite-direction order would open a reverse position rather than close
    anything, so the path must not be reached once the cache shows the position closed.
    """
    strat = _make_strategy(now_ns=1_000)
    calls: list = []
    ctx = _make_ctx(calls)
    coord = SignalExecutionCoordinator(strat)

    ctx.order_tracker.record_close_reject()
    strat._position.is_closed = True

    coord.execute_exit_for_pair(ctx, _exit_signal(), bar=object())

    assert calls == [], "no close order may be created when there is no open position"
    assert strat.submitted == []
