# tests/test_native_trailing_mode.py
"""The native_trailing sl_tp_mode wiring.

native_trailing is a new sl_tp_mode that submits an exchange-managed
TrailingStopMarketOrder as the protective stop (Spike §C.5). This module
verifies the wiring without touching exchange/tick/hybrid behavior:

- "native_trailing" is an accepted sl_tp_mode; invalid values are rejected at
  construction, where TradeRiskConfig.__post_init__ refuses an invalid mode
- on_start does NOT build a tick_monitor in native_trailing mode
- ExecutionCoordinator.handle_trade_tick / handle_quote_tick early-return in
  native_trailing mode
- position open submits the trailing stop via native_trailing_submitter
- StopLossTrailingConfig exposes trigger_price_type (default "mark")

Tests use SimpleNamespace stubs with unbound method calls or a directly
constructed component (ExecutionCoordinator(stub)) to avoid instantiating the
Cython Strategy base, which cannot be constructed here.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")
pytest.importorskip("msgspec")

from custos_toolkit.signals.types import Signal
from custos_toolkit_nautilus.adapter.config.risk import StopLossTrailingConfig, build_risk_config
from custos_toolkit_nautilus.adapter.coordinators import (
    ExecutionCoordinator,
    OrderReconciler,
    PairContextCoordinator,
    SignalExecutionCoordinator,
    SLTPCoordinator,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.pair_context import PairContext
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from nautilus_trader.model import BarType, InstrumentId, OrderSide, OrderType

INSTRUMENT = "BTCUSDT-PERP.BINANCE"
BAR = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"


def _make_ctx() -> PairContext:
    return PairContext(
        pair="BTC-USDT",
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        bar_type=BarType.from_str(BAR),
    )


# =============================================================================
# config: StopLossTrailingConfig.trigger_price_type
# =============================================================================


def test_trailing_config_trigger_price_type_default_mark():
    cfg = StopLossTrailingConfig()
    assert cfg.trigger_price_type == "mark"


def test_trailing_config_trigger_price_type_override():
    cfg = StopLossTrailingConfig(trigger_price_type="last")
    assert cfg.trigger_price_type == "last"


def test_build_risk_config_passes_trigger_price_type():
    risk = build_risk_config(
        {"trade": {"stop_loss": {"trailing": {"trigger_price_type": "last", "trailing_pct": 0.02}}}}
    )
    assert risk.trade.stop_loss.trailing.trigger_price_type == "last"


# =============================================================================
# sl_tp_mode validity — the fail-fast check now lives in TradeRiskConfig.__post_init__.
# The thorough cover is in test_config_self_validation.py; here only that it is accepted.
# =============================================================================


def test_trade_risk_config_accepts_native_trailing():
    from custos_toolkit_nautilus.adapter.config.risk import TradeRiskConfig

    assert TradeRiskConfig(sl_tp_mode="native_trailing").sl_tp_mode == "native_trailing"


# =============================================================================
# tick_monitor build gating (PairContextCoordinator._init_tick_monitor)
# =============================================================================


def test_native_trailing_does_not_build_tick_monitor():
    ctx = _make_ctx()
    stub = SimpleNamespace(_mode=SLTPMode.NATIVE_TRAILING, config=MagicMock())
    PairContextCoordinator(stub)._init_tick_monitor(ctx)
    assert ctx.tick_monitor is None


@pytest.mark.parametrize("mode", ["exchange"])
def test_exchange_does_not_build_tick_monitor(mode):
    ctx = _make_ctx()
    stub = SimpleNamespace(_mode=SLTPMode(mode), config=MagicMock())
    PairContextCoordinator(stub)._init_tick_monitor(ctx)
    assert ctx.tick_monitor is None


# =============================================================================
# tick handlers early-return in native_trailing mode
# =============================================================================


def test_handle_trade_tick_native_trailing_early_returns():
    ctx = _make_ctx()
    ctx.tick_monitor = MagicMock()
    cache = MagicMock()
    stub = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        _mode=SLTPMode.NATIVE_TRAILING,
        cache=cache,
    )
    tick = SimpleNamespace(instrument_id=ctx.instrument_id, price=Decimal("100"))
    ExecutionCoordinator(stub).handle_trade_tick(tick)
    cache.positions_open.assert_not_called()
    ctx.tick_monitor.check.assert_not_called()


def test_handle_quote_tick_native_trailing_early_returns():
    ctx = _make_ctx()
    ctx.tick_monitor = MagicMock()
    cache = MagicMock()
    stub = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        _mode=SLTPMode.NATIVE_TRAILING,
        cache=cache,
    )
    tick = SimpleNamespace(
        instrument_id=ctx.instrument_id, bid_price=Decimal("100"), ask_price=Decimal("100.1")
    )
    ExecutionCoordinator(stub).handle_quote_tick(tick)
    cache.positions_open.assert_not_called()
    ctx.tick_monitor.check.assert_not_called()


# =============================================================================
# submit_native_trailing
# =============================================================================


def _native_submit_stub(cache):
    return SimpleNamespace(
        cache=cache,
        config=MagicMock(),
        log=MagicMock(),
        submit_order=MagicMock(),
        _order_signal_map={},
    )


def test_submit_native_trailing_submits_and_tracks():
    ctx = _make_ctx()
    submitter = MagicMock()
    order = MagicMock(client_order_id="O-TR-1")
    submitter.create_order.return_value = order
    ctx.native_trailing_submitter = submitter

    position = MagicMock(avg_px_open=100.0, is_long=True, quantity=Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [position]
    stub = _native_submit_stub(cache)

    SLTPCoordinator(stub).submit_native_trailing(ctx, Signal.enter_long(price=100.0))

    submitter.create_order.assert_called_once()
    assert ctx.order_tracker.exchange_sl_order_id == "O-TR-1"
    stub.submit_order.assert_called_once_with(order)


def test_submit_native_trailing_none_order_does_not_submit():
    """fail-fast (submitter returns None) -> no submit, no tracking."""
    ctx = _make_ctx()
    submitter = MagicMock()
    submitter.create_order.return_value = None
    ctx.native_trailing_submitter = submitter

    position = MagicMock(avg_px_open=100.0, is_long=True, quantity=Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [position]
    stub = _native_submit_stub(cache)

    result = SLTPCoordinator(stub).submit_native_trailing(ctx, Signal.enter_long(price=100.0))

    assert result is None
    stub.submit_order.assert_not_called()
    assert ctx.order_tracker.exchange_sl_order_id is None
    # Failing fast must surface loudly: an open position is left unprotected
    stub.log.error.assert_called_once()


def test_submit_native_trailing_no_position_returns():
    ctx = _make_ctx()
    submitter = MagicMock()
    ctx.native_trailing_submitter = submitter
    cache = MagicMock()
    cache.positions_open.return_value = []
    stub = _native_submit_stub(cache)

    SLTPCoordinator(stub).submit_native_trailing(ctx, Signal.enter_long(price=100.0))
    submitter.create_order.assert_not_called()


# =============================================================================
# handle_order_filled dispatch to native_trailing
# =============================================================================


def test_handle_order_filled_dispatches_native_trailing():
    ctx = _make_ctx()
    ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100.0), None)

    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True)]
    stub = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        log=MagicMock(),
        _event_publisher=MagicMock(enabled=False),
        cache=cache,
        config=MagicMock(position=MagicMock(capital_mode="compound")),
        _get_effective_capital=lambda: Decimal("1000"),
        _get_risk_equity=lambda: Decimal("1000"),
        _risk_controller=MagicMock(),
        _mode=SLTPMode.NATIVE_TRAILING,
        _sltp_coordinator=SimpleNamespace(
            submit_native_trailing=MagicMock(),
            submit_stop_loss=MagicMock(),
            submit_take_profit=MagicMock(),
        ),
    )
    event = MagicMock()
    event.instrument_id = ctx.instrument_id
    event.last_px = 100.0
    event.last_qty = Decimal("1")
    event.client_order_id = "owned-entry"
    ctx.order_tracker.set_entry_order(event.client_order_id, side=1)

    TradeEventHandler.handle_order_filled(SimpleNamespace(_strategy=stub), event)

    stub._sltp_coordinator.submit_native_trailing.assert_called_once()
    stub._sltp_coordinator.submit_stop_loss.assert_not_called()
    stub._sltp_coordinator.submit_take_profit.assert_not_called()


def test_foreign_fill_does_not_consume_pending_entry_or_submit_protection():
    ctx = _make_ctx()
    pending = Signal.enter_long(price=100.0)
    ctx.position_tracker.set_pending_signal(pending, None)
    ctx.order_tracker.set_entry_order("owned-entry", side=1)

    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True)]
    stub = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        log=MagicMock(),
        _event_publisher=MagicMock(enabled=False),
        cache=cache,
        config=MagicMock(position=MagicMock(capital_mode="compound")),
        _get_risk_equity=lambda: Decimal("1000"),
        _risk_controller=MagicMock(),
        _mode=SLTPMode.NATIVE_TRAILING,
        _sltp_coordinator=SimpleNamespace(
            submit_native_trailing=MagicMock(),
            submit_stop_loss=MagicMock(),
            submit_take_profit=MagicMock(),
        ),
    )
    event = MagicMock()
    event.instrument_id = ctx.instrument_id
    event.client_order_id = "foreign-entry"
    event.last_px = 100.0

    TradeEventHandler.handle_order_filled(SimpleNamespace(_strategy=stub), event)

    assert ctx.position_tracker.pending_signal is pending
    assert ctx.order_tracker.entry_order_id == "owned-entry"
    stub._sltp_coordinator.submit_native_trailing.assert_not_called()
    stub._sltp_coordinator.submit_stop_loss.assert_not_called()
    stub._sltp_coordinator.submit_take_profit.assert_not_called()


def test_partial_entry_fills_keep_correlation_and_protect_each_exposure_lot():
    ctx = _make_ctx()
    pending = Signal.enter_long(price=100.0)
    ctx.position_tracker.set_pending_signal(pending, None)
    ctx.order_tracker.set_entry_order("owned-entry", side=1)

    partial_order = SimpleNamespace(is_closed=False, tags=[])
    filled_order = SimpleNamespace(is_closed=True, tags=[])
    cache = MagicMock()
    cache.order.side_effect = [partial_order, filled_order]
    cache.positions_open.return_value = [MagicMock(is_long=True)]
    mode = SimpleNamespace(on_entry_filled=MagicMock())
    stub = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        log=MagicMock(),
        _event_publisher=MagicMock(enabled=False),
        cache=cache,
        config=MagicMock(position=MagicMock(capital_mode="fixed")),
        _risk_controller=MagicMock(),
        _mode=mode,
    )
    first = SimpleNamespace(
        instrument_id=ctx.instrument_id,
        client_order_id="owned-entry",
        order_side="BUY",
        last_qty=Decimal("0.0031"),
        last_px=Decimal("100.0"),
    )
    second = SimpleNamespace(
        instrument_id=ctx.instrument_id,
        client_order_id="owned-entry",
        order_side="BUY",
        last_qty=Decimal("0.0039"),
        last_px=Decimal("100.1"),
    )
    handler = TradeEventHandler(stub)

    handler.handle_order_filled(first)

    assert ctx.position_tracker.pending_signal is pending
    assert ctx.order_tracker.entry_order_id == "owned-entry"
    mode.on_entry_filled.assert_called_once_with(
        stub,
        ctx,
        pending,
        cache.positions_open.return_value[0],
        first.last_px,
        None,
        protection_quantity=Decimal("0.0031"),
        initialize_position=True,
    )

    handler.handle_order_filled(second)

    assert mode.on_entry_filled.call_count == 2
    assert mode.on_entry_filled.call_args_list[1].kwargs == {
        "protection_quantity": Decimal("0.0039"),
        "initialize_position": False,
    }
    assert ctx.position_tracker.pending_signal is None
    assert ctx.order_tracker.entry_order_id is None


def test_native_protection_keeps_every_partial_fill_order_and_quantity():
    ctx = _make_ctx()
    submitter = MagicMock()
    first_order = MagicMock(client_order_id="stop-lot-1")
    second_order = MagicMock(client_order_id="stop-lot-2")
    submitter.create_order.side_effect = [first_order, second_order]
    ctx.native_trailing_submitter = submitter

    position = MagicMock(avg_px_open=100.0, is_long=True, quantity=Decimal("0.0070"))
    cache = MagicMock()
    cache.positions_open.return_value = [position]
    stub = _native_submit_stub(cache)
    coordinator = SLTPCoordinator(stub)
    signal = Signal.enter_long(price=100.0)

    coordinator.submit_native_trailing(ctx, signal, quantity=Decimal("0.0031"))
    coordinator.submit_native_trailing(ctx, signal, quantity=Decimal("0.0039"))

    assert [call.kwargs["quantity"] for call in submitter.create_order.call_args_list] == [
        Decimal("0.0031"),
        Decimal("0.0039"),
    ]
    assert ctx.order_tracker.exchange_sl_order_ids == ["stop-lot-1", "stop-lot-2"]
    assert stub.submit_order.call_count == 2


@pytest.mark.parametrize(
    ("mode", "order_type", "tracker_property"),
    [
        (SLTPMode.HYBRID, OrderType.STOP_MARKET, "exchange_sl_order_ids"),
        (SLTPMode.EXCHANGE, OrderType.STOP_MARKET, "sl_order_ids"),
        (SLTPMode.NATIVE_TRAILING, OrderType.TRAILING_STOP_MARKET, "exchange_sl_order_ids"),
    ],
)
def test_restart_reclaims_every_partial_fill_protection_lot(mode, order_type, tracker_property):
    ctx = _make_ctx()
    orders = [
        MagicMock(
            client_order_id="stop-lot-1",
            order_type=order_type,
            is_reduce_only=True,
            side=OrderSide.SELL,
            quantity=Decimal("0.0031"),
        ),
        MagicMock(
            client_order_id="stop-lot-2",
            order_type=order_type,
            is_reduce_only=True,
            side=OrderSide.SELL,
            quantity=Decimal("0.0039"),
        ),
    ]
    cache = MagicMock()
    cache.orders_open.return_value = orders
    strategy = SimpleNamespace(
        _mode=mode,
        cache=cache,
        log=MagicMock(),
        _sltp_coordinator=SimpleNamespace(
            submit_stop_loss=MagicMock(),
            submit_safety_stop_loss=MagicMock(),
            submit_native_trailing=MagicMock(),
        ),
    )
    reconciler = OrderReconciler(strategy)
    position = MagicMock(is_long=True, avg_px_open=100.0, quantity=Decimal("0.0070"))

    if mode is SLTPMode.NATIVE_TRAILING:
        reconciler.ensure_native_trailing_exists(ctx, position)
    else:
        reconciler.ensure_exchange_sl_exists(ctx, position)

    assert getattr(ctx.order_tracker, tracker_property) == ["stop-lot-1", "stop-lot-2"]
    strategy._sltp_coordinator.submit_stop_loss.assert_not_called()
    strategy._sltp_coordinator.submit_safety_stop_loss.assert_not_called()
    strategy._sltp_coordinator.submit_native_trailing.assert_not_called()


# =============================================================================
# break-even gating: native_trailing must NOT move stop to break-even
# (self-reflect round 1: the trailing stop IS the dynamic stop; a break-even
# stop_market would be untracked and collide with it)
# =============================================================================


def _break_even_stub(sl_tp_mode, cache, risk_manager):
    be = SimpleNamespace(enabled=True, activation_pct=0.015, offset=0.001)
    stop_loss = SimpleNamespace(break_even=be)
    trade = SimpleNamespace(stop_loss=stop_loss)
    config = SimpleNamespace(risk=SimpleNamespace(trade=trade))
    return SimpleNamespace(
        cache=cache,
        config=config,
        _mode=SLTPMode(sl_tp_mode),
        _risk_manager=risk_manager,
        _sltp_coordinator=SimpleNamespace(move_stop_to_break_even=MagicMock()),
    )


def test_native_trailing_skips_break_even():
    ctx = _make_ctx()
    ctx.position_tracker.record_entry(Decimal("100"), Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True, is_closed=False)]
    risk_manager = MagicMock()
    risk_manager.should_move_to_break_even.return_value = True
    stub = _break_even_stub("native_trailing", cache, risk_manager)
    bar = SimpleNamespace(close=Decimal("110"))

    SignalExecutionCoordinator(stub).manage_positions_for_pair(ctx, bar)

    stub._sltp_coordinator.move_stop_to_break_even.assert_not_called()
    # gating short-circuits before the risk_manager is even consulted
    risk_manager.should_move_to_break_even.assert_not_called()


def test_hybrid_still_triggers_break_even():
    """Gating must not regress existing modes."""
    ctx = _make_ctx()
    ctx.position_tracker.record_entry(Decimal("100"), Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True, is_closed=False)]
    risk_manager = MagicMock()
    risk_manager.should_move_to_break_even.return_value = True
    stub = _break_even_stub("hybrid", cache, risk_manager)
    bar = SimpleNamespace(close=Decimal("110"))

    SignalExecutionCoordinator(stub).manage_positions_for_pair(ctx, bar)

    stub._sltp_coordinator.move_stop_to_break_even.assert_called_once()


# =============================================================================
# Per-bar self-heal: rebuild the native_trailing protection after a
# venue rejection or a lost order leaves an open position unprotected
# =============================================================================


def _protection_stub(sl_tp_mode, cache, now_ns=1_000_000_000_000):
    # ensure_native_trailing_protection moved into OrderReconciler; returns the component,
    from custos_toolkit_nautilus.adapter.coordinators import OrderReconciler

    clock = MagicMock()
    clock.timestamp_ns.return_value = now_ns
    strategy = SimpleNamespace(
        _mode=SLTPMode(sl_tp_mode),
        cache=cache,
        clock=clock,
        log=MagicMock(),
    )
    reconciler = OrderReconciler(strategy)
    # ensure_native_trailing_exists is called from inside the component — mocked to isolate
    reconciler.ensure_native_trailing_exists = MagicMock()
    return reconciler


def test_protection_rebuilds_when_missing():
    """Open position + no tracked trailing order -> rebuild + loud error + arm rate guard."""
    ctx = _make_ctx()  # exchange_sl_order_id is None (reject cleared the tracker)
    cache = MagicMock()
    cache.positions_open.return_value = [
        MagicMock(is_long=True, avg_px_open=100.0, quantity=Decimal("1"))
    ]
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_called_once()
    stub._strategy.log.error.assert_called_once()
    assert ctx.native_trailing_rebuild_deadline_ns > 0


def test_protection_noop_when_open_trailing_exists():
    """A live tracked trailing order means the position is protected -> no rebuild."""
    ctx = _make_ctx()
    ctx.order_tracker.set_exchange_sl_order("O-TR-1", Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True, quantity=Decimal("1"))]
    cache.order.return_value = MagicMock(is_closed=False)
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_not_called()


def test_protection_noop_when_trailing_inflight():
    """A just-submitted (SUBMITTED, is_open=False) trailing is still in-flight, not
    closed -> must NOT be mistaken for unprotected and rebuilt (race fix)."""
    ctx = _make_ctx()
    ctx.order_tracker.set_exchange_sl_order("O-TR-1", Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True, quantity=Decimal("1"))]
    cache.order.return_value = MagicMock(is_open=False, is_closed=False)
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_not_called()


def test_protection_rebuilds_when_tracked_order_closed():
    """Tracked trailing order is terminal (REJECTED/CANCELED/EXPIRED) -> rebuild."""
    ctx = _make_ctx()
    ctx.order_tracker.set_exchange_sl_order("O-TR-1", Decimal("1"))
    cache = MagicMock()
    cache.positions_open.return_value = [
        MagicMock(is_long=True, avg_px_open=100.0, quantity=Decimal("1"))
    ]
    cache.order.return_value = MagicMock(is_closed=True)
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_called_once()


def test_protection_rebuilds_only_the_missing_partial_fill_delta():
    ctx = _make_ctx()
    ctx.order_tracker.add_exchange_sl_order("stop-lot-1", Decimal("0.0031"))
    position = MagicMock(is_long=True, avg_px_open=100.0, quantity=Decimal("0.0070"))
    cache = MagicMock()
    cache.positions_open.return_value = [position]
    cache.order.return_value = MagicMock(is_closed=False)
    strategy = SimpleNamespace(
        _mode=SLTPMode.NATIVE_TRAILING,
        cache=cache,
        clock=MagicMock(timestamp_ns=MagicMock(return_value=1_000_000_000_000)),
        log=MagicMock(),
        _sltp_coordinator=SimpleNamespace(submit_native_trailing=MagicMock()),
    )
    reconciler = OrderReconciler(strategy)
    reconciler.find_existing_trailing_orders = MagicMock(return_value=[])

    reconciler.ensure_native_trailing_protection(ctx)

    strategy._sltp_coordinator.submit_native_trailing.assert_called_once()
    assert strategy._sltp_coordinator.submit_native_trailing.call_args.kwargs == {
        "quantity": Decimal("0.0039")
    }


def test_hybrid_protection_rebuilds_only_the_missing_partial_fill_delta():
    ctx = _make_ctx()
    ctx.order_tracker.add_exchange_sl_order("stop-lot-1", Decimal("0.0031"))
    position = MagicMock(is_long=True, avg_px_open=100.0, quantity=Decimal("0.0070"))
    cache = MagicMock()
    cache.positions_open.return_value = [position]
    cache.order.return_value = MagicMock(is_closed=False)
    strategy = SimpleNamespace(
        _mode=SLTPMode.HYBRID,
        cache=cache,
        clock=MagicMock(timestamp_ns=MagicMock(return_value=1_000_000_000_000)),
        log=MagicMock(),
        _sltp_coordinator=SimpleNamespace(submit_safety_stop_loss=MagicMock()),
    )
    reconciler = OrderReconciler(strategy)
    reconciler.find_existing_sl_orders = MagicMock(return_value=[])

    reconciler.ensure_exchange_sl_protection(ctx)

    strategy._sltp_coordinator.submit_safety_stop_loss.assert_called_once()
    assert strategy._sltp_coordinator.submit_safety_stop_loss.call_args.kwargs == {
        "quantity": Decimal("0.0039")
    }


def test_rejected_partial_fill_stop_preserves_entry_and_other_protection():
    ctx = _make_ctx()
    pending = Signal.enter_long(price=100.0)
    ctx.position_tracker.set_pending_signal(pending, None)
    ctx.order_tracker.set_entry_order("entry-1", side=1)
    ctx.order_tracker.record_entry_fill(Decimal("0.0031"))
    ctx.order_tracker.add_exchange_sl_order("stop-lot-1", Decimal("0.0015"))
    ctx.order_tracker.add_exchange_sl_order("stop-lot-2", Decimal("0.0016"))
    rejected_order = MagicMock(is_reduce_only=True, tags=[])
    cache = MagicMock()
    cache.order.return_value = rejected_order
    strategy = SimpleNamespace(
        _get_context_from_instrument=lambda _iid: ctx,
        cache=cache,
        clock=MagicMock(timestamp_ns=MagicMock(return_value=1_000_000_000_000)),
        log=MagicMock(),
        _event_publisher=MagicMock(enabled=False),
        _order_signal_map={},
        cancel_all_orders=MagicMock(),
        pause=MagicMock(),
    )
    event = SimpleNamespace(
        instrument_id=ctx.instrument_id,
        client_order_id="stop-lot-2",
        reason="venue rejected protective stop",
    )

    OrderReconciler(strategy).handle_order_rejected(event)

    assert ctx.order_tracker.entry_order_id == "entry-1"
    assert ctx.position_tracker.pending_signal is pending
    assert ctx.order_tracker.exchange_sl_order_ids == ["stop-lot-1"]
    assert ctx.order_tracker.protected_quantity(exchange_managed=True) == Decimal("0.0015")
    strategy.cancel_all_orders.assert_not_called()
    strategy.pause.assert_called_once()
    assert ctx.order_tracker.close_reject_count == 0


def test_protection_respects_rate_guard():
    """Within the rebuild cooldown, do not rebuild again (avoid reject->rebuild flood)."""
    ctx = _make_ctx()
    ctx.native_trailing_rebuild_deadline_ns = 2_000_000_000_000  # future
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True, quantity=Decimal("1"))]
    stub = _protection_stub("native_trailing", cache, now_ns=1_000_000_000_000)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_not_called()


def test_protection_noop_non_native_mode():
    ctx = _make_ctx()
    cache = MagicMock()
    stub = _protection_stub("hybrid", cache)

    stub.ensure_native_trailing_protection(ctx)

    cache.positions_open.assert_not_called()
    stub.ensure_native_trailing_exists.assert_not_called()


def test_protection_noop_no_position():
    ctx = _make_ctx()
    cache = MagicMock()
    cache.positions_open.return_value = []
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_not_called()


# =============================================================================
# Protective-path failures in submit_native_trailing
# must surface loudly, except for the ordinary "no position" case
# =============================================================================


def test_submit_native_trailing_submitter_missing_logs_error():
    ctx = _make_ctx()  # native_trailing_submitter is None
    cache = MagicMock()
    stub = _native_submit_stub(cache)

    result = SLTPCoordinator(stub).submit_native_trailing(ctx, Signal.enter_long(price=100.0))

    assert result is None
    stub.log.error.assert_called_once()


def test_submit_native_trailing_avg_px_none_logs_error():
    ctx = _make_ctx()
    ctx.native_trailing_submitter = MagicMock()
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(avg_px_open=None, is_long=True)]
    stub = _native_submit_stub(cache)

    SLTPCoordinator(stub).submit_native_trailing(ctx, Signal.enter_long(price=100.0))

    stub.log.error.assert_called_once()
    ctx.native_trailing_submitter.create_order.assert_not_called()


def test_submit_native_trailing_no_position_is_silent():
    """No position is normal — must NOT log an error."""
    ctx = _make_ctx()
    ctx.native_trailing_submitter = MagicMock()
    cache = MagicMock()
    cache.positions_open.return_value = []
    stub = _native_submit_stub(cache)

    SLTPCoordinator(stub).submit_native_trailing(ctx, Signal.enter_long(price=100.0))

    stub.log.error.assert_not_called()
    ctx.native_trailing_submitter.create_order.assert_not_called()


# =============================================================================
# native_trailing must not subscribe to tick data (the handlers return early
# return anyway — avoid a useless tick stream)
# =============================================================================


def _tick_sub_stub(sl_tp_mode):
    ctx = _make_ctx()
    return SimpleNamespace(
        _mode=SLTPMode(sl_tp_mode),
        _get_tick_monitoring_config=lambda: SimpleNamespace(enabled=True, tick_type="both"),
        _contexts={"BTC-USDT": ctx},
        subscribe_trade_ticks=MagicMock(),
        subscribe_quote_ticks=MagicMock(),
        log=MagicMock(),
    )


def test_native_trailing_skips_tick_subscription():
    stub = _tick_sub_stub("native_trailing")
    PairContextCoordinator(stub).subscribe_ticks()
    stub.subscribe_trade_ticks.assert_not_called()
    stub.subscribe_quote_ticks.assert_not_called()


def test_hybrid_still_subscribes_ticks():
    """Gating must not regress tick/hybrid tick subscription."""
    stub = _tick_sub_stub("hybrid")
    PairContextCoordinator(stub).subscribe_ticks()
    stub.subscribe_trade_ticks.assert_called_once()
    stub.subscribe_quote_ticks.assert_called_once()


# The sl_tp_mode fallback tests went with _get_sl_tp_mode and _warn_sl_tp_mode_fallback:
# an invalid value is now refused in TradeRiskConfig.__post_init__ rather than quietly
# falling back, so there is no fallback left to warn about. See test_config_self_validation.py.
