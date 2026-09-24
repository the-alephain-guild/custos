"""Read the trading parameters an offline deployment runs with.

Connector, pairs and leverage decide which venue client the host builds, which
instruments it loads and what leverage it sets on the exchange; the strategy
reads the same three to decide what it trades and how it checks a stop against
liquidation. Both now read them from the strategy's own ``config.yaml``, so the
two cannot disagree the way a spec field and a config field once did.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from custos.offline.spec import OfflineDeploymentSpec

CONFIG_FILENAME = "config.yaml"
TRADING_KEYS = ("connector", "pairs", "leverage")


@dataclass(frozen=True, slots=True)
class StrategyTradingConfig:
    connector: str
    pairs: tuple[str, ...]
    leverage: int
    digest: str


def read_strategy_trading_config(strategy_path: Path) -> StrategyTradingConfig:
    """Resolve connector, pairs and leverage from ``strategy_path/config.yaml``.

    Each of the three must be written in the strategy's own file. The toolkit
    base config supplies defaults for all of them, and inheriting one would
    trade a venue or a pair the strategy never named.
    """

    config_path = strategy_path / CONFIG_FILENAME
    try:
        content = config_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"strategy config {config_path} is unreadable: {exc}") from exc
    try:
        declared = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"strategy config {config_path} is not valid YAML: {exc}") from exc
    trading = declared.get("trading") if isinstance(declared, dict) else None
    if not isinstance(trading, dict):
        raise ValueError(f"strategy config {config_path} has no trading section")
    for key in TRADING_KEYS:
        if key not in trading:
            raise ValueError(f"strategy config {config_path} does not set trading.{key}")

    # The strategy reads its config through the toolkit loader; reading the same
    # merged view here is what makes the host's values the strategy's values.
    from custos_toolkit.config import load_config

    merged = load_config(config_path).trading
    connector = merged.get("connector")
    pairs = merged.get("pairs")
    leverage = merged.get("leverage")
    if not isinstance(connector, str) or not connector:
        raise ValueError(f"trading.connector in {config_path} must be a non-empty string")
    if (
        not isinstance(pairs, list)
        or not pairs
        or not all(isinstance(pair, str) and pair for pair in pairs)
    ):
        raise ValueError(f"trading.pairs in {config_path} must be a non-empty list of pairs")
    if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
        raise ValueError(f"trading.leverage in {config_path} must be a positive integer")
    return StrategyTradingConfig(
        connector=connector,
        pairs=tuple(pairs),
        leverage=leverage,
        digest=hashlib.sha256(content).hexdigest(),
    )


def strategy_trading_config_for(spec: OfflineDeploymentSpec) -> StrategyTradingConfig:
    return read_strategy_trading_config(Path(spec.strategy_path))
