"""The offline lane reads a strategy's trading parameters from its own config.

The engine host and the strategy used to read connector, pairs and leverage from
two different documents, and they disagreed. They now read the same file, so
these tests pin what that file must say and what happens when it does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custos.offline.strategy_config import read_strategy_trading_config

_CONFIG = """\
strategy:
  name: "Probe"
parameters:
  atr_period:
    value: 10
    type: integer
trading:
  connector:
    value: "binance"
    type: string
  leverage:
    value: 3
    type: integer
  pairs:
    value: ["ETH-USDT", "BTC-USDT"]
    type: array
"""


def _strategy(tmp_path: Path, text: str = _CONFIG) -> Path:
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_trading_parameters_come_from_the_strategy_config(tmp_path: Path) -> None:
    config = read_strategy_trading_config(_strategy(tmp_path))

    assert config.connector == "binance"
    assert config.pairs == ("ETH-USDT", "BTC-USDT")
    assert config.leverage == 3


def test_a_missing_config_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    # The toolkit loader falls back to its own base config when the strategy file
    # is absent, which would quietly trade the base defaults.
    with pytest.raises(ValueError, match="config.yaml"):
        read_strategy_trading_config(tmp_path)


@pytest.mark.parametrize("key", ["connector", "pairs", "leverage"])
def test_each_trading_parameter_must_be_written_by_the_strategy(tmp_path: Path, key: str) -> None:
    # The toolkit base config supplies all three, so a strategy that omits one
    # would otherwise trade binance_perpetual, BTC-USDT or 1x without saying so.
    lines = _CONFIG.splitlines(keepends=True)
    start = lines.index(f"  {key}:\n")
    text = "".join(lines[:start] + lines[start + 3 :])

    with pytest.raises(ValueError, match=f"trading.{key}"):
        read_strategy_trading_config(_strategy(tmp_path, text))


@pytest.mark.parametrize(
    ("replacement", "key"),
    [
        ('    value: ""\n', "connector"),
        ("    value: []\n", "pairs"),
        ("    value: 0\n", "leverage"),
        ("    value: true\n", "leverage"),
        ('    value: "3"\n', "leverage"),
    ],
)
def test_an_unusable_trading_parameter_is_refused(
    tmp_path: Path, replacement: str, key: str
) -> None:
    lines = _CONFIG.splitlines(keepends=True)
    lines[lines.index(f"  {key}:\n") + 1] = replacement

    with pytest.raises(ValueError, match=f"trading.{key}"):
        read_strategy_trading_config(_strategy(tmp_path, "".join(lines)))


def test_the_digest_follows_the_config_content(tmp_path: Path) -> None:
    first = read_strategy_trading_config(_strategy(tmp_path))
    again = read_strategy_trading_config(_strategy(tmp_path))
    changed = read_strategy_trading_config(_strategy(tmp_path, _CONFIG.replace('"ETH-USDT", ', "")))

    assert first.digest == again.digest
    assert changed.digest != first.digest
    assert changed.pairs == ("BTC-USDT",)
