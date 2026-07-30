"""What SandboxSimulationHost says it is holding.

The offline reconciler asks this before choosing between deploying an instance
and reconfiguring it, so a host that answered from anything other than its own
live bookkeeping would put a restarted runner back on the reconfigure path the
real NT host refuses. The same question against NtTradingNodeHost is covered in
tests/test_nt_trading_node_host.py — each host implements it separately, so each
is asked separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from custos.engines.nautilus.host import SandboxSimulationHost


@dataclass(frozen=True, slots=True)
class _Artifact:
    activation_id: str = "activation-test"
    strategy: object = field(default_factory=object)


def _spec(deployment_instance_id: str) -> dict:
    return {
        "deployment_instance_id": deployment_instance_id,
        "deployment_spec_id": str(uuid4()),
        "deployment_spec_digest": "d" * 64,
        "generation": 1,
        "trading_mode": "sandbox",
        "pairs": ["BTC-USDT"],
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }


async def test_a_host_that_has_deployed_nothing_holds_nothing() -> None:
    """This is what a restarted process looks like: the bookkeeping is in memory."""

    assert SandboxSimulationHost().attached(str(uuid4())) is False


async def test_a_deployed_instance_is_held() -> None:
    host = SandboxSimulationHost()
    instance = str(uuid4())

    await host.deploy(_spec(instance), {}, _Artifact())

    assert host.attached(instance) is True


async def test_a_stopped_instance_is_no_longer_held() -> None:
    host = SandboxSimulationHost()
    instance = str(uuid4())
    await host.deploy(_spec(instance), {}, _Artifact())

    await host.stop(instance)

    assert host.attached(instance) is False


async def test_holding_one_instance_is_not_holding_another() -> None:
    """Answered per instance, not as one flag for the host."""

    host = SandboxSimulationHost()
    deployed = str(uuid4())
    await host.deploy(_spec(deployed), {}, _Artifact())

    assert host.attached(str(uuid4())) is False
