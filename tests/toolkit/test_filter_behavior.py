# tests/test_filter_behavior.py
"""Behaviour-mode tests for FilterManager.check.

BehaviorConfig (mode / min_score / weights / on_filter_fail / reduce_size_factor)
was fully unconsumed -- check() hardcoded all-mode. These tests lock the three
combine modes and the three failure actions on absolute outcomes:
fake filters with fixed pass/fail feed the real aggregation.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("msgspec")

from custos_toolkit.protocols.filter import FilterResult
from custos_toolkit_nautilus.adapter.config.filters import (
    BehaviorConfig,
    FiltersConfig,
    FilterWeightsConfig,
)


def _fake(name: str, passed: bool, size_factor: float = 1.0):
    """A direction-agnostic filter that returns a fixed result."""
    return SimpleNamespace(
        name=name,
        direction_aware=False,
        check=lambda _bar: FilterResult(passed=passed, size_factor=size_factor),
    )


def _fm(behavior: BehaviorConfig, fakes, cooldown_after_exit: int | None = None):
    from custos_toolkit_nautilus.adapter.config.filters import CooldownConfig
    from custos_toolkit_nautilus.adapter.filter_manager import FilterManager

    cooldown = (
        CooldownConfig(after_exit=cooldown_after_exit)
        if cooldown_after_exit is not None
        else CooldownConfig()
    )
    cfg = FiltersConfig(behavior=behavior, cooldown=cooldown)
    fm = FilterManager(config=cfg, instrument_id=None, scope_filter="all")
    fm._filters = fakes
    fm._initialized = True
    return fm


def _bar(ts: int = 1_000_000_000):
    return SimpleNamespace(ts_event=ts)


# --- mode: all (default / backward compat) ---


def test_all_mode_blocks_on_any_failure():
    fm = _fm(BehaviorConfig(mode="all"), [_fake("adx", True), _fake("regime", False)])
    assert fm.check(_bar()).passed is False


def test_all_mode_passes_when_all_pass():
    fm = _fm(BehaviorConfig(mode="all"), [_fake("adx", True), _fake("regime", True)])
    assert fm.check(_bar()).passed is True


# --- mode: any ---


def test_any_mode_passes_when_one_passes():
    fm = _fm(BehaviorConfig(mode="any"), [_fake("adx", True), _fake("regime", False)])
    assert fm.check(_bar()).passed is True


def test_any_mode_blocks_when_all_fail():
    fm = _fm(BehaviorConfig(mode="any"), [_fake("adx", False), _fake("regime", False)])
    assert fm.check(_bar()).passed is False


# --- mode: weighted ---


def test_weighted_mode_passes_at_min_score():
    # adx weight 0.6 passes, momentum weight 0.4 fails -> score 0.6/1.0 == min_score 0.6
    weights = FilterWeightsConfig(adx_filter=0.6, momentum_filter=0.4)
    fm = _fm(
        BehaviorConfig(mode="weighted", min_score=0.6, weights=weights),
        [_fake("adx", True), _fake("momentum", False)],
    )
    assert fm.check(_bar()).passed is True


def test_weighted_mode_blocks_below_min_score():
    weights = FilterWeightsConfig(adx_filter=0.6, momentum_filter=0.4)
    fm = _fm(
        BehaviorConfig(mode="weighted", min_score=0.7, weights=weights),
        [_fake("adx", True), _fake("momentum", False)],
    )
    assert fm.check(_bar()).passed is False


# --- on_filter_fail ---


def test_on_filter_fail_skip_blocks():
    fm = _fm(
        BehaviorConfig(mode="all", on_filter_fail="skip"),
        [_fake("adx", True), _fake("regime", False)],
    )
    result = fm.check(_bar())
    assert result.passed is False
    assert result.delay_until == 0


def test_on_filter_fail_reduce_size_passes_with_reduced_factor():
    fm = _fm(
        BehaviorConfig(mode="all", on_filter_fail="reduce_size", reduce_size_factor=0.5),
        [_fake("adx", True), _fake("regime", False)],
    )
    result = fm.check(_bar())
    # entry proceeds but at reduced size
    assert result.passed is True
    assert result.size_factor == Decimal("0.5")


def test_on_filter_fail_delay_blocks_and_sets_window():
    fm = _fm(
        BehaviorConfig(mode="all", on_filter_fail="delay"),
        [_fake("adx", True), _fake("regime", False)],
        cooldown_after_exit=300,
    )
    result = fm.check(_bar(ts=1_000_000_000))
    assert result.passed is False
    # delay_until = ts + 300s in ns
    assert result.delay_until == 1_000_000_000 + 300 * 1_000_000_000


# --- coordinator honors a persistent per-pair delay window ---


def test_check_pair_honors_persistent_delay_window():
    """Once a per-pair filter fails with on_filter_fail=delay, later entries stay
    blocked until the window elapses, even if filters would now pass."""
    from custos_toolkit_nautilus.adapter.coordinators import FilterCoordinator

    # Manager returns a delay window on the first (failing) check, then a pass.
    fm = MagicMock()
    fm.check.side_effect = [
        SimpleNamespace(
            passed=False, failed_filters=["regime"], size_factor=Decimal("1"), delay_until=5_000
        ),
        SimpleNamespace(passed=True, failed_filters=[], size_factor=Decimal("1"), delay_until=0),
    ]
    ctx = SimpleNamespace(
        pair="BTC-USDT", filter_manager=fm, size_reduction_factor=1.0, filter_delay_until=0
    )
    strategy = SimpleNamespace(log=MagicMock(), _base_size_factor=1.0)
    coord = FilterCoordinator(strategy)

    # First bar (ts=1000 < 5000 window set): fails, records the window.
    assert coord.check_pair(ctx, _bar(ts=1_000), None) is False
    assert ctx.filter_delay_until == 5_000

    # Second bar still inside the window (ts=4000): blocked WITHOUT re-running filters.
    assert coord.check_pair(ctx, _bar(ts=4_000), None) is False
    assert fm.check.call_count == 1  # second call short-circuited by the window
