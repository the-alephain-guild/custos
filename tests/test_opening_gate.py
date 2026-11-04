from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from custos.cli._daemon import _build_runner_safety_boundary_factory
from custos.core.engine_protocol import EngineLifecycleAuthority
from tests.cli.test_runner_safety_daemon_composition import _Resolver
from tests.test_runner_fact_store import _runner_fact_store, _verified_command


async def test_signed_running_command_persists_a_closed_opening_gate_across_reopen(tmp_path):
    database = tmp_path / "runner.sqlite3"
    _, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    authority = EngineLifecycleAuthority.from_verified_command(verified)

    assert store.load_opening_gate_state_sync(authority) == "awaiting_release"

    _, restarted = _runner_fact_store(database)
    assert restarted.load_opening_gate_state_sync(authority) == "awaiting_release"


async def test_signed_boundary_refuses_a_reduce_only_submit_before_reserving(tmp_path):
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.engines.nautilus.test_runner_safety_execution_boundary import _Semantics

    _, store = _runner_fact_store(tmp_path / "runner.sqlite3")
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    boundary = await _build_runner_safety_boundary_factory(
        state_store=store, safety_policy_resolver=_Resolver()
    )(verified.command.to_runtime_spec().model_dump(mode="python"))
    boundary.bind_runtime(semantics=_Semantics())
    sent, refusals = [], []
    gate = RunnerSafetyOrderGate(
        boundary=boundary, client_order_id_len_limit=None, on_refusal=refusals.append
    )
    order = SimpleNamespace(
        client_order_id="opening-order-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="BUY",
        reduce_only=True,
        quantity="1",
        notional="10",
        emulation_trigger=None,
        exec_algorithm_id=None,
    )

    assert gate.submit_order(sent.append, order) is False
    assert sent == []
    assert [fact.reason_code for fact in refusals] == ["awaiting_opening_release"]


def test_signed_host_cannot_install_an_ungated_strategy():
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.engines.nautilus.test_runner_safety_host_wiring import _Strategy

    host, strategy = NtTradingNodeHost(), _Strategy()
    with pytest.raises(RuntimeError, match="platform opening gate"):
        host._attach_runtime_bridges(
            "instance-1", strategy, object(), (SimpleNamespace(), None), None
        )


@pytest.fixture
async def runtime(tmp_path):
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.engines.nautilus.test_runner_safety_execution_boundary import _Semantics

    database = tmp_path / "runner.sqlite3"
    outbox, store = _runner_fact_store(database)
    _, _, verified = _verified_command()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    spec = verified.command.to_runtime_spec().model_dump(mode="python")
    boundary = await _build_runner_safety_boundary_factory(
        state_store=store, safety_policy_resolver=_Resolver()
    )(spec)
    boundary.bind_runtime(semantics=_Semantics())
    refusals = []
    gate = RunnerSafetyOrderGate(
        boundary=boundary, client_order_id_len_limit=None, on_refusal=refusals.append
    )
    return SimpleNamespace(
        database=database,
        outbox=outbox,
        store=store,
        verified=verified,
        spec=spec,
        boundary=boundary,
        gate=gate,
        refusals=refusals,
    )


@pytest.mark.parametrize(
    ("method", "arguments", "keywords"),
    [
        ("submit_order", (SimpleNamespace(client_order_id="entry"),), {}),
        ("submit_order_list", (SimpleNamespace(orders=()),), {}),
        ("modify_order", ("resting-order",), {}),
        ("modify_orders", ([],), {}),
        ("market_exit", (), {}),
        ("close_position", (object(),), {"reduce_only": True}),
        ("close_position", (object(),), {"reduce_only": None}),
        ("close_all_positions", ("BTCUSDT-PERP.BINANCE",), {}),
        ("close_all_positions", ("BTCUSDT-PERP.BINANCE",), {"reduce_only": None}),
    ],
)
async def test_every_order_creating_operation_is_stopped_before_native_dispatch(
    runtime, method, arguments, keywords
):
    sent = []
    getattr(runtime.gate, method)(
        lambda *_args, **_kwargs: sent.append(True), *arguments, **keywords
    )
    assert sent == []
    assert [fact.reason_code for fact in runtime.refusals] == ["awaiting_opening_release"]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM runner_opening_gate",
        "DELETE FROM desired_deployments",
        "UPDATE runner_opening_gate SET generation = generation + 1",
        "UPDATE runner_opening_gate SET deployment_spec_digest = 'bad'",
        "UPDATE runner_opening_gate SET command_fingerprint = 'bad'",
        "UPDATE runner_opening_gate SET state = 'released'",
        "UPDATE desired_deployments SET canonical_command = '{bad'",
        "DROP TABLE runner_opening_gate",
    ],
)
async def test_missing_corrupt_or_foreign_gate_never_falls_back_to_offline(runtime, statement):
    with sqlite3.connect(runtime.database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(statement)
    sent = []
    runtime.gate.close_position(lambda *_args, **_kwargs: sent.append(True), object())
    assert sent == []
    assert len(runtime.refusals) == 1
    assert runtime.refusals[0].reason_code == "custos_runtime_control_unavailable"


@pytest.mark.parametrize("lifecycle", ["paused", "stopped", "archived"])
async def test_new_non_running_desired_command_retires_the_old_gate(runtime, lifecycle):
    def change(event):
        event["aggregate_version"] = 2
        event["payload"]["generation"] = 2
        event["payload"]["lifecycle_state"] = lifecycle

    _, _, next_command = _verified_command(mutate_event=change)
    await runtime.store.record_desired_command(
        command=next_command.command,
        command_fingerprint=next_command.command_fingerprint,
        verification_receipt=next_command.verification_receipt,
    )
    assert (
        runtime.store.load_opening_gate_state_sync(
            EngineLifecycleAuthority.from_verified_command(next_command)
        )
        == "revoked"
    )
    sent = []
    runtime.gate.close_position(lambda *_args, **_kwargs: sent.append(True), object())
    runtime.gate.cancel(lambda *_args, **_kwargs: sent.append(True), "old-order")
    assert sent == []
    assert len(runtime.refusals) == 2


async def test_closed_gate_allows_cancellation_for_its_current_signed_authority(runtime):
    sent = []
    runtime.gate.cancel(sent.append, "leftover-order")
    assert sent == ["leftover-order"]
    assert runtime.refusals == []


async def test_strategy_config_cannot_forge_a_release_or_select_an_unsigned_lane(runtime):
    from custos.engines.nautilus.runner_safety import install_order_gate
    from tests.engines.nautilus.test_runner_safety_execution_boundary import _GatedStrategy, _order

    strategy = _GatedStrategy()
    strategy.config = SimpleNamespace(
        manage_contingent_orders=False,
        manage_gtd_expiry=False,
        manage_stop=False,
        opening_gate="released",
        opening_release=True,
        offline=True,
    )
    install_order_gate(strategy, runtime.gate)
    assert strategy.submit_order(_order("external-signal", reduce_only=True)) is False
    assert strategy.submitted == []


async def test_a_refusal_can_write_the_shared_fact_outbox_without_a_control_lock(runtime):
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.test_runner_fact_store import _fact, _identity, _runner_authority

    authority = _runner_authority(runtime.verified)
    gate = RunnerSafetyOrderGate(
        boundary=runtime.boundary,
        client_order_id_len_limit=None,
        on_refusal=lambda _refusal: runtime.outbox.enqueue_sync(
            authority, _identity(), (_fact("refused"),)
        ),
    )
    gate.close_position(lambda *_args: pytest.fail("native close reached"), object())
    assert len(await runtime.outbox.pending()) == 1


async def test_installed_native_strategy_cannot_submit_from_on_start(runtime, tmp_path):
    import gc

    from nautilus_trader.backtest import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
    from nautilus_trader.model import AccountType, AggressorSide, Money, OmsType, OrderSide

    from custos.engines.nautilus.runner_safety import install_order_gate
    from tests.fixtures import nt_data_stubs
    from tests.test_client_order_id_sandbox_execution import (
        _INSTRUMENT,
        _VENUE,
        USDT,
        _strategy_config,
        _SubmitsOneOrder,
    )

    class AttemptsFromStart(_SubmitsOneOrder):
        def on_start(self):
            self.submit_order(
                self.order_factory.market(
                    instrument_id=_INSTRUMENT.id,
                    order_side=OrderSide.BUY,
                    quantity=_INSTRUMENT.make_qty(1),
                )
            )
            self.submit_order_list(object())
            self.modify_order("unregistered-order")
            self.modify_orders([])
            self.close_position(object(), reduce_only=None)
            self.close_all_positions(_INSTRUMENT.id)
            self.market_exit()

    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True)))
    engine.add_venue(
        venue=_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(_INSTRUMENT)
    strategy = AttemptsFromStart(_strategy_config(tmp_path))
    install_order_gate(strategy, runtime.gate)
    engine.add_strategy(strategy)
    engine.add_data(
        [
            nt_data_stubs.quote_tick(
                _INSTRUMENT,
                bid_price=100.0,
                ask_price=100.0,
                ts_event=1_000_000_000,
                ts_init=1_000_000_000,
            ),
            nt_data_stubs.trade_tick(
                _INSTRUMENT,
                price=100.0,
                size=1.0,
                aggressor_side=AggressorSide.BUY,
                ts_event=1_000_000_000,
                ts_init=1_000_000_000,
            ),
        ]
    )
    try:
        engine.run()
        assert strategy.filled_client_order_id is None
        assert engine.cache.orders() == []
        assert [item.reason_code for item in runtime.refusals] == ["awaiting_opening_release"] * 7
    finally:
        engine.dispose()
        # Native strategies are thread-affine. Collect hook cycles here rather
        # than letting a later concurrency fixture collect them on its worker.
        del strategy, engine
        gc.collect()


async def test_missing_gate_on_an_old_database_is_not_repaired_into_permission(runtime):
    from custos.core.runner_fact import RunnerStateDurabilityError

    with sqlite3.connect(runtime.database) as connection:
        connection.execute("DELETE FROM runner_opening_gate")
    _, restarted = _runner_fact_store(runtime.database)
    await restarted.record_desired_command(
        command=runtime.verified.command,
        command_fingerprint=runtime.verified.command_fingerprint,
        verification_receipt=runtime.verified.verification_receipt,
    )
    with pytest.raises(RunnerStateDurabilityError, match="missing"):
        restarted.load_opening_gate_state_sync(
            EngineLifecycleAuthority.from_verified_command(runtime.verified)
        )


async def test_gate_creation_failure_rolls_back_the_new_signed_desired_command(tmp_path):
    database = tmp_path / "runner.sqlite3"
    _, store = _runner_fact_store(database)
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TRIGGER deny_gate BEFORE INSERT ON runner_opening_gate
            BEGIN SELECT RAISE(ABORT, 'fixture gate write failed'); END;
        """)
    _, _, verified = _verified_command()
    with pytest.raises(sqlite3.IntegrityError, match="gate write failed"):
        await store.record_desired_command(
            command=verified.command,
            command_fingerprint=verified.command_fingerprint,
            verification_receipt=verified.verification_receipt,
        )
    assert await store.list_recoverable_desired_command_identities() == ()


@pytest.mark.parametrize("lifecycle", ["paused", "stopped"])
async def test_desired_retirement_waits_for_current_cancellation_then_fences_it(runtime, lifecycle):
    def change(event):
        event["aggregate_version"] = 2
        event["payload"]["generation"] = 2
        event["payload"]["lifecycle_state"] = lifecycle

    _, _, newer = _verified_command(mutate_event=change)
    entered = Event()

    def retire():
        entered.set()
        asyncio.run(
            runtime.store.record_desired_command(
                command=newer.command,
                command_fingerprint=newer.command_fingerprint,
                verification_receipt=newer.verification_receipt,
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with runtime.boundary.dispatch_control():
            pending = executor.submit(retire)
            assert entered.wait(timeout=2)
            assert not pending.done()
        pending.result(timeout=2)
    cancelled = []
    runtime.gate.cancel(cancelled.append, "old-generation-order")
    assert cancelled == []


async def test_recovery_factory_and_forged_strategy_configuration_stay_closed(runtime):
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate

    _, restarted = _runner_fact_store(runtime.database)
    durable = await restarted.load_durable_desired_command(
        runtime.verified.command.deployment_instance_id
    )
    assert durable.command.generation == runtime.verified.command.generation
    spec = runtime.verified.command.to_runtime_spec().model_dump(mode="python")
    spec["offline"] = True
    spec["opening_release"] = {"outcome": "acknowledged"}
    boundary = await _build_runner_safety_boundary_factory(
        state_store=restarted, safety_policy_resolver=_Resolver()
    )(spec)
    refusals, sent = [], []
    gate = RunnerSafetyOrderGate(
        boundary=boundary, client_order_id_len_limit=None, on_refusal=refusals.append
    )
    gate.close_all_positions(lambda *_args: sent.append(True), "BTCUSDT-PERP.BINANCE")
    assert sent == []
    assert [item.reason_code for item in refusals] == ["awaiting_opening_release"]


async def test_actual_command_runtime_recovery_keeps_its_execution_boundary_closed(runtime):
    from custos.core.runner_command_intake import CommandDeliveryPolicy
    from custos.core.runner_command_runtime import RunnerCommandRuntimeCoordinator
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.test_engine_recovery_persistence import supervisor
    from tests.test_runner_command_runtime import _CredentialResolver, _Intake
    from tests.test_runner_command_runtime_failure_modes import (
        _ActivatingArtifactRuntime,
        _RecordingResolver,
        _SandboxEngine,
    )

    _, restarted = _runner_fact_store(runtime.database)
    sent, refusals, events = [], [], []

    class OpeningAttemptingEngine(_SandboxEngine):
        async def deploy(self, spec, credential, artifact):
            boundary = await _build_runner_safety_boundary_factory(
                state_store=restarted, safety_policy_resolver=_Resolver()
            )(spec)
            gate = RunnerSafetyOrderGate(
                boundary=boundary, client_order_id_len_limit=None, on_refusal=refusals.append
            )
            gate.close_position(lambda *_args: sent.append(True), object())
            return await self._engine.deploy(spec, credential, artifact)

    engine = OpeningAttemptingEngine()
    coordinator = RunnerCommandRuntimeCoordinator(
        intake=_Intake(verified=runtime.verified),
        durability=restarted,
        release_resolver=_RecordingResolver(events),
        artifact_runtime=_ActivatingArtifactRuntime(events, restarted, runtime.verified),
        entry_point_loader=object(),
        credential_resolver=_CredentialResolver(),
        engine_lifecycle=supervisor(restarted, engine),
        delivery_policy=CommandDeliveryPolicy(in_progress_interval_seconds=0.01),
        capability_binding=lambda _verified: None,
    )
    try:
        ready = await coordinator.recover(runtime.verified)
        assert ready is not None
        assert sent == []
        assert [item.reason_code for item in refusals] == ["awaiting_opening_release"]
        assert (
            restarted.load_opening_gate_state_sync(
                EngineLifecycleAuthority.from_verified_command(runtime.verified)
            )
            == "awaiting_release"
        )
    finally:
        await coordinator._cancel_engine_supervision(
            runtime.verified.command.deployment_instance_id
        )


@pytest.mark.parametrize("component", ["emitter", "capability", "factory"])
def test_partial_signed_composition_cannot_silently_become_an_offline_host(component):
    pytest.importorskip("nautilus_trader")
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.engines.nautilus.test_runner_safety_host_wiring import _Strategy

    components = {
        "emitter": {"runner_fact_emitter": object()},
        "capability": {"capability_receipt": object()},
        "factory": {"runner_safety_boundary_factory": lambda _spec: None},
    }
    host, strategy = NtTradingNodeHost(**components[component]), _Strategy()
    with pytest.raises(RuntimeError, match="platform opening gate"):
        host._attach_runtime_bridges("instance-1", strategy, object(), None, None)
    assert "submit_order" not in vars(strategy)


async def test_host_installs_a_persisted_gate_before_add_strategy_and_run(runtime, monkeypatch):
    import gc

    from custos.core.runner_fact import RunnerFactEmitter
    from custos.engines.nautilus import host as host_module
    from tests.engines.nautilus.test_runner_safety_execution_boundary import _GatedStrategy, _order
    from tests.fixtures.fake_live_node import FakeBuilder, FakeLiveNode, FakeLiveNodeType
    from tests.test_nt_trading_node_host import (
        _Artifact,
        _credential,
        _FactCapabilityReceipt,
        _StrategyDouble,
    )
    from tests.test_runner_fact_store import _identity

    class Strategy(_StrategyDouble, _GatedStrategy):
        def __init__(self, spec):
            _StrategyDouble.__init__(self, spec)
            self.submitted = []

    authority = EngineLifecycleAuthority.from_verified_command(runtime.verified)
    stages = []
    original_add, original_run = FakeLiveNode.add_strategy, FakeLiveNode.run_async

    def add(node, strategy):
        stages.append("add")
        assert runtime.store.load_opening_gate_state_sync(authority) == "awaiting_release"
        assert strategy.submit_order(_order("before-native-start", reduce_only=True)) is False
        assert strategy.submitted == []
        original_add(node, strategy)

    async def run(node):
        stages.append("run")
        assert runtime.store.load_opening_gate_state_sync(authority) == "awaiting_release"
        await original_run(node)

    monkeypatch.setattr(host_module, "LiveNode", FakeLiveNodeType)
    monkeypatch.setattr(FakeLiveNode, "add_strategy", add)
    monkeypatch.setattr(FakeLiveNode, "run_async", run)
    capability = _FactCapabilityReceipt()
    capability.runner_id = runtime.verified.command.runner_id
    identity = _identity()
    host = host_module.NtTradingNodeHost(
        tenant_id=runtime.verified.command.tenant_id,
        runner_id=str(runtime.verified.command.runner_id),
        runner_fact_emitter=RunnerFactEmitter(runtime.outbox, identity, lambda: None),
        capability_receipt=capability,
        runner_safety_boundary_factory=_build_runner_safety_boundary_factory(
            state_store=runtime.store, safety_policy_resolver=_Resolver()
        ),
    )
    strategy = Strategy(runtime.spec)
    await host.deploy(runtime.spec, _credential(), _Artifact(strategy=strategy))
    try:
        await asyncio.sleep(0)
        assert stages == ["add", "run"]
        assert len(await runtime.outbox.pending()) == 1
    finally:
        await host.stop(str(authority.deployment_instance_id))
        FakeLiveNode.instances.clear()
        FakeBuilder.instances.clear()
        del host, strategy
        gc.collect()


async def test_synchronous_cancel_callback_can_refuse_submit_and_persist_the_signed_refusal(
    runtime,
):
    import base64
    import json
    from time import monotonic

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from custos.core.runner_fact import RunnerFactEmitter, runner_fact_signing_preimage
    from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.test_nt_trading_node_host import _FactCapabilityReceipt
    from tests.test_runner_fact_store import _identity, _runner_authority

    authority, identity = _runner_authority(runtime.verified), _identity()
    refusals = []
    logs = RunnerRuntimeLogEmitter(
        emitter=RunnerFactEmitter(runtime.outbox, identity, lambda: None),
        capability=_FactCapabilityReceipt(),
        redactor=RuntimeLogRedactor(),
    )

    def record(refusal):
        logs.emit_sync(
            authority,
            level="WARN",
            component="custos.execution.order",
            message="order_refused",
            structured_fields={"lifecycle": "refused", "reason_code": refusal.reason_code},
            correlation_id=authority.deployment_instance_id,
        )
        refusals.append(refusal)

    gate = RunnerSafetyOrderGate(
        boundary=runtime.boundary, client_order_id_len_limit=None, on_refusal=record
    )
    sent = []

    def native_cancel(order):
        # Native pending-cancel delivery is synchronous. Its user callback may
        # try another order before the original cancellation reaches the venue.
        gate.close_position(lambda *_args: pytest.fail("nested close escaped"), object())
        sent.append(order)

    started = monotonic()
    gate.cancel(native_cancel, "leftover-order")
    assert sent == ["leftover-order"]
    assert [item.reason_code for item in refusals] == ["awaiting_opening_release"]
    assert len(await runtime.outbox.pending()) == 1
    assert monotonic() - started < 2
    [pending] = await runtime.outbox.pending()
    document = json.loads(pending.payload)
    assert document["facts"][0]["message"] == "order_refused"
    assert document["facts"][0]["structured_fields"]["reason_code"] == "awaiting_opening_release"
    signature = document["signature"]
    Ed25519PublicKey.from_public_bytes(identity.public_key_bytes).verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        runner_fact_signing_preimage(document),
    )


async def test_real_native_pending_cancel_callback_records_a_nested_submit_refusal(
    runtime, tmp_path, monkeypatch
):
    import gc
    import json
    from time import monotonic

    from custos.core.runner_fact import RunnerFactEmitter
    from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate, install_order_gate
    from tests import test_cancel_still_reaches_the_venue as native
    from tests.test_nt_trading_node_host import _FactCapabilityReceipt
    from tests.test_runner_fact_store import _identity, _runner_authority

    authority = _runner_authority(runtime.verified)
    logs = RunnerRuntimeLogEmitter(
        emitter=RunnerFactEmitter(runtime.outbox, _identity(), lambda: None),
        capability=_FactCapabilityReceipt(),
        redactor=RuntimeLogRedactor(),
    )

    def record(refusal):
        logs.emit_sync(
            authority,
            level="WARN",
            component="custos.execution.order",
            message="order_refused",
            structured_fields={"lifecycle": "refused", "reason_code": refusal.reason_code},
            correlation_id=authority.deployment_instance_id,
        )

    gate = RunnerSafetyOrderGate(
        boundary=runtime.boundary, client_order_id_len_limit=None, on_refusal=record
    )
    callbacks, callback_errors = [], []

    class PendingCancelProbe(native._RestsThenCancels):
        def on_start(self):
            self._nested_order = self.order_factory.market(
                instrument_id=native._INSTRUMENT.id,
                order_side=native.OrderSide.BUY,
                quantity=native._INSTRUMENT.make_qty(1),
            )
            super().on_start()

        def on_order_accepted(self, event):
            # Seed native venue history first, then install the new gate over an
            # already resting order. This models an inherited native cache, not
            # permission for a signed deployment to start trading before its gate.
            install_order_gate(self, gate)

        def on_order_pending_cancel(self, event):
            callbacks.append(str(event.client_order_id))
            try:
                self.submit_order(self._nested_order)
            except Exception as error:
                callback_errors.append(error)

    monkeypatch.setattr(native, "_RestsThenCancels", PendingCancelProbe)
    started = monotonic()
    try:
        canceled, orders = native._run(tmp_path, bulk=False)
        assert len(callbacks) == 1
        assert callback_errors == []
        assert len(orders) == 1
        assert orders[0].status == native.OrderStatus.CANCELED
        assert canceled == [orders[0].client_order_id.value]
        assert monotonic() - started < 2
        [pending] = await runtime.outbox.pending()
        document = json.loads(pending.payload)
        assert (
            document["facts"][0]["structured_fields"]["reason_code"] == "awaiting_opening_release"
        )
    finally:
        gc.collect()


def _signed_refusal_recorder(runtime):
    from custos.core.runner_fact import RunnerFactEmitter
    from custos.core.runtime_log_fact import RunnerRuntimeLogEmitter, RuntimeLogRedactor
    from tests.test_nt_trading_node_host import _FactCapabilityReceipt
    from tests.test_runner_fact_store import _identity, _runner_authority

    authority = _runner_authority(runtime.verified)
    logs = RunnerRuntimeLogEmitter(
        emitter=RunnerFactEmitter(runtime.outbox, _identity(), lambda: None),
        capability=_FactCapabilityReceipt(),
        redactor=RuntimeLogRedactor(),
    )
    refusals = []

    def record(refusal):
        logs.emit_sync(
            authority,
            level="WARN",
            component="custos.execution.order",
            message="order_refused",
            structured_fields={"lifecycle": "refused", "reason_code": refusal.reason_code},
            correlation_id=authority.deployment_instance_id,
        )
        refusals.append(refusal)

    return record, refusals


async def test_outer_cancel_exception_preserves_deferred_signed_refusals(runtime):
    import json

    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate

    record, refusals = _signed_refusal_recorder(runtime)
    gate = RunnerSafetyOrderGate(
        boundary=runtime.boundary, client_order_id_len_limit=None, on_refusal=record
    )

    def failing_native_cancel(_order):
        gate.close_position(lambda *_args: pytest.fail("close escaped"), object())
        raise ValueError("native cancellation failed after callback")

    with pytest.raises(ValueError, match="native cancellation failed"):
        gate.cancel(failing_native_cancel, "leftover")
    assert [item.reason_code for item in refusals] == ["awaiting_opening_release"]
    [pending] = await runtime.outbox.pending()
    assert json.loads(pending.payload)["facts"][0]["message"] == "order_refused"


async def test_nested_store_sharing_the_outbox_rechecks_its_different_authority(runtime):
    from custos.core.runner_fact import RunnerStateStore
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.test_runner_fact_store import _identity, _runner_authority

    second_store = RunnerStateStore(
        outbox=runtime.outbox,
        identity=_identity(),
        tenant_id=runtime.verified.command.tenant_id,
        runner_id=runtime.verified.command.runner_id,
        authority_resolver=_runner_authority,
    )
    stale_spec = {**runtime.spec, "generation": runtime.spec["generation"] + 1}
    boundary = await _build_runner_safety_boundary_factory(
        state_store=second_store, safety_policy_resolver=_Resolver()
    )(stale_spec)
    record, refusals = _signed_refusal_recorder(runtime)
    nested = RunnerSafetyOrderGate(
        boundary=boundary, client_order_id_len_limit=None, on_refusal=record
    )
    sent = []

    def native_cancel(order):
        nested.close_position(lambda *_args: pytest.fail("wrong authority escaped"), object())
        sent.append(order)

    runtime.gate.cancel(native_cancel, "leftover")
    assert sent == ["leftover"]
    assert [item.reason_code for item in refusals] == ["custos_runtime_control_unavailable"]
    assert len(await runtime.outbox.pending()) == 1


async def test_another_thread_waits_for_the_outer_control_connection(runtime):
    import gc

    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate

    gc.collect()
    record, refusals = _signed_refusal_recorder(runtime)
    gate = RunnerSafetyOrderGate(
        boundary=runtime.boundary, client_order_id_len_limit=None, on_refusal=record
    )
    entered = Event()

    def request_close():
        entered.set()
        gate.close_position(lambda *_args: pytest.fail("close escaped"), object())

    with ThreadPoolExecutor(max_workers=1) as executor:
        with runtime.boundary.dispatch_control():
            pending = executor.submit(request_close)
            assert entered.wait(timeout=2)
            assert not pending.done()
        pending.result(timeout=2)
    assert [item.reason_code for item in refusals] == ["awaiting_opening_release"]
    assert len(await runtime.outbox.pending()) == 1
