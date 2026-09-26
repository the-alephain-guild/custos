"""A strategy runs only on the trading scope its deployment authorizes.

The exchange clients are built from the deployment's connector, pairs and
leverage; the strategy trades what its own config declares. When the two
differed, the strategy subscribed to an instrument the clients never loaded,
logged ``Instrument not found`` once and sat idle while the runner reported the
deployment ready and online. The host now compares the two before the node is
built and refuses a mismatch as a terminal decision.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.engine_protocol import EngineDeploymentRefused  # noqa: E402
from custos.engines.nautilus import host as nautilus_host  # noqa: E402
from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from tests.fixtures.fake_live_node import FakeLiveNode, FakeLiveNodeType  # noqa: E402
from tests.fixtures.trading_scope import declare_trading_scope  # noqa: E402
from tests.test_nt_trading_node_host import (  # noqa: E402
    _Artifact,
    _credential,
    _spec,
    _StrategyDouble,
)

PERPETUAL = {
    "connector": "binance_perpetual",
    "pairs": ["BTC-USDT"],
    "leverage": 3,
    "instrument_ids": ["BTCUSDT-PERP.BINANCE"],
}


def _strategy(**overrides) -> _StrategyDouble:
    scope = {**PERPETUAL, **overrides}
    return declare_trading_scope(_StrategyDouble(), **scope)  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _fake_node(monkeypatch):
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    FakeLiveNode.instances.clear()
    yield
    FakeLiveNode.instances.clear()


async def _deploy(strategy: object, **spec_overrides):
    host = NtTradingNodeHost()
    spec = _spec(**spec_overrides)
    try:
        return await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    finally:
        await host.stop(spec["deployment_instance_id"])


async def test_a_strategy_on_its_authorized_scope_deploys() -> None:
    await _deploy(_strategy())

    assert len(FakeLiveNode.instances) == 1


async def test_a_perpetual_strategy_on_a_spot_deployment_is_refused() -> None:
    # The acceptance run's case: spot clients, a strategy that trades the perpetual.
    with pytest.raises(EngineDeploymentRefused) as refused:
        await _deploy(_strategy(), connector="binance", leverage=1)

    assert refused.value.reason_code == "strategy_trading_scope_mismatch"
    assert "BTCUSDT-PERP.BINANCE" in str(refused.value)
    assert "BTCUSDT.BINANCE" in str(refused.value)
    assert FakeLiveNode.instances == []


async def test_a_different_leverage_is_refused() -> None:
    with pytest.raises(EngineDeploymentRefused, match="leverage") as refused:
        await _deploy(_strategy(leverage=3), leverage=1)

    assert refused.value.reason_code == "strategy_trading_scope_mismatch"
    assert FakeLiveNode.instances == []


async def test_different_pairs_are_refused() -> None:
    with pytest.raises(EngineDeploymentRefused) as refused:
        await _deploy(_strategy(), pairs=["ETH-USDT"])

    assert refused.value.reason_code == "strategy_trading_scope_mismatch"
    assert "ETHUSDT-PERP.BINANCE" in str(refused.value)
    assert FakeLiveNode.instances == []


class _Undeclared:
    """Has the event callbacks a deployable strategy needs, and no trading scope."""

    def on_order_event(self, event) -> None:
        pass

    def on_position_event(self, event) -> None:
        pass


async def test_a_strategy_that_does_not_declare_its_scope_is_refused() -> None:
    with pytest.raises(EngineDeploymentRefused) as refused:
        await _deploy(_Undeclared())

    assert refused.value.reason_code == "strategy_trading_scope_undeclared"
    assert FakeLiveNode.instances == []


async def test_a_signed_strategy_config_may_not_override_trading() -> None:
    # A trading section in the signed strategy config would be a second source
    # for the same scope, the arrangement this check exists to end.
    with pytest.raises(EngineDeploymentRefused) as refused:
        await _deploy(
            _strategy(),
            strategy_config={"trading": {"connector": "binance_perpetual"}},
        )

    assert refused.value.reason_code == "signed_strategy_config_overrides_trading"
    assert FakeLiveNode.instances == []
