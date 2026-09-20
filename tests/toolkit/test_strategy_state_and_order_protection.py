"""Regressions for the strategy-coordinator review of 2026-09-20.

Each class corresponds to one finding from
``.forge/reviews/2026-09-20-custos-strategy-deep-review.md``. The review shipped
probes asserting the defective behaviour; these assert the repaired behaviour
against the same real coordinators, so a regression flips them back to red.

The shared theme is that an intent -- a price reached, a cancel requested, a
reservation recorded, an order submitted -- is not an outcome. Only an execution
report may advance the confirmed state.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from types import SimpleNamespace as NS  # noqa: E402

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    OrderReconciler,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from nautilus_trader.model import OrderSide, Price, Quantity  # noqa: E402


class TestAtrRepairKeepsItsAtr:
    """ST-3: the repair path must not blank the ATR it needs to size a stop."""

    def test_repair_submits_a_stop_when_atr_is_available(self):
        h = Harness(mode=SLTPMode.EXCHANGE)
        signal = Signal.enter_long(price=100.0)
        h.ctx.position_tracker.set_pending_signal(signal, Decimal("2"))
        # The calculator can price this stop: entry 100, ATR 2, multiplier 2 -> 96.
        assert h._order_calculator.calculate_stop_loss(
            Decimal("100"), signal.direction, Decimal("2")
        ) == Decimal("96")

        OrderReconciler(h).ensure_exchange_sl_protection(h.ctx)

        assert len(h.sent) == 1, "an available ATR must produce a protective stop"
        assert h.sent[0].trigger_price == Decimal("96")
        assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == Decimal("1")

    def test_repair_preserves_the_pending_entry_context(self):
        """A partial entry still in flight owns the pending signal and its ATR."""
        h = Harness(mode=SLTPMode.EXCHANGE)
        signal = Signal.enter_long(price=100.0)
        h.ctx.position_tracker.set_pending_signal(signal, Decimal("2"))

        OrderReconciler(h).ensure_exchange_sl_protection(h.ctx)

        assert h.ctx.position_tracker.pending_signal is signal
        assert h.ctx.position_tracker.pending_entry_atr == Decimal("2")

    def test_repair_falls_back_to_the_live_indicator(self):
        """With no pending entry, the current ATR reading is the sizing input."""
        h = Harness(mode=SLTPMode.EXCHANGE)
        assert h.ctx.position_tracker.pending_entry_atr is None

        OrderReconciler(h).ensure_exchange_sl_protection(h.ctx)

        assert len(h.sent) == 1
        assert h.sent[0].trigger_price == Decimal("96")

    def test_unprotected_state_is_reported_when_no_atr_exists(self):
        """Genuinely missing data must surface, not pass silently as 'repaired'."""
        h = Harness(mode=SLTPMode.EXCHANGE)
        h.ctx.indicators["atr"] = None

        OrderReconciler(h).ensure_exchange_sl_protection(h.ctx)

        assert h.sent == []
        # The generic "coverage is below position" line is logged before the repair is
        # attempted, so it cannot distinguish a repair that worked from one that could
        # not price an order. Only the outcome line can.
        outcomes = [c.args[0] for c in h.log.error.call_args_list if "unprotected" in c.args[0]]
        assert outcomes, "a repair that produced no order must say the position is unprotected"

    def test_repeated_repair_windows_still_protect(self):
        """Crossing the cooldown repeatedly must not leave coverage at zero."""
        h = Harness(mode=SLTPMode.EXCHANGE)
        h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100.0), Decimal("2"))
        reconciler = OrderReconciler(h)

        for _ in range(3):
            reconciler.ensure_exchange_sl_protection(h.ctx)
            h.now_ns += 61_000_000_000

        assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) >= Decimal("1")


class TestACancelRequestIsNotADisappearance:
    """ST-4: entry ownership may only be released on a confirmed terminal state."""

    @staticmethod
    def _open_an_entry(h: Harness):
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h.flat()
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )
        return h.sent[0]

    def test_a_refused_cancel_keeps_a_still_open_entry(self):
        h = Harness(mode=SLTPMode.EXCHANGE)
        entry = self._open_an_entry(h)

        OrderReconciler(h).handle_order_cancel_rejected(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                reason="temporary cancellation failure",
            )
        )

        assert entry.is_open, "precondition: the venue still has this order"
        assert h.ctx.order_tracker.entry_order_id == entry.client_order_id

    def test_an_entry_that_fills_after_a_refused_cancel_still_gets_protection(self):
        h = Harness(mode=SLTPMode.EXCHANGE)
        entry = self._open_an_entry(h)
        OrderReconciler(h).handle_order_cancel_rejected(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                reason="temporary cancellation failure",
            )
        )

        # The order the venue would not cancel now fills.
        h.positions = [h.position]
        entry.is_open = False
        entry.is_closed = True
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_str("1.000"),
                last_px=Price.from_str("100.00"),
            )
        )

        assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == Decimal("1"), (
            "our own fill must not be mistaken for an external one"
        )

    def test_a_confirmed_disappearance_still_releases_ownership(self):
        """The original intent stays intact when the order really is gone."""
        h = Harness(mode=SLTPMode.EXCHANGE)
        entry = self._open_an_entry(h)
        entry.is_open = False
        entry.is_closed = True

        OrderReconciler(h).handle_order_cancel_rejected(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                reason="order already gone",
            )
        )

        assert h.ctx.order_tracker.entry_order_id is None

    def test_a_replacement_waits_for_the_old_entry_to_be_confirmed_gone(self):
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        old = self._open_an_entry(h)

        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )

        assert old.is_open, "precondition: the cancel is still unconfirmed"
        assert old.client_order_id in h.cancelled, "the cancel request was sent"
        assert len(h.sent) == 1, "a second entry would leave one of the two untracked"
        assert h.ctx.order_tracker.entry_order_id == old.client_order_id

    def test_the_next_bar_enters_once_the_old_entry_is_gone(self):
        """Waiting must not mean never: the entry resumes on confirmation."""
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        coordinator = SignalExecutionCoordinator(h)
        old = self._open_an_entry(h)
        coordinator.execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )

        old.is_open = False
        old.is_closed = True
        coordinator.execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )

        assert len(h.sent) == 2
        assert h.ctx.order_tracker.entry_order_id == h.sent[1].client_order_id
