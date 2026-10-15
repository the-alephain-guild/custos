"""An order denied before it leaves the process ends the same way as one the venue refused.

``OrderDenied`` and ``OrderRejected`` both mean "this order will never fill", but they
arrive through different callbacks, and the toolkit only ever answered the second. On
the SoDEX spot testnet every reduce-only close was denied locally by the adapter, and
because nothing handled the denial:

* a denied close was never counted, never paused anything, and went out again as soon
  as the five-second in-flight window lapsed;
* a denied entry kept the capital it had reserved;
* a denied protective stop left new entries flowing as if the position were covered;
* a denied take-profit lot stayed marked as dispatched, so its level was never retried.

Each kind of order is settled the way its refused counterpart already is, with two
deliberate differences for closes. A local denial says nothing about the venue's view of
the position -- the order never got there -- so it is not evidence that reduce-only
itself was refused, and it must not arm the one plain attempt. And it is not caused by
resting orders crowding the position, so it must not cancel every open order, which
would take live protection down with it.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness, scaled_monitor  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    OrderReconciler,
    SignalExecutionCoordinator,
)
from custos_toolkit_nautilus.adapter.coordinators.order_reconciler import (  # noqa: E402
    _CLOSE_REJECT_COOLDOWN_NS,
    _CLOSE_REJECT_HALT_THRESHOLD,
)
from custos_toolkit_nautilus.adapter.execution import ExecutionManager  # noqa: E402
from custos_toolkit_nautilus.adapter.orders import _CLOSE_INFLIGHT_TIMEOUT_NS  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy  # noqa: E402
from nautilus_trader.model import AccountType, Money, OrderSide, OrderType, Price  # noqa: E402

# The adapter's own words for its refusal, as the probe run logged them.
_SODEX_REDUCE_ONLY = "reduce-only on spot is not supported by SoDEX"


def _deny(h: Harness, order, reason: str = _SODEX_REDUCE_ONLY) -> None:
    """Deliver an OrderDenied to the strategy the way the engine does: by callback name.

    The engine calls ``on_order_denied`` on the strategy. When the toolkit's strategy does
    not define one, the engine's default runs, and the default does nothing -- which is
    exactly the behaviour these tests exist to rule out.
    """
    if not hasattr(h, "_reconciler"):
        h._reconciler = OrderReconciler(h)
    event = NS(instrument_id=h.instrument.id, client_order_id=order.client_order_id, reason=reason)
    shell = NautilusTradingStrategy.__dict__.get("on_order_denied")
    if shell is not None:
        shell(h, event)


def _closing(account_type: AccountType) -> Harness:
    """A harness with an open long and the real bar-exit path wired to it."""
    h = Harness(mode=SLTPMode.TICK)
    # More base than the position, so a cash exit is never capped by the balance.
    held = lambda currency: Money(Decimal("1"), currency)  # noqa: E731
    h.cache.account_for_venue = lambda _venue: NS(
        account_type=account_type, balance_free=held, balance_total=held
    )
    h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
    h.ctx.execution_manager.create_entry_order = h.entry_order
    return h


def _send_exit(h: Harness):
    sent_before = len(h.sent)
    SignalExecutionCoordinator(h).execute_exit_for_pair(
        h.ctx, Signal.exit_long(Decimal("100"), pair=h.ctx.pair), bar=NS(close=Decimal("100"))
    )
    assert len(h.sent) == sent_before + 1, "precondition: the exit path sent its close"
    return h.sent[-1]


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def _entered() -> tuple[Harness, object]:
    from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
    from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

    h = Harness()
    h.flat()
    h._capital_allocator = CapitalAllocator(
        AllocationConfig(tiers={"BTC-USDT": 1.0}), Decimal("1000"), h.cache
    )
    h._capital_allocator.register_pair(h.ctx.pair, h.instrument.id)
    SignalExecutionCoordinator(h).execute_entry_for_pair(
        h.ctx,
        Signal.enter_long(price=100.0),
        size=Decimal("400"),
        bar=NS(close=Decimal("100")),
    )
    entry = h.sent[0]
    entry.is_open = False
    entry.is_closed = True
    assert h._capital_allocator.available_cash < Decimal("1000"), "precondition: reserved"
    return h, entry


def test_a_denied_entry_returns_its_reservation() -> None:
    h, entry = _entered()

    _deny(h, entry)

    assert h._capital_allocator.available_cash == Decimal("1000")
    assert h.ctx.allocated_capital == Decimal("0")
    assert h.ctx.order_tracker.entry_order_id is None
    assert h.ctx.position_tracker.pending_signal is None
    assert h.ctx.position_tracker.entry_count == 0, "an entry that bought nothing is no entry"


# ---------------------------------------------------------------------------
# Closes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("account_type", [AccountType.MARGIN, AccountType.CASH])
def test_a_denied_close_is_counted_but_does_not_arm_the_hatch(account_type) -> None:
    """Whatever form the close took, a denial of it is a failed close."""
    h = _closing(account_type)
    close = _send_exit(h)
    assert h.ctx.order_tracker.can_submit_close(h.now_ns) is False, "precondition: in flight"

    _deny(h, close)

    tracker = h.ctx.order_tracker
    assert tracker.close_reject_count == 1
    assert tracker.can_submit_close(h.now_ns) is False, "a short cooldown follows the denial"
    assert _CLOSE_REJECT_COOLDOWN_NS < _CLOSE_INFLIGHT_TIMEOUT_NS
    assert tracker.can_submit_close(h.now_ns + _CLOSE_REJECT_COOLDOWN_NS) is True, (
        "the in-flight window is released: the close is known to be dead"
    )
    assert tracker.reduce_only_refused is False, (
        "a local denial carries no evidence about the venue's position"
    )
    assert h.cancelled == [], "a denial is not caused by resting orders; cancel nothing"
    h.pause.assert_not_called()


def test_repeated_denied_closes_pause_the_strategy() -> None:
    h = _closing(AccountType.CASH)

    for attempt in range(1, _CLOSE_REJECT_HALT_THRESHOLD + 1):
        close = _send_exit(h)
        _deny(h, close)
        if attempt < _CLOSE_REJECT_HALT_THRESHOLD:
            h.pause.assert_not_called()
        h.now_ns += _CLOSE_REJECT_COOLDOWN_NS

    h.pause.assert_called()
    assert h.ctx.order_tracker.close_reject_count == _CLOSE_REJECT_HALT_THRESHOLD


@pytest.mark.parametrize("account_type", [AccountType.MARGIN, AccountType.CASH])
def test_a_rejected_close_is_read_as_a_close_whatever_its_form(account_type) -> None:
    """Guard for the cash close form: a venue rejection of it is still a failed close.

    The rejection path recognises a close by its reduce-only flag. A plain close on a
    cash account has none, and must not fall through to "unknown order" -- that would
    skip the backoff and the count, and let the close be re-sent every bar.
    """
    h = _closing(account_type)
    close = _send_exit(h)

    OrderReconciler(h).handle_order_rejected(
        NS(
            instrument_id=h.instrument.id,
            client_order_id=close.client_order_id,
            reason="insufficient balance",
        )
    )

    assert h.ctx.order_tracker.close_reject_count == 1


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------


def test_a_denied_stop_pauses_new_risk() -> None:
    h = Harness(mode=SLTPMode.HYBRID)
    stop = h.order(
        OrderType.STOP_MARKET,
        quantity=h.instrument.make_qty(Decimal("1")),
        order_side=OrderSide.SELL,
        reduce_only=True,
        trigger_price=Price.from_str("90.00"),
    )
    h.ctx.order_tracker.add_exchange_sl_order(stop.client_order_id, Decimal("1"))
    h.ctx.exchange_sl_rebuild_deadline_ns = h.now_ns + 60_000_000_000

    _deny(h, stop, "StopMarket orders is not supported by SoDEX")

    assert stop.client_order_id not in h.ctx.order_tracker.exchange_sl_order_ids
    h.pause.assert_called()
    assert h.ctx.exchange_sl_rebuild_deadline_ns == 0, "repair may run again at once"
    assert h.cancelled == [], "other protection stays in place"


def test_a_denied_take_profit_lot_is_retryable() -> None:
    monitor = scaled_monitor(1)
    monitor.init_position(Decimal("100"), True)
    h = Harness(monitor=monitor)
    h.tick("103")
    lot = h.sent[-1]

    _deny(h, lot)
    h.tick("103")

    assert len(h.sent) == 2, "nothing was taken, so the level is still owed"
    assert lot.client_order_id not in h.ctx.order_tracker.tp_order_ids
