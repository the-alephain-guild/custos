"""Guards for the SLTPCoordinator extraction.

The SL/TP submission / cancellation / break-even bodies live in SLTPCoordinator.
Callers (SLTPMode.on_entry_filled, OrderReconciler, TradeEventHandler, the bar
pipeline) reach them through ``strategy._sltp_coordinator``. These guards lock the
component API, the wiring, and that the old private methods are gone from the class
(single address).
"""

from __future__ import annotations

import pytest

requires_nautilus = pytest.mark.skipif(
    __import__("importlib").util.find_spec("nautilus_trader") is None,
    reason="nautilus_trader not installed",
)

# Public coordinator methods the strategy delegates to.
_PUBLIC_API = [
    "submit_stop_loss",
    "submit_take_profit",
    "submit_safety_stop_loss",
    "submit_native_trailing",
    "move_stop_to_break_even",
    "cancel_sl_tp_orders",
    "cancel_exchange_safety_sl",
]


@requires_nautilus
@pytest.mark.parametrize("method", _PUBLIC_API)
def test_component_exposes_method(method):
    from custos_toolkit_nautilus.adapter.coordinators import SLTPCoordinator

    assert callable(getattr(SLTPCoordinator, method)), method


@requires_nautilus
def test_public_api_sentinel():
    # Guard against the API list silently collapsing.
    assert len(_PUBLIC_API) == 7


# Private methods now on the component, gone from the Strategy class.
_MOVED_FROM_STRATEGY = [
    "_submit_stop_loss_for_pair",
    "_submit_take_profit_for_pair",
    "_submit_safety_stop_loss_for_pair",
    "_submit_native_trailing_for_pair",
    "_cancel_sl_tp_orders_for_pair",
    "_cancel_exchange_safety_sl_for_pair",
    "_move_stop_to_break_even",
    "_link_order_to_signal",
    "_signal_tags",
]


@requires_nautilus
def test_moved_methods_no_longer_on_strategy_class():
    """The SL/TP cluster must be gone from the whole class hierarchy (single address).
    hasattr (not just vars) also catches re-exposure via a parent/mixin,
    matching Option B's intent that strategy instances cannot call the old API."""
    from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy

    for name in _MOVED_FROM_STRATEGY:
        assert not hasattr(NautilusTradingStrategy, name), (
            f"{name} should have moved to SLTPCoordinator (not reachable on the strategy)"
        )


@requires_nautilus
def test_moved_methods_sentinel():
    assert len(_MOVED_FROM_STRATEGY) == 9


# --- cancel_exchange_safety_sl direct behavior (dormant: no production caller,
#     so a behavior test guards it from silent breakage — not just source/existence) ---


def _coord_with(cache):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custos_toolkit_nautilus.adapter.coordinators import SLTPCoordinator

    return SLTPCoordinator(SimpleNamespace(cache=cache, log=MagicMock(), cancel_order=MagicMock()))


@requires_nautilus
def test_cancel_exchange_safety_sl_none_returns_false():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    coord = _coord_with(MagicMock())
    ctx = SimpleNamespace(pair="P", order_tracker=SimpleNamespace(exchange_sl_order_id=None))

    assert coord.cancel_exchange_safety_sl(ctx) is False


@requires_nautilus
def test_cancel_exchange_safety_sl_open_cancels_removes_returns_true():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    cache = MagicMock()
    order = MagicMock(is_open=True)
    cache.order.return_value = order
    coord = _coord_with(cache)
    tracker = MagicMock(exchange_sl_order_id="O-SAFE")
    ctx = SimpleNamespace(pair="P", order_tracker=tracker)

    assert coord.cancel_exchange_safety_sl(ctx) is True
    coord._strategy.cancel_order.assert_called_once_with(order)
    tracker.remove_order.assert_called_once_with("O-SAFE")


@requires_nautilus
def test_cancel_exchange_safety_sl_closed_order_removes_returns_false():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    cache = MagicMock()
    cache.order.return_value = MagicMock(is_open=False)
    coord = _coord_with(cache)
    tracker = MagicMock(exchange_sl_order_id="O-SAFE")
    ctx = SimpleNamespace(pair="P", order_tracker=tracker)

    assert coord.cancel_exchange_safety_sl(ctx) is False
    coord._strategy.cancel_order.assert_not_called()
    tracker.remove_order.assert_called_once_with("O-SAFE")
