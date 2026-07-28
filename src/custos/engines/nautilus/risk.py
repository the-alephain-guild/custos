"""Pure local Nautilus risk configuration.

Risk policy is owned and signed by the control plane. Custos compiles the accepted
DeploymentSpec policy into local engine limits and never publishes an ARX
business event from the trade path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PreTradeRuleConfig:
    rule_id: str
    strategy_id: str | None
    symbol: str | None
    max_qty: Decimal
    max_notional: Decimal
    notional_ccy: str
    price_collar_bps: int
    dedup_window_ms: int

    @classmethod
    def from_dict(cls, raw: dict) -> PreTradeRuleConfig:
        return cls(
            rule_id=str(raw["rule_id"]),
            strategy_id=raw.get("strategy_id"),
            symbol=raw.get("symbol"),
            max_qty=Decimal(str(raw["max_qty"])),
            max_notional=Decimal(str(raw["max_notional"])),
            notional_ccy=str(raw["notional_ccy"]),
            price_collar_bps=int(raw["price_collar_bps"]),
            dedup_window_ms=int(raw["dedup_window_ms"]),
        )


def build_nt_risk_engine_config(rules: list[PreTradeRuleConfig]) -> dict:
    """Compile signed policy rows into deterministic local NT limits."""
    max_notionals: dict[str, str] = {}
    local_rules: list[dict] = []
    for rule in sorted(rules, key=lambda value: value.rule_id):
        max_notionals[rule.symbol or "*"] = str(rule.max_notional)
        local_rules.append(
            {
                "rule_id": rule.rule_id,
                "symbol": rule.symbol,
                "strategy_id": rule.strategy_id,
                "max_qty": str(rule.max_qty),
                "max_notional": str(rule.max_notional),
                "notional_ccy": rule.notional_ccy,
                "price_collar_bps": rule.price_collar_bps,
                "dedup_window_ms": rule.dedup_window_ms,
            }
        )
    return {
        "bypass": False,
        "max_notionals_per_order": max_notionals,
        "local_pre_trade": local_rules,
    }
