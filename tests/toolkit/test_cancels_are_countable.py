"""A cancel request and its outcome leave matching records, so both can be counted.

Plan 29's first task, and the reason it comes before any fix: on 2026-07-30 four
reduce-only stops from four earlier runs were resting at the venue at once. Three
explanations fit -- the bar-driven sweep never ran, the cancels never took effect, or the
age and cooldown windows outlived the process -- and they could not be told apart,
because the containers were gone and nothing had been retained. Picking one to fix would
have been guesswork.

So the point here is not to fix cancelling. It is to make the next run answer the
question: how many cancels were asked for, and how many came back.

Counting is by design, not by prose. Every record is one line beginning with a fixed
event name and carrying ``order_id=``, so a run's totals are a ``grep -c`` away.

What the numbers do and do not mean. ``requested`` should not be expected to equal
``confirmed``: an order can fill in the window between the request and the venue acting
on it, and a rejection is a third outcome. The evidence Plan 29 is after is a *persistent*
shortfall together with orders still resting afterwards -- not any single mismatch.

Requests are recorded on the base class by overriding the two venue calls, so no path can
route around it and a future cancel site is covered without anyone remembering to. That
matters more than attribution here: the sites that need to say who asked already log it
themselves (the sweep names the orphan it found).
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.cancel_audit import (
    CANCEL_CONFIRMED,
    CANCEL_REFUSED,
    CANCEL_REQUESTED,
)
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore
from nautilus_trader.trading.strategy import Strategy


def _RecordingStrategy(open_orders: list) -> SimpleNamespace:  # noqa: N802 — reads as a ctor
    """Bind the two cancel overrides onto a plain object, capturing the venue handoff.

    Same binding trick as this module's siblings: ``NautilusStrategyCore`` extends a
    Cython type that will not instantiate without an engine, and the handoff itself needs
    a live one. Replacing ``_venue_cancel_*`` is what makes the recording testable at all
    -- and it still exercises the real overrides, including that the arguments reach the
    handoff untouched.
    """
    stub = SimpleNamespace()
    stub.log = MagicMock()
    stub.cache = SimpleNamespace(orders_open=lambda instrument_id=None: open_orders)
    stub.delegated = []
    for name in ("cancel_order", "cancel_all_orders", "_log_warning"):
        setattr(stub, name, types.MethodType(getattr(NautilusStrategyCore, name), stub))
    stub._venue_cancel_order = lambda *a, **k: stub.delegated.append(("cancel_order", a, k))
    stub._venue_cancel_all_orders = lambda *a, **k: stub.delegated.append(
        ("cancel_all_orders", a, k)
    )
    return stub


def _order(order_id: str, side=None) -> SimpleNamespace:
    return SimpleNamespace(
        client_order_id=order_id, side=side, instrument_id="BTCUSDT-PERP.BINANCE"
    )


def _lines(strategy) -> list[str]:
    return [str(call.args[0]) for call in strategy.log.info.call_args_list]


def _lines_with(strategy, event: str) -> list[str]:
    return [line for line in _lines(strategy) if line.startswith(event)]


def test_a_single_cancel_is_recorded_with_its_order_id() -> None:
    strat = _RecordingStrategy([])

    strat.cancel_order(_order("O-1"))

    recorded = _lines_with(strat, CANCEL_REQUESTED)
    assert len(recorded) == 1
    assert "order_id=O-1" in recorded[0]


def test_a_bulk_cancel_records_one_line_per_order_it_asked_for() -> None:
    """The case that made the incident uncountable.

    ``cancel_all_orders`` names an instrument, not orders, so on its own it leaves no
    trace of how many cancels the venue was asked for -- and that count is precisely the
    left-hand side of the question.
    """
    strat = _RecordingStrategy([_order("O-1"), _order("O-2"), _order("O-3")])

    strat.cancel_all_orders("BTCUSDT-PERP.BINANCE")

    recorded = _lines_with(strat, CANCEL_REQUESTED)
    assert len(recorded) == 3
    assert {"O-1", "O-2", "O-3"} == {line.split("order_id=")[1].split()[0] for line in recorded}


def test_a_bulk_cancel_with_nothing_open_records_nothing_and_still_delegates() -> None:
    strat = _RecordingStrategy([])

    strat.cancel_all_orders("BTCUSDT-PERP.BINANCE")

    assert _lines_with(strat, CANCEL_REQUESTED) == []
    assert [name for name, _a, _k in strat.delegated] == ["cancel_all_orders"]


def test_a_bulk_cancel_restricted_to_one_side_records_only_that_side() -> None:
    """Recording the orders the venue was not asked about would inflate the left-hand side."""
    from nautilus_trader.model.enums import OrderSide

    strat = _RecordingStrategy([_order("O-BUY", OrderSide.BUY), _order("O-SELL", OrderSide.SELL)])

    strat.cancel_all_orders("BTCUSDT-PERP.BINANCE", OrderSide.BUY)

    recorded = _lines_with(strat, CANCEL_REQUESTED)
    assert len(recorded) == 1
    assert "order_id=O-BUY" in recorded[0]


def test_the_optional_arguments_reach_the_venue_untouched() -> None:
    """The override must not quietly narrow the API it stands in front of."""
    from nautilus_trader.model.enums import OrderSide

    strat = _RecordingStrategy([])
    order = _order("O-1")

    strat.cancel_order(order, "CLIENT-A", {"k": "v"})
    strat.cancel_all_orders("BTCUSDT-PERP.BINANCE", OrderSide.SELL, "CLIENT-A", {"k": "v"})

    assert strat.delegated == [
        ("cancel_order", (order, "CLIENT-A", {"k": "v"}), {}),
        ("cancel_all_orders", ("BTCUSDT-PERP.BINANCE", OrderSide.SELL, "CLIENT-A", {"k": "v"}), {}),
    ]


def test_a_failure_to_record_never_stops_the_cancel() -> None:
    """Observability is not allowed to cost a cancel. This is a money path."""
    strat = _RecordingStrategy([])
    strat.log.info.side_effect = RuntimeError("log exploded")

    strat.cancel_order(_order("O-1"))

    assert [name for name, _a, _k in strat.delegated] == ["cancel_order"]


def test_a_broken_cache_does_not_stop_a_bulk_cancel() -> None:
    """Same rule on the enumeration the bulk record needs."""
    strat = _RecordingStrategy([])
    strat.cache = SimpleNamespace(orders_open=MagicMock(side_effect=RuntimeError("cache exploded")))

    strat.cancel_all_orders("BTCUSDT-PERP.BINANCE")

    assert [name for name, _a, _k in strat.delegated] == ["cancel_all_orders"]


def test_the_override_is_actually_in_place() -> None:
    """Deleting the override would silence every request record while all else stays green."""
    assert NautilusStrategyCore.cancel_order is not Strategy.cancel_order
    assert NautilusStrategyCore.cancel_all_orders is not Strategy.cancel_all_orders


# ---------------------------------------------------------------------------
# The other side of the count
# ---------------------------------------------------------------------------


def test_a_confirmed_cancel_is_recorded_with_the_same_order_id() -> None:
    from custos_toolkit_nautilus.adapter.cancel_audit import record_cancel_confirmed

    log = MagicMock()
    record_cancel_confirmed(log, order_id="O-1", instrument_id="BTCUSDT-PERP.BINANCE")

    line = str(log.info.call_args.args[0])
    assert line.startswith(CANCEL_CONFIRMED)
    assert "order_id=O-1" in line


def test_a_refused_cancel_is_recorded_with_its_reason() -> None:
    """A refusal is the third outcome, and it has to be visible as its own bucket.

    Today the refusal handler reads every refusal as "the order is already gone" and does
    nothing at all for a stop-loss. Whether that reading is right is Plan 29's own
    question -- being able to see that it happened comes first.
    """
    from custos_toolkit_nautilus.adapter.cancel_audit import record_cancel_refused

    log = MagicMock()
    record_cancel_refused(
        log, order_id="O-1", instrument_id="BTCUSDT-PERP.BINANCE", reason="UNKNOWN_ORDER"
    )

    line = str(log.info.call_args.args[0])
    assert line.startswith(CANCEL_REFUSED)
    assert "order_id=O-1" in line
    assert "UNKNOWN_ORDER" in line


def test_the_strategy_records_both_outcomes_before_handling_them() -> None:
    """Recording sits ahead of the handler bodies, so a throwing body cannot hide it."""
    import inspect

    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for handler, expected in (
        (NautilusTradingStrategy.on_order_canceled, "record_cancel_confirmed"),
        (NautilusTradingStrategy.on_order_cancel_rejected, "record_cancel_refused"),
    ):
        source = inspect.getsource(handler)
        assert expected in source
        assert source.index(expected) < source.index("try:"), (
            f"{expected} must run before the guarded body, or a raising body loses the record"
        )
