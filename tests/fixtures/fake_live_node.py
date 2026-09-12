"""A stand-in for a built NautilusTrader 2.0 LiveNode and its builder.

Shared by the host suites so the assembly is described once. The builder records
what it was asked for rather than performing it; the node parks on an event so a
test can end the run when it wants to, and reports the state a real node would
be in while it does.
"""

from __future__ import annotations

import asyncio

from nautilus_trader.live import NodeState

__all__ = [
    "FakeBuilder",
    "FakeCache",
    "FakeHandle",
    "FakeLiveNode",
    "FakeLiveNodeType",
    "FakePortfolio",
    "only_exec_client",
]


class FakeCache:
    def __init__(self) -> None:
        self.positions: list = []
        self.orders: list = []

    def positions_open(self, instrument_id=None) -> list:
        if instrument_id is None:
            return list(self.positions)
        return [item for item in self.positions if item.instrument_id == instrument_id]

    def orders_open(self, instrument_id=None) -> list:
        if instrument_id is None:
            return list(self.orders)
        return [item for item in self.orders if item.instrument_id == instrument_id]


class FakePortfolio:
    def __init__(self) -> None:
        self.initialized = True


class FakeHandle:
    """The control handle 2.0 hands back, and the only way to stop a hosted run."""

    def __init__(self, node) -> None:
        self._node = node
        self.state = NodeState.IDLE
        # Counted so a test can tell a graceful stop from a bare task cancellation:
        # both end the run, only one of them asked.
        self.stop_requests = 0

    def stop(self) -> None:
        self.stop_requests += 1
        self._node.request_stop()


class FakeLiveNode:
    """Stand-in for a built 2.0 LiveNode: records calls, no network."""

    instances: list = []

    def __init__(self, builder) -> None:
        self.builder = builder
        self.disposed = False
        self.strategies: list = []
        self.cache = FakeCache()
        self.portfolio = FakePortfolio()
        self.stop_hangs = False
        self._handle = FakeHandle(self)
        self._stop = asyncio.Event()
        FakeLiveNode.instances.append(self)

    def handle(self) -> FakeHandle:
        return self._handle

    def add_strategy(self, strategy) -> None:
        self.strategies.append(strategy)

    async def run_async(self) -> None:
        self._handle.state = NodeState.RUNNING
        try:
            await self._stop.wait()
        finally:
            self._handle.state = NodeState.STOPPED

    def request_stop(self) -> None:
        if self.stop_hangs:
            return  # the handle is asked and the run never ends: the timeout path
        self._stop.set()

    def dispose(self) -> None:
        self.disposed = True


class FakeBuilder:
    """Records the assembly rather than performing it."""

    instances: list = []
    build_raises = False
    build_error_msg = "nt build boom"

    def __init__(self, name, trader_id, environment) -> None:
        self.name = name
        self.trader_id = trader_id
        self.environment = environment
        self.logging = None
        self.reconciliation = None
        self.knobs: dict = {}
        self.data_clients: list = []
        self.exec_clients: list = []
        self.simulated_exec_clients: list = []
        FakeBuilder.instances.append(self)

    def with_logging(self, logging):
        self.logging = logging
        return self

    def with_reconciliation(self, reconciliation):
        self.reconciliation = reconciliation
        return self

    def _knob(self, name, value):
        self.knobs[name] = value
        return self

    def with_timeout_connection(self, value):
        return self._knob("timeout_connection", value)

    def with_timeout_reconciliation(self, value):
        return self._knob("timeout_reconciliation", value)

    def with_timeout_portfolio(self, value):
        return self._knob("timeout_portfolio", value)

    def with_timeout_disconnection_secs(self, value):
        return self._knob("timeout_disconnection", value)

    def with_reconciliation_lookback_mins(self, value):
        return self._knob("reconciliation_lookback_mins", value)

    def add_data_client(self, name, factory, config, routing=None):
        self.data_clients.append((name, factory, config))
        return self

    def add_exec_client(self, name, factory, config, routing=None):
        self.exec_clients.append((name, factory, config))
        return self

    def add_simulated_exec_client(self, name, factory, config):
        self.simulated_exec_clients.append((name, factory, config))
        return self

    def build(self) -> FakeLiveNode:
        if FakeBuilder.build_raises:
            raise RuntimeError(FakeBuilder.build_error_msg)
        return FakeLiveNode(self)


class FakeLiveNodeType:
    """Stands in for the LiveNode class the host reaches for."""

    @staticmethod
    def builder(name, trader_id, environment) -> FakeBuilder:
        return FakeBuilder(name, trader_id, environment)


def only_exec_client(node: FakeLiveNode) -> tuple:
    """The single execution client registered, whichever shape it went in as."""
    registered = node.builder.exec_clients + node.builder.simulated_exec_clients
    assert len(registered) == 1, registered
    return registered[0]
