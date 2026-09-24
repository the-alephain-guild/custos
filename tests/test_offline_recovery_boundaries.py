from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from nats.errors import ConnectionClosedError

from custos.offline.daemon import BindMountedStrategy, _artifact_for
from custos.offline.reconciler import Settlement, runtime_identity
from custos.offline.state import OfflineAppliedStore
from tests.test_offline_reconciler import (
    TRADING,
    _FakeEngine,
    _message,
    _reconciler,
    _RecordingPublisher,
    _spec,
)


@pytest.mark.asyncio
async def test_paused_state_stops_the_engine_and_stays_stopped_after_replay(tmp_path):
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    store = OfflineAppliedStore(tmp_path / "applied.sqlite")
    reconciler = _reconciler(engine, publisher, applied_store=store)
    assert await reconciler.apply(_spec()) == Settlement.APPLIED
    paused = _spec(generation=2, lifecycle_state="paused")
    assert await reconciler.apply(paused) == Settlement.APPLIED
    assert not engine.attached(str(runtime_identity(paused, TRADING).deployment_instance_id))
    assert len(engine.deployed) == 1
    assert store.load()[paused.spec_id].generation == 2
    assert await reconciler.apply(paused) == Settlement.APPLIED
    restarted = _reconciler(_FakeEngine(), publisher, applied_store=store)
    assert await restarted.apply(paused) == Settlement.APPLIED
    assert not restarted._engine.deployed
    assert publisher.payloads[-1]["phase"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [False, True])
async def test_delivery_acknowledgement_failure_does_not_end_the_offline_loop(retryable):
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    if retryable:
        engine.deploy_error = RuntimeError("temporary engine refusal")
    reconciler = _reconciler(engine, publisher)
    stop = asyncio.Event()
    deliveries = 0

    class Subscription:
        async def next_msg(self, **_):
            nonlocal deliveries
            deliveries += 1
            current = deliveries

            async def settle(**_):
                if current == 1:
                    raise ConnectionClosedError()
                stop.set()

            return SimpleNamespace(data=_message(_spec()), ack=settle, nak=settle)

    await asyncio.wait_for(reconciler.run(Subscription(), stop), timeout=2)
    assert deliveries == 2
    assert len(engine.deployed) == (0 if retryable else 1)


def test_artifact_factory_reads_only_the_mounted_strategy_config(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader")
    from custos_toolkit_nautilus.adapter import registry

    monkeypatch.setattr(registry, "_STRATEGY_REGISTRY", {})
    monkeypatch.setattr(registry, "_ensure_discovery", lambda: None)
    monkeypatch.setattr(BindMountedStrategy, "select_discovery_path", lambda self: None)

    class Config:
        def __init__(self, parameters, **_):
            self.parameters = parameters

    class Strategy:
        def __init__(self, config):
            self.period = config.parameters["period"]

    registry.register_strategy(
        "config-regression", Strategy, Config, lambda config: config.parameters
    )
    mounted = tmp_path / "config.yaml"
    original = "parameters:\n  period:\n    value: 10\n    type: integer\n"
    mounted.write_text(original)
    # The spec carries no strategy configuration any more, so the parameters
    # the strategy runs with are exactly what its mounted config.yaml says.
    spec = _spec(strategy_path=str(tmp_path), strategy_registry_name="config-regression")
    assert _artifact_for(spec).strategy.period == 10
    assert mounted.read_text() == original
