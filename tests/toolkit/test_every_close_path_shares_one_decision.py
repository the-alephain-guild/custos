"""Every path that closes a position asks the same question about reduce-only.

The regular exit path already falls back to a plain order when the venue refuses the
reduce-only form (proven on real hardware 2026-07-31: a live ``-2022`` on a trend
reversal, closed on the next bar by a plain ``BUY MARKET 0.0070 IOC``). Two other paths
close positions and neither could do that -- the breaker's containment flatten and
``emergency_close`` -- so on a venue in that state the two moments that most need a close
to succeed were the two that could not.

The decision is one pure function here in ``strategy_core``, which is where the owner
asked for it. The three call sites differ only in what they do with the answer, and that
difference is real rather than incidental:

* the regular exit runs once per bar, so when the single plain attempt is spent it sends
  nothing -- re-sending a refused reduce-only order every bar is the flood shape lesson
  #13 records;
* containment and emergency run once, so when the attempt is spent they still send the
  reduce-only form. It will very likely be refused, but a refused reduce-only order
  cannot open a reverse position, and in those two moments attempting beats abstaining.

What the breaker flatten does *not* get from this, stated plainly because it would be
easy to read the opposite: the venue's rejection arrives asynchronously, as an event,
while the flatten is a single synchronous pass. So a refusal that happens *during* the
flatten cannot be recovered inside it. The flatten benefits when the refusal was already
recorded -- which is the shape of the real incident, where the strategy was refused at
08:30 and anything containing the position afterwards would have known.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.orders import OrderTracker
from custos_toolkit_nautilus.adapter.strategy_core import (
    CloseAttempt,
    NautilusStrategyCore,
    plan_close_attempt,
)

_CLOSE_METHODS = (
    "emergency_close",
    "close_all_positions_with_fallback",
    "_close_position_with_fallback",
    "order_tracker_for",
    "_log_warning",
)


def _make_stub(tracker: OrderTracker | None = None) -> SimpleNamespace:
    """Bind the close methods onto a plain object.

    ``NautilusStrategyCore`` extends a Cython type that cannot be instantiated through
    ``object.__new__`` with assignable ``log`` / ``cache``, which is why the rest of this
    module's sibling tests bind methods this way too.
    """
    stub = SimpleNamespace()
    stub.log = MagicMock()
    stub.cache = MagicMock()
    stub.cancel_all_orders = MagicMock()
    stub.close_position = MagicMock()
    for name in _CLOSE_METHODS:
        setattr(stub, name, types.MethodType(getattr(NautilusStrategyCore, name), stub))
    if tracker is not None:
        stub.order_tracker_for = lambda _instrument_id, _t=tracker: _t
    return stub


def _position(instrument_id: str = "BTCUSDT-PERP.BINANCE") -> SimpleNamespace:
    return SimpleNamespace(instrument_id=instrument_id)


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------


def test_the_protective_form_is_used_until_the_venue_refuses_it() -> None:
    assert (
        plan_close_attempt(reduce_only_refused=False, plain_close_submitted=False)
        is CloseAttempt.REDUCE_ONLY
    )


def test_a_refusal_of_reduce_only_earns_the_plain_attempt() -> None:
    assert (
        plan_close_attempt(reduce_only_refused=True, plain_close_submitted=False)
        is CloseAttempt.PLAIN
    )


def test_the_plain_attempt_is_spent_after_one_use() -> None:
    assert (
        plan_close_attempt(reduce_only_refused=True, plain_close_submitted=True)
        is CloseAttempt.PLAIN_ALREADY_SPENT
    )


def test_a_spent_attempt_without_a_refusal_is_still_protective() -> None:
    """Defensive: the two flags disagreeing must not read as permission for a plain order."""
    assert (
        plan_close_attempt(reduce_only_refused=False, plain_close_submitted=True)
        is CloseAttempt.REDUCE_ONLY
    )


# ---------------------------------------------------------------------------
# emergency_close
# ---------------------------------------------------------------------------


def test_emergency_close_keeps_the_protective_form_when_nothing_was_refused() -> None:
    from nautilus_trader.model.enums import TimeInForce

    stub = _make_stub()
    position = _position()
    stub.cache.positions_open.return_value = [position]

    stub.emergency_close()

    stub.close_position.assert_called_once_with(
        position, reduce_only=True, time_in_force=TimeInForce.IOC
    )


def test_emergency_close_drops_reduce_only_after_the_venue_refused_it() -> None:
    from nautilus_trader.model.enums import TimeInForce

    tracker = OrderTracker()
    tracker.record_reduce_only_refusal()
    stub = _make_stub(tracker)
    position = _position()
    stub.cache.positions_open.return_value = [position]

    stub.emergency_close()

    stub.close_position.assert_called_once_with(
        position, reduce_only=False, time_in_force=TimeInForce.IOC
    )
    assert tracker.plain_close_submitted, "the one plain attempt has to be recorded as spent"


def test_emergency_close_spends_the_plain_attempt_only_once() -> None:
    """A second emergency falls back to the protective form rather than a second plain order."""
    from nautilus_trader.model.enums import TimeInForce

    tracker = OrderTracker()
    tracker.record_reduce_only_refusal()
    stub = _make_stub(tracker)
    position = _position()
    stub.cache.positions_open.return_value = [position]

    stub.emergency_close()
    stub.emergency_close()

    assert [call.kwargs["reduce_only"] for call in stub.close_position.call_args_list] == [
        False,
        True,
    ]
    assert stub.close_position.call_args_list[-1].kwargs["time_in_force"] is TimeInForce.IOC


def test_emergency_close_is_still_fail_safe_around_the_decision() -> None:
    """The decision must not become a new way for shutdown's best-effort layer to raise."""
    stub = _make_stub()
    stub.order_tracker_for = MagicMock(side_effect=RuntimeError("tracker exploded"))
    stub.cache.positions_open.return_value = [_position()]

    stub.emergency_close()  # must not raise

    stub.close_position.assert_called_once()
    assert stub.close_position.call_args.kwargs["reduce_only"] is True, (
        "an unreadable tracker means no evidence, and no evidence means the protective form"
    )


# ---------------------------------------------------------------------------
# The containment flatten's entry point
# ---------------------------------------------------------------------------


def test_the_containment_flatten_uses_the_protective_form_by_default() -> None:
    from nautilus_trader.model.enums import TimeInForce

    stub = _make_stub()
    position = _position()
    stub.cache.positions_open.return_value = [position]

    stub.close_all_positions_with_fallback("BTCUSDT-PERP.BINANCE")

    stub.cache.positions_open.assert_called_once_with(instrument_id="BTCUSDT-PERP.BINANCE")
    stub.close_position.assert_called_once_with(
        position, reduce_only=True, time_in_force=TimeInForce.IOC
    )


def test_the_containment_flatten_drops_reduce_only_after_a_recorded_refusal() -> None:
    """The breaker trips after the strategy has already been refused -- the case that matters."""
    tracker = OrderTracker()
    tracker.record_reduce_only_refusal()
    stub = _make_stub(tracker)
    stub.cache.positions_open.return_value = [_position()]

    stub.close_all_positions_with_fallback("BTCUSDT-PERP.BINANCE")

    assert stub.close_position.call_args.kwargs["reduce_only"] is False


def test_the_containment_flatten_closes_nothing_when_nothing_is_open() -> None:
    stub = _make_stub()
    stub.cache.positions_open.return_value = []

    stub.close_all_positions_with_fallback("BTCUSDT-PERP.BINANCE")

    stub.close_position.assert_not_called()


# ---------------------------------------------------------------------------
# Where the evidence comes from
# ---------------------------------------------------------------------------


def test_the_base_class_reports_no_refusal_evidence() -> None:
    """A Core subclass that tracks no per-instrument order state gets the safe answer."""
    stub = SimpleNamespace()
    stub.order_tracker_for = types.MethodType(NautilusStrategyCore.order_tracker_for, stub)

    assert stub.order_tracker_for("BTCUSDT-PERP.BINANCE") is None


def test_the_trading_strategy_answers_from_its_pair_context() -> None:
    """The subclass that does keep the state has to hand it to the base class's close paths."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    tracker = OrderTracker()
    strat = SimpleNamespace(
        _contexts={"BTCUSDT-PERP.BINANCE": SimpleNamespace(order_tracker=tracker)}
    )
    strat.order_tracker_for = types.MethodType(NautilusTradingStrategy.order_tracker_for, strat)

    assert strat.order_tracker_for("BTCUSDT-PERP.BINANCE") is tracker
    assert strat.order_tracker_for("ETHUSDT-PERP.BINANCE") is None
