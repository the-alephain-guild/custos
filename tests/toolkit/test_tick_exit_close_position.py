"""The tick full-position exit keeps its three defences, checked by what it does.

These tests used to read the method's source text: that it contained
``s.close_position(``, ``TimeInForce.IOC``, ``reduce_only=True``, ``resting_reduce_only``
and ``is_reduce_only``, and did not contain ``order_factory.market``. A cash account's
close cannot take that shape -- it carries no reduce-only flag and is submitted as a
market order, because the runner's safety gate refuses a plain ``close_position`` -- so
the text assertions were replaced by the behaviour they stood for, on a margin account
where that behaviour is unchanged:

  1. ``can_submit_close`` in-flight/cooldown gate (protection 1)
  2. protection still resting is cancelled before the close (protection 2)
  3. ``mark_closing`` in-flight gate after submit (protection 3)

and the close itself is the native ``close_position`` with IOC and reduce-only.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    OrderSide,
    OrderType,
    Price,
    TimeInForce,
)


def _margin_close() -> Harness:
    monitor = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.02"))
    monitor.init_position(Decimal("100"), True)
    h = Harness(mode=SLTPMode.TICK, monitor=monitor)
    h.cache.account_for_venue = lambda _venue: NS(account_type=AccountType.MARGIN)
    return h


def test_a_margin_full_close_is_native_ioc_and_reduce_only() -> None:
    h = _margin_close()

    h.tick("103")

    assert h.sent == [], "no hand-built order on a margin account"
    assert len(h.closed) == 1
    (position,), kwargs = h.closed[0]
    assert position is h.position
    assert kwargs == {"time_in_force": TimeInForce.IOC, "reduce_only": True}


def test_the_close_arms_the_in_flight_gate() -> None:
    h = _margin_close()

    h.tick("103")

    assert h.ctx.order_tracker.can_submit_close(h.now_ns) is False


def test_a_close_in_flight_is_not_sent_again() -> None:
    h = _margin_close()

    h.tick("103")
    h.tick("103")

    assert len(h.closed) == 1


def test_resting_protection_is_cancelled_before_the_close() -> None:
    h = _margin_close()
    stop = h.order(
        OrderType.STOP_MARKET,
        quantity=h.instrument.make_qty(Decimal("1")),
        order_side=OrderSide.SELL,
        reduce_only=True,
        trigger_price=Price.from_str("90.00"),
    )
    h.ctx.order_tracker.add_sl_order(stop.client_order_id, Decimal("1"))

    h.tick("103")

    assert h.cancelled == [stop.client_order_id]
    assert h.closed == [], "the close waits for the next tick"
