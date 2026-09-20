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

from _strategy_harness import Harness, scaled_monitor  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    OrderReconciler,
    TradeEventHandler,
)
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from nautilus_trader.model import OrderSide, OrderType, Price, Quantity  # noqa: E402


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


class TestAScaledLevelIsSpentOnlyByAFill:
    """ST-2: reaching a price is not taking profit at it."""

    def test_a_locally_refused_partial_retries_at_the_same_price(self):
        monitor = scaled_monitor(1)
        monitor.init_position(Decimal("100"), True)
        h = Harness(monitor=monitor)
        attempts = []

        def refuse(order):
            attempts.append(order)
            return False  # refused locally, before dispatch

        h.submit_order = refuse

        h.tick("103")
        h.tick("103")

        assert len(attempts) == 2, "a level nothing was sent for must remain available"

    def test_recovery_reads_the_market_without_spending_a_level(self):
        h = Harness(monitor=scaled_monitor(1))

        OrderReconciler(h).recover_from_existing_positions()
        h.tick("105")

        assert len(h.sent) == 1, "the recovered position must still take its profit"

    def test_a_rejected_partial_does_not_cancel_a_valid_stop(self):
        monitor = scaled_monitor(1)
        monitor.init_position(Decimal("100"), True)
        h = Harness(mode=SLTPMode.HYBRID, monitor=monitor)
        protection = h.order(
            OrderType.STOP_MARKET,
            quantity=h.instrument.make_qty(Decimal("1")),
            order_side=OrderSide.SELL,
            reduce_only=True,
            trigger_price=Price.from_str("90.00"),
        )
        h.ctx.order_tracker.add_exchange_sl_order(protection.client_order_id, Decimal("1"))

        h.tick("103")
        partial = h.sent[-1]
        assert partial.client_order_id in h.ctx.order_tracker.tp_order_ids, (
            "a partial take-profit must be owned, or its rejection is read as a failed close"
        )

        OrderReconciler(h).handle_order_rejected(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=partial.client_order_id,
                reason="-2022 ReduceOnly Order is rejected.",
            )
        )

        assert protection.client_order_id not in h.cancelled
        assert h.ctx.order_tracker.exchange_sl_order_ids == [protection.client_order_id]

    def test_a_rejected_partial_leaves_its_level_available(self):
        monitor = scaled_monitor(1)
        monitor.init_position(Decimal("100"), True)
        h = Harness(monitor=monitor)
        h.tick("103")
        partial = h.sent[-1]

        OrderReconciler(h).handle_order_rejected(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=partial.client_order_id,
                reason="-2022 ReduceOnly Order is rejected.",
            )
        )
        h.tick("103")

        assert len(h.sent) == 2, "the target quantity was never taken; the level stands"

    def test_a_filled_partial_spends_its_level(self):
        monitor = scaled_monitor(1)
        monitor.init_position(Decimal("100"), True)
        h = Harness(monitor=monitor)
        h.tick("103")
        partial = h.sent[-1]

        h.position.quantity = Decimal("0.5")
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=partial.client_order_id,
                order_side=OrderSide.SELL,
                last_qty=Quantity.from_str("0.500"),
                last_px=Price.from_str("103.00"),
            )
        )
        h.tick("103")

        assert len(h.sent) == 1, "a confirmed level must not fire again"


class TestARefusedAllocationBlocksTheOrder:
    """ST-5: a reservation that was refused is not capital you may spend."""

    @staticmethod
    def _allocated_harness(tier: float, capital: str = "1000") -> Harness:
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        h = Harness()
        h.flat()
        h._capital_allocator = CapitalAllocator(
            AllocationConfig(tiers={"BTC-USDT": tier}), Decimal(capital), h.cache
        )
        h._capital_allocator.register_pair(h.ctx.pair, h.instrument.id)
        return h

    def _enter(self, h: Harness, size: str) -> None:
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx,
            Signal.enter_long(price=100.0),
            size=Decimal(size),
            bar=NS(close=Decimal("100")),
        )

    def test_a_pair_tier_refusal_submits_nothing(self):
        h = self._allocated_harness(tier=0.1)  # tier limit 100
        assert h._capital_allocator.get_tier_limit(h.ctx.pair) == Decimal("100")

        self._enter(h, "200")

        assert h.sent == []

    def test_a_pair_tier_refusal_leaves_no_state_behind(self):
        h = self._allocated_harness(tier=0.1)

        self._enter(h, "200")

        assert h.ctx.allocated_capital == Decimal("0")
        assert h._capital_allocator.available_cash == Decimal("1000")
        assert h.ctx.position_tracker.entry_count == 0
        assert h.ctx.order_tracker.entry_order_id is None

    def test_exhausted_total_cash_also_blocks(self):
        """The second refusal reason must take the same path as the first."""
        h = self._allocated_harness(tier=1.0)
        self._enter(h, "900")
        assert len(h.sent) == 1, "precondition: the first entry fits"
        # Settle the first entry. Without this the second call is stopped by the
        # replacement guard instead of the allocator, and this would assert nothing
        # about capital.
        h.sent[0].is_open = False
        h.sent[0].is_closed = True

        self._enter(h, "200")

        assert len(h.sent) == 1
        assert h._capital_allocator.available_cash == Decimal("100")

    def test_an_accepted_allocation_still_enters(self):
        h = self._allocated_harness(tier=1.0)

        self._enter(h, "400")

        assert len(h.sent) == 1
        assert h.ctx.allocated_capital == Decimal("400")
        assert h._capital_allocator.available_cash == Decimal("600")


class TestAnUnfilledEntryReleasesWhatItReserved:
    """ST-6: a submitted order is not a position, and its reservation is not spent."""

    @staticmethod
    def _entered(size: str = "400") -> tuple[Harness, object]:
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness()
        h.flat()
        h._capital_allocator = CapitalAllocator(
            AllocationConfig(tiers={"BTC-USDT": 1.0}), Decimal("1000"), h.cache
        )
        h._capital_allocator.register_pair(h.ctx.pair, h.instrument.id)
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx,
            Signal.enter_long(price=100.0),
            size=Decimal(size),
            bar=NS(close=Decimal("100")),
        )
        entry = h.sent[0]
        entry.is_open = False
        entry.is_closed = True
        return h, entry

    @staticmethod
    def _terminal_event(h: Harness, entry, outcome: str) -> None:
        event = NS(
            instrument_id=h.instrument.id,
            client_order_id=entry.client_order_id,
            reason="terminated before any fill",
        )
        if outcome == "cancel":
            TradeEventHandler(h).handle_order_canceled(event)
        else:
            OrderReconciler(h).handle_order_rejected(event)

    @pytest.mark.parametrize("outcome", ["cancel", "reject"])
    def test_a_zero_fill_terminal_returns_the_reservation(self, outcome):
        h, entry = self._entered()

        self._terminal_event(h, entry, outcome)

        assert h._capital_allocator.available_cash == Decimal("1000")
        assert h.ctx.allocated_capital == Decimal("0")

    @pytest.mark.parametrize("outcome", ["cancel", "reject"])
    def test_a_zero_fill_terminal_leaves_no_phantom_entry(self, outcome):
        h, entry = self._entered()

        self._terminal_event(h, entry, outcome)

        assert h.positions == [], "precondition: the venue has no position"
        assert h.ctx.position_tracker.entry_count == 0
        assert not h.ctx.position_tracker.has_position

    def test_a_partial_fill_keeps_the_part_that_filled(self):
        """Cancelling after a partial fill releases only what was never bought."""
        h, entry = self._entered()  # 400 at 100 -> 4 units
        entry.is_open = True
        entry.is_closed = False
        h.positions = [h.position]
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_str("1.000"),
                last_px=Price.from_str("100.00"),
            )
        )
        entry.is_open = False
        entry.is_closed = True

        self._terminal_event(h, entry, "cancel")

        assert h._capital_allocator.available_cash == Decimal("900"), "3 of 4 units unfilled"
        assert h.ctx.allocated_capital == Decimal("100")
        assert h.ctx.position_tracker.has_position, "the filled unit is a real position"


class TestScaledExitsUseOneBase:
    """ST-1: the same config must exit the same total in either mode."""

    @staticmethod
    def _tick_exits(exit_pcts: tuple[str, ...], prices: tuple[str, ...]) -> list[Decimal]:
        monitor = scaled_monitor(len(exit_pcts), exit_pcts)
        monitor.init_position(Decimal("100"), True, quantity=Decimal("1"))
        h = Harness(monitor=monitor)

        def fill(order):
            h.sent.append(order)
            h.position.quantity -= Decimal(str(order.quantity))

        h.submit_order = fill
        for price in prices:
            h.tick(price)
        return [Decimal(str(o.quantity)) for o in h.sent]

    @staticmethod
    def _exchange_exits(exit_pcts: tuple[str, ...]) -> list[Decimal]:
        from custos_toolkit_nautilus.adapter.config.risk import (
            ScaledTakeProfitConfig,
            ScaledTakeProfitLevelConfig,
        )
        from custos_toolkit_nautilus.adapter.orders import TakeProfitSubmitter

        h = Harness()
        targets = [0.02, 0.04, 0.06]
        kwargs = {
            f"level_{i + 1}": ScaledTakeProfitLevelConfig(
                target_pct=targets[i], exit_pct=float(exit_pcts[i])
            )
            for i in range(len(exit_pcts))
        }
        config = ScaledTakeProfitConfig(levels=len(exit_pcts), **kwargs)
        submitter = TakeProfitSubmitter(h.order_factory, h.cache, h.log, h._order_calculator)
        orders = submitter.create_scaled_orders(
            h.instrument.id,
            Signal.enter_long(price=100.0),
            Decimal("100"),
            h.position,
            config,
        )
        return [Decimal(str(o.quantity)) for o in orders]

    def test_halves_exit_the_whole_position_in_tick_mode(self):
        assert sum(self._tick_exits((".5", ".5"), ("103", "105", "110"))) == Decimal("1")

    def test_halves_agree_across_modes(self):
        assert self._tick_exits((".5", ".5"), ("103", "105", "110")) == self._exchange_exits(
            (".5", ".5")
        )

    def test_thirds_agree_across_modes(self):
        """The last level carries the rounding remainder, in both modes."""
        tick = self._tick_exits((".33", ".33", ".34"), ("103", "105", "107"))
        assert sum(tick) == Decimal("1")
        assert tick == self._exchange_exits((".33", ".33", ".34"))


class TestABreakEvenStopIsOwned:
    """ST-7: an order the exit path must cancel has to be findable."""

    @staticmethod
    def _broken_even() -> Harness:
        from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

        monitor = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal(".04"))
        monitor.init_position(Decimal("100"), True, quantity=Decimal("1"))
        h = Harness(monitor=monitor)
        assert h._mode.allows_break_even, "precondition: tick mode permits break-even"
        h._sltp_coordinator.move_stop_to_break_even(h.ctx, h.position, Decimal("100"))
        assert len(h.sent) == 1, "precondition: a stop was actually sent"
        return h

    def test_the_break_even_stop_is_tracked(self):
        h = self._broken_even()

        tracked = h.ctx.order_tracker.sl_order_ids + h.ctx.order_tracker.exchange_sl_order_ids
        assert tracked == [h.sent[0].client_order_id]

    def test_the_take_profit_can_cancel_it(self):
        h = self._broken_even()
        stop = h.sent[0]

        h.tick("105")  # past the 4% target

        assert stop.client_order_id in h.cancelled, (
            "the exit waits for this cancel, so it must be requested"
        )

    def test_the_position_closes_once_the_stop_is_gone(self):
        h = self._broken_even()
        stop = h.sent[0]
        h.tick("105")
        stop.is_open = False
        stop.is_closed = True

        h.tick("106")

        assert h.closed, "with nothing resting, the full exit must go through"


class TestEqualAllocationIgnoresRegistrationOrder:
    """ST-8: registration order is not configuration."""

    @staticmethod
    def _allocator(pairs: tuple[str, ...], capital: str = "200", tiers: dict | None = None):
        from unittest.mock import MagicMock

        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig
        from nautilus_trader.model import InstrumentId

        allocator = CapitalAllocator(
            AllocationConfig(mode="equal", tiers=tiers or {}), Decimal(capital), MagicMock()
        )
        for pair in pairs:
            allocator.register_pair(pair, InstrumentId.from_str(f"{pair.replace('-', '')}.BINANCE"))
        return allocator

    def test_two_pairs_split_evenly(self):
        allocator = self._allocator(("BTC-USDT", "ETH-USDT"))

        assert allocator.get_tier_limit("BTC-USDT") == Decimal("100")
        assert allocator.get_tier_limit("ETH-USDT") == Decimal("100")

    def test_swapping_registration_order_changes_nothing(self):
        forward = self._allocator(("BTC-USDT", "ETH-USDT"))
        reverse = self._allocator(("ETH-USDT", "BTC-USDT"))

        for pair in ("BTC-USDT", "ETH-USDT"):
            assert forward.get_tier_limit(pair) == reverse.get_tier_limit(pair)

    def test_the_first_pair_cannot_take_everything(self):
        allocator = self._allocator(("BTC-USDT", "ETH-USDT"))

        assert not allocator.allocate("BTC-USDT", Decimal("200"))
        assert allocator.get_available_capital("ETH-USDT") == Decimal("100")

    def test_a_third_pair_redivides_the_capital(self):
        allocator = self._allocator(("BTC-USDT", "ETH-USDT", "SOL-USDT"), capital="300")

        for pair in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
            assert allocator.get_tier_limit(pair) == Decimal("100")

    def test_an_explicit_tier_is_honoured_and_the_rest_is_shared(self):
        """A configured share is not up for redistribution; the remainder is."""
        allocator = self._allocator(
            ("BTC-USDT", "ETH-USDT", "SOL-USDT"), capital="100", tiers={"BTC-USDT": 0.5}
        )

        assert allocator.get_tier_limit("BTC-USDT") == Decimal("50")
        assert allocator.get_tier_limit("ETH-USDT") == Decimal("25")
        assert allocator.get_tier_limit("SOL-USDT") == Decimal("25")
