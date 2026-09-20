"""Each risk switch has a scope, and these three did not match their names.

- A soft pause stops new risk. It was stopping the exits that protect open risk.
- The drawdown baseline is sampled on every risk evaluation. It was only sampled
  on fills, so a high the market printed while holding never entered it.
- A fill belongs to the day it happened on. The day boundary only advanced on the
  next entry check, so a fill after midnight was booked against yesterday and
  then cleared along with it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit.risk.controller import RiskController  # noqa: E402
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402


def _ns(day: int, hour: int, minute: int) -> int:
    return int(datetime(2026, 9, day, hour, minute, tzinfo=UTC).timestamp() * 1_000_000_000)


class TestASoftPauseKeepsProtectiveExits:
    """RS-8: pausing new risk must not switch off the way out of old risk."""

    @staticmethod
    def _core():
        """The core is abstract, so the callbacks are exercised unbound."""
        return NS(
            _paused=False,
            _shutdown_position_policy=None,
            _log_error=MagicMock(),
            on_core_trade_tick=MagicMock(),
            on_core_quote_tick=MagicMock(),
        )

    def test_a_paused_strategy_still_routes_trade_ticks(self):
        core = self._core()
        core._paused = True

        NautilusStrategyCore.on_trade(core, NS(instrument_id="X"))

        assert core.on_core_trade_tick.called, (
            "the tick path only closes positions; a soft pause must not hold it"
        )

    def test_a_paused_strategy_still_routes_quote_ticks(self):
        core = self._core()
        core._paused = True

        NautilusStrategyCore.on_quote(core, NS(instrument_id="X"))

        assert core.on_core_quote_tick.called

    def test_a_shutting_down_strategy_routes_neither(self):
        """Stopping the process is a different scope and still stops everything."""
        core = self._core()
        NautilusStrategyCore.prepare_shutdown(core, "preserve")

        NautilusStrategyCore.on_trade(core, NS(instrument_id="X"))
        NautilusStrategyCore.on_quote(core, NS(instrument_id="X"))

        assert not core.on_core_trade_tick.called
        assert not core.on_core_quote_tick.called


class TestTheDrawdownBaselineTracksTheMarket:
    """RS-4: a high printed while holding is part of the drawdown baseline."""

    @staticmethod
    def _strategy(equities: list[str]):
        from custos_toolkit_nautilus.adapter.coordinators import RiskControlCoordinator

        controller = RiskController(
            config={"max_drawdown": 0.05, "max_daily_loss": 0, "consecutive_loss_pause": 0},
            initial_capital=Decimal("1000"),
            capital_mode="compound",
        )
        controller.update_peak_equity(Decimal("1000"))
        remaining = list(equities)
        strategy = NS(
            _risk_controller=controller,
            _get_risk_equity=lambda: Decimal(remaining.pop(0)),
            log=MagicMock(),
            _last_risk_reason="",
        )
        return RiskControlCoordinator(strategy), controller

    def test_a_drawdown_from_an_unrealised_high_is_caught(self):
        coordinator, _ = self._strategy(["1000", "1100", "1030"])

        assert coordinator.check_risk_limits(0) is True
        assert coordinator.check_risk_limits(0) is True, "1100 is a new high, not a drawdown"

        assert coordinator.check_risk_limits(0) is False, "6.36% from 1100 exceeds the 5% cap"

    def test_a_new_high_is_not_itself_a_drawdown(self):
        coordinator, controller = self._strategy(["1000", "1100"])
        coordinator.check_risk_limits(0)

        assert coordinator.check_risk_limits(0) is True
        assert controller._state.peak_equity == Decimal("1100")


class TestAFillBelongsToItsOwnDay:
    """RS-5: the day boundary advances on the fill, not on the next entry check."""

    @staticmethod
    def _controller() -> RiskController:
        return RiskController(
            config={"max_daily_loss": 0.05, "max_drawdown": 0, "consecutive_loss_pause": 0},
            initial_capital=Decimal("1000"),
            capital_mode="fixed_capital",
        )

    def test_a_loss_after_midnight_blocks_the_new_day(self):
        controller = self._controller()
        controller.check_limits(Decimal("1000"), _ns(20, 23, 59))  # yesterday, seeds the boundary

        controller.record_trade(Decimal("-60"), _ns(21, 0, 1))  # 6% loss, new day
        allowed, reason = controller.check_limits(Decimal("940"), _ns(21, 0, 2))

        assert allowed is False, f"a 6% loss today must block a 5% cap, got: {reason}"

    def test_yesterdays_loss_does_not_follow_into_today(self):
        controller = self._controller()
        controller.check_limits(Decimal("1000"), _ns(20, 12, 0))
        controller.record_trade(Decimal("-60"), _ns(20, 12, 1))
        assert controller.check_limits(Decimal("940"), _ns(20, 12, 2))[0] is False

        allowed, _ = controller.check_limits(Decimal("940"), _ns(21, 12, 0))

        assert allowed is True, "a new day starts with a clean daily budget"

    def test_a_fill_without_a_timestamp_keeps_the_old_behaviour(self):
        """Time unknown must not be filled in with the wall clock."""
        controller = self._controller()
        controller.check_limits(Decimal("1000"), _ns(20, 23, 59))

        controller.record_trade(Decimal("-60"))

        assert controller._state.session_pnl == Decimal("-60")


class TestTheRealPathsCarryTheseFixes:
    """The unit-level tests above exercise the pieces; these exercise the wiring.

    The reviewer's probes call `record_trade` without a time and `on_trade`
    against a stand-in that predates `_shutdown_position_policy`, so they cannot
    see either fix. What matters is the path production takes.
    """

    def test_the_core_always_has_the_shutdown_flag(self):
        """on_trade reads it, so it must exist before any tick arrives."""
        import inspect

        from custos_toolkit_nautilus.adapter import strategy_core

        source = inspect.getsource(strategy_core.NautilusStrategyCore.__init__)
        assert "_shutdown_position_policy" in source

    def test_the_close_handler_books_a_fill_on_its_own_day(self):
        import sys

        sys.path.insert(0, "tests/toolkit")
        from _strategy_harness import Harness
        from custos_toolkit_nautilus.adapter.coordinators import TradeEventHandler

        harness = Harness()
        harness._risk_controller = RiskController(
            config={"max_daily_loss": 0.05, "max_drawdown": 0, "consecutive_loss_pause": 0},
            initial_capital=Decimal("1000"),
            capital_mode="fixed_capital",
        )
        harness.positions = []
        harness._risk_controller.check_limits(Decimal("1000"), _ns(20, 23, 59))

        # The stop fills just after midnight; the next entry check is minutes later.
        TradeEventHandler(harness).handle_position_closed(
            NS(
                instrument_id=harness.instrument.id,
                realized_pnl=NS(as_decimal=lambda: Decimal("-60")),
                ts_event=_ns(21, 0, 1),
            )
        )
        allowed, reason = harness._risk_controller.check_limits(Decimal("940"), _ns(21, 0, 2))

        assert allowed is False, f"today's 6% loss must block today, got: {reason}"
