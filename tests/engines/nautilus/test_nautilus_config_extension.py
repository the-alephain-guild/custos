"""Assert deploy plumbs the nautilus_config knobs through to the node builder.

ps ``runner.py._create_node_config`` reads timeout_connection / timeout_reconciliation /
timeout_portfolio / timeout_disconnection + reconciliation_lookback_mins from the
strategy config. Custos ships the same knobs through the typed V1 execution config
and passes a verified activated strategy as a separate engine ABI input.

2.0 takes them as builder calls rather than as fields on a config object, so what
is asserted is which calls deploy made. An absent knob is asserted as *no call* --
the default then belongs to nautilus, and restating its value here would pin a
number this repository does not own (lesson C7).

Venue admission is covered by the host capability and lifecycle suites; this
module is intentionally limited to node assembly.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.engine_protocol import EngineLifecycleAuthority  # noqa: E402
from custos.engines.nautilus import host as nautilus_host  # noqa: E402
from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from tests.fixtures.fake_live_node import FakeLiveNode, FakeLiveNodeType  # noqa: E402
from tests.fixtures.minimal_supertrend_strategy import create_strategy  # noqa: E402


def _deployment_instance_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"custos-test-instance:{label}"))


def _deployment_spec_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"custos-test-spec:{label}"))


def _spec(label: str = "cfg-1", **overrides: Any) -> dict:
    spec = {
        "deployment_spec_id": _deployment_spec_id(label),
        "deployment_instance_id": _deployment_instance_id(label),
        "deployment_spec_digest": "d" * 64,
        "generation": 1,
        "trading_mode": "sandbox",
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "sandbox": {"starting_balances": ["10_000 USDT"]},
        "credential_scope": {
            "scope_id": "c0000000-0000-4000-8000-00000000000c",
            "scope_digest": "c" * 64,
        },
    }
    spec.update(overrides)
    return spec


def _identity(spec: dict):
    """The same reading of the spec that ``deploy`` makes, built the same way.

    Hand-writing one here would let this test pass against a normalisation the real
    path never performs.
    """
    return nautilus_host._deployment_identity(  # noqa: SLF001
        spec, EngineLifecycleAuthority.from_spec(spec)
    )


def _credential() -> dict:
    return {
        "api_key": "test-key",
        "api_secret": "test-secret",
        "permission_scope": "trade_no_withdraw",
    }


def _artifact():
    return SimpleNamespace(
        activation_id="activation-cfg-1",
        strategy=create_strategy({}),
    )


async def _teardown(host: NtTradingNodeHost, deployment_instance_id: str) -> None:
    """Drop the parked node without driving a real shutdown."""
    runtime = host._active_nodes.pop(deployment_instance_id, None)
    if runtime is None:
        return
    runtime.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runtime.task


@pytest.fixture(autouse=True)
def _fake_node(monkeypatch: pytest.MonkeyPatch):
    FakeLiveNode.instances.clear()
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    yield
    FakeLiveNode.instances.clear()


def _knobs() -> dict:
    """What deploy asked the builder for, on the node it just built."""
    return FakeLiveNode.instances[-1].builder.knobs


@pytest.mark.asyncio
async def test_nautilus_config_timeouts_plumbed() -> None:
    """spec['nautilus_config'] timeout/reconciliation knobs reach the builder."""
    host = NtTradingNodeHost()
    spec = _spec(
        nautilus_config={
            "timeout_connection": 45.0,
            "timeout_reconciliation": 20.0,
            "timeout_portfolio": 15.0,
            "timeout_disconnection": 12.0,
            "reconciliation_lookback_mins": 720,
        },
    )
    try:
        await host.deploy(spec, _credential(), _artifact())
        assert _knobs() == {
            "timeout_connection": 45.0,
            "timeout_reconciliation": 20.0,
            "timeout_portfolio": 15.0,
            "timeout_disconnection": 12.0,
            "reconciliation_lookback_mins": 720,
        }
    finally:
        await _teardown(host, spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_nautilus_config_defaults_when_key_absent() -> None:
    """No nautilus_config key => nothing is set, so nautilus keeps its own defaults."""
    host = NtTradingNodeHost()
    spec = _spec("cfg-def")
    try:
        await host.deploy(spec, _credential(), _artifact())
        assert _knobs() == {}
    finally:
        await _teardown(host, spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_nautilus_config_partial_dict_uses_defaults_for_missing_keys() -> None:
    """A partial nautilus_config dict sets only the keys it names; the rest are left
    unset, so an operator can bump one knob without restating the whole block."""
    host = NtTradingNodeHost()
    spec = _spec(
        "cfg-partial",
        nautilus_config={"timeout_reconciliation": 25.0},
    )
    try:
        await host.deploy(spec, _credential(), _artifact())
        assert _knobs() == {"timeout_reconciliation": 25.0}
    finally:
        await _teardown(host, spec["deployment_instance_id"])


def test_real_venue_rejects_a_second_active_instance_on_the_same_credential_scope() -> None:
    host = NtTradingNodeHost()
    first = _spec("partition-a", trading_mode="testnet", sandbox=None)
    second = _spec("partition-b", trading_mode="testnet", sandbox=None)

    host._claim_execution_account_partition(first, _identity(first))  # noqa: SLF001

    with pytest.raises(RuntimeError, match="credential scope already has an active"):
        host._claim_execution_account_partition(second, _identity(second))  # noqa: SLF001


def test_sandbox_instances_do_not_claim_a_real_venue_account_partition() -> None:
    host = NtTradingNodeHost()

    sandbox_a = _spec("sandbox-a")
    host._claim_execution_account_partition(  # noqa: SLF001
        sandbox_a, _identity(sandbox_a)
    )
    sandbox_b = _spec("sandbox-b")
    host._claim_execution_account_partition(  # noqa: SLF001
        sandbox_b, _identity(sandbox_b)
    )

    assert host._execution_account_partitions == {}  # noqa: SLF001
