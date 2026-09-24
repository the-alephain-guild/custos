"""The offline lane's reconcile loop and the observed state it reports.

The consumer waits on that observed state — `deploy/custos/scripts/wait_status.py`
reads `observed_generation`, `phase` and `health` out of the published payload —
so the shape of what is reported is a contract, not a log line.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

from custos.contracts import TradingMode
from custos.core.engine_protocol import EngineStatus
from custos.offline.mode_guard import OfflineModeRefused
from custos.offline.reconciler import (
    OfflineReconciler,
    Settlement,
    runtime_identity,
    runtime_spec,
)
from custos.offline.safety import OfflineExposureGuard
from custos.offline.spec import OfflineDeploymentMessage, OfflineDeploymentSpec
from custos.offline.state import AppliedRecord
from custos.offline.strategy_config import StrategyTradingConfig

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
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }
    document.update(overrides)
    return OfflineDeploymentSpec.model_validate(document)


# What the strategy's config.yaml resolves to; the spec no longer carries it.
TRADING = StrategyTradingConfig(
    connector="binance_perpetual", pairs=("BTC-USDT",), leverage=3, digest="a" * 64
)


def _message(spec: OfflineDeploymentSpec) -> bytes:
    return OfflineDeploymentMessage.create(
        tenant_id=TENANT, strategy_id=STRATEGY, spec=spec
    ).to_bytes()


class _FakeEngine:
    def __init__(
        self,
        *,
        supported: tuple[str, ...] = ("sandbox", "testnet"),
        open_notional: str = "100",
    ) -> None:
        self.supported = supported
        self.deployed: list[dict[str, Any]] = []
        self.reconfigured: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.flattened: list[tuple[str, str]] = []
        self.deploy_error: Exception | None = None
        self.open_notional = Decimal(open_notional)
        # Attachment is process-scoped in both real hosts, so a freshly built
        # fake is attached to nothing — exactly what a restart looks like.
        self._attached: set[str] = set()

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
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

    def forget_what_it_was_asked_to_do(self) -> None:
        self.deployed.clear()
        self.reconfigured.clear()
        self.stopped.clear()

    async def deploy(self, spec: dict, credential: dict, artifact: Any) -> str:
        if self.deploy_error is not None:
            raise self.deploy_error
        self.deployed.append(spec)
        self._attached.add(str(spec["deployment_instance_id"]))
        return f"container-{spec['deployment_instance_id']}"

    async def reconfigure(self, spec: dict) -> None:
        self.reconfigured.append(spec)

    async def stop(self, deployment_instance_id: str) -> None:
        self.stopped.append(deployment_instance_id)
        self._attached.discard(deployment_instance_id)

    def attached(self, deployment_instance_id: str) -> bool:
        return deployment_instance_id in self._attached

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


class _WhatThePreviousProcessWrote:
    """An applied store already holding a record written before a restart."""

    def __init__(self, spec_id: str, record: AppliedRecord) -> None:
        self.records = {spec_id: record}

    def load(self) -> dict[str, AppliedRecord]:
        return dict(self.records)

    def save(self, spec_id: str, record: AppliedRecord) -> None:
        self.records[spec_id] = record


def _reconciler(
    engine: _FakeEngine,
    publisher: _RecordingPublisher,
    guard: OfflineExposureGuard | None = None,
    applied_store: _WhatThePreviousProcessWrote | None = None,
    strategy_config_for: Any = None,
) -> OfflineReconciler:
    return OfflineReconciler(
        tenant_id=TENANT,
        runner_label=RUNNER,
        strategy_id=STRATEGY,
        engine=engine,
        publish=publisher,
        artifact_for=lambda spec: object(),
        credential_for=lambda spec: {"api_key": "k", "api_secret": "s"},
        strategy_config_for=strategy_config_for or (lambda spec: TRADING),
        applied_store=applied_store,
        guard=guard,
    )


def _after_a_restart(
    engine: _FakeEngine,
    publisher: _RecordingPublisher,
    *,
    generation: int,
) -> OfflineReconciler:
    """A reconciler that loaded a record naming a container this process never built."""

    return _reconciler(
        engine,
        publisher,
        applied_store=_WhatThePreviousProcessWrote(
            "supertrend-sandbox",
            AppliedRecord(generation=generation, container_id="container-from-the-last-process"),
        ),
    )


def test_runtime_identity_is_stable_for_a_spec_id() -> None:
    first = runtime_identity(_spec(), TRADING)
    second = runtime_identity(_spec(generation=9), TRADING)

    assert first.deployment_instance_id == second.deployment_instance_id
    assert first.deployment_spec_id == second.deployment_spec_id


def test_runtime_identity_separates_distinct_specs() -> None:
    assert (
        runtime_identity(_spec(), TRADING).deployment_instance_id
        != runtime_identity(_spec(spec_id="other-sandbox"), TRADING).deployment_instance_id
    )


def test_runtime_identity_digest_follows_the_content() -> None:
    assert runtime_identity(_spec(), TRADING).deployment_spec_digest != (
        runtime_identity(_spec(log_level="DEBUG"), TRADING).deployment_spec_digest
    )


def test_runtime_identity_digest_follows_the_strategy_config() -> None:
    # Changing only the pairs in config.yaml changes what is deployed, so the
    # digest that describes the deployment has to change with it.
    edited = StrategyTradingConfig(
        connector="binance_perpetual", pairs=("ETH-USDT",), leverage=3, digest="b" * 64
    )

    assert runtime_identity(_spec(), TRADING).deployment_spec_digest != (
        runtime_identity(_spec(), edited).deployment_spec_digest
    )


def test_the_host_reads_the_trading_parameters_the_strategy_config_resolved() -> None:
    spec = _spec()
    translated = runtime_spec(spec, runtime_identity(spec, TRADING), TRADING)

    assert translated["connector"] == "binance_perpetual"
    assert translated["pairs"] == ["BTC-USDT"]
    assert translated["leverage"] == 3


async def test_an_unreadable_strategy_config_is_refused_and_reported() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()

    def unreadable(spec: OfflineDeploymentSpec) -> StrategyTradingConfig:
        raise ValueError("strategy config does not set trading.pairs")

    settlement = await _reconciler(engine, publisher, strategy_config_for=unreadable).handle(
        _message(_spec())
    )

    assert settlement is Settlement.REJECTED
    assert engine.deployed == []
    assert publisher.payloads[-1]["health"] == "unhealthy"


async def test_a_broken_strategy_config_does_not_stand_in_the_way_of_stopping() -> None:
    # Stopping needs no trading parameters. Refusing a stop because the config
    # it no longer uses went bad would leave the strategy trading.
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))

    def unreadable(spec: OfflineDeploymentSpec) -> StrategyTradingConfig:
        raise ValueError("strategy config is unreadable")

    reconciler._strategy_config_for = unreadable
    settlement = await reconciler.handle(_message(_spec(generation=2, lifecycle_state="stopped")))

    assert settlement is Settlement.APPLIED
    assert engine.stopped
    assert publisher.payloads[-1]["phase"] == "stopped"


def test_runtime_spec_carries_the_keys_the_engine_host_reads() -> None:
    spec = _spec()
    translated = runtime_spec(spec, runtime_identity(spec, TRADING), TRADING)

    for key in (
        "deployment_instance_id",
        "deployment_spec_id",
        "deployment_spec_digest",
        "generation",
        "trading_mode",
        "lifecycle_state",
    ):
        assert key in translated, f"engine host reads {key}"


def test_a_testnet_spec_carries_the_scope_the_host_partitions_by() -> None:
    """Outside sandbox the host refuses a deployment with no credential scope.

    The spec schema forbids unknown keys and has no scope field, so PS cannot
    supply one: if this translation does not derive it, every testnet
    deployment on this lane fails at the host with "real-venue deployment
    requires instance and credential-scope identity" and no spec can fix it.
    """
    spec = _spec(trading_mode="testnet", sandbox=None)
    translated = runtime_spec(spec, runtime_identity(spec, TRADING), TRADING)

    scope = translated.get("credential_scope")
    assert isinstance(scope, dict), "the host reads credential_scope as an object"
    scope_id = str(scope.get("scope_id") or "").strip()
    assert scope_id, "an empty scope id is what the host refuses"

    # Republishing bumps the generation. A scope that moved with it would read
    # as a different exchange account on every publish, and the partition would
    # stop refusing the thing it exists to refuse.
    republished = _spec(trading_mode="testnet", sandbox=None, generation=spec.generation + 1)
    assert (
        str(
            runtime_spec(republished, runtime_identity(republished, TRADING), TRADING)[
                "credential_scope"
            ]["scope_id"]
        )
        == scope_id
    )


def test_two_credentials_partition_apart_and_one_credential_does_not() -> None:
    """The partition exists to keep two deployments off one exchange account.

    Deriving the scope from anything that is not the credential would either
    let two deployments share an account (same scope for different
    credentials) or let one account carry two deployments (different scopes
    for the same credential). Both defeat the refusal this feeds.
    """

    def scope_of(credential_id: str, spec_id: str) -> str:
        spec = _spec(
            spec_id=spec_id,
            trading_mode="testnet",
            sandbox=None,
            provenance_ref={"credential_id": credential_id},
        )
        return str(
            runtime_spec(spec, runtime_identity(spec, TRADING), TRADING)["credential_scope"][
                "scope_id"
            ]
        )

    # Same credential, different deployments — one account, one partition.
    assert scope_of("binance-supertrend", "a-testnet") == scope_of(
        "binance-supertrend", "b-testnet"
    )
    # Different credentials — different accounts, different partitions.
    assert scope_of("binance-supertrend", "a-testnet") != scope_of("binance-other", "a-testnet")


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


async def test_a_second_generation_stops_before_redeploying() -> None:
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    await reconciler.handle(_message(_spec()))

    await reconciler.handle(_message(_spec(generation=2)))

    assert len(engine.deployed) == 2
    assert len(engine.stopped) == 1
    assert engine.reconfigured == []


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


async def test_a_new_generation_after_a_restart_is_deployed_not_reconfigured() -> None:
    """The record names a container, but this process is attached to nothing.

    Dispatching on the recorded container id sends an unchanged strategy into
    reconfigure, which the real host refuses as a structural change it cannot
    hot-swap. Nothing structural changed — only the attachment was lost.
    """

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _after_a_restart(engine, publisher, generation=1)

    await reconciler.handle(_message(_spec(generation=2)))

    assert len(engine.deployed) == 1
    assert engine.reconfigured == []


async def test_the_applied_generation_after_a_restart_is_not_healthy_on_paper() -> None:
    """Repeating a generation is how the consumer re-asserts desired state.

    Answering healthy without asking the engine anything is worse than failing:
    the consumer's wait-status passes while no strategy is running.
    """

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _after_a_restart(engine, publisher, generation=1)

    await reconciler.handle(_message(_spec(generation=1)))

    engaged = bool(engine.deployed or engine.reconfigured or engine.stopped)
    assert engaged or publisher.payloads[-1]["health"] == "unhealthy"
    assert len(engine.deployed) == 1, "nothing is attached, so this generation is deployed"


async def test_an_attached_generation_is_still_reported_healthy_without_redeploying() -> None:
    """Asking the engine must not cost an in-process redelivery its cheap answer."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _after_a_restart(engine, publisher, generation=1)
    await reconciler.handle(_message(_spec(generation=2)))

    await reconciler.handle(_message(_spec(generation=2)))

    assert len(engine.deployed) == 1
    assert publisher.payloads[-1]["health"] == "healthy"


@pytest.mark.parametrize("lifecycle_state", ["stopped", "archived"])
async def test_a_terminal_generation_after_a_restart_is_healthy_without_stopping_again(
    lifecycle_state: str,
) -> None:
    """A terminal spec asks for the absence of an engine, and absence is what it has.

    The question the reconciler asks is whether the engine is where the spec wants
    it, not whether it is running — otherwise a restarted runner would re-stop an
    instance nobody is holding on every redelivery. Both terminal states take the
    same path today; asking each of them is what would notice if they stopped.
    """

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _after_a_restart(engine, publisher, generation=2)

    await reconciler.handle(_message(_spec(generation=2, lifecycle_state=lifecycle_state)))

    assert engine.stopped == []
    assert engine.deployed == []
    assert publisher.payloads[-1]["health"] == "healthy"


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

    await reconciler.handle(_message(_spec()))

    assert engine.stopped == []
    assert engine.reconfigured == []


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


async def _latched(engine: _FakeEngine, publisher: _RecordingPublisher) -> OfflineReconciler:
    """One applied generation, then a tick that finds it beyond its ceiling."""

    guard = OfflineExposureGuard(engine=engine, interval=0.001)
    reconciler = _reconciler(engine, publisher, guard=guard)
    await reconciler.apply(_spec())
    await guard.evaluate_once()

    assert not guard.allows_new_generations(), "the tick did not trip on the breach"
    engine.forget_what_it_was_asked_to_do()
    return reconciler


async def test_a_new_generation_is_refused_once_the_guard_has_tripped() -> None:
    """Flattening alone is undone by the next generation, so the lane stops taking them."""

    engine = _FakeEngine(open_notional="10000")
    publisher = _RecordingPublisher()
    reconciler = await _latched(engine, publisher)

    settlement = await reconciler.apply(_spec(generation=2))

    assert settlement is Settlement.REJECTED
    assert (engine.deployed, engine.reconfigured, engine.stopped) == ([], [], [])
    assert publisher.payloads[-1]["health"] == "unhealthy"


async def test_a_redelivered_generation_is_refused_too_rather_than_reported_healthy() -> None:
    """The operator's harness polls this status; a stale healthy would mislead it."""

    engine = _FakeEngine(open_notional="10000")
    publisher = _RecordingPublisher()
    reconciler = await _latched(engine, publisher)

    settlement = await reconciler.apply(_spec())

    assert settlement is Settlement.REJECTED
    assert publisher.payloads[-1]["health"] == "unhealthy"


async def test_the_refusal_is_terminal_because_redelivery_will_not_clear_it() -> None:
    engine = _FakeEngine(open_notional="10000")
    publisher = _RecordingPublisher()
    reconciler = await _latched(engine, publisher)

    with capture_logs() as logs:
        await reconciler.apply(_spec(generation=2))

    assert any(entry["event"] == "offline_generation_refused_after_trip" for entry in logs)


async def test_a_spec_whose_ceilings_cannot_be_read_is_refused_before_it_is_deployed() -> None:
    """Deploying first and failing afterwards would leave a strategy nobody is guarding."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()
    guard = OfflineExposureGuard(engine=engine, interval=0.001)
    reconciler = _reconciler(engine, publisher, guard=guard)

    settlement = await reconciler.apply(_spec(risk_config={"max_total_notional": "lots"}))

    assert settlement is Settlement.REJECTED
    assert engine.deployed == []
    assert publisher.payloads[-1]["health"] == "unhealthy"
    assert await guard.evaluate_once() == []


async def test_unreadable_ceilings_are_refused_even_with_no_guard_composed() -> None:
    """The ceilings are part of accepting desired state, not a feature of the guard."""

    engine, publisher = _FakeEngine(), _RecordingPublisher()

    settlement = await _reconciler(engine, publisher).apply(
        _spec(risk_config={"max_drawdown_pct": "-5"})
    )

    assert settlement is Settlement.REJECTED
    assert engine.deployed == []


async def test_the_ceiling_a_spec_raised_is_the_one_the_guard_ends_up_enforcing() -> None:
    """The exposure below is nine times the default ceiling and well under the raised one.

    Guard-level tests call ``watch`` directly, so nothing else here notices if the
    reconciler hands over the default instead of what the spec asked for.
    """

    engine = _FakeEngine(open_notional="9000")
    guard = OfflineExposureGuard(engine=engine, interval=0.001)
    reconciler = _reconciler(engine, _RecordingPublisher(), guard=guard)

    await reconciler.apply(_spec(risk_config={"max_total_notional": "25000"}))
    await guard.evaluate_once()

    assert engine.flattened == []
    assert guard.allows_new_generations()


async def test_a_guard_within_its_ceiling_changes_nothing() -> None:
    engine = _FakeEngine()
    publisher = _RecordingPublisher()
    guard = OfflineExposureGuard(engine=engine, interval=0.001)
    reconciler = _reconciler(engine, publisher, guard=guard)

    await reconciler.apply(_spec())
    await guard.evaluate_once()

    assert await reconciler.apply(_spec(generation=2)) is Settlement.APPLIED
    assert engine.flattened == []


async def test_a_reconciler_without_a_guard_behaves_as_it_always_did() -> None:
    engine, publisher = _FakeEngine(open_notional="10000"), _RecordingPublisher()

    assert await _reconciler(engine, publisher).apply(_spec()) is Settlement.APPLIED


async def test_a_restart_starts_from_an_unlatched_guard() -> None:
    """The latch lives in memory on purpose: restarting is how an operator clears it."""

    engine = _FakeEngine(open_notional="10000")
    publisher = _RecordingPublisher()
    await _latched(engine, publisher)

    restarted = _reconciler(engine, publisher, guard=OfflineExposureGuard(engine=engine))

    assert await restarted.apply(_spec(generation=2)) is Settlement.APPLIED


async def test_a_stopped_deployment_is_released_from_the_guard() -> None:
    """A stopped instance cannot answer, and failing closed on that would be noise."""

    engine = _FakeEngine()
    guard = OfflineExposureGuard(engine=engine, interval=0.001)
    reconciler = _reconciler(engine, _RecordingPublisher(), guard=guard)
    await reconciler.apply(_spec())

    await reconciler.apply(_spec(generation=2, lifecycle_state="stopped"))

    assert await guard.evaluate_once() == []


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


async def test_replacement_material_failure_preserves_old_node():
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    assert await reconciler.apply(_spec()) is Settlement.APPLIED

    def unavailable(spec):
        raise ValueError("artifact unavailable")

    reconciler._artifact_for = unavailable
    assert await reconciler.apply(_spec(generation=2)) is Settlement.RETRYABLE
    assert engine.stopped == []
    assert len(engine.deployed) == 1


async def test_failed_replacement_retries_deploy_without_repeated_stop():
    engine, publisher = _FakeEngine(), _RecordingPublisher()
    reconciler = _reconciler(engine, publisher)
    assert await reconciler.apply(_spec()) is Settlement.APPLIED
    engine.deploy_error = RuntimeError("fixture start failure")
    assert await reconciler.apply(_spec(generation=2)) is Settlement.RETRYABLE
    assert reconciler._applied[_spec().spec_id].generation == 1
    engine.deploy_error = None
    assert await reconciler.apply(_spec(generation=2)) is Settlement.APPLIED
    assert len(engine.stopped) == 1
    assert len(engine.deployed) == 2
    assert await reconciler.apply(_spec(generation=2)) is Settlement.APPLIED
    assert len(engine.deployed) == 2
