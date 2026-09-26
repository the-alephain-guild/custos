"""NtTradingNodeHost — deploy / stop / reconfigure unit tests.

Uses a fake TradingNode (no network, no real NT engine loop) to drive the host
control flow, plus a real self-contained NT strategy fixture for the load path.
The real-NT end-to-end assembly is covered in
test_nt_trading_node_host_integration.py.

Failure-mode contract (plan §failure-mode coverage table):
- NT extra missing -> RuntimeError with install hint (test_deploy_missing_nt_extra_fails_fast)
- TradingNode.build() raises -> nt_startup_failure logged + re-raised
- stop() unknown spec_id -> idempotent no-op
- stop() when stop_async hangs -> timeout forces dispose (nt_stop_timeout)
- reconfigure() structural change -> NotImplementedError
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

import pytest
import structlog

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.binance import BinanceExecutionClientFactory  # noqa: E402
from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory  # noqa: E402
from nautilus_trader.common import Environment  # noqa: E402

from custos.engines.nautilus import host as nautilus_host  # noqa: E402
from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from custos.engines.nautilus.settlement import SettlementCurrencyError  # noqa: E402
from tests.fixtures.fake_live_node import (  # noqa: E402
    FakeBuilder,
    FakeLiveNode,
    FakeLiveNodeType,
    only_exec_client,
)
from tests.fixtures.trading_scope import declare_trading_scope_of  # noqa: E402


def _deployment_instance_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"custos-test-instance:{label}"))


def _deployment_spec_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"custos-test-spec:{label}"))


def _spec(label: str = "spec-1", **overrides) -> dict:
    spec = {
        "deployment_spec_id": _deployment_spec_id(label),
        "deployment_instance_id": _deployment_instance_id(label),
        "deployment_spec_digest": "d" * 64,
        "generation": 1,
        "trading_mode": "sandbox",
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "credential_scope": {
            "scope_id": str(uuid5(NAMESPACE_URL, f"custos-test-scope:{label}")),
            "scope_digest": "c" * 64,
        },
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }
    spec.update(overrides)
    return spec


def _credential() -> dict:
    return {
        "api_key": "test-key",
        "api_secret": "test-secret",
        "permission_scope": "trade_no_withdraw",
    }


class _StrategyDouble:
    """The least a strategy can be and still be deployable.

    2.0 delivers execution events only to these two callbacks, so the host installs
    its event forwarding on them and refuses a strategy without them. A bare object
    is therefore no longer a usable stand-in -- which is the point, and is asserted
    directly by ``test_a_strategy_without_the_event_callbacks_is_refused``.
    """

    def __init__(self, spec: dict | None = None) -> None:
        self.order_events: list = []
        self.position_events: list = []
        # The host refuses a strategy that does not declare what it trades; a
        # double stands in for one trading exactly what its deployment authorizes.
        declare_trading_scope_of(self, spec if spec is not None else _spec())

    def on_order_event(self, event) -> None:
        self.order_events.append(event)

    def on_position_event(self, event) -> None:
        self.position_events.append(event)


@dataclass(frozen=True, slots=True)
class _Artifact:
    activation_id: str = "activation-test"
    strategy: object = field(default_factory=_StrategyDouble)


class _RenewableArtifact:
    activation_id = "activation-renewable"

    def __init__(self) -> None:
        self.instances: list[object] = []

    def create_strategy(self) -> object:
        strategy = _StrategyDouble()
        self.instances.append(strategy)
        return strategy


@pytest.fixture(autouse=True)
def _reset_fake_nodes():
    FakeLiveNode.instances.clear()
    FakeBuilder.instances.clear()
    FakeBuilder.build_raises = False
    yield
    FakeLiveNode.instances.clear()
    FakeBuilder.instances.clear()
    FakeBuilder.build_raises = False


@pytest.mark.asyncio
async def test_deploy_missing_nt_extra_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", None)
    host = NtTradingNodeHost()
    with pytest.raises(RuntimeError, match="nautilus"):
        await host.deploy(_spec(), _credential(), _Artifact())


@pytest.mark.asyncio
async def test_build_failure_records_startup_error(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    monkeypatch.setattr(FakeBuilder, "build_raises", True)
    host = NtTradingNodeHost()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError, match="nt build boom"):
            await host.deploy(_spec(), _credential(), _Artifact())
    assert "nt_startup_failure" in [e.get("event") for e in logs]
    # failed deploy must not leave a registered node
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_deploy_sandbox_success(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    artifact = _Artifact()
    deployment_instance_id = _deployment_instance_id("spec-42")
    container_id = await host.deploy(_spec("spec-42"), _credential(), artifact)
    try:
        assert container_id == deployment_instance_id
        assert deployment_instance_id in host._active_nodes
        node = FakeLiveNode.instances[-1]
        # sandbox exec + binance data clients registered under the venue name
        assert [name for name, _factory, _cfg in node.builder.data_clients] == ["BINANCE"]
        assert only_exec_client(node)[0] == "BINANCE"
        assert node.strategies == [artifact.strategy]
        # A locally matched venue is a sandbox environment with a simulated client;
        # both halves of that have to agree or the node matches somewhere else.
        assert node.builder.environment == Environment.SANDBOX
        assert node.builder.exec_clients == []
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_a_strategy_without_the_event_callbacks_is_refused(monkeypatch) -> None:
    """Red lines 0.2 and 0.3, and 'the reconciliation is never silent'.

    2.0 delivers order and position events to nowhere but the strategy's typed
    callbacks. A strategy that has none of them cannot be observed at all, and the
    artifact it came from is signed rather than subclassed -- so this is refused at
    deploy rather than discovered when the first fill goes unrecorded.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()

    class _NoCallbacks:
        pass

    spec = _spec("no-callbacks")
    with pytest.raises(RuntimeError, match="on_order_event"):
        await host.deploy(
            spec,
            _credential(),
            _Artifact(strategy=declare_trading_scope_of(_NoCallbacks(), spec)),
        )

    assert host._active_nodes == {}
    assert FakeLiveNode.instances[-1].disposed is True


@pytest.mark.asyncio
async def test_deploy_sandbox_uses_sandbox_exec_factory(monkeypatch) -> None:
    # Mode dispatch: sandbox routes to the locally-simulated exec factory, never
    # a real Binance one (regression guard on the mode fan-out).
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("sb-1")
    await host.deploy(_spec("sb-1", trading_mode="sandbox"), _credential(), _Artifact())
    try:
        node = FakeLiveNode.instances[-1]
        # Registered as simulated, which is the shape a locally matched venue takes.
        assert node.builder.exec_clients == []
        assert isinstance(node.builder.simulated_exec_clients[0][1], SandboxExecutionClientFactory)
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_testnet_uses_binance_exec_factory(monkeypatch) -> None:
    # testnet routes to the real Binance exec factory (against the testnet endpoint),
    # not the sandbox simulator.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("tn-1")
    await host.deploy(_spec("tn-1", trading_mode="testnet"), _credential(), _Artifact())
    try:
        node = FakeLiveNode.instances[-1]
        assert node.builder.simulated_exec_clients == []
        assert isinstance(node.builder.exec_clients[0][1], BinanceExecutionClientFactory)
        # A real endpoint, so the node's environment is LIVE even on testnet: which
        # endpoint it is belongs to the adapter's own environment setting.
        assert node.builder.environment == Environment.LIVE
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_live_success_with_owner_evidence(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    spec = _spec(
        "live-ok",
        trading_mode="live",
        promotion_id="44444444-4444-4444-8444-444444444444",
        promotion_evidence_digest="a" * 64,
    )
    deployment_instance_id = spec["deployment_instance_id"]
    with structlog.testing.capture_logs() as logs:
        await host.deploy(spec, _credential(), _Artifact())
    try:
        node = FakeLiveNode.instances[-1]
        assert isinstance(node.builder.exec_clients[0][1], BinanceExecutionClientFactory)
        assert node.builder.environment == Environment.LIVE
        assert "nt_live_deploy_requested" in [e.get("event") for e in logs]
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_live_rejects_missing_owner_evidence(monkeypatch) -> None:
    # Custos verifies the immutable Crucible promotion receipt, not human SoD.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        await host.deploy(_spec("live-bad", trading_mode="live"), _credential(), _Artifact())
    assert FakeLiveNode.instances == []
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_deploy_unknown_trading_mode_rejected(monkeypatch) -> None:
    # An unrecognised trading_mode is refused at dispatch (no silent fallback to a
    # default execution path), before any node is constructed.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    with pytest.raises(ValueError, match="trading mode"):
        await host.deploy(
            _spec("weird-1", trading_mode="paper_trading"), _credential(), _Artifact()
        )
    assert FakeLiveNode.instances == []
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_deploy_does_not_retain_credential(monkeypatch) -> None:
    # non-custodial red line 0.1: credential must not live in host state after deploy.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    cred = _credential()
    deployment_instance_id = _deployment_instance_id("spec-7")
    await host.deploy(_spec("spec-7"), cred, _Artifact())
    try:
        state_blob = repr(host._active_nodes)
        assert "test-key" not in state_blob
        assert "test-secret" not in state_blob
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_stop_idempotent() -> None:
    # Failure-mode contract: stopping an unknown instance is a no-op, not an error.
    host = NtTradingNodeHost()
    with structlog.testing.capture_logs() as logs:
        await host.stop(_deployment_instance_id("never-deployed"))
    assert "nt_stop_noop_unknown_instance" in [e.get("event") for e in logs]


@pytest.mark.asyncio
async def test_stop_timeout_forces_dispose(monkeypatch) -> None:
    """Failure-mode contract: a run that ignores the handle times out, then disposes.

    Stopping a hosted run means asking the handle and waiting for the run task. A
    node that never ends must not hold the reconcile loop open indefinitely, and it
    must still be disposed and dropped from the registry.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    host._stop_timeout_secs = 0.05
    deployment_instance_id = _deployment_instance_id("spec-hang")
    await host.deploy(_spec("spec-hang"), _credential(), _Artifact())
    node = host._active_nodes[deployment_instance_id].node
    node.stop_hangs = True
    with structlog.testing.capture_logs() as logs:
        await host.stop(deployment_instance_id)
    assert "nt_stop_timeout" in [e.get("event") for e in logs]
    assert node.disposed is True
    assert deployment_instance_id not in host._active_nodes


@pytest.mark.asyncio
async def test_failed_start_cleanup_preserves_runner_loop_for_restart_budget(monkeypatch) -> None:
    """Disposing one node must leave the runner's own loop able to run the next.

    In 1.x this needed care: TradingNode.dispose assumed it owned the loop and would
    cancel every task on it, taking the lifecycle supervisor with it before the
    restart budget could schedule another attempt. 2.0 disposal does not reach into
    the host loop, so the guard is now that a restart still works -- asserted by
    running work on the loop after the disposal and then redeploying.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    spec = _spec("retry-after-first-connect-failure", trading_mode="testnet")
    deployment_instance_id = spec["deployment_instance_id"]

    await host.deploy(spec, _credential(), _Artifact())
    failed_node = host._active_nodes[deployment_instance_id].node
    await host.stop(deployment_instance_id)

    assert failed_node.disposed is True
    assert await asyncio.to_thread(lambda: "runner-executor-alive") == "runner-executor-alive"

    restarted_handle = await host.deploy(spec, _credential(), _Artifact())
    try:
        assert restarted_handle == deployment_instance_id
        assert len(FakeLiveNode.instances) == 2
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_restart_requests_fresh_strategy_from_activated_artifact(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    artifact = _RenewableArtifact()
    spec = _spec("retry-with-fresh-strategy", trading_mode="testnet")
    deployment_instance_id = spec["deployment_instance_id"]

    await host.deploy(spec, _credential(), artifact)
    first_node = host._active_nodes[deployment_instance_id].node
    await host.stop(deployment_instance_id)
    await host.deploy(spec, _credential(), artifact)
    second_node = host._active_nodes[deployment_instance_id].node
    try:
        assert len(artifact.instances) == 2
        assert first_node.strategies[0] is artifact.instances[0]
        assert second_node.strategies[0] is artifact.instances[1]
        assert artifact.instances[0] is not artifact.instances[1]
    finally:
        await host.stop(deployment_instance_id)


@dataclass(slots=True)
class _VenuePosition:
    instrument_id: str = "BTCUSDT-PERP.BINANCE"


@dataclass(slots=True)
class _VenueOrder:
    is_reduce_only: bool
    instrument_id: str = "BTCUSDT-PERP.BINANCE"
    client_order_id: str = "O-1"


class _ShutdownAwareStrategy(_StrategyDouble):
    def __init__(self) -> None:
        super().__init__()
        self.node = None
        self.prepared: list[str] = []
        self.cancelled_all: list[str] = []
        self.cancelled: list[_VenueOrder] = []
        self.closed: list[str] = []

    def prepare_shutdown(self, position_policy: str) -> None:
        self.prepared.append(position_policy)

    def cancel_all_orders(self, instrument_id: str) -> None:
        self.cancelled_all.append(instrument_id)
        self.node.cache.orders.clear()

    def cancel_order(self, client_order_id: str) -> None:
        """As strict as 2.0's cancel_order, which takes an id and not an order.

        Accepting an order here is what let the host keep passing one.
        """
        if isinstance(client_order_id, _VenueOrder):
            raise TypeError("argument 'client_order_id': 'Order' object cannot be converted")
        self.cancelled.append(client_order_id)
        self.node.cache.orders[:] = [
            order for order in self.node.cache.orders if order.client_order_id != client_order_id
        ]

    def close_all_positions_with_fallback(self, instrument_id: str) -> None:
        self.closed.append(instrument_id)
        self.node.cache.positions.clear()


@pytest.mark.asyncio
async def test_explicit_flatten_shutdown_confirms_zero_before_dispose(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    strategy = _ShutdownAwareStrategy()
    host = NtTradingNodeHost()
    host._shutdown_poll_secs = 0
    host._shutdown_stable_polls = 1
    spec = _spec(
        "flatten-stop",
        trading_mode="testnet",
        shutdown_policy={
            "schema_version": 1,
            "position_policy": "flatten",
            "confirmation_timeout_secs": 5,
        },
    )
    deployment_instance_id = spec["deployment_instance_id"]
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    node = host._active_nodes[deployment_instance_id].node
    strategy.node = node
    node.cache.positions.append(_VenuePosition())
    node.cache.orders.append(_VenueOrder(is_reduce_only=True))

    await host.stop(deployment_instance_id)

    assert strategy.prepared == ["flatten"]
    assert strategy.cancelled_all == ["BTCUSDT-PERP.BINANCE"]
    assert strategy.closed == ["BTCUSDT-PERP.BINANCE"]
    assert node.cache.positions == []
    assert node.cache.orders == []
    assert node.disposed is True


@pytest.mark.asyncio
async def test_default_preserve_shutdown_keeps_reduce_only_protection(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    strategy = _ShutdownAwareStrategy()
    host = NtTradingNodeHost()
    host._shutdown_poll_secs = 0
    host._shutdown_stable_polls = 1
    spec = _spec("preserve-stop", trading_mode="testnet")
    deployment_instance_id = spec["deployment_instance_id"]
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    node = host._active_nodes[deployment_instance_id].node
    strategy.node = node
    risk_order = _VenueOrder(is_reduce_only=False, client_order_id="O-risk")
    protection = _VenueOrder(is_reduce_only=True, client_order_id="O-protective")
    node.cache.positions.append(_VenuePosition())
    node.cache.orders.extend((risk_order, protection))

    await host.stop(deployment_instance_id)

    assert strategy.prepared == ["preserve"]
    assert strategy.cancelled == ["O-risk"], "2.0 cancels by id, not by order object"
    assert node.cache.positions == [_VenuePosition()]
    assert node.cache.orders == [protection]
    assert node.disposed is True


@pytest.mark.asyncio
async def test_reconfigure_structural_raises() -> None:
    # Failure-mode contract: structural reconfigure is rejected (needs re-deploy).
    host = NtTradingNodeHost()
    with pytest.raises(NotImplementedError, match="re-deploy"):
        await host.reconfigure(_spec("spec-x", connector="binance"))


@pytest.mark.asyncio
async def test_reconfigure_runtime_tunable_logs() -> None:
    # relaxed double: the runtime-tunable branch is a live path, not a dead one.
    host = NtTradingNodeHost()
    spec = {
        "deployment_instance_id": _deployment_instance_id("spec-y"),
        "reconfigure": {"runtime_tunable_only": True, "params": {"leverage": 5}},
    }
    with structlog.testing.capture_logs() as logs:
        await host.reconfigure(spec)
    assert "nt_reconfigure_runtime_tunable" in [e.get("event") for e in logs]


@pytest.mark.asyncio
async def test_exception_log_redacts_credential_material(monkeypatch) -> None:
    # non-custodial red line 0.1: an exception message that could carry credential
    # material must be redacted before it reaches the log.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    monkeypatch.setattr(FakeBuilder, "build_raises", True)
    monkeypatch.setattr(
        FakeBuilder, "build_error_msg", "connection failed with api_key=REAL_SECRET_KEY"
    )
    host = NtTradingNodeHost()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError):
            await host.deploy(_spec(), _credential(), _Artifact())
    startup = [e for e in logs if e.get("event") == "nt_startup_failure"]
    assert startup
    assert "REAL_SECRET_KEY" not in str(startup[0])
    assert "redacted" in startup[0].get("error", "")
    assert startup[0].get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_exception_log_passthrough_when_no_credential(monkeypatch) -> None:
    # Redaction is targeted, not blanket: a benign message is preserved for triage.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    monkeypatch.setattr(FakeBuilder, "build_raises", True)
    monkeypatch.setattr(FakeBuilder, "build_error_msg", "instrument BTCUSDT-PERP.BINANCE not found")
    host = NtTradingNodeHost()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError):
            await host.deploy(_spec(), _credential(), _Artifact())
    startup = [e for e in logs if e.get("event") == "nt_startup_failure"]
    assert startup
    assert startup[0].get("error") == "instrument BTCUSDT-PERP.BINANCE not found"
    assert startup[0].get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_deploy_duplicate_instance_id_raises(monkeypatch) -> None:
    # Re-deploying a live instance is rejected (must stop first), never silently replaced.
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("dup-1")
    await host.deploy(_spec("dup-1"), _credential(), _Artifact())
    try:
        with pytest.raises(RuntimeError, match="already deployed"):
            await host.deploy(_spec("dup-1"), _credential(), _Artifact())
        # original node untouched; the duplicate never constructed a second node
        assert len(FakeLiveNode.instances) == 1
        assert deployment_instance_id in host._active_nodes
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_task_done_callback_cleans_active_entry(monkeypatch) -> None:
    # A self-terminated node run task removes its own registry entry (no stale leak).
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("self-term")
    await host.deploy(_spec("self-term"), _credential(), _Artifact())
    runtime = host._active_nodes[deployment_instance_id]
    runtime.node.request_stop()  # end the run loop without going through stop()
    await runtime.task
    await asyncio.sleep(0.01)  # let the done-callback run
    assert deployment_instance_id not in host._active_nodes


@pytest.mark.asyncio
async def test_missing_strategy_activation_identity_builds_no_node(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    with pytest.raises(RuntimeError, match="activation identity"):
        await host.deploy(
            _spec("leak-check"),
            _credential(),
            _Artifact(activation_id=""),
        )
    assert FakeLiveNode.instances == []
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_a_host_that_has_deployed_nothing_holds_nothing() -> None:
    # No NT needed to answer: this is what the offline reconciler sees on the
    # first message after a restart, and it is why that message deploys.
    assert NtTradingNodeHost().attached(_deployment_instance_id("never-deployed")) is False


@pytest.mark.asyncio
async def test_a_deployed_instance_is_held_until_it_is_stopped(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("held")
    await host.deploy(_spec("held"), _credential(), _Artifact())

    assert host.attached(deployment_instance_id) is True
    assert host.attached(_deployment_instance_id("some-other")) is False

    await host.stop(deployment_instance_id)

    assert host.attached(deployment_instance_id) is False


@pytest.mark.asyncio
async def test_a_node_that_ended_on_its_own_is_no_longer_held(monkeypatch) -> None:
    """The answer tracks the live node, not the authority record beside it.

    A node loop that ends by itself is the case the recorded container id cannot
    describe: nothing called stop, so a host answering from the lifecycle record
    would still claim the instance and send the next generation to reconfigure.
    """

    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("self-terminated")
    await host.deploy(_spec("self-terminated"), _credential(), _Artifact())
    runtime = host._active_nodes[deployment_instance_id]

    runtime.node.request_stop()
    await runtime.task
    await asyncio.sleep(0.01)

    assert host.attached(deployment_instance_id) is False


@pytest.mark.asyncio
async def test_a_finished_node_is_not_held_before_its_callback_runs(monkeypatch) -> None:
    """A done-callback is scheduled, not immediate — the answer cannot wait for it.

    asyncio runs the callback on a later loop iteration, so between the run task
    finishing and the registry being cleaned there is a window where the entry is
    still there. Answering yes in that window tells the reconciler an already
    applied generation is healthy while its node has just exited.
    """

    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("just-finished")
    await host.deploy(_spec("just-finished"), _credential(), _Artifact())
    node = FakeLiveNode.instances[-1]

    node.request_stop()
    await asyncio.sleep(0)

    assert deployment_instance_id in host._active_nodes, "the callback has not run yet"
    assert host.attached(deployment_instance_id) is False


@pytest.mark.asyncio
async def test_a_second_deployment_is_refused_while_one_holds_the_loop(monkeypatch) -> None:
    """2.0 runs one live node per event loop, and the reason is not conservatism.

    The runner's senders and the message bus are both thread-local, so two hosted
    nodes on one loop deliver each other's events rather than fail. Nautilus refuses
    the second run for that reason; the host refuses earlier so the message names the
    deployment that already holds the loop, and so no node is built to be disposed.

    This is a capability reduction against 1.x, where the host tracked several
    instances. Running more than one per runner needs a thread or a process each.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    first = _deployment_instance_id("loop-holder")
    await host.deploy(_spec("loop-holder"), _credential(), _Artifact())

    try:
        with pytest.raises(RuntimeError, match="already holds this runner's event loop"):
            await host.deploy(_spec("loop-contender"), _credential(), _Artifact())
        # Refused before a node was built, so there is nothing to dispose.
        assert len(FakeLiveNode.instances) == 1
        assert list(host._active_nodes) == [first]
    finally:
        await host.stop(first)


@pytest.mark.asyncio
async def test_the_loop_is_free_again_once_the_holder_stops(monkeypatch) -> None:
    """The guard tracks the running node, not a flag someone has to clear."""
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    first = _deployment_instance_id("serial-a")
    second = _deployment_instance_id("serial-b")

    await host.deploy(_spec("serial-a"), _credential(), _Artifact())
    await host.stop(first)
    await host.deploy(_spec("serial-b"), _credential(), _Artifact())

    try:
        assert list(host._active_nodes) == [second]
    finally:
        await host.stop(second)


@pytest.mark.asyncio
async def test_a_sink_that_failed_makes_the_deployment_report_itself_degraded(
    monkeypatch,
) -> None:
    """'The reconciliation is never silent', under a dispatch that discards errors.

    An event that never reached one of the runner's sinks left a fact unrecorded or
    a reservation held. Raising is not an option -- the rust dispatch drops whatever
    a python callback raises -- so the deployment has to stop claiming its own state
    is trustworthy, which is what the reconciler reads.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("sink-failure")
    await host.deploy(_spec("sink-failure"), _credential(), _Artifact())

    try:
        before = await host.get_engine_status(deployment_instance_id)
        assert before.reliable is False  # no portfolio behind the fake, but not for this reason
        assert "runner_event_forwarding" not in (before.unreliable_reason or "")

        host._record_forwarding_failure(deployment_instance_id, "runner_facts", "boom")

        after = await host.get_engine_status(deployment_instance_id)
        assert after.reliable is False
        assert after.phase == "degraded"
        assert after.unreliable_reason == "runner_event_forwarding_failed:boom"
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_the_degraded_mark_does_not_outlive_the_deployment(monkeypatch) -> None:
    """A redeploy is a new deployment, and must not inherit the old one's failure."""
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("degraded-reset")

    await host.deploy(_spec("degraded-reset"), _credential(), _Artifact())
    host._record_forwarding_failure(deployment_instance_id, "runner_facts", "boom")
    await host.stop(deployment_instance_id)

    await host.deploy(_spec("degraded-reset"), _credential(), _Artifact())
    try:
        status = await host.get_engine_status(deployment_instance_id)
        assert "runner_event_forwarding" not in (status.unreliable_reason or "")
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_the_forwarding_is_installed_before_the_node_is_given_the_strategy(
    monkeypatch,
) -> None:
    """Order matters: a strategy the node already holds could receive an event
    before the runner's sinks were attached to it."""
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    strategy = _StrategyDouble()
    installed_when: list[int] = []

    unwired = _StrategyDouble.on_order_event
    real_add_strategy = FakeLiveNode.add_strategy

    def _record_add_strategy(self, added):
        # The wrapper replaces the bound method with a plain function, so a callback
        # that still resolves to the class's own has not been wired yet.
        installed_when.append(getattr(added.on_order_event, "__func__", None) is not unwired)
        real_add_strategy(self, added)

    monkeypatch.setattr(FakeLiveNode, "add_strategy", _record_add_strategy)
    deployment_instance_id = _deployment_instance_id("install-order")
    await host.deploy(_spec("install-order"), _credential(), _Artifact(strategy=strategy))

    try:
        assert installed_when == [True], "the strategy reached the node unwired"
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_stop_asks_the_handle_rather_than_only_cancelling_the_task(monkeypatch) -> None:
    """A hosted run is stopped through its handle, which runs the shutdown sequence.

    Cancelling the run task alone also ends it, and ends it quietly enough that
    every other test here would still pass -- which is why this asserts the request
    was made. The sequence behind the handle is what disconnects clients and drains
    residual events; skipping it would tear the node down mid-flight.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("graceful-stop")
    await host.deploy(_spec("graceful-stop"), _credential(), _Artifact())
    node = FakeLiveNode.instances[-1]

    assert node.handle().stop_requests == 0

    await host.stop(deployment_instance_id)

    assert node.handle().stop_requests == 1


def _sodex_spec(label: str, **overrides) -> dict:
    """A SoDEX deployment. Pairs are the venue's own symbols, not BASE-QUOTE.

    The perps engine lists ``BTC-USD`` and settles in USD, so the sandbox wallet is
    denominated in it; the spot engine lists ``vBTC_vUSDC``. Neither is derivable
    from a canonical pair, which is why the spec carries them verbatim.
    """
    spec = _spec(
        label,
        connector="sodex_perpetual",
        pairs=["BTC-USD"],
        nautilus_config={
            "venue": {
                "wallet_address": "0x" + "a" * 40,
                "sodex_account_id": 4242,
                "settlement_currency": "VUSDC",
            }
        },
        sandbox={"starting_balances": ["10_000 vUSDC"]},
    )
    spec.update(overrides)
    return spec


@pytest.mark.asyncio
async def test_deploy_sodex_sandbox_simulates_execution_against_the_venues_own_feed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    spec = _sodex_spec("sodex-sb")
    await host.deploy(spec, _credential(), _Artifact(strategy=_StrategyDouble(spec)))
    try:
        node = FakeLiveNode.instances[-1]
        # Both clients register under the engine's venue, not the adapter's registry
        # key: one node can hold a spot and a perps client built from one factory.
        assert node.builder.data_clients[0][0] == "SODEX_PERPS"
        assert node.builder.simulated_exec_clients[0][0] == "SODEX_PERPS"
        assert node.builder.exec_clients == []
        assert isinstance(node.builder.simulated_exec_clients[0][1], SandboxExecutionClientFactory)
    finally:
        await host.stop(spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_deploy_sodex_testnet_uses_the_adapters_own_execution_client(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from nautilus_trader.adapters.sodex import SodexExecutionClientFactory

    from custos.engines.nautilus import venue_sodex

    monkeypatch.setattr(venue_sodex, "validate_account_configuration", AsyncMock())

    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    spec = _sodex_spec("sodex-tn", trading_mode="testnet")
    await host.deploy(spec, _credential(), _Artifact(strategy=_StrategyDouble(spec)))
    try:
        node = FakeLiveNode.instances[-1]
        assert node.builder.simulated_exec_clients == []
        assert isinstance(node.builder.exec_clients[0][1], SodexExecutionClientFactory)
        assert node.builder.environment == Environment.LIVE
    finally:
        await host.stop(spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_a_connector_with_no_venue_wiring_is_refused_before_a_node_exists(
    monkeypatch,
) -> None:
    """Admission and assembly must agree on which venues exist.

    Admission answers from a table of strings; this is the other end of it. A
    connector that got past the allow-list with no module behind it has to fail here
    with a message naming the connector, not deep inside a Binance config builder.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host = NtTradingNodeHost()
    before = len(FakeLiveNode.instances)
    with pytest.raises(NotImplementedError, match="unsupported_perpetual"):
        await host.deploy(
            _spec("unsupported-1", connector="unsupported_perpetual"), _credential(), _Artifact()
        )
    assert len(FakeLiveNode.instances) == before


class _FactCapabilityReceipt:
    """The least a capability receipt can be and still bind a deployment."""

    def __init__(self) -> None:
        self.runner_id = uuid5(NAMESPACE_URL, "custos-test-runner")
        self.capability_version_id = uuid5(NAMESPACE_URL, "custos-test-capability")
        self.capability_version = 3
        self.manifest_digest = "c" * 64
        self.capability_manifest: dict = {}
        # The runtime-log emitter refuses an unvalidated binding, and the fact bridge
        # is built during deploy -- so a stand-in has to carry this to reach the code
        # under test at all.
        self.binding_status = "validated"
        self.bindings: dict | None = None

    def require_scope_bindings(self, **bindings) -> None:
        self.bindings = bindings


def _fact_spec(label: str, connector: str, **overrides) -> dict:
    spec = _spec(
        label,
        connector=connector,
        strategy_id=str(uuid5(NAMESPACE_URL, f"custos-test-strategy:{label}")),
    )
    spec.update(overrides)
    return spec


class _FactEmitterStub:
    """Enough of the emitter for the bridge to attach; order attribution starts empty."""

    def remember_order(self, **_attribution) -> None:
        return None

    def recall_orders(self, _deployment_instance_id) -> dict[str, tuple[str, str]]:
        return {}

    def forget_order(self, **_identity) -> None:
        return None


def _fact_host() -> tuple[NtTradingNodeHost, _FactCapabilityReceipt]:
    receipt = _FactCapabilityReceipt()
    host = NtTradingNodeHost(
        tenant_id="tenant-a",
        runner_fact_emitter=_FactEmitterStub(),
        capability_receipt=receipt,
    )
    return host, receipt


@pytest.mark.asyncio
async def test_a_deployments_facts_name_the_venue_it_actually_trades_on(monkeypatch) -> None:
    """The venue reaches the wire twice: as a field and inside the fill event id.

    ``RunnerFactEventBridge`` takes it from the deployment (``runner_fact_producer``
    binds ``venue = self._deployment.venue`` once and uses it for every fill), so a
    deployment that names the wrong venue signs every fill under it. Nothing
    downstream can catch that -- the value is the runner's own claim.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host, _receipt = _fact_host()
    spec = _sodex_spec(
        "facts-sodex",
        strategy_id=str(uuid5(NAMESPACE_URL, "custos-test-strategy:facts-sodex")),
    )
    await host.deploy(spec, _credential(), _Artifact(strategy=_StrategyDouble(spec)))
    try:
        assert [d.venue for d in host.runner_fact_deployments()] == ["SODEX_PERPS"]
    finally:
        await host.stop(spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_a_binance_deployments_facts_still_name_binance(monkeypatch) -> None:
    """The other half: naming the venue truthfully must not rename the old one."""
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host, _receipt = _fact_host()
    spec = _fact_spec("facts-binance", "binance_perpetual")
    await host.deploy(spec, _credential(), _Artifact())
    try:
        assert [d.venue for d in host.runner_fact_deployments()] == ["BINANCE"]
    finally:
        await host.stop(spec["deployment_instance_id"])


@pytest.mark.asyncio
async def test_pairs_with_no_quote_are_refused_as_having_no_settlement_currency(
    monkeypatch,
) -> None:
    """One derivation of the settlement currency, not one per caller.

    ``settlement_currency_for_pairs`` discards an empty quote and reports "these
    pairs settle in none"; a second copy that skipped that step reported the empty
    string as an unsupported currency instead, which sends the reader looking for a
    currency rather than for a malformed pair.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host, _receipt = _fact_host()
    spec = _fact_spec("facts-badpair", "binance_perpetual", pairs=["BTC-"])
    with pytest.raises(SettlementCurrencyError, match="settle in none"):
        await host.deploy(spec, _credential(), _Artifact())


@pytest.mark.asyncio
async def test_the_mode_is_validated_once_and_everything_downstream_reads_that(
    monkeypatch,
) -> None:
    """Why nothing in this host re-normalises the trading mode.

    ``EngineLifecycleAuthority.from_spec`` runs first thing in ``deploy`` and accepts
    only the three exact lower-case names, so by the time the exec plan, the account
    partition or the capability binding read the mode, no other spelling can be
    present. The ``.lower()`` calls that used to sit in front of each of those were
    describing a case that cannot arrive, while a fourth reader compared raw -- and a
    reader cannot tell which of the two was the deliberate one.
    """
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    host, _receipt = _fact_host()
    before = len(FakeLiveNode.instances)
    with pytest.raises(ValueError, match="trading mode is invalid"):
        await host.deploy(
            _fact_spec("facts-mode", "binance_perpetual", trading_mode="LIVE"),
            _credential(),
            _Artifact(),
        )
    assert len(FakeLiveNode.instances) == before
