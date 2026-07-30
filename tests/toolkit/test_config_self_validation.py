"""Regression cover for validation that lives on the config objects themselves.

The checks used to sit on the coordinator. Now that each config validates itself,
this module guards:
- TradeRiskConfig.__post_init__ refusing an invalid sl_tp_mode
- NautilusTradingStrategyConfig.__post_init__ refusing fixed_risk with no stop
- validation_warnings() returning graded warnings, keeping error/warning/info apart
- the residue guards for checks that were removed rather than moved
"""

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config.risk import (
    SL_TP_MODES,
    TradeRiskConfig,
    build_risk_config,
)

# =============================================================================
# sl_tp_mode is refused in TradeRiskConfig.__post_init__
# =============================================================================


@pytest.mark.parametrize("mode", SL_TP_MODES)
def test_trade_risk_config_accepts_valid_sl_tp_mode(mode):
    assert TradeRiskConfig(sl_tp_mode=mode).sl_tp_mode == mode


@pytest.mark.parametrize("bad", ["bogus", "", "NATIVE_TRAILING", "exchnage"])
def test_trade_risk_config_rejects_invalid_sl_tp_mode(bad):
    with pytest.raises(ValueError, match="sl_tp_mode"):
        TradeRiskConfig(sl_tp_mode=bad)


def test_trade_risk_config_default_is_valid():
    assert TradeRiskConfig().sl_tp_mode == "hybrid"


def test_build_risk_config_rejects_invalid_sl_tp_mode():
    # The live path: a YAML dict through build_risk_config into TradeRiskConfig.
    # Direct construction is not covered by the Literal, so __post_init__ is the gate.
    with pytest.raises(ValueError, match="sl_tp_mode"):
        build_risk_config({"trade": {"sl_tp_mode": "bogus"}})


@pytest.mark.parametrize("mode", SL_TP_MODES)
def test_build_risk_config_accepts_valid_sl_tp_mode(mode):
    assert build_risk_config({"trade": {"sl_tp_mode": mode}}).trade.sl_tp_mode == mode


# =============================================================================
# Top-level config builder shared by the warnings and regression sections below.
# The fixed_risk behaviour itself is in test_fixed_risk_fail_fast.py.
# =============================================================================

from custos_toolkit_nautilus.adapter.config import (  # noqa: E402
    BacktestingConfig,
    FiltersConfig,
    PlatformsConfig,
    PositionConfig,
    RiskConfig,
    SnapshotConfig,
    TradingConfig,
)
from custos_toolkit_nautilus.adapter.config.risk import (  # noqa: E402
    StopLossConfig,
    StopLossFixedConfig,
)
from custos_toolkit_nautilus.adapter.trading_config import (  # noqa: E402
    NautilusTradingStrategyConfig,
)


def _make_config(*, leverage=1, stop_loss=None, snapshot=None, platforms=None):
    kwargs = {
        "trading": TradingConfig(leverage=leverage),
        "position": PositionConfig(),
        "risk": RiskConfig(trade=TradeRiskConfig(stop_loss=stop_loss or StopLossConfig())),
        "filters": FiltersConfig(),
        "platforms": platforms if platforms is not None else PlatformsConfig(),
        "backtesting": BacktestingConfig(),
    }
    if snapshot is not None:
        kwargs["snapshot"] = snapshot
    return NautilusTradingStrategyConfig(**kwargs)


# =============================================================================
# Non-blocking warnings, graded into error / warning / info by validation_warnings()
# =============================================================================


def _fixed_sl(value: float) -> StopLossConfig:
    return StopLossConfig(method="fixed", fixed=StopLossFixedConfig(value=value))


def test_warnings_empty_for_clean_config():
    assert _make_config(leverage=1).validation_warnings() == []


def test_warnings_sl_ge_liquidation_is_error():
    # leverage=10 puts the estimated liquidation distance at 0.1; a fixed stop of
    warnings = _make_config(leverage=10, stop_loss=_fixed_sl(0.15)).validation_warnings()
    assert any(level == "error" and "liquidation" in msg for level, msg in warnings)


def test_warnings_sl_near_liquidation_is_warning():
    # 0.085 falls inside [0.8*0.1, 0.1) — too little buffer, so a warning.
    warnings = _make_config(leverage=10, stop_loss=_fixed_sl(0.085)).validation_warnings()
    assert any(level == "warning" and "buffer" in msg for level, msg in warnings)


def test_warnings_atr_high_leverage_is_info():
    warnings = _make_config(
        leverage=5, stop_loss=StopLossConfig(method="atr")
    ).validation_warnings()
    assert any(level == "info" for level, _ in warnings)


def test_warnings_snapshot_without_db_is_warning():
    warnings = _make_config(snapshot=SnapshotConfig(enabled=True)).validation_warnings()
    assert any(level == "warning" and "database" in msg for level, msg in warnings)


def test_warnings_no_snapshot_warning_when_disabled():
    warnings = _make_config(snapshot=SnapshotConfig(enabled=False)).validation_warnings()
    assert not any("database" in msg for _, msg in warnings)


# Boundary assertions use absolute values rather than ranges or directions.
# leverage=10 gives liq_distance=0.1, so 0.8*liq is 0.08.


def test_warnings_boundary_sl_exactly_liquidation_is_error():
    # sl_pct == liq_distance lands on the ">=" side, so error.
    levels = [
        lvl for lvl, _ in _make_config(leverage=10, stop_loss=_fixed_sl(0.10)).validation_warnings()
    ]
    assert "error" in levels and "warning" not in levels


def test_warnings_boundary_sl_exactly_80pct_is_warning():
    # sl_pct == 0.8*liq_distance lands on ">=", so warning and not error. The threshold
    # must be computed in float like the implementation: 0.8*(1/10) is 0.08000000000000002,
    threshold = 0.8 * (1.0 / 10)
    levels = [
        lvl
        for lvl, _ in _make_config(
            leverage=10, stop_loss=_fixed_sl(threshold)
        ).validation_warnings()
    ]
    assert "warning" in levels and "error" not in levels


def test_warnings_boundary_sl_just_below_80pct_is_silent():
    # Just under 0.8*liq — no stop-versus-liquidation warning at all.
    warnings = _make_config(leverage=10, stop_loss=_fixed_sl(0.079)).validation_warnings()
    assert not any("stop-loss" in msg for _, msg in warnings)


def test_warnings_boundary_leverage_exactly_5_atr_is_info():
    # leverage == 5 is the boundary itself, and ATR there is info.
    levels = [
        lvl
        for lvl, _ in _make_config(
            leverage=5, stop_loss=StopLossConfig(method="atr")
        ).validation_warnings()
    ]
    assert "info" in levels


def test_warnings_leverage_4_atr_is_silent():
    # leverage < 5 with ATR is the other side of the boundary — no info at all.
    warnings = _make_config(
        leverage=4, stop_loss=StopLossConfig(method="atr")
    ).validation_warnings()
    assert warnings == []


# =============================================================================
# Regression and faithfulness guards
# =============================================================================

import inspect  # noqa: E402

from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy  # noqa: E402


def test_subclass_inherits_base_post_init():
    # A subclass is validated too, because __post_init__ runs along the chain.
    class _SubConfig(NautilusTradingStrategyConfig, frozen=True):
        extra: int = 0

    with pytest.raises(ValueError, match="fixed_risk"):
        _SubConfig(
            trading=TradingConfig(),
            position=PositionConfig(size_type="fixed_risk"),
            risk=RiskConfig(trade=TradeRiskConfig(stop_loss=StopLossConfig(method="none"))),
            filters=FiltersConfig(),
            platforms=PlatformsConfig(),
            backtesting=BacktestingConfig(),
            extra=1,
        )


@pytest.mark.parametrize(
    "removed",
    [
        "_get_sl_tp_mode",
        "_warn_sl_tp_mode_fallback",
        "_validate_fixed_risk_has_stop_loss",
        "_validate_sl_vs_liquidation",
        "_validate_snapshot_persistence_active",
    ],
)
def test_lowered_methods_removed_from_strategy(removed):
    # The checks moved onto the configs, so these methods must not linger on the strategy.
    assert not hasattr(NautilusTradingStrategy, removed)


def test_initial_capital_validation_retained():
    # Needs a runtime account balance, so it cannot move down into the config layer.
    # The strategy keeps a thin delegate; the check itself lives in StartupValidator.
    from custos_toolkit_nautilus.adapter.coordinators import StartupValidator

    assert hasattr(NautilusTradingStrategy, "_validate_initial_capital_vs_balance")
    src = inspect.getsource(StartupValidator.validate_startup_config)
    assert "validate_initial_capital_vs_balance" in src
    assert "validation_warnings" in src
