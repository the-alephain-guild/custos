# tests/test_config_summary_logger.py
"""ConfigSummaryLogger against a mock strategy.

The component formats the configuration log and was lifted off the strategy class. A
SimpleNamespace stands in for the injected strategy, carrying a whole config plus the
reads it delegates. These check that log_config_summary (the configuration as intended)
and log_active_config (the configuration as it ended up) emit the expected number of
log.info calls, with pairs, sl_tp_mode, effective_capital and peak_equity in the text.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.coordinators import ConfigSummaryLogger
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode


def _config(*, pairs=("BTC-USDT",), sl_tp_mode="exchange"):
    return SimpleNamespace(
        trading=SimpleNamespace(
            pairs=list(pairs),
            connector="binance",
            leverage=3,
            enable_long=True,
            enable_short=True,
            order_type="market",
        ),
        position=SimpleNamespace(
            size_type="percentage",
            size_value=0.1,
            capital_mode="compound",
            initial_capital=5000,
            base_size_factor=1.0,
        ),
        risk=SimpleNamespace(
            global_risk=SimpleNamespace(
                max_daily_loss=0.05, max_drawdown=0.2, consecutive_loss_pause=3
            ),
            trade=SimpleNamespace(
                sl_tp_mode=sl_tp_mode,
                atr_period=14,
                max_loss_pct=0.02,  # decimal (0.02 = 2%), matches real config convention
                stop_loss=SimpleNamespace(
                    method="atr",
                    atr=SimpleNamespace(multiplier=2.0),
                    fixed=SimpleNamespace(value=0.01),
                    trailing=SimpleNamespace(enabled=True),
                ),
                take_profit=SimpleNamespace(
                    method="fixed",
                    atr=SimpleNamespace(multiplier=3.0),
                    fixed=SimpleNamespace(value=0.02),
                    scaled=SimpleNamespace(levels=3),
                ),
            ),
        ),
        filters=SimpleNamespace(
            adx_filter=SimpleNamespace(enabled=True, threshold=25),
            volatility_filter=SimpleNamespace(enabled=False, min_atr_pct=0.003),
            volume_filter=SimpleNamespace(enabled=False),
            time_filter=SimpleNamespace(enabled=False, trading_hours=""),
            cooldown=SimpleNamespace(after_exit=0, after_stop_loss=0, after_take_profit=0),
        ),
        snapshot=SimpleNamespace(enabled=False),
    )


def _strategy(*, sl_tp_mode="exchange", warmup_mode="warmup", snapshot_restored=False):
    warmup = SimpleNamespace(mode=warmup_mode, min_bars=100, preferred_bars=200)
    ctx = SimpleNamespace(
        instrument_id=SimpleNamespace(venue="BINANCE"),
        bar_type="BTC-USDT-1H",
        filter_manager=SimpleNamespace(filter_count=1),
    )
    return SimpleNamespace(
        config=_config(sl_tp_mode=sl_tp_mode),
        _get_warmup_config=lambda: warmup,
        _contexts={"BTC-USDT": ctx},
        _risk_controller=SimpleNamespace(peak_equity=Decimal("5000")),
        _get_risk_equity=lambda: Decimal("4800"),
        _get_effective_capital=lambda: Decimal("5000"),
        _global_filter_manager=SimpleNamespace(filter_count=2),
        _mode=SLTPMode(sl_tp_mode),
        _snapshot_restored=snapshot_restored,
        log=MagicMock(),
    )


def _messages(log) -> str:
    return " ".join(str(c.args[0]) for c in log.info.call_args_list)


def test_log_config_summary_emits_expected_lines():
    s = _strategy()
    ConfigSummaryLogger(s).log_config_summary()

    # trading / position / risk.global / risk.trade / filters(adx enabled) / warmup
    assert s.log.info.call_count == 6
    msgs = _messages(s.log)
    assert "pairs=['BTC-USDT']" in msgs
    assert "connector=binance" in msgs
    assert "sl_tp_mode=exchange" in msgs
    assert "size_type=percentage" in msgs
    assert "enabled=['adx']" in msgs
    assert "mode=warmup" in msgs


def test_log_active_config_emits_effective_capital_and_peak():
    s = _strategy()
    ConfigSummaryLogger(s).log_active_config()

    # trading instrument / effective_capital / risk.global / risk.trade / filters / warmup
    assert s.log.info.call_count == 6
    msgs = _messages(s.log)
    assert "effective_capital=5000.00 USDT" in msgs
    assert "sl_tp_mode=exchange" in msgs
    assert "peak_equity=5000.00" in msgs
    # #8: max_loss_pct is a decimal (0.02); the summary must render it as a percent.
    assert "max_loss_pct=2.00%" in msgs


def test_log_active_config_reports_snapshot_restored_when_snapshot_mode():
    s = _strategy(warmup_mode="snapshot", snapshot_restored=True)
    ConfigSummaryLogger(s).log_active_config()

    msgs = _messages(s.log)
    assert "restored_from_snapshot=True" in msgs
