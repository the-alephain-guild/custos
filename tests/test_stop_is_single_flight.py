"""A stop runs once, to its own end, however many callers wait on it.

The offline guard waits ten seconds for a stop and then gives up on it. Giving up
used to cancel the stop itself: the shutdown policy was abandoned half way through,
and the next tick started it again from the top -- preparing the strategy again and
withdrawing again every order it had already withdrawn. A preserve stop has thirty
seconds to confirm, so on a venue that would not answer a cancel the policy never
once ran to its end.

These tests drive the host's real stop against a strategy whose order the venue
does not withdraw until the test says so.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

pytest.importorskip("nautilus_trader")

from custos.engines.nautilus import host as nautilus_host  # noqa: E402
from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from tests.fixtures.fake_live_node import FakeLiveNodeType  # noqa: E402
from tests.test_nt_trading_node_host import (  # noqa: E402
    _Artifact,
    _credential,
    _spec,
    _StrategyDouble,
    _VenueOrder,
)


class _UnansweredVenue(_StrategyDouble):
    """A strategy whose venue takes every cancel and withdraws nothing."""

    def __init__(self, spec: dict) -> None:
        super().__init__(spec)
        self.node = None
        self.prepared: list[str] = []
        self.cancels: list[str] = []

    def prepare_shutdown(self, position_policy: str) -> None:
        self.prepared.append(position_policy)

    def cancel_order(self, client_order_id: str) -> None:
        self.cancels.append(client_order_id)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> NtTradingNodeHost:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    built = NtTradingNodeHost()
    built._shutdown_poll_secs = 0.005  # noqa: SLF001 - fixture wiring
    return built


async def _deployed(
    host: NtTradingNodeHost, label: str, *, timeout_secs: int = 5
) -> tuple[_UnansweredVenue, str]:
    spec = _spec(
        label,
        trading_mode="testnet",
        shutdown_policy={
            "schema_version": 1,
            "position_policy": "preserve",
            "confirmation_timeout_secs": timeout_secs,
        },
    )
    strategy = _UnansweredVenue(spec)
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    instance = spec["deployment_instance_id"]
    strategy.node = host._active_nodes[instance].node  # noqa: SLF001 - fixture wiring
    strategy.node.cache.orders.append(_VenueOrder(is_reduce_only=False, client_order_id="entry"))
    return strategy, instance


async def _until(condition, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_cancel_the_stop(host: NtTradingNodeHost) -> None:
    """The guard's deadline ends its own wait; the stop goes on to its end."""
    strategy, instance = await _deployed(host, "single-flight-waiter")

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(host.stop(instance), timeout=0.05)
    assert host.attached(instance), "the venue still holds the order"

    strategy.node.cache.orders.clear()

    await _until(lambda: not host.attached(instance))
    assert strategy.prepared == ["preserve"]
    assert strategy.cancels == ["entry"]


@pytest.mark.asyncio
async def test_a_second_stop_waits_for_the_first(host: NtTradingNodeHost) -> None:
    """Both callers get the one stop's answer; the policy is not run twice."""
    strategy, instance = await _deployed(host, "single-flight-second")

    first = asyncio.create_task(host.stop_at_boundary(instance))
    await _until(lambda: strategy.prepared)
    second = asyncio.create_task(host.stop_at_boundary(instance))
    await asyncio.sleep(0.05)
    strategy.node.cache.orders.clear()

    first_boundary, second_boundary = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=2
    )

    assert strategy.prepared == ["preserve"]
    assert first_boundary is second_boundary
    assert not host.attached(instance)


@pytest.mark.asyncio
async def test_a_failed_stop_reaches_every_waiter_and_the_next_stop_starts_afresh(
    host: NtTradingNodeHost,
) -> None:
    """The failure is the stop's answer for everyone who waited on it.

    It is not remembered past that: an unconfirmed preserve stop leaves the node
    running, and the next stop has to be able to try again.
    """
    strategy, instance = await _deployed(host, "single-flight-failure", timeout_secs=1)

    waiters = [asyncio.create_task(host.stop(instance)) for _ in range(2)]
    results = await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), timeout=3)

    assert [str(result) for result in results] == ["shutdown preserve confirmation timed out"] * 2
    assert strategy.prepared == ["preserve"]
    assert host.attached(instance)

    strategy.node.cache.orders.clear()
    await asyncio.wait_for(host.stop(instance), timeout=3)

    assert strategy.prepared == ["preserve", "preserve"]
    assert not host.attached(instance)


@pytest.mark.asyncio
async def test_the_residue_of_an_unconfirmed_stop_names_what_is_left(
    host: NtTradingNodeHost,
) -> None:
    """What the lane's shutdown summary reports for a stop that did not finish."""
    strategy, instance = await _deployed(host, "single-flight-residue", timeout_secs=1)
    strategy.node.cache.orders.append(_VenueOrder(is_reduce_only=True, client_order_id="stop"))

    with pytest.raises(RuntimeError, match="preserve confirmation timed out"):
        await host.stop(instance)

    assert host.stop_residue(instance) == {"in_flight": 1}, "protection is not residue"

    strategy.node.cache.orders.clear()
    await host.stop(instance)

    assert host.stop_residue(instance) == {}
