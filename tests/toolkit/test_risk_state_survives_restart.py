"""Risk state has to outlive a restart, or the daily budget refills for free.

The daily loss tally, the consecutive-loss count, the pause deadline and the
equity peak all lived in memory only. The snapshot's global section comes from
`get_snapshot_state()`, a hook for strategy authors that returns an empty dict
by default, so an ordinary on_save/on_load never carried any of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit.risk.controller import RiskController  # noqa: E402


def _ns(day: int, hour: int, minute: int) -> int:
    return int(datetime(2026, 9, day, hour, minute, tzinfo=UTC).timestamp() * 1_000_000_000)


def _blocked_controller() -> RiskController:
    """A controller that has already spent today's loss budget."""
    controller = RiskController(
        config={"max_daily_loss": 0.05, "max_drawdown": 0, "consecutive_loss_pause": 0},
        initial_capital=Decimal("1000"),
        capital_mode="fixed_capital",
    )
    controller.check_limits(Decimal("1000"), _ns(20, 9, 0))
    controller.record_trade(Decimal("-60"), _ns(20, 10, 0))
    assert controller.check_limits(Decimal("940"), _ns(20, 11, 0))[0] is False
    return controller


def _restored_from(controller: RiskController) -> RiskController:
    restored = RiskController(
        config={"max_daily_loss": 0.05, "max_drawdown": 0, "consecutive_loss_pause": 0},
        initial_capital=Decimal("1000"),
        capital_mode="fixed_capital",
    )
    restored.restore_state(controller.export_state())
    return restored


class TestTheDailyBudgetSurvivesARestart:
    def test_a_blocked_controller_is_still_blocked_after_a_restart(self):
        restored = _restored_from(_blocked_controller())

        allowed, reason = restored.check_limits(Decimal("940"), _ns(20, 11, 30))

        assert allowed is False, f"today's budget was already spent, got: {reason}"

    def test_the_next_day_starts_clean(self):
        """Carrying state forward must not carry it past the day boundary."""
        restored = _restored_from(_blocked_controller())

        allowed, _ = restored.check_limits(Decimal("940"), _ns(21, 9, 0))

        assert allowed is True

    def test_the_equity_peak_is_carried(self):
        controller = RiskController(
            config={"max_drawdown": 0.05, "max_daily_loss": 0, "consecutive_loss_pause": 0},
            initial_capital=Decimal("1000"),
            capital_mode="compound",
        )
        controller.update_peak_equity(Decimal("1100"))

        restored = RiskController(
            config={"max_drawdown": 0.05, "max_daily_loss": 0, "consecutive_loss_pause": 0},
            initial_capital=Decimal("1000"),
            capital_mode="compound",
        )
        restored.restore_state(controller.export_state())

        assert restored.check_limits(Decimal("1030"), 0)[0] is False, "6.36% from the carried peak"

    def test_a_pause_deadline_is_carried(self):
        controller = RiskController(
            config={"max_daily_loss": 0, "max_drawdown": 0, "consecutive_loss_pause": 0},
            initial_capital=Decimal("1000"),
            capital_mode="fixed_capital",
        )
        controller.apply_pause(_ns(20, 9, 0))

        restored = _restored_from(controller)

        assert restored.check_limits(Decimal("1000"), _ns(20, 9, 1))[0] is False


class TestTheSnapshotCarriesRiskStateOnItsOwn:
    """The risk section must not ride on the strategy author's hook."""

    def test_the_snapshot_has_a_risk_section_of_its_own(self):
        from custos_toolkit_nautilus.adapter.state_persistence import build_snapshot

        snapshot = build_snapshot(
            {}, {}, "S-multi", 0, risk_state=_blocked_controller().export_state()
        )

        assert "risk" in snapshot
        assert snapshot["risk"], "an empty risk section would restore nothing"

    def test_an_overridden_state_hook_does_not_displace_it(self):
        """get_snapshot_state belongs to the strategy; risk state is not its tenant."""
        from custos_toolkit_nautilus.adapter.state_persistence import build_snapshot

        snapshot = build_snapshot(
            {}, {"prev_trend": 1}, "S-multi", 0, risk_state=_blocked_controller().export_state()
        )

        assert snapshot["global_state"] == {"prev_trend": 1}
        assert "risk" in snapshot and snapshot["risk"]


class TestTheRestoreIsIndependentOfWarmupMode:
    """Risk state must come back whether or not indicator warmup is accelerated.

    The snapshot application path returns early unless warmup mode is "snapshot",
    so restoring risk there would make the daily budget depend on an unrelated
    configuration choice.
    """

    def test_the_restore_happens_when_the_controller_is_built(self):
        import inspect

        from custos_toolkit_nautilus.adapter.coordinators import risk_control

        source = inspect.getsource(risk_control.RiskControlCoordinator.init_risk_controls)
        assert "restore_state" in source, (
            "risk state is restored at construction, before any admission decision"
        )

    def test_the_snapshot_application_path_is_warmup_gated(self):
        """Guards the premise above: that path is not a reliable restore point."""
        import inspect

        from custos_toolkit_nautilus.adapter.coordinators import snapshot as snapshot_module

        source = inspect.getsource(snapshot_module.SnapshotCoordinator.apply_loaded_snapshot)
        assert 'mode != "snapshot"' in source
