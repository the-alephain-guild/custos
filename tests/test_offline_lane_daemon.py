"""Composing the offline lane, and keeping it out of the signed daemon's way.

`--reconcile-strategy-id` selects a different composition rather than a variant of
the signed one. The signed daemon verifies a control plane, loads transport
authorities and publishes RunnerFacts; none of that exists on the operator's own
machine, and stubbing those checks to reuse the path would hollow them out.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from custos.contracts import TradingMode
from custos.core.engine_protocol import EngineStatus
from custos.offline.daemon import run_offline_lane
from custos.offline.mode_guard import OfflineModeRefused
from custos.offline.spec import OfflineDeploymentMessage, OfflineDeploymentSpec
from custos.offline.state import AppliedRecord, OfflineAppliedStore

TENANT = "local"
RUNNER = "ps-supertrend"
RUNNER_ID = "11111111-1111-4111-8111-111111111111"
STRATEGY = "supertrend"


def _spec(**overrides: Any) -> OfflineDeploymentSpec:
    document = {
        "spec_id": "supertrend-sandbox",
        "generation": 1,
        "trading_mode": "sandbox",
        "lifecycle_state": "running",
        "strategy_path": "/opt/ps/trend/supertrend",
        "provenance_ref": {"credential_id": "binance-supertrend"},
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "strategy_registry_name": "supertrend",
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }
    document.update(overrides)
    return OfflineDeploymentSpec.model_validate(document)


class _FakeEngine:
    def __init__(self, open_notional: str = "100") -> None:
        self.deployed: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.flattened: list[tuple[str, str]] = []
        self.flatten_error: Exception | None = None
        self.open_notional = Decimal(open_notional)
        # In-memory like both real hosts: a fresh engine holds nothing.
        self._attached: set[str] = set()

    async def deploy(self, spec: dict, credential: dict, artifact: Any) -> str:
        self.deployed.append(spec)
        self._attached.add(str(spec["deployment_instance_id"]))
        return "container-1"

    async def reconfigure(self, spec: dict) -> None: ...

    async def stop(self, deployment_instance_id: str) -> None:
        self._attached.discard(deployment_instance_id)

    def attached(self, deployment_instance_id: str) -> bool:
        return deployment_instance_id in self._attached

    def supports_trading_mode(self, mode: str) -> bool:
        return mode in {"sandbox", "testnet"}

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.status_calls.append(deployment_instance_id)
        return EngineStatus(
            phase="running",
            position_count=1,
            order_count=0,
            open_notional=self.open_notional,
            peak_equity=Decimal("10000"),
            current_equity=Decimal("10000"),
            drawdown_pct=Decimal("0"),
        )

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        self.flattened.append((deployment_instance_id, reason))
        if self.flatten_error is not None:
            raise self.flatten_error


class _FakeSubscription:
    def __init__(self, messages: list[bytes], stop: asyncio.Event) -> None:
        self._messages = messages
        self._stop = stop

    async def next_msg(self, timeout: float) -> Any:
        if not self._messages:
            self._stop.set()
            raise TimeoutError
        return type("_Msg", (), {"data": self._messages.pop(0)})()


class _FakeJetStream:
    def __init__(self, messages: list[bytes], stop: asyncio.Event) -> None:
        self._messages = messages
        self._stop = stop
        self.subscribed: list[str] = []
        self.published: list[tuple[str, bytes]] = []

    async def subscribe(self, subject: str) -> _FakeSubscription:
        self.subscribed.append(subject)
        return _FakeSubscription(self._messages, self._stop)

    async def publish(self, subject: str, payload: bytes) -> object:
        self.published.append((subject, payload))
        return object()


class _FakeConnection:
    def __init__(self, jetstream: _FakeJetStream) -> None:
        self._jetstream = jetstream
        self.drained = False

    def jetstream(self) -> _FakeJetStream:
        return self._jetstream

    async def drain(self) -> None:
        self.drained = True


async def _run(tmp_path: Path, messages: list[bytes], **overrides: Any) -> dict[str, Any]:
    stop = asyncio.Event()
    jetstream = _FakeJetStream(messages, stop)
    connection = _FakeConnection(jetstream)
    engine = overrides.pop("engine", _FakeEngine())
    overrides.setdefault("credential_for", lambda spec: {"api_key": "k", "api_secret": "s"})

    async def connect(url: str) -> _FakeConnection:
        return connection

    await asyncio.wait_for(
        run_offline_lane(
            tenant_id=TENANT,
            runner_id=RUNNER_ID,
            runner_label=RUNNER,
            strategy_id=STRATEGY,
            nats_url="nats://nats:4222",
            vault_dir=tmp_path / "vault",
            engine=engine,
            ready_file=tmp_path / "ready.json",
            state_path=tmp_path / "state" / "offline.db",
            connect_factory=connect,
            stop=stop,
            **overrides,
        ),
        timeout=2,
    )
    return {"jetstream": jetstream, "connection": connection, "engine": engine}


async def test_subscribes_to_the_strategy_it_was_asked_to_reconcile(tmp_path: Path) -> None:
    result = await _run(tmp_path, [])

    assert result["jetstream"].subscribed == [f"arx.{TENANT}.deployment_spec.{STRATEGY}"]


async def test_applies_desired_state_and_reports_observed_state(tmp_path: Path) -> None:
    message = OfflineDeploymentMessage.create(
        tenant_id=TENANT, strategy_id=STRATEGY, spec=_spec()
    ).to_bytes()

    result = await _run(tmp_path, [message])

    assert len(result["engine"].deployed) == 1
    (subject, payload) = result["jetstream"].published[0]
    assert subject == f"arx.{TENANT}.deployment_status.{RUNNER}.supertrend-sandbox"
    assert json.loads(payload)["payload"]["observed_generation"] == 1


async def test_marks_the_runner_ready_so_the_health_probe_can_pass(tmp_path: Path) -> None:
    """The consumer gates publishing on `arx-runner health`, which reads this file."""

    from custos.core.readiness import ReadinessFile, is_ready_file

    ready_file = tmp_path / "ready.json"
    seen: list[bool] = []

    readiness = ReadinessFile(
        ready_file,
        tenant_id=TENANT,
        runner_id=RUNNER,
        credential_id="cred-1",
        credential_version=1,
        credential_valid_until="2099-01-01T00:00:00Z",
        machine_key_id="key-1",
    )

    class _WatchingSubscription(_FakeSubscription):
        async def next_msg(self, timeout: float) -> Any:
            seen.append(is_ready_file(ready_file))
            return await super().next_msg(timeout)

    stop = asyncio.Event()
    jetstream = _FakeJetStream([], stop)
    jetstream.subscribe = lambda subject: _ready(_WatchingSubscription([], stop))  # type: ignore[method-assign]
    connection = _FakeConnection(jetstream)

    async def connect(url: str) -> _FakeConnection:
        return connection

    await asyncio.wait_for(
        run_offline_lane(
            tenant_id=TENANT,
            runner_id=RUNNER_ID,
            runner_label=RUNNER,
            strategy_id=STRATEGY,
            nats_url="nats://nats:4222",
            vault_dir=tmp_path / "vault",
            engine=_FakeEngine(),
            ready_file=ready_file,
            state_path=tmp_path / "state" / "offline.db",
            readiness=readiness,
            connect_factory=connect,
            credential_for=lambda spec: {},
            stop=stop,
        ),
        timeout=2,
    )

    assert seen and all(seen), "the runner was never ready while it was subscribed"
    assert not ready_file.exists(), "readiness outlived the lane that claimed it"


async def _ready(value: Any) -> Any:
    return value


async def test_drains_the_connection_it_opened(tmp_path: Path) -> None:
    result = await _run(tmp_path, [])

    assert result["connection"].drained


class _BrokenTransport(_FakeJetStream):
    """A JetStream that delivers once and is unreachable from then on."""

    async def subscribe(self, subject: str) -> Any:
        self.subscribed.append(subject)
        return _BrokenAfterFirstMessage(self._messages)

    async def publish(self, subject: str, payload: bytes) -> object:
        raise RuntimeError("the connection to nats is gone")


class _BrokenAfterFirstMessage:
    def __init__(self, messages: list[bytes]) -> None:
        self._messages = messages

    async def next_msg(self, timeout: float) -> Any:
        if not self._messages:
            raise RuntimeError("the connection to nats is gone")
        return type("_Msg", (), {"data": self._messages.pop(0)})()


async def _run_with_broken_transport(
    tmp_path: Path, engine: _FakeEngine, stop: asyncio.Event
) -> None:
    message = OfflineDeploymentMessage.create(
        tenant_id=TENANT, strategy_id=STRATEGY, spec=_spec()
    ).to_bytes()
    connection = _FakeConnection(_BrokenTransport([message], stop))

    async def connect(url: str) -> _FakeConnection:
        return connection

    await asyncio.wait_for(
        run_offline_lane(
            tenant_id=TENANT,
            runner_id=RUNNER_ID,
            runner_label=RUNNER,
            strategy_id=STRATEGY,
            nats_url="nats://nats:4222",
            vault_dir=tmp_path / "vault",
            engine=engine,
            ready_file=tmp_path / "ready.json",
            state_path=tmp_path / "state" / "offline.db",
            connect_factory=connect,
            credential_for=lambda spec: {"api_key": "k", "api_secret": "s"},
            safety_interval=0.001,
            stop=stop,
        ),
        timeout=5,
    )


async def test_the_exposure_tick_outlives_a_transport_that_has_failed(tmp_path: Path) -> None:
    """Red line 0.3: losing the cloud must not also lose the local guard."""

    stop = asyncio.Event()

    class _StopsAfterThreeTicks(_FakeEngine):
        async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
            status = await super().get_engine_status(deployment_instance_id)
            if len(self.status_calls) >= 3:
                stop.set()
            return status

    engine = _StopsAfterThreeTicks()
    await _run_with_broken_transport(tmp_path, engine, stop)

    assert len(engine.status_calls) >= 3, "the guard stopped when the transport did"
    assert engine.flattened == []


async def test_the_lane_ends_rather_than_trading_on_with_a_dead_guard(tmp_path: Path) -> None:
    """A tick that cannot contain a breach is not a tick that may be ignored."""

    engine = _FakeEngine(open_notional="10000")
    engine.flatten_error = RuntimeError("the venue refused the close")

    with pytest.raises(RuntimeError, match="refused the close"):
        await _run_with_broken_transport(tmp_path, engine, asyncio.Event())


def _desired_state(**overrides: Any) -> bytes:
    return OfflineDeploymentMessage.create(
        tenant_id=TENANT, strategy_id=STRATEGY, spec=_spec(**overrides)
    ).to_bytes()


async def test_a_restart_still_refuses_a_generation_it_has_already_passed(
    tmp_path: Path,
) -> None:
    """The applied generation is what survives a restart, and it still decides."""

    await _run(tmp_path, [_desired_state(generation=2)])

    second = await _run(tmp_path, [_desired_state(generation=1)])

    assert second["engine"].deployed == []


async def test_a_restart_redeploys_the_generation_whose_engine_it_no_longer_has(
    tmp_path: Path,
) -> None:
    """The record survives the restart; the engine it describes does not.

    Reporting this generation applied without deploying anything would hand the
    consumer a passing wait-status over a runner that is running no strategy.
    """

    await _run(tmp_path, [_desired_state()])

    second = await _run(tmp_path, [_desired_state()])

    assert len(second["engine"].deployed) == 1


def test_the_applied_store_reports_sqlite_own_verdict(tmp_path: Path) -> None:
    store = OfflineAppliedStore(tmp_path / "state" / "offline.db")

    assert store.quick_check() == "ok"


def test_the_applied_store_round_trips(tmp_path: Path) -> None:
    store = OfflineAppliedStore(tmp_path / "state" / "offline.db")
    store.save("supertrend-sandbox", AppliedRecord(generation=7, container_id="c1"))

    reopened = OfflineAppliedStore(tmp_path / "state" / "offline.db").load()

    assert reopened["supertrend-sandbox"].generation == 7


def test_no_credential_is_read_for_a_mode_the_lane_refuses(tmp_path: Path) -> None:
    """The vault stays closed for a live spec rather than being read and discarded."""

    from custos.offline.daemon import _credential_reader

    read = _credential_reader(tmp_path / "vault", TENANT, RUNNER)
    live = _spec().model_copy(update={"trading_mode": TradingMode.LIVE})

    with pytest.raises(OfflineModeRefused, match="live"):
        read(live)


def _mounted(tmp_path: Path, name: str = "supertrend") -> Any:
    from custos.offline.daemon import BindMountedStrategy

    return BindMountedStrategy(strategy_path=tmp_path / name, registry_name=name, digest="a" * 64)


def _unbound_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a process where nothing has bound strategy discovery yet.

    Other suites import the toolkit registry, and once imported it has already read
    the variable. Without pinning both, these tests would pass or fail on whatever
    ran before them.
    """

    import sys

    monkeypatch.delenv("STRATEGY_INJECT_PATH", raising=False)
    monkeypatch.delitem(sys.modules, "custos_toolkit_nautilus.adapter.registry", raising=False)


def test_the_activation_identity_follows_the_mounted_digest(tmp_path: Path) -> None:
    from custos.offline.daemon import BindMountedStrategy

    one = _mounted(tmp_path)
    other = BindMountedStrategy(
        strategy_path=tmp_path / "supertrend", registry_name="supertrend", digest="b" * 64
    )

    assert one.activation_id != other.activation_id
    assert one.activation_id == _mounted(tmp_path).activation_id


def test_choosing_the_same_directory_twice_is_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unbound_registry(monkeypatch)
    mounted = _mounted(tmp_path)

    mounted.select_discovery_path()
    mounted.select_discovery_path()

    import os

    assert os.environ["STRATEGY_INJECT_PATH"] == str(tmp_path / "supertrend")


def test_a_second_strategy_directory_is_refused_rather_than_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving the first deployment's strategy to the second is silent and wrong."""

    _unbound_registry(monkeypatch)
    _mounted(tmp_path, "first").select_discovery_path()

    with pytest.raises(RuntimeError, match="cannot also serve"):
        _mounted(tmp_path, "second").select_discovery_path()


def test_choosing_after_the_registry_is_imported_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry reads the variable once, at import; setting it later is a no-op."""

    import sys

    monkeypatch.delenv("STRATEGY_INJECT_PATH", raising=False)
    monkeypatch.setitem(sys.modules, "custos_toolkit_nautilus.adapter.registry", object())

    with pytest.raises(RuntimeError, match="imported before"):
        _mounted(tmp_path).select_discovery_path()


def test_start_offers_the_flag_the_consumer_passes() -> None:
    """`deploy/custos/docker-compose.yaml` starts the runner with these flags."""

    import argparse

    from custos.cli.subcommands import start

    parser = argparse.ArgumentParser()
    start.register(parser.add_subparsers())
    parsed = parser.parse_args(
        [
            "start",
            "--nats-url",
            "nats://nats:4222",
            "--reconcile-strategy-id",
            "supertrend-sandbox",
            "--engine",
            "nautilus",
        ]
    )

    assert parsed.reconcile_strategy_id == "supertrend-sandbox"
    assert parsed.nats_url == "nats://nats:4222"


def test_the_signed_composition_is_untouched_without_the_flag() -> None:
    import argparse

    from custos.cli.subcommands import start

    parser = argparse.ArgumentParser()
    start.register(parser.add_subparsers())

    assert parser.parse_args(["start"]).reconcile_strategy_id is None
