"""fixed_risk PositionConfig.

A new sizing mode `fixed_risk` sizes positions off stop-loss distance via the
native FixedRiskSizer (wired in block 3b). This task adds the config surface:
FixedRiskConfig(risk_pct) + PositionConfig.fixed_risk + build parsing.

risk_pct is a decimal fraction, so 0.01 means 1%, consistent with all
other ratio fields in the codebase.
"""

import pytest

pytest.importorskip("msgspec")

from custos_toolkit_nautilus.adapter.config.position import (  # noqa: E402
    FixedRiskConfig,
    PositionConfig,
    build_position_config,
)


class TestFixedRiskConfig:
    def test_struct_exists_with_risk_pct(self):
        cfg = FixedRiskConfig(risk_pct=0.02)
        assert cfg.risk_pct == 0.02

    def test_struct_is_frozen(self):
        cfg = FixedRiskConfig(risk_pct=0.01)
        with pytest.raises(AttributeError):
            cfg.risk_pct = 0.05  # frozen msgspec.Struct

    def test_position_config_has_fixed_risk_default(self):
        cfg = PositionConfig()
        assert isinstance(cfg.fixed_risk, FixedRiskConfig)


class TestBuildFixedRisk:
    def test_build_parses_fixed_risk(self):
        cfg = build_position_config({"size_type": "fixed_risk", "fixed_risk": {"risk_pct": 0.01}})
        assert cfg.size_type == "fixed_risk"
        assert isinstance(cfg.fixed_risk, FixedRiskConfig)
        assert cfg.fixed_risk.risk_pct == 0.01

    def test_build_without_fixed_risk_uses_default(self):
        cfg = build_position_config({"size_type": "percentage", "size_value": 0.1})
        assert isinstance(cfg.fixed_risk, FixedRiskConfig)

    def test_risk_pct_is_decimal_semantics(self):
        """0.01 is a fraction meaning 1%, not 0.01%."""
        cfg = build_position_config({"fixed_risk": {"risk_pct": 0.015}})
        # 0.015 must round-trip as the decimal fraction (1.5%), not be scaled.
        assert cfg.fixed_risk.risk_pct == 0.015
        assert cfg.fixed_risk.risk_pct < 1.0, "ratio fields are decimals, not percentages"
