"""Owner-declared shutdown position disposition for toolkit strategies."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402
from custos_toolkit_nautilus.adapter.trading_strategy import (  # noqa: E402
    NautilusTradingStrategy,
)


def test_prepare_shutdown_freezes_bar_hygiene_and_new_decisions() -> None:
    strategy = SimpleNamespace(
        _shutdown_position_policy=None,
        _paused=False,
        _on_bar_risk_hygiene=MagicMock(),
        on_core_bar=MagicMock(),
        _log_error=MagicMock(),
    )

    NautilusStrategyCore.prepare_shutdown(strategy, "flatten")
    NautilusStrategyCore.on_bar(strategy, object())

    assert strategy._shutdown_position_policy == "flatten"
    assert strategy._paused is True
    strategy._on_bar_risk_hygiene.assert_not_called()
    strategy.on_core_bar.assert_not_called()


def _stop_stub(tmp_path, position_policy: str):
    instrument_id = "BTCUSDT-PERP.BINANCE"
    context = SimpleNamespace(instrument_id=instrument_id, bar_type="bar-type")
    entry = SimpleNamespace(is_reduce_only=False)
    protection = SimpleNamespace(is_reduce_only=True)
    strategy = SimpleNamespace(
        _shutdown_position_policy=position_policy,
        _contexts={"BTC-USDT": context},
        cache=SimpleNamespace(orders_open=MagicMock(return_value=[entry, protection])),
        cancel_order=MagicMock(),
        cancel_all_orders=MagicMock(),
        unsubscribe_bars=MagicMock(),
        unsubscribe_trades=MagicMock(),
        unsubscribe_quotes=MagicMock(),
        _get_tick_monitoring_config=MagicMock(return_value=None),
        on_strategy_stop=MagicMock(),
        _ready_file=str(tmp_path / "ready"),
        _ready=True,
        log=SimpleNamespace(info=MagicMock(), warning=MagicMock()),
    )
    return strategy, entry, protection


def test_preserve_shutdown_cancels_entry_but_keeps_reduce_only_protection(tmp_path) -> None:
    strategy, entry, _protection = _stop_stub(tmp_path, "preserve")

    NautilusTradingStrategy.on_stop(strategy)

    strategy.cancel_order.assert_called_once_with(entry)
    strategy.cancel_all_orders.assert_not_called()
    assert strategy._ready is False


def test_flatten_shutdown_retains_full_cancel_cleanup(tmp_path) -> None:
    strategy, _entry, _protection = _stop_stub(tmp_path, "flatten")

    NautilusTradingStrategy.on_stop(strategy)

    strategy.cancel_all_orders.assert_called_once_with("BTCUSDT-PERP.BINANCE")
    strategy.cancel_order.assert_not_called()
