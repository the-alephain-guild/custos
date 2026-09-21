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
from custos_toolkit_nautilus.adapter.tick_monitor import (  # noqa: E402
    TakeProfitLevelState,
)
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


class TestAPartiallyFilledLevelKeepsItsRemainder:
    """ST-2, partial-fill arm: an IOC lot that only half filled took only half.

    The remainder of that level's target was never sold, so the level is not done.
    """

    @staticmethod
    def _dispatched() -> tuple[Harness, object]:
        monitor = scaled_monitor(2)
        monitor.init_position(Decimal("100"), True, quantity=Decimal("1"))
        h = Harness(monitor=monitor)
        h.tick("103")  # level 1 targets 0.5
        return h, h.sent[-1]

    def test_a_partial_fill_then_cancel_leaves_the_level_available(self):
        h, partial = self._dispatched()
        h.position.quantity = Decimal("0.7")
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=partial.client_order_id,
                order_side=OrderSide.SELL,
                last_qty=Quantity.from_str("0.300"),
                last_px=Price.from_str("103.00"),
            )
        )
        TradeEventHandler(h).handle_order_canceled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=partial.client_order_id,
                reason="IOC remainder cancelled",
            )
        )

        h.tick("103")

        assert len(h.sent) == 2, "0.2 of this level's target is still owed"
        assert Decimal(str(h.sent[1].quantity)) == Decimal("0.2")

    def test_a_full_fill_closes_the_level(self):
        h, partial = self._dispatched()
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

        assert len(h.sent) == 1, "the level's whole target was taken"


class TestRecoveryArmsATrailingStop:
    """Audit H1: observing the market must not disarm a trailing stop.

    Activation is not a consumable. Unlike a scaled level it carries no exit
    quota, so recording it costs nothing -- while losing it leaves a restarted
    runner holding a stop that will not fire.
    """

    @staticmethod
    def _trailing_harness() -> Harness:
        from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

        monitor = TickMonitorManager(
            mode="tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal(".02"),
            trailing_pct=Decimal(".01"),
        )
        # Entry 100; the harness cache reports the last bar at 105, i.e. +5%,
        # already past the 2% activation threshold.
        return Harness(monitor=monitor)

    def test_recovery_arms_a_stop_already_past_activation(self):
        h = self._trailing_harness()

        OrderReconciler(h).recover_from_existing_positions()

        assert h.ctx.tick_monitor.peak_price == Decimal("105")
        assert h.ctx.tick_monitor._trailing_manager._activated, (
            "the recovered position is already past activation"
        )

    def test_a_recovered_trailing_stop_fires_on_the_drawdown(self):
        """The consequence: price falls back under activation, stop must still fire."""
        h = self._trailing_harness()
        OrderReconciler(h).recover_from_existing_positions()

        # 101.5 is only +1.5% from entry -- below the activation threshold -- but
        # 3.3% down from the 105 peak, well past the 1% trail.
        h.tick("101.5")

        assert h.closed, "a stop armed before the restart must not need re-arming"


class TestAPersistentlyRefusedCancelKeepsHoldingTheEntry:
    """Audit M2: the entry keeps yielding while the old order is still live.

    This is the deliberate trade-off behind ST-4, written down so it is not read
    as a defect and 'fixed'. The old order is still at the venue and can still
    fill; the tracker holds one entry identity, so opening a second live order
    would leave one of the two unowned. Withholding new risk is the safe side,
    and the signal is re-evaluated every bar, so nothing is forfeited.
    """

    def test_repeated_refusals_never_open_a_second_live_entry(self):
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        h.flat()
        coordinator = SignalExecutionCoordinator(h)
        reconciler = OrderReconciler(h)
        coordinator.execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )
        entry = h.sent[0]

        for _ in range(3):
            coordinator.execute_entry_for_pair(
                h.ctx,
                Signal.enter_long(price=100.0),
                size=Decimal("100"),
                bar=NS(close=Decimal("100")),
            )
            reconciler.handle_order_cancel_rejected(
                NS(
                    instrument_id=h.instrument.id,
                    client_order_id=entry.client_order_id,
                    reason="venue will not cancel it",
                )
            )

        assert entry.is_open, "precondition: the venue still holds the order"
        assert len(h.sent) == 1
        assert h.ctx.order_tracker.entry_order_id == entry.client_order_id, (
            "ownership must survive every refusal, or a late fill arrives unowned"
        )

    def test_the_held_entry_still_gets_protection_when_it_finally_fills(self):
        """Yielding must not cost protection on the order that is still live."""
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        h.flat()
        coordinator = SignalExecutionCoordinator(h)
        coordinator.execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )
        entry = h.sent[0]
        for _ in range(3):
            coordinator.execute_entry_for_pair(
                h.ctx,
                Signal.enter_long(price=100.0),
                size=Decimal("100"),
                bar=NS(close=Decimal("100")),
            )
            OrderReconciler(h).handle_order_cancel_rejected(
                NS(
                    instrument_id=h.instrument.id,
                    client_order_id=entry.client_order_id,
                    reason="venue will not cancel it",
                )
            )

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

        assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == Decimal("1")


class TestScaledExitsAgreeAcrossAllThreePaths:
    """ST-1 acceptance, in full: the same config exits the same total whether the
    levels are reached one at a time, jumped over in a single tick, or filled in
    parts.

    The gap-through path is the one that bites. A price that spikes through two
    levels and falls straight back is exactly what scaled take-profit is for, and
    a level left behind there is never revisited.
    """

    @staticmethod
    def _exits(pcts: tuple[str, ...], prices: tuple[str, ...]) -> list[Decimal]:
        monitor = scaled_monitor(len(pcts), pcts)
        monitor.init_position(Decimal("100"), True, quantity=Decimal("1"))
        h = Harness(monitor=monitor)

        def fill(order):
            h.sent.append(order)
            h.position.quantity -= Decimal(str(order.quantity))

        h.submit_order = fill
        for price in prices:
            h.tick(price)
        return [Decimal(str(o.quantity)) for o in h.sent]

    @pytest.mark.parametrize(
        "pcts,stepped",
        [
            ((".5", ".5"), ("103", "105")),
            ((".33", ".33", ".34"), ("103", "105", "107")),
        ],
    )
    def test_a_single_tick_through_every_level_exits_the_same_total(self, pcts, stepped):
        step_by_step = self._exits(pcts, stepped)
        assert sum(step_by_step) == Decimal("1"), "precondition: stepping exits the whole position"

        # One tick at +10% clears every target at once.
        in_one_gap = self._exits(pcts, ("110",))

        assert sum(in_one_gap) == sum(step_by_step)

    def test_a_gap_that_falls_straight_back_still_took_everything(self):
        """No second chance: the price never returns above the targets."""
        exits = self._exits((".5", ".5"), ("110", "101"))

        assert sum(exits) == Decimal("1")

    def test_a_gap_exits_each_level_its_own_share(self):
        """The totals agreeing must not come from one oversized lot."""
        assert self._exits((".33", ".33", ".34"), ("110",)) == [
            Decimal("0.330"),
            Decimal("0.330"),
            Decimal("0.340"),
        ]


class TestARefusedEntryLeavesOldProtectionAlone:
    """EE-2: a refusal must not cost the position its stop.

    Introduced by the ST-5 fix: the reversal branch cancels every open order for
    the instrument before the capital reservation is asked for, so a refusal that
    correctly sends no entry still leaves the old position bare.
    """

    @staticmethod
    def _long_with_a_stop(capital: str = "100") -> tuple[Harness, object]:
        """An open long of 1 at 100, its only stop at 95, and `capital` total.

        Reversing costs 100 (new short) + 100 (closing the long) = 200, so the
        default capital of 100 -- already held by the open long -- cannot cover it.
        """
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        h = Harness(mode=SLTPMode.HYBRID)
        stop = h.order(
            OrderType.STOP_MARKET,
            quantity=h.instrument.make_qty(Decimal("1")),
            order_side=OrderSide.SELL,
            reduce_only=True,
            trigger_price=Price.from_str("95.00"),
        )
        h.ctx.order_tracker.add_exchange_sl_order(stop.client_order_id, Decimal("1"))
        allocator = CapitalAllocator(
            AllocationConfig(tiers={"BTC-USDT": 1.0}), Decimal(capital), h.cache
        )
        allocator.register_pair(h.ctx.pair, h.instrument.id)
        allocator.allocate(h.ctx.pair, Decimal("100"))  # the open long already holds it
        h._capital_allocator = allocator
        h.ctx.allocated_capital = Decimal("100")
        return h, stop

    def _reverse(self, h: Harness) -> None:
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx,
            Signal.enter_short(price=100.0),
            size=Decimal("100"),
            bar=NS(close=Decimal("100")),
        )

    def test_a_refused_reversal_sends_no_entry(self):
        h, _ = self._long_with_a_stop()

        self._reverse(h)

        assert h.sent == [], "precondition: the reservation is refused"

    def test_a_refused_reversal_keeps_the_stop(self):
        h, stop = self._long_with_a_stop()

        self._reverse(h)

        assert stop.client_order_id not in h.cancelled, (
            "the old long is still open; cancelling its only stop leaves it bare"
        )
        assert h.ctx.order_tracker.exchange_sl_order_ids == [stop.client_order_id]
        assert h.ctx.order_tracker.protected_quantity(exchange_managed=True) == Decimal("1")

    def test_a_refused_reversal_leaves_no_reversal_flag_behind(self):
        h, _ = self._long_with_a_stop()

        self._reverse(h)

        assert not h.ctx.pending_entry_is_reversal, (
            "a reversal that never happened must not arm the reversal handling"
        )

    def test_an_affordable_reversal_still_clears_the_way(self):
        """The cancel must still happen when the entry actually proceeds."""
        h, stop = self._long_with_a_stop(capital="1000")  # 900 spare covers the 200

        self._reverse(h)

        assert len(h.sent) == 1
        assert stop.client_order_id in h.cancelled


class TestAnEntryRefusedLocallyIsRolledBack:
    """EE-2, adjacent exit: the gate can refuse before anything reaches the venue.

    The exit path already reads that refusal (`dispatched is False`); the entry
    path did not, so a locally refused entry left its capital reserved and an
    entry recorded for an order that was never sent.
    """

    @staticmethod
    def _refusing_harness() -> Harness:
        from custos_toolkit_nautilus.adapter.capital_allocator import CapitalAllocator
        from custos_toolkit_nautilus.adapter.config.allocation import AllocationConfig

        h = Harness()
        h.flat()
        allocator = CapitalAllocator(
            AllocationConfig(tiers={"BTC-USDT": 1.0}), Decimal("1000"), h.cache
        )
        allocator.register_pair(h.ctx.pair, h.instrument.id)
        h._capital_allocator = allocator
        h.submit_order = lambda order: False  # the local gate refuses it
        return h

    def _enter(self, h: Harness) -> None:
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx,
            Signal.enter_long(price=100.0),
            size=Decimal("400"),
            bar=NS(close=Decimal("100")),
        )

    def test_a_locally_refused_entry_returns_its_capital(self):
        h = self._refusing_harness()

        self._enter(h)

        assert h._capital_allocator.available_cash == Decimal("1000")
        assert h.ctx.allocated_capital == Decimal("0")

    def test_a_locally_refused_entry_records_no_position(self):
        h = self._refusing_harness()

        self._enter(h)

        assert h.ctx.position_tracker.entry_count == 0
        assert h.ctx.position_tracker.pending_signal is None
        assert h.ctx.order_tracker.entry_order_id is None


class TestTheExitBaseFollowsTheWholeEntry:
    """EE-5: the scaled base must be the exposure that ended up open.

    Introduced by the ST-1 fix: the base is taken when the tick monitor is seeded,
    which happens on the first lot that opens exposure. An entry that fills in two
    parts therefore sizes its exits against the first part alone.
    """

    @staticmethod
    def _entry_filling_in(parts: tuple[str, ...]) -> Harness:
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        monitor = scaled_monitor(2)
        h = Harness(monitor=monitor)
        h.flat()
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )
        entry = h.sent[0]
        h.positions = [h.position]
        filled = Decimal("0")
        for part in parts:
            filled += Decimal(part)
            h.position.quantity = filled
            TradeEventHandler(h).handle_order_filled(
                NS(
                    instrument_id=h.instrument.id,
                    client_order_id=entry.client_order_id,
                    order_side=OrderSide.BUY,
                    last_qty=Quantity.from_str(f"{Decimal(part):.3f}"),
                    last_px=Price.from_str("100.00"),
                )
            )
        h.sent.clear()

        def fill(order):
            h.sent.append(order)
            h.position.quantity -= Decimal(str(order.quantity))

        h.submit_order = fill
        return h

    def test_an_entry_filled_in_two_parts_exits_all_of_it(self):
        h = self._entry_filling_in(("0.5", "0.5"))
        assert h.position.quantity == Decimal("1"), "precondition: the whole entry filled"

        h.tick("110")  # clears both 50% levels

        exits = [Decimal(str(o.quantity)) for o in h.sent]
        assert sum(exits) == Decimal("1"), f"half the position would be stranded: {exits}"

    def test_a_single_fill_still_exits_all_of_it(self):
        """The one-lot case must keep working."""
        h = self._entry_filling_in(("1",))

        h.tick("110")

        assert sum(Decimal(str(o.quantity)) for o in h.sent) == Decimal("1")

    def test_a_level_already_taken_survives_the_later_fill(self):
        """A later lot extends the base; it must not reset finished levels."""
        h = self._entry_filling_in(("0.5",))
        h.tick("103")  # level 1 fires against the 0.5 open so far
        first = [Decimal(str(o.quantity)) for o in h.sent]
        assert first, "precondition: a level fired before the entry finished"

        assert h.ctx.tick_monitor.level_state(1) is TakeProfitLevelState.PENDING


class TestReversalSizingRespectsTheContractMultiplier:
    """RS-1: contract units are not quote amounts.

    A linear contract's notional is quantity * price * multiplier. Reversal sizing
    wrote quantity * price, so the closing half of the reversal was inflated by
    1 / multiplier before quantity_from_notional divided the multiplier back out.
    """

    @staticmethod
    def _reverse_with_multiplier(multiplier: str, open_contracts: str, target_notional: str):
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator
        from custos_toolkit_nautilus.adapter.execution import ExecutionManager
        from nautilus_trader.model import CryptoPerpetual, Currency, InstrumentId, Symbol

        h = Harness()
        h.instrument = CryptoPerpetual(
            InstrumentId.from_str("BTC-USDT-SWAP.OKX"),
            Symbol("BTC-USDT-SWAP"),
            Currency.from_str("BTC"),
            Currency.from_str("USDT"),
            Currency.from_str("USDT"),
            False,
            2,
            0,
            Price.from_str("0.01"),
            Quantity.from_str("1"),
            0,
            0,
            multiplier=Quantity.from_str(multiplier),
        )
        h.ctx.instrument_id = h.instrument.id
        h.ctx.execution_manager = ExecutionManager(h.order_factory, h.cache, h.log)
        h.position.quantity = Decimal(open_contracts)
        h.position.is_long, h.position.is_short = False, True  # an open short
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx,
            Signal.enter_long(price=100.0),
            size=Decimal(target_notional),
            bar=NS(close=Decimal("100")),
        )
        return h, Decimal(str(h.sent[0].quantity))

    @pytest.mark.parametrize(
        "multiplier,open_contracts,target_notional,expected_net",
        [
            # multiplier .01: the short's 10 contracts are $10, not $1000.
            (".01", "10", "10", "10"),
            ("1", "10", "1000", "10"),
            ("10", "10", "10000", "10"),
        ],
    )
    def test_the_reversal_reaches_the_target_net_position(
        self, multiplier, open_contracts, target_notional, expected_net
    ):
        """Accept on the net position left open, not on the order size."""
        _, submitted = self._reverse_with_multiplier(multiplier, open_contracts, target_notional)

        net_after = submitted - Decimal(open_contracts)
        assert net_after == Decimal(expected_net)

    def test_a_multiplier_contract_is_not_inflated_a_hundredfold(self):
        """The headline case from the review, stated as the order actually sent."""
        _, submitted = self._reverse_with_multiplier(".01", "10", "10")

        assert submitted == Decimal("20"), "ten to close plus ten to open"


class TestARefusedProtectionIsNotCoverage:
    """RS-2: registering protection must agree with what was dispatched.

    The local gate refuses before the order reaches the native cache, so there is
    no OrderRejected callback and nothing for the repairer to find. A tracker
    entry written before dispatch therefore counts as coverage forever, and the
    position never gets the stop it is missing.
    """

    @staticmethod
    def _refusing(mode: SLTPMode) -> Harness:
        h = Harness(mode=mode)
        h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100.0), Decimal("2"))
        h.config.risk.trade.max_loss_pct = 0.05

        def refuse(order):
            # The gate refuses before the order reaches the native cache, so the
            # cache must not hold it either -- that absence is precisely why the
            # repairer cannot notice the order is gone.
            h.orders.pop(order.client_order_id, None)
            return False

        h.submit_order = refuse
        return h

    def test_a_refused_exchange_stop_leaves_zero_coverage(self):
        h = self._refusing(SLTPMode.EXCHANGE)

        h._sltp_coordinator.submit_stop_loss(h.ctx, Signal.enter_long(price=100.0))

        assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == Decimal("0")
        assert h.ctx.order_tracker.sl_order_ids == []

    def test_a_refused_safety_stop_leaves_zero_coverage(self):
        h = self._refusing(SLTPMode.HYBRID)

        h._sltp_coordinator.submit_safety_stop_loss(h.ctx, Signal.enter_long(price=100.0))

        assert h.ctx.order_tracker.protected_quantity(exchange_managed=True) == Decimal("0")
        assert h.ctx.order_tracker.exchange_sl_order_ids == []

    def test_a_refused_break_even_stop_leaves_zero_coverage(self):
        h = self._refusing(SLTPMode.EXCHANGE)

        h._sltp_coordinator.move_stop_to_break_even(h.ctx, h.position, Decimal("100"))

        assert h.ctx.order_tracker.protected_quantity(exchange_managed=False) == Decimal("0")

    def test_the_repairer_tries_again_after_a_refusal(self):
        """The point of not counting it: the next repair window must act."""
        h = self._refusing(SLTPMode.EXCHANGE)
        reconciler = OrderReconciler(h)

        reconciler.ensure_exchange_sl_protection(h.ctx)
        attempts_after_first = len(h.cancelled)  # nothing dispatched; count attempts below
        h.now_ns += 61_000_000_000
        sent_orders: list = []
        h.submit_order = sent_orders.append  # the gate lets it through this time
        reconciler.ensure_exchange_sl_protection(h.ctx)

        assert attempts_after_first == 0
        assert len(sent_orders) == 1, "a position with no coverage must be repaired"


class TestCloseCleanupRespectsWhatSurvives:
    """RS-7: a close tidies up after one position, not after the pair.

    Two things can outlive it. A netting reversal has already opened the new
    position by the time the old PositionClosed arrives, and a partly filled
    entry is still sitting at the venue when the half it did fill gets stopped
    out.
    """

    @staticmethod
    def _closed_event(h: Harness):
        # PositionClosed always carries ts_event; the double has to as well, or it
        # proves the handler works against something the venue never sends.
        return NS(instrument_id=h.instrument.id, realized_pnl=None, ts_event=0)

    def test_a_reversal_keeps_the_new_position_s_tick_protection(self):
        monitor = scaled_monitor(1)
        h = Harness(monitor=monitor)
        # The new short is already in the cache and its protection is seeded.
        h.position.is_long, h.position.is_short = False, True
        monitor.init_position(Decimal("100"), False, quantity=Decimal("1"))

        TradeEventHandler(h).handle_position_closed(self._closed_event(h))

        assert h.ctx.tick_monitor.is_active
        assert h.ctx.tick_monitor._entry_price == Decimal("100"), (
            "the old position closing must not erase the new one's monitor"
        )

    def test_the_reversed_position_can_still_take_profit(self):
        """The consequence: the surviving short must still exit at its target."""
        monitor = scaled_monitor(1)
        h = Harness(monitor=monitor)
        h.position.is_long, h.position.is_short = False, True
        monitor.init_position(Decimal("100"), False, quantity=Decimal("1"))
        TradeEventHandler(h).handle_position_closed(self._closed_event(h))

        h.tick("97")  # 3% in favour of a short, past the 2% first tier

        assert h.sent, "a surviving position with a live monitor must still exit"

    def test_a_partly_filled_entry_keeps_its_ownership_through_a_stop_out(self):
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        h.flat()
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )
        entry = h.sent[0]
        h.positions = [h.position]
        h.position.quantity = Decimal("0.5")
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_str("0.500"),
                last_px=Price.from_str("100.00"),
            )
        )
        # That half is stopped out. The entry order is still open at the venue.
        h.positions = []
        assert entry.is_open, "precondition: the rest of the entry can still fill"

        TradeEventHandler(h).handle_position_closed(self._closed_event(h))

        assert h.ctx.order_tracker.entry_order_id == entry.client_order_id
        assert h.ctx.position_tracker.pending_signal is not None

    def test_the_rest_of_that_entry_still_gets_protection(self):
        """The consequence: the later fill must not arrive as an untracked one."""
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        h.flat()
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx, Signal.enter_long(price=100.0), size=Decimal("100"), bar=NS(close=Decimal("100"))
        )
        entry = h.sent[0]
        h.positions = [h.position]
        h.position.quantity = Decimal("0.5")
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_str("0.500"),
                last_px=Price.from_str("100.00"),
            )
        )
        h.positions = []
        TradeEventHandler(h).handle_position_closed(self._closed_event(h))

        # The rest of the entry fills.
        h.positions = [h.position]
        h.position.quantity = Decimal("0.5")
        before = len(h.sent)
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=Quantity.from_str("0.500"),
                last_px=Price.from_str("100.00"),
            )
        )

        assert len(h.sent) > before, "the remaining exposure must be protected"


class TestProtectionIsPricedOffTheFill:
    """RS-3: the stop belongs to what the position cost, not to the signal bar.

    bar.close is the reference the signal was formed on. A limit offset, price
    improvement or slippage all move the actual cost away from it, and a stop
    priced off the bar can land on the wrong side of the market.
    """

    @staticmethod
    def _entered_at(signal_bar: str, fill_price: str, fills: tuple[str, ...] = ("1",)) -> Harness:
        from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator

        h = Harness(mode=SLTPMode.EXCHANGE)
        h.flat()
        h.config.risk.trade.stop_loss = NS(method="fixed", fixed=NS(stop_loss_pct=0.02))
        SignalExecutionCoordinator(h).execute_entry_for_pair(
            h.ctx,
            Signal.enter_long(price=float(signal_bar)),
            size=Decimal("100"),
            bar=NS(close=Decimal(signal_bar)),
        )
        entry = h.sent[0]
        h.positions = [h.position]
        h.position.avg_px_open = Decimal(fill_price)
        filled = Decimal("0")
        for part in fills:
            filled += Decimal(part)
            h.position.quantity = filled
            TradeEventHandler(h).handle_order_filled(
                NS(
                    instrument_id=h.instrument.id,
                    client_order_id=entry.client_order_id,
                    order_side=OrderSide.BUY,
                    last_qty=Quantity.from_str(f"{Decimal(part):.3f}"),
                    last_px=Price.from_str(f"{Decimal(fill_price):.2f}"),
                )
            )
        return h

    def test_a_limit_offset_fill_prices_the_stop_off_the_fill(self):
        """Signal bar 100, filled at 90: a 2% stop is 88.2, not 98."""
        h = self._entered_at("100", "90")

        assert h.ctx.position_tracker.first_entry_price == Decimal("90")

    def test_price_improvement_is_honoured_too(self):
        h = self._entered_at("100", "101")

        assert h.ctx.position_tracker.first_entry_price == Decimal("101")

    def test_multiple_fills_use_the_venue_s_average(self):
        """The venue reports the weighted average; nothing is recomputed here."""
        h = self._entered_at("100", "95", fills=("0.5", "0.5"))

        assert h.ctx.position_tracker.first_entry_price == Decimal("95")

    def test_the_submitted_stop_sits_below_a_long_entry(self):
        """The consequence: a stop priced off the bar would be above the market."""
        h = self._entered_at("100", "90")
        stops = [o for o in h.sent if o.trigger_price is not None]

        assert stops, "the entry fill must arm a stop"
        assert Decimal(str(stops[-1].trigger_price)) < Decimal("90")


class TestARefilledEntryOpensANewPositionToProtect:
    """FR-2: the entry order's running total is not the position's lifecycle.

    fix 11 kept a partly filled entry's ownership through a stop-out, which is what
    lets the rest of it arrive owned and get a stop. But ``record_entry_fill``
    answers "is this the first exposure this order opened" from the order's own
    cumulative protected quantity, and that total survived the close along with the
    ownership. The monitor was reset by the close; the next lot reported itself as a
    continuation and only extended a base that no longer described anything, so the
    new position had no entry price, no direction and no trailing state.
    """

    @staticmethod
    def _entry_of(h: Harness, quantity: str = "1.000"):
        entry = h.order(
            OrderType.LIMIT, quantity=Quantity.from_str(quantity), order_side=OrderSide.BUY
        )
        h.ctx.order_tracker.set_entry_order(
            entry.client_order_id, 1, order_quantity=Decimal(quantity)
        )
        return entry

    @staticmethod
    def _fill(h: Harness, entry, quantity: str = "0.5"):
        TradeEventHandler(h).handle_order_filled(
            NS(
                instrument_id=h.instrument.id,
                client_order_id=entry.client_order_id,
                order_side=OrderSide.BUY,
                last_qty=Decimal(quantity),
                last_px=Price.from_str("100.00"),
            )
        )

    @classmethod
    def _stopped_then_refilled(cls, mode: SLTPMode):
        from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

        monitor = TickMonitorManager(
            mode="hybrid" if mode is SLTPMode.HYBRID else "tick",
            tp_method="trailing",
            trailing_activation_pct=Decimal(".02"),
            trailing_pct=Decimal(".01"),
        )
        h = Harness(mode=mode, monitor=monitor)
        h.config.risk.trade.max_loss_pct = Decimal(".05")
        h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100), Decimal("2"))
        entry = cls._entry_of(h)

        h.position.quantity = Decimal("0.5")
        cls._fill(h, entry)
        assert monitor._entry_price == 100, "precondition: the first half armed the monitor"

        # That half is stopped out; the rest of the entry is still at the venue.
        h.positions.clear()
        TradeEventHandler(h).handle_position_closed(
            NS(instrument_id=h.instrument.id, realized_pnl=None, ts_event=10**9)
        )
        h.positions.append(h.position)
        h.position.quantity = Decimal("0.5")
        cls._fill(h, entry)
        return h, monitor

    @pytest.mark.parametrize("mode", [SLTPMode.HYBRID, SLTPMode.TICK])
    def test_the_new_position_knows_what_it_paid(self, mode):
        _h, monitor = self._stopped_then_refilled(mode)

        assert monitor._entry_price == 100

    @pytest.mark.parametrize("mode", [SLTPMode.HYBRID, SLTPMode.TICK])
    def test_the_new_position_can_still_trail_out(self, mode):
        """The consequence: without an entry price the trailing exit never fires."""
        _h, monitor = self._stopped_then_refilled(mode)

        assert monitor.check(Decimal("120")) is None, "rising is not an exit"

        assert monitor.check(Decimal("115")) is not None, (
            "a give-back from the peak past the trailing distance has to exit"
        )

    def test_only_the_quantity_that_reopened_is_protected(self):
        """The lot is 0.5, not the 1.0 the order has filled in total."""
        h, _monitor = self._stopped_then_refilled(SLTPMode.HYBRID)

        protective = [order for order in h.sent if order.is_reduce_only]
        assert protective, "the refilled position still gets its exchange safety stop"
        assert Decimal(str(protective[-1].quantity)) == Decimal("0.5")

    def test_a_second_lot_of_the_same_position_still_only_extends(self):
        """The control: two fills into one live position arm the monitor once.

        Rebasing on every fill would re-seed the entry price from the latest lot and
        move the stop with it, which is the behaviour fix 11 and its predecessors
        deliberately do not have.
        """
        from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager

        monitor = TickMonitorManager(
            mode="hybrid", tp_method="trailing", trailing_activation_pct=Decimal(".02")
        )
        h = Harness(mode=SLTPMode.HYBRID, monitor=monitor)
        h.config.risk.trade.max_loss_pct = Decimal(".05")
        h.ctx.position_tracker.set_pending_signal(Signal.enter_long(price=100), Decimal("2"))
        entry = self._entry_of(h)
        h.position.quantity = Decimal("0.5")
        self._fill(h, entry)

        h.position.quantity = Decimal("1")
        h.position.avg_px_open = Decimal("110")
        self._fill(h, entry)

        assert monitor._entry_price == 100, "the position's basis is its average, not the last lot"
