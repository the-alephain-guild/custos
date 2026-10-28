import subprocess
import sys
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from custos.core.execution_events import (
    ExecutionEventStore,
    LocalExecutionRecorder,
    OrderRefusalRecord,
)
from custos.core.local_runtime_state import (
    BoundRuntimeStore,
    RuntimeAuthorityRegistry,
    RuntimeStorageBindings,
)
from custos.core.runtime_authority import (
    RuntimeAuthorityError,
    RuntimeFenceStale,
    RuntimeStorageUnavailable,
)
from tests.test_local_reservations import fill, reserve
from tests.test_local_reservations import local_risk as local_risk
from tests.test_local_runtime_state import probe
from tests.test_runtime_authority import account


@pytest.fixture
def recording(tmp_path):
    store = ExecutionEventStore.initialize(tmp_path / "events.sqlite3")
    journal_id = uuid4()
    journal = tmp_path / "journal.identity"
    journal.write_text(f"{journal_id}:0")

    def identity_probe(path):
        return ExecutionEventStore.open(path).identity() if path == store.path else probe(path)

    registry = RuntimeAuthorityRegistry.initialize(
        tmp_path / "control", identity_probe=identity_probe
    )
    bindings = RuntimeStorageBindings(
        BoundRuntimeStore(store.path, store.store_id), BoundRuntimeStore(journal, journal_id)
    )
    lease = registry.claim(
        account(),
        run_id=uuid4(),
        permit_digest="a" * 64,
        expected_authorization_epoch=0,
        stores=bindings,
    )
    try:
        yield store, registry, bindings, lease
    finally:
        lease.close()


def test_a_refusal_survives_reopen_without_any_platform_identity(recording):
    store, _, _, lease = recording
    recorder = LocalExecutionRecorder(store, lease, configuration_revision=1)
    recorder.record_local_refusal(
        client_order_id="entry-1",
        instrument_id="BTCUSD.PAPERTRADE",
        side="BUY",
        reason_code="order_limit_exceeded",
    )
    events = ExecutionEventStore.open(store.path).events(lease.fence.run_id)
    assert len(events) == 1
    assert events[0].identity.run_id == lease.fence.run_id
    assert events[0].refusal.reason_code == "order_limit_exceeded"
    assert store.identity().sequence == 1


def record(recorder):
    recorder.record_local_refusal(
        client_order_id="entry-1",
        instrument_id="BTCUSD.PAPERTRADE",
        side="BUY",
        reason_code="order_limit_exceeded",
    )


def test_replaying_after_a_process_restart_preserves_one_immutable_event(recording):
    store, registry, bindings, lease = recording
    first = LocalExecutionRecorder(store, lease, configuration_revision=1)
    record(first)
    original = store.events(lease.fence.run_id)
    lease.close()
    with registry.claim(
        account(),
        run_id=lease.fence.run_id,
        permit_digest="a" * 64,
        expected_authorization_epoch=1,
        stores=bindings,
    ) as current:
        assert current.fence.process_generation > lease.fence.process_generation
        second = LocalExecutionRecorder(
            ExecutionEventStore.open(store.path), current, configuration_revision=1
        )
        record(second)
        assert store.events(current.fence.run_id) == original
        assert store.identity().sequence == 1
        with pytest.raises(RuntimeFenceStale):
            record(first)
    assert "process_generation" not in asdict(original[0])


def test_reusing_an_event_identity_with_another_payload_is_refused(recording):
    store, _, _, lease = recording
    recorder = LocalExecutionRecorder(store, lease, configuration_revision=1)
    record(recorder)
    original = store.events(lease.fence.run_id)[0]
    with pytest.raises(RuntimeAuthorityError, match="event identity conflicts"):
        store.append_refusal(
            lease=lease,
            identity=recorder.identity,
            event_id=original.event_id,
            refusal=replace(original.refusal, side="SELL"),
        )
    assert store.events(lease.fence.run_id) == (original,)


def test_a_configuration_change_cannot_rewrite_the_run_history(recording):
    store, _, _, lease = recording
    record(LocalExecutionRecorder(store, lease, configuration_revision=1))
    with pytest.raises(RuntimeAuthorityError, match="run identity conflicts"):
        record(LocalExecutionRecorder(store, lease, configuration_revision=2))
    assert len(store.events(lease.fence.run_id)) == 1


@pytest.mark.parametrize(
    "field,value", [("run_id", uuid4()), ("account_scope", "b" * 64), ("authorization_epoch", 2)]
)
def test_events_cannot_claim_another_authority(recording, field, value):
    store, _, _, lease = recording
    recorder = LocalExecutionRecorder(store, lease, configuration_revision=1)
    with pytest.raises(RuntimeAuthorityError, match="differs from current authority"):
        store.append_refusal(
            lease=lease,
            identity=replace(recorder.identity, **{field: value}),
            event_id="event-1",
            refusal=OrderRefusalRecord(
                "entry-1", "BTCUSD.PAPERTRADE", "BUY", "order_limit_exceeded"
            ),
        )
    assert store.identity().sequence == 0


def test_a_second_unregistered_event_database_cannot_record_the_account(recording, tmp_path):
    _, _, _, lease = recording
    other = ExecutionEventStore.initialize(tmp_path / "other.sqlite3")
    with pytest.raises(RuntimeStorageUnavailable, match="outside registered storage"):
        record(LocalExecutionRecorder(other, lease, configuration_revision=1))
    assert other.identity().sequence == 0


@pytest.mark.parametrize("replacement", [False, True])
def test_lost_storage_is_never_reinitialized_on_recovery(tmp_path, replacement):
    path = tmp_path / "lost.sqlite3"
    if replacement:
        path.touch()
    with pytest.raises(RuntimeStorageUnavailable):
        ExecutionEventStore.open(path)
    assert path.exists() is replacement


@pytest.mark.parametrize("reason", ["secret\nvalue", "an unfiltered exception", "x" * 129])
def test_refusals_accept_structured_reason_codes_only(reason):
    with pytest.raises(RuntimeAuthorityError):
        OrderRefusalRecord("entry-1", "BTCUSD.PAPERTRADE", "BUY", reason)


def test_identity_probes_release_connections_without_garbage_collection(tmp_path):
    script = """
import gc, resource, sys
from pathlib import Path
from custos.core.execution_events import ExecutionEventStore
gc.disable()
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
ceiling = 128 if hard == resource.RLIM_INFINITY else min(128, hard)
resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 128
path = Path(sys.argv[1])
ExecutionEventStore.initialize(path)
for _ in range(200):
    ExecutionEventStore.open(path).identity()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "probed.sqlite3")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_refusals_and_risk_mutations_share_one_durable_watermark(local_risk):
    risk = local_risk
    reserve(risk)
    record(LocalExecutionRecorder(risk.events, risk.lease, configuration_revision=1))
    fill(risk)
    reopened = ExecutionEventStore.open(risk.events.path)
    assert reopened.identity().sequence == 3
    assert [item.sequence for item in reopened.risk_events(risk.manifest.run_id)] == [1, 3]
    assert [item.sequence for item in reopened.events(risk.manifest.run_id)] == [2]


@pytest.mark.parametrize("field", ["session_private_key", "signed_intent", "raw_exception"])
def test_risk_audit_never_accepts_unfiltered_fields(field):
    from custos.core.execution_risk_events import RiskMutationRecord

    with pytest.raises(RuntimeAuthorityError, match="allowlisted"):
        RiskMutationRecord("reserve", {field: "must-not-be-stored"}, {})


@pytest.mark.parametrize("error", [None, "", "unfiltered exception message", "secret\nvalue"])
def test_refused_risk_audit_requires_a_structured_error(error):
    from custos.core.execution_risk_events import RiskMutationRecord

    with pytest.raises(RuntimeAuthorityError):
        RiskMutationRecord("risk_refused", {}, {"error_code": error})


async def test_a_refusal_record_failure_reaches_host_health(monkeypatch):
    pytest.importorskip("nautilus_trader")
    from custos.core.runner_fact_producer import RunnerFactEventBridge
    from custos.engines.nautilus import host as host_module
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.engines.nautilus.test_runner_safety_execution_boundary import (
        _boundary,
        _GatedStrategy,
        _order,
        _Store,
    )
    from tests.fixtures.fake_live_node import FakeLiveNodeType
    from tests.fixtures.trading_scope import declare_trading_scope_of
    from tests.test_nt_trading_node_host import (
        _Artifact,
        _credential,
        _fact_spec,
        _FactCapabilityReceipt,
        _FactEmitterStub,
    )

    def unavailable(self, **_fields):
        raise OSError("private storage error must not appear in status")

    monkeypatch.setattr(RunnerFactEventBridge, "record_local_refusal", unavailable)
    monkeypatch.setattr(host_module, "LiveNode", FakeLiveNodeType)
    boundary = _boundary(_Store([]))
    host = NtTradingNodeHost(
        tenant_id="tenant-a",
        runner_fact_emitter=_FactEmitterStub(),
        capability_receipt=_FactCapabilityReceipt(),
        runner_safety_boundary_factory=lambda _: boundary,
    )
    spec = _fact_spec("refusal-health", "binance_perpetual")
    strategy = _GatedStrategy()
    strategy.on_order_event = lambda _event: None
    strategy.on_position_event = lambda _event: None
    declare_trading_scope_of(strategy, spec)
    instance = await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    try:
        strategy.submit_order(_order("x" * 100))
        status = await host.get_engine_status(instance)
        assert status.phase == "degraded"
        assert status.reliable is False
        assert (
            status.unreliable_reason
            == "runner_event_forwarding_failed:execution_refusal_recording_failed:OSError"
        )
        assert boundary.fallback_breaker.frozen
        strategy.submit_order(_order("next"))
        strategy.submit_order(_order("exit", reduce_only=True))
        assert [str(order.client_order_id) for order in strategy.submitted] == ["exit"]
    finally:
        await host.stop(instance)
