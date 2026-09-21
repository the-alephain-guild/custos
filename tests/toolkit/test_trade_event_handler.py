"""Delegation guards for TradeEventHandler extraction.

The engine dispatches ``on_*`` callbacks by name on the Strategy instance, so those
public shells must stay on the class; only their bodies delegate to the component.
These guards assert the shell→component wiring and that the shell keeps top-level
exception protection.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)


# (callback on Strategy, method on TradeEventHandler)
_DELEGATIONS = [
    ("on_order_canceled", "handle_order_canceled"),
    ("on_order_filled", "handle_order_filled"),
    ("on_position_closed", "handle_position_closed"),
]


@requires_nautilus
@pytest.mark.parametrize("callback,handler_method", _DELEGATIONS)
def test_callback_delegates_to_component(callback, handler_method):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    delegate = MagicMock()
    stub = SimpleNamespace(
        _trade_event_handler=SimpleNamespace(**{handler_method: delegate}),
        log=MagicMock(),
    )
    event = MagicMock()

    getattr(NautilusTradingStrategy, callback)(stub, event)

    delegate.assert_called_once_with(event)


@requires_nautilus
def test_delegations_sentinel():
    # Guard against the parametrize list silently collapsing to empty.
    assert len(_DELEGATIONS) == 3


# Private handler bodies now on the component, gone from the Strategy class.
_MOVED_HANDLERS = [
    "_handle_order_filled",
    "_handle_position_closed",
    "_handle_order_canceled",
]


@requires_nautilus
def test_moved_handlers_no_longer_on_strategy_class():
    """The private _handle_* bodies must be gone from the class (single address)."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_HANDLERS:
        assert name not in vars(NautilusTradingStrategy), (
            f"{name} should have moved to the component"
        )


@requires_nautilus
def test_public_callbacks_stay_on_strategy_class():
    """Engine dispatches on_* by name and subclasses chain super(), so the public
    shells must stay on the class even though their bodies delegate to the component."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for callback, _ in _DELEGATIONS:
        assert callback in vars(NautilusTradingStrategy), f"{callback} must stay on the class"


@requires_nautilus
def test_component_exposes_all_handlers():
    """Each delegated handler must exist and be callable on the component."""
    from custos_toolkit_nautilus.adapter.coordinators import TradeEventHandler

    for _, handler_method in _DELEGATIONS:
        assert callable(getattr(TradeEventHandler, handler_method)), handler_method


@requires_nautilus
def test_handle_position_closed_resets_close_reject_count():
    """A confirmed position close resets the consecutive close-reject halt count.

    This is the convergence point both the normal and reversal close paths reach, so
    binding the reset here (not to clear()/clear_closing(), which also run on the reject
    path) is what keeps the halt count from being zeroed before it can fire.
    """
    from decimal import Decimal

    from custos_toolkit_nautilus.adapter.coordinators import TradeEventHandler
    from custos_toolkit_nautilus.adapter.orders import OrderTracker

    tracker = OrderTracker()
    tracker.record_close_reject()
    tracker.record_close_reject()
    assert tracker.close_reject_count == 2

    ctx = SimpleNamespace(
        pair="BTC-USDT",
        instrument_id="BTCUSDT.BINANCE",
        order_tracker=tracker,
        sl_tp_submitted_for_reversal=False,
        allocated_capital=Decimal("0"),
        position_tracker=MagicMock(),
        break_even_applied=True,
        tick_monitor=None,
    )
    strategy = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        log=MagicMock(),
        cache=SimpleNamespace(positions_open=lambda **_kw: [], order=lambda _oid: None),
        _risk_controller=MagicMock(),
        _capital_allocator=None,
        _sltp_coordinator=SimpleNamespace(cancel_sl_tp_orders=lambda _c: 0),
        on_trade_closed=MagicMock(),
    )
    event = SimpleNamespace(
        instrument_id="BTCUSDT.BINANCE",
        ts_event=0,
        realized_pnl=SimpleNamespace(as_decimal=lambda: Decimal("0")),
    )

    TradeEventHandler(strategy).handle_position_closed(event)

    assert tracker.close_reject_count == 0


@requires_nautilus
def test_position_close_preserves_partial_reversal_entry_before_new_exposure():
    from decimal import Decimal

    from custos_toolkit.position.tracker import PositionTracker
    from custos_toolkit.signals.types import Signal
    from custos_toolkit_nautilus.adapter.coordinators import TradeEventHandler
    from custos_toolkit_nautilus.adapter.orders import OrderTracker

    pending = Signal.enter_short(price=100.0)
    position_tracker = PositionTracker()
    position_tracker.set_pending_signal(pending, Decimal("2"))
    tracker = OrderTracker()
    tracker.set_entry_order(
        "reverse-entry",
        side=-1,
        exposure_offset_quantity=Decimal("0.007"),
    )
    tracker.record_entry_fill(Decimal("0.007"))
    tracker.add_exchange_sl_order("old-long-stop")
    ctx = SimpleNamespace(
        pair="BTC-USDT",
        instrument_id="BTCUSDT.BINANCE",
        order_tracker=tracker,
        sl_tp_submitted_for_reversal=False,
        pending_entry_is_reversal=True,
        allocated_capital=Decimal("0"),
        position_tracker=position_tracker,
        break_even_applied=False,
        tick_monitor=None,
    )
    calls = []

    def cancel_sl_tp_orders(_ctx, *, preserve_entry=False):
        calls.append(preserve_entry)
        _ctx.order_tracker.clear_protection_orders()
        return 1

    strategy = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        log=MagicMock(),
        cache=SimpleNamespace(positions_open=lambda **_kw: [], order=lambda _oid: None),
        _risk_controller=MagicMock(),
        _capital_allocator=None,
        _sltp_coordinator=SimpleNamespace(cancel_sl_tp_orders=cancel_sl_tp_orders),
        on_trade_closed=MagicMock(),
    )
    event = SimpleNamespace(
        instrument_id="BTCUSDT.BINANCE",
        ts_event=0,
        realized_pnl=SimpleNamespace(as_decimal=lambda: Decimal("0")),
    )

    TradeEventHandler(strategy).handle_position_closed(event)

    assert calls == [True]
    assert tracker.entry_order_id == "reverse-entry"
    assert tracker.entry_filled_quantity == Decimal("0.007")
    assert tracker.exchange_sl_order_ids == []
    assert position_tracker.pending_signal is pending
    assert position_tracker.pending_entry_atr == Decimal("2")
    assert ctx.pending_entry_is_reversal is False


@requires_nautilus
def test_position_close_keeps_new_protection_during_nonterminal_reversal_fill():
    from decimal import Decimal

    from custos_toolkit.position.tracker import PositionTracker
    from custos_toolkit.signals.types import Signal
    from custos_toolkit_nautilus.adapter.coordinators import TradeEventHandler
    from custos_toolkit_nautilus.adapter.orders import OrderTracker

    pending = Signal.enter_short(price=100.0)
    position_tracker = PositionTracker()
    position_tracker.set_pending_signal(pending, None)
    tracker = OrderTracker()
    tracker.set_entry_order(
        "reverse-entry",
        side=-1,
        exposure_offset_quantity=Decimal("0.007"),
    )
    tracker.record_entry_fill(Decimal("0.008"))
    tracker.add_exchange_sl_order("new-short-stop")
    ctx = SimpleNamespace(
        pair="BTC-USDT",
        instrument_id="BTCUSDT.BINANCE",
        order_tracker=tracker,
        sl_tp_submitted_for_reversal=True,
        pending_entry_is_reversal=False,
        allocated_capital=Decimal("0"),
        position_tracker=position_tracker,
        break_even_applied=False,
        tick_monitor=None,
    )
    cancel = MagicMock(return_value=0)
    strategy = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        log=MagicMock(),
        cache=SimpleNamespace(positions_open=lambda **_kw: [], order=lambda _oid: None),
        _risk_controller=MagicMock(),
        _capital_allocator=None,
        _sltp_coordinator=SimpleNamespace(cancel_sl_tp_orders=cancel),
        on_trade_closed=MagicMock(),
    )
    event = SimpleNamespace(
        instrument_id="BTCUSDT.BINANCE",
        ts_event=0,
        realized_pnl=SimpleNamespace(as_decimal=lambda: Decimal("0")),
    )

    TradeEventHandler(strategy).handle_position_closed(event)

    cancel.assert_not_called()
    assert tracker.entry_order_id == "reverse-entry"
    assert tracker.exchange_sl_order_ids == ["new-short-stop"]
    assert position_tracker.pending_signal is pending
    assert ctx.sl_tp_submitted_for_reversal is False


# accepted/opened had no body once the retired event lane went: their whole job was
# publishing. Asserted here so a reader does not take their absence for an oversight.
_CALLBACKS_WITHOUT_A_BODY = ["on_order_accepted", "on_position_opened"]


@requires_nautilus
@pytest.mark.parametrize("callback", _CALLBACKS_WITHOUT_A_BODY)
def test_a_callback_with_nothing_left_to_do_is_not_overridden(callback):
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    assert callback not in vars(NautilusTradingStrategy)
