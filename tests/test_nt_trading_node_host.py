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

from nautilus_trader.adapters.binance.factories import BinanceLiveExecClientFactory  # noqa: E402
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory  # noqa: E402

from custos.engines.nautilus import host as nautilus_host  # noqa: E402
from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402


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


@dataclass(frozen=True, slots=True)
class _Artifact:
    activation_id: str = "activation-test"
    strategy: object = field(default_factory=object)


class _FakeTrader:
    def __init__(self) -> None:
        self.strategies: list = []

    def add_strategy(self, strategy) -> None:
        self.strategies.append(strategy)


class _FakeMsgBus:
    def __init__(self) -> None:
        self.subscriptions: list = []

    def subscribe(self, topic, handler) -> None:
        self.subscriptions.append((topic, handler))


class _FakeKernel:
    def __init__(self) -> None:
        self.msgbus = _FakeMsgBus()


class _FakeTradingNode:
    """Stand-in for nautilus_trader TradingNode: records calls, no network."""

    instances: list = []

    def __init__(self, config) -> None:
        self.config = config
        self.built = False
        self.disposed = False
        self.data_factories: list = []
        self.exec_factories: list = []
        self.trader = _FakeTrader()
        self.kernel = _FakeKernel()
        self._stop = asyncio.Event()
        self.build_raises = False
        self.build_error_msg = "nt build boom"
        self.stop_hangs = False
        _FakeTradingNode.instances.append(self)

    def add_data_client_factory(self, name, factory) -> None:
        self.data_factories.append((name, factory))

    def add_exec_client_factory(self, name, factory) -> None:
        self.exec_factories.append((name, factory))

    def build(self) -> None:
        if self.build_raises:
            raise RuntimeError(self.build_error_msg)
        self.built = True

    async def run_async(self) -> None:
        await self._stop.wait()

    async def stop_async(self) -> None:
        if self.stop_hangs:
            await asyncio.Event().wait()  # never resolves
        self._stop.set()

    def dispose(self) -> None:
        self.disposed = True


@pytest.fixture(autouse=True)
def _reset_fake_nodes():
    _FakeTradingNode.instances.clear()
    yield
    _FakeTradingNode.instances.clear()


@pytest.mark.asyncio
async def test_deploy_missing_nt_extra_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "TradingNode", None)
    host = NtTradingNodeHost()
    with pytest.raises(RuntimeError, match="nautilus"):
        await host.deploy(_spec(), _credential(), _Artifact())


@pytest.mark.asyncio
async def test_build_failure_records_startup_error(monkeypatch) -> None:
    def _factory(config):
        node = _FakeTradingNode(config)
        node.build_raises = True
        return node

    monkeypatch.setattr(nautilus_host, "TradingNode", _factory)
    host = NtTradingNodeHost()
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError, match="nt build boom"):
            await host.deploy(_spec(), _credential(), _Artifact())
    assert "nt_startup_failure" in [e.get("event") for e in logs]
    # failed deploy must not leave a registered node
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_deploy_sandbox_success(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    artifact = _Artifact()
    deployment_instance_id = _deployment_instance_id("spec-42")
    container_id = await host.deploy(_spec("spec-42"), _credential(), artifact)
    try:
        assert container_id == deployment_instance_id
        assert deployment_instance_id in host._active_nodes
        node = _FakeTradingNode.instances[-1]
        assert node.built is True
        # sandbox exec + binance data factories registered under the venue name
        assert [n for n, _ in node.exec_factories] == ["BINANCE"]
        assert [n for n, _ in node.data_factories] == ["BINANCE"]
        assert node.trader.strategies == [artifact.strategy]
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_sandbox_uses_sandbox_exec_factory(monkeypatch) -> None:
    # Mode dispatch: sandbox routes to the locally-simulated exec factory, never
    # a real Binance one (regression guard on the mode fan-out).
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("sb-1")
    await host.deploy(_spec("sb-1", trading_mode="sandbox"), _credential(), _Artifact())
    try:
        node = _FakeTradingNode.instances[-1]
        assert node.exec_factories[0][1] is SandboxLiveExecClientFactory
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_testnet_uses_binance_exec_factory(monkeypatch) -> None:
    # testnet routes to the real Binance exec factory (against the testnet endpoint),
    # not the sandbox simulator.
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("tn-1")
    await host.deploy(_spec("tn-1", trading_mode="testnet"), _credential(), _Artifact())
    try:
        node = _FakeTradingNode.instances[-1]
        assert node.exec_factories[0][1] is BinanceLiveExecClientFactory
        assert node.exec_factories[0][1] is not SandboxLiveExecClientFactory
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_live_success_with_owner_evidence(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
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
        node = _FakeTradingNode.instances[-1]
        assert node.exec_factories[0][1] is BinanceLiveExecClientFactory
        assert "nt_live_deploy_requested" in [e.get("event") for e in logs]
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_deploy_live_rejects_missing_owner_evidence(monkeypatch) -> None:
    # Custos verifies the immutable Crucible promotion receipt, not human SoD.
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    with pytest.raises(RuntimeError, match="live_owner_evidence_missing"):
        await host.deploy(_spec("live-bad", trading_mode="live"), _credential(), _Artifact())
    assert _FakeTradingNode.instances == []
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_deploy_unknown_trading_mode_rejected(monkeypatch) -> None:
    # An unrecognised trading_mode is refused at dispatch (no silent fallback to a
    # default execution path), before any node is constructed.
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    with pytest.raises(ValueError, match="trading mode"):
        await host.deploy(
            _spec("weird-1", trading_mode="paper_trading"), _credential(), _Artifact()
        )
    assert _FakeTradingNode.instances == []
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_deploy_does_not_retain_credential(monkeypatch) -> None:
    # non-custodial red line 0.1: credential must not live in host state after deploy.
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
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
    # Failure-mode contract: a hung stop_async times out, then dispose is forced.
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    host._stop_timeout_secs = 0.05
    deployment_instance_id = _deployment_instance_id("spec-hang")
    await host.deploy(_spec("spec-hang"), _credential(), _Artifact())
    node = host._active_nodes[deployment_instance_id][0]
    node.stop_hangs = True
    with structlog.testing.capture_logs() as logs:
        await host.stop(deployment_instance_id)
    assert "nt_stop_timeout" in [e.get("event") for e in logs]
    assert node.disposed is True
    assert deployment_instance_id not in host._active_nodes


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
    def _factory(config):
        node = _FakeTradingNode(config)
        node.build_raises = True
        node.build_error_msg = "connection failed with api_key=REAL_SECRET_KEY"
        return node

    monkeypatch.setattr(nautilus_host, "TradingNode", _factory)
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
    def _factory(config):
        node = _FakeTradingNode(config)
        node.build_raises = True
        node.build_error_msg = "instrument BTCUSDT-PERP.BINANCE not found"
        return node

    monkeypatch.setattr(nautilus_host, "TradingNode", _factory)
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
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("dup-1")
    await host.deploy(_spec("dup-1"), _credential(), _Artifact())
    try:
        with pytest.raises(RuntimeError, match="already deployed"):
            await host.deploy(_spec("dup-1"), _credential(), _Artifact())
        # original node untouched; the duplicate never constructed a second node
        assert len(_FakeTradingNode.instances) == 1
        assert deployment_instance_id in host._active_nodes
    finally:
        await host.stop(deployment_instance_id)


@pytest.mark.asyncio
async def test_task_done_callback_cleans_active_entry(monkeypatch) -> None:
    # A self-terminated node run task removes its own registry entry (no stale leak).
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("self-term")
    await host.deploy(_spec("self-term"), _credential(), _Artifact())
    node, task = host._active_nodes[deployment_instance_id]
    node._stop.set()  # end the run loop without going through stop()
    await task
    await asyncio.sleep(0.01)  # let the done-callback run
    assert deployment_instance_id not in host._active_nodes


@pytest.mark.asyncio
async def test_missing_strategy_activation_identity_builds_no_node(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    with pytest.raises(RuntimeError, match="activation identity"):
        await host.deploy(
            _spec("leak-check"),
            _credential(),
            _Artifact(activation_id=""),
        )
    assert _FakeTradingNode.instances == []
    assert host._active_nodes == {}


@pytest.mark.asyncio
async def test_a_host_that_has_deployed_nothing_holds_nothing() -> None:
    # No NT needed to answer: this is what the offline reconciler sees on the
    # first message after a restart, and it is why that message deploys.
    assert NtTradingNodeHost().attached(_deployment_instance_id("never-deployed")) is False


@pytest.mark.asyncio
async def test_a_deployed_instance_is_held_until_it_is_stopped(monkeypatch) -> None:
    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
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

    monkeypatch.setattr(nautilus_host, "TradingNode", _FakeTradingNode)
    host = NtTradingNodeHost()
    deployment_instance_id = _deployment_instance_id("self-terminated")
    await host.deploy(_spec("self-terminated"), _credential(), _Artifact())
    node, task = host._active_nodes[deployment_instance_id]

    node._stop.set()
    await task
    await asyncio.sleep(0.01)

    assert host.attached(deployment_instance_id) is False
