"""fixed_risk sizing with no stop loss is refused when the config is constructed.

The check used to run at on_start. The invariant now sits in
NautilusTradingStrategyConfig.__post_init__, so a config is legal by the time it exists.
fixed_risk sizing derives the position from the stop distance, so a missing stop leaves
"""

import pytest

pytest.importorskip("msgspec")
pytest.importorskip("nautilus_trader")

from custos_toolkit_nautilus.adapter.config import (  # noqa: E402
    BacktestingConfig,
    FiltersConfig,
    PlatformsConfig,
    PositionConfig,
    RiskConfig,
    TradingConfig,
)
from custos_toolkit_nautilus.adapter.config.risk import (  # noqa: E402
    StopLossConfig,
    TradeRiskConfig,
)
from custos_toolkit_nautilus.adapter.trading_config import (
    NautilusTradingStrategyConfig,  # noqa: E402
)
from custos_toolkit_nautilus.adapter.trading_strategy import NautilusTradingStrategy  # noqa: E402


def _config(size_type: str, sl_method: str) -> NautilusTradingStrategyConfig:
    return NautilusTradingStrategyConfig(
        trading=TradingConfig(),
        position=PositionConfig(size_type=size_type),
        risk=RiskConfig(trade=TradeRiskConfig(stop_loss=StopLossConfig(method=sl_method))),
        filters=FiltersConfig(),
        platforms=PlatformsConfig(),
        backtesting=BacktestingConfig(),
    )


def test_construction_raises_when_fixed_risk_lacks_stop_loss():
    with pytest.raises(ValueError, match="fixed_risk"):
        _config("fixed_risk", "none")


def test_construction_ok_when_fixed_risk_has_stop_loss():
    assert _config("fixed_risk", "atr").position.size_type == "fixed_risk"


def test_construction_raises_when_section_is_none():
    """Passing None for a section that is not Optional must be refused at construction.

    msgspec's plain constructor does not type-check, and the fixed_risk invariant
    short-circuits on the non-fixed_risk path without touching risk. So an explicit
    section-is-None check is what makes \"never None\" a runtime guarantee rather than
    """
    with pytest.raises(ValueError, match="risk"):
        NautilusTradingStrategyConfig(
            trading=TradingConfig(),
            position=PositionConfig(size_type="percentage"),  # not fixed_risk, so the old path
            risk=None,  # type: ignore[arg-type]
            filters=FiltersConfig(),
            platforms=PlatformsConfig(),
            backtesting=BacktestingConfig(),
        )


def test_construction_ok_when_not_fixed_risk():
    # percentage sizing does not derive the position from a stop, so a missing stop is fine.
    assert _config("percentage", "none").position.size_type == "percentage"


def test_old_strategy_method_removed():
    # Migration guard: the check moved into config.__post_init__, so the strategy drops it.
    assert not hasattr(NautilusTradingStrategy, "_validate_fixed_risk_has_stop_loss")
