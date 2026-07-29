"""The offline lane's reconcile loop and the observed state it reports.

The consumer waits on that observed state — `deploy/custos/scripts/wait_status.py`
reads `observed_generation`, `phase` and `health` out of the published payload —
so the shape of what is reported is a contract, not a log line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from structlog.testing import capture_logs

from custos.contracts import TradingMode
from custos.offline.mode_guard import OfflineModeRefused
from custos.offline.reconciler import (
    OfflineReconciler,
    Settlement,
    runtime_identity,
    runtime_spec,
)
from custos.offline.spec import OfflineDeploymentMessage, OfflineDeploymentSpec

TENANT = "local"
RUNNER = "ps-supertrend"
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
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }
    document.update(overrides)
    return OfflineDeploymentSpec.model_validate(document)


def _message(spec: OfflineDeploymentSpec) -> bytes:
    return OfflineDeploymentMessage.create(
        tenant_id=TENANT, strategy_id=STRATEGY, spec=spec
    ).to_bytes()


class _FakeEngine:
    def __init__(self, *, supported: tuple[str, ...] = ("sandbox", "testnet")) -> None:
        self.supported = supported
        self.deployed: list[dict[str, Any]] = []
        self.reconfigured: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.deploy_error: Exception | None = None

    async def deploy(self, spec: dict, credential: dict, artifact: Any) -> str:
        if self.deploy_error is not None:
            raise self.deploy_error
        self.deployed.append(spec)
        return f"container-{spec['deployment_instance_id']}"

    async def reconfigure(self, spec: dict) -> None:
        self.reconfigured.append(spec)

    async def stop(self, deployment_instance_id: str) -> None:
        self.stopped.append(deployment_instance_id)

    def supports_trading_mode(self, mode: str) -> bool:
        return mode in self.supported


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    async def __call__(self, subject: str, payload: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.published.append((subject, json.loads(payload)))

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [body["payload"] for _, body in self.published]


def _reconciler(engine: _FakeEngine, publisher: _RecordingPublisher) -> OfflineReconciler:
    return OfflineReconciler(
        tenant_id=TENANT,
        runner_id=RUNNER,
        strategy_id=STRATEGY,
        engine=engine,
        publish=publisher,
        artifact_for=lambda spec: object(),
        credential_for=lambda spec: {"api_key": "k", "api_secret": "s"},
    )


def test_runtime_identity_is_stable_for_a_spec_id() -> None:
    first = runtime_identity(_spec())
    second = runtime_identity(_spec(generation=9))

    assert first.deployment_instance_id == second.deployment_instance_id
    assert first.deployment_spec_id == second.deployment_spec_id


def test_runtime_identity_separates_distinct_specs() -> None:
    assert (
        runtime_identity(_spec()).deployment_instance_id
        != runtime_identity(_spec(spec_id="other-sandbox")).deployment_instance_id
    )


def test_runtime_identity_digest_follows_the_content() -> None:
    assert runtime_identity(_spec()).deployment_spec_digest != (
        runtime_identity(_spec(leverage=5)).deployment_spec_digest
    )


def test_runtime_spec_carries_the_keys_the_engine_host_reads() -> None:
    spec = _spec()
    translated = runtime_spec(spec, runtime_identity(spec))

    for key in (
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
        "trading_mode",
        "lifecycle_state",
    ):
        assert key in translated, f"engine host reads {key}"


async def test_a_running_spec_deploys_and_reports_its_generation() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()

    await _reconciler(engine, publisher).handle(_message(_spec()))

    assert len(engine.deployed) == 1
    (subject, _) = publisher.published[0]
    assert subject == f"arx.{TENANT}.deployment_status.{RUNNER}.supertrend-sandbox"
    assert publisher.payloads[0] == {
        "observed_generation": 1,
        "phase": "running",
        "health": "healthy",
    }


async def test_a_stopped_spec_stops_the_engine_and_says_so() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))

    await reconciler.handle(_message(_spec(generation=2, lifecycle_state="stopped")))

    assert engine.stopped
    assert publisher.payloads[-1] == {
        "observed_generation": 2,
        "phase": "stopped",
        "health": "healthy",
    }


async def test_a_second_generation_reconfigures_rather_than_redeploying() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))

    await reconciler.handle(_message(_spec(generation=2, leverage=5)))

    assert len(engine.deployed) == 1
    assert len(engine.reconfigured) == 1


async def test_an_older_generation_is_ignored_without_touching_the_engine() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec(generation=5)))
    engine.deployed.clear()

    await reconciler.handle(_message(_spec(generation=4)))

    assert engine.deployed == []
    assert engine.reconfigured == []
    assert [payload["observed_generation"] for payload in publisher.payloads] == [5]


async def test_a_redelivered_generation_reports_again_without_reapplying() -> None:
    """At-least-once delivery means the same generation can arrive twice."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))

    await reconciler.handle(_message(_spec()))

    assert len(engine.deployed) == 1
    assert [payload["observed_generation"] for payload in publisher.payloads] == [1, 1]


async def test_a_message_for_another_tenant_is_refused() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    foreign = OfflineDeploymentMessage.create(
        tenant_id="someone-else", strategy_id=STRATEGY, spec=_spec()
    ).to_bytes()

    settlement = await _reconciler(engine, publisher).handle(foreign)

    assert settlement is Settlement.REJECTED
    assert engine.deployed == []


async def test_an_engine_that_cannot_run_the_mode_is_not_asked_to() -> None:
    engine = _FakeEngine(supported=("sandbox",))
    publisher = _RecordingPublisher()

    settlement = await _reconciler(engine, publisher).handle(
        _message(_spec(trading_mode="testnet", sandbox=None))
    )

    assert settlement is Settlement.REJECTED
    assert engine.deployed == []
    assert publisher.payloads[-1]["health"] == "unhealthy"


async def test_a_failed_apply_is_reported_as_unhealthy_and_not_claimed() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    engine.deploy_error = RuntimeError("engine refused to start")

    settlement = await _reconciler(engine, publisher).handle(_message(_spec()))

    assert settlement is Settlement.RETRYABLE
    assert publisher.payloads[-1] == {
        "observed_generation": 1,
        "phase": "running",
        "health": "unhealthy",
    }


async def test_a_failed_apply_can_be_retried_by_the_same_generation() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    engine.deploy_error = RuntimeError("engine refused to start")
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))
    engine.deploy_error = None

    settlement = await reconciler.handle(_message(_spec()))

    assert settlement is Settlement.APPLIED
    assert len(engine.deployed) == 1


async def test_losing_the_status_channel_does_not_stop_a_running_engine() -> None:
    """Red line 0.3: the cloud going quiet is not an instruction to stop trading."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))
    publisher.error = ConnectionError("status transport down")

    await reconciler.handle(_message(_spec(generation=2, leverage=5)))

    assert engine.stopped == []
    assert len(engine.reconfigured) == 1


async def test_the_guard_still_refuses_live_at_the_engine_boundary() -> None:
    """Proves the reconciler's own refusal is live code, not a branch the model shadows.

    A validated spec can never carry live, so the only way to reach this guard is
    to hand the reconciler a spec that skipped validation — which is exactly what
    a future caller building specs another way would do.
    """

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    unvalidated = _spec().model_copy(update={"trading_mode": TradingMode.LIVE})

    with pytest.raises(OfflineModeRefused, match="live"):
        await _reconciler(engine, publisher).apply(unvalidated)

    assert engine.deployed == []


async def test_the_loop_stops_when_asked() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    stop = asyncio.Event()

    class _Subscription:
        async def next_msg(self, timeout: float) -> Any:
            await asyncio.sleep(0)
            raise TimeoutError

    task = asyncio.create_task(_reconciler(engine, publisher).run(_Subscription(), stop))
    await asyncio.sleep(0)
    stop.set()

    await asyncio.wait_for(task, timeout=1)


class _SettleableMessage:
    """A delivery that records how the loop settled it."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = False
        self.naked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, delay: float | None = None) -> None:
        self.naked = True


async def _deliver(reconciler: OfflineReconciler, message: Any) -> None:
    stop = asyncio.Event()
    delivered = [message]

    class _Subscription:
        async def next_msg(self, timeout: float) -> Any:
            if not delivered:
                stop.set()
                raise TimeoutError
            return delivered.pop(0)

    await asyncio.wait_for(reconciler.run(_Subscription(), stop), timeout=1)


async def test_an_applied_generation_is_acknowledged() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    message = _SettleableMessage(_message(_spec()))

    await _deliver(_reconciler(engine, publisher), message)

    assert message.acked and not message.naked


async def test_a_failed_apply_is_not_acknowledged() -> None:
    """Acknowledging a failure tells the stream to forget it; nothing would retry."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    engine.deploy_error = RuntimeError("engine refused to start")
    message = _SettleableMessage(_message(_spec()))

    await _deliver(_reconciler(engine, publisher), message)

    assert message.naked and not message.acked


async def test_an_unreadable_message_is_terminally_rejected() -> None:
    """Safety rule: invalid commands are terminal. Redelivery cannot make them parse."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    message = _SettleableMessage(b"not a deployment message")

    await _deliver(_reconciler(engine, publisher), message)

    assert message.acked and not message.naked


async def test_a_mode_the_engine_cannot_run_is_terminally_rejected() -> None:
    engine, publisher = _FakeEngine(supported=("sandbox",)), _RecordingPublisher()
    message = _SettleableMessage(_message(_spec(trading_mode="testnet", sandbox=None)))

    await _deliver(_reconciler(engine, publisher), message)

    assert message.acked and not message.naked


async def test_a_delivery_that_cannot_be_settled_says_so() -> None:
    """A transport that offers neither ack nor nak leaves the loop unable to settle."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    bare = type("_Bare", (), {"data": _message(_spec())})()

    with capture_logs() as events:
        await _deliver(_reconciler(engine, publisher), bare)

    assert len(engine.deployed) == 1
    assert any(event["event"] == "offline_delivery_not_settled" for event in events), events


async def test_the_loop_survives_a_message_it_cannot_read() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    stop = asyncio.Event()
    delivered = [b"not a deployment message", _message(_spec())]

    class _Subscription:
        async def next_msg(self, timeout: float) -> Any:
            if not delivered:
                stop.set()
                raise TimeoutError
            payload = delivered.pop(0)
            return type("_Msg", (), {"data": payload, "ack": _ack})()

    async def _ack(self: Any) -> None:
        return None

    await asyncio.wait_for(_reconciler(engine, publisher).run(_Subscription(), stop), timeout=1)

    assert len(engine.deployed) == 1
