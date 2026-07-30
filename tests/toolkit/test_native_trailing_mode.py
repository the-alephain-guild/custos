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
    PairContextCoordinator,
    SignalExecutionCoordinator,
    SLTPCoordinator,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.pair_context import PairContext
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

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

    TradeEventHandler.handle_order_filled(SimpleNamespace(_strategy=stub), event)

    stub._sltp_coordinator.submit_native_trailing.assert_called_once()
    stub._sltp_coordinator.submit_stop_loss.assert_not_called()
    stub._sltp_coordinator.submit_take_profit.assert_not_called()


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
    cache.positions_open.return_value = [MagicMock(is_long=True, avg_px_open=100.0)]
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_called_once()
    stub._strategy.log.error.assert_called_once()
    assert ctx.native_trailing_rebuild_deadline_ns > 0


def test_protection_noop_when_open_trailing_exists():
    """A live tracked trailing order means the position is protected -> no rebuild."""
    ctx = _make_ctx()
    ctx.order_tracker.set_exchange_sl_order("O-TR-1")
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True)]
    cache.order.return_value = MagicMock(is_closed=False)
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_not_called()


def test_protection_noop_when_trailing_inflight():
    """A just-submitted (SUBMITTED, is_open=False) trailing is still in-flight, not
    closed -> must NOT be mistaken for unprotected and rebuilt (race fix)."""
    ctx = _make_ctx()
    ctx.order_tracker.set_exchange_sl_order("O-TR-1")
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True)]
    cache.order.return_value = MagicMock(is_open=False, is_closed=False)
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_not_called()


def test_protection_rebuilds_when_tracked_order_closed():
    """Tracked trailing order is terminal (REJECTED/CANCELED/EXPIRED) -> rebuild."""
    ctx = _make_ctx()
    ctx.order_tracker.set_exchange_sl_order("O-TR-1")
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True, avg_px_open=100.0)]
    cache.order.return_value = MagicMock(is_closed=True)
    stub = _protection_stub("native_trailing", cache)

    stub.ensure_native_trailing_protection(ctx)

    stub.ensure_native_trailing_exists.assert_called_once()


def test_protection_respects_rate_guard():
    """Within the rebuild cooldown, do not rebuild again (avoid reject->rebuild flood)."""
    ctx = _make_ctx()
    ctx.native_trailing_rebuild_deadline_ns = 2_000_000_000_000  # future
    cache = MagicMock()
    cache.positions_open.return_value = [MagicMock(is_long=True)]
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
