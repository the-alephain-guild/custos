"""Bulk history completes warmup without replaying bars through live decisions."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators.warmup import WarmupCoordinator  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy  # noqa: E402


def _strategy(*, initialized=True, checkpoints=False):
    indicator = SimpleNamespace(initialized=initialized, handle_bar=Mock())
    config = SimpleNamespace(checkpoints=SimpleNamespace(points=[object()] if checkpoints else []))
    strategy = SimpleNamespace(
        _contexts={"BTC": SimpleNamespace(indicators={"trend": indicator})},
        _warmup_complete_called=False,
        _get_warmup_config=lambda: config,
        on_warmup_complete=Mock(),
        on_bar=Mock(),
        stop=Mock(),
        log=Mock(),
    )
    strategy._warmup_coordinator = WarmupCoordinator(strategy)
    return strategy, indicator


def test_bulk_callback_completes_once_without_reprocessing_indicators_or_live_bars():
    strategy, indicator = _strategy()
    NautilusTradingStrategy.on_historical_bars(strategy, [object()])
    NautilusTradingStrategy.on_historical_bars(strategy, [object()])
    assert strategy._warmup_complete_called
    strategy.on_warmup_complete.assert_called_once_with()
    indicator.handle_bar.assert_not_called()
    strategy.on_bar.assert_not_called()
    strategy.stop.assert_not_called()


@pytest.mark.parametrize("initialized,bars", [(False, [object()]), (True, [])])
def test_empty_or_uninitialized_bulk_history_never_completes_warmup(initialized, bars):
    strategy, _indicator = _strategy(initialized=initialized)
    NautilusTradingStrategy.on_historical_bars(strategy, bars)
    assert strategy._warmup_complete_called is False
    strategy.on_warmup_complete.assert_not_called()


def test_bulk_history_refuses_intermediate_checkpoint_validation_instead_of_using_final_values():
    strategy, _indicator = _strategy(checkpoints=True)
    NautilusTradingStrategy.on_historical_bars(strategy, [object()])
    strategy.stop.assert_called_once_with()
    strategy.log.error.assert_called_once()
    assert strategy._warmup_complete_called is False
    strategy.on_warmup_complete.assert_not_called()


def test_bulk_warmup_completion_failure_stops_the_strategy():
    strategy, _indicator = _strategy()
    strategy.on_warmup_complete.side_effect = RuntimeError("ready callback failed")
    NautilusTradingStrategy.on_historical_bars(strategy, [object()])
    strategy.stop.assert_called_once_with()
    strategy.log.error.assert_called_once()
