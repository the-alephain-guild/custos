"""Local risk mutations and their audit records share one registered SQLite store."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import select
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from custos_toolkit.contracts.strategy_execution import canonical_json_bytes

from custos.core.execution_events import ExecutionEventStore
from custos.core.local_runtime_state import (
    BoundRuntimeStore,
    RuntimeAuthorityRegistry,
    RuntimeStorageBindings,
)
from custos.core.runtime_authority import RuntimeAuthorityError, RuntimeStorageUnavailable
from custos.local.authorization import (
    LocalOperatorAuthority,
    LocalPermitAuthenticator,
    LocalRunPermitV1,
    local_operator_key_id,
    sign_local_permit,
)
from custos.local.manifest import parse_local_manifest
from tests.test_local_permit import utc
from tests.test_local_run_manifest import manifest_document
from tests.test_local_runtime_state import probe
from tests.test_runtime_authority import account

NOW = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)


class _Blocker:
    def __init__(self):
        self.frozen = False

    def freeze(self):
        self.frozen = True

    def is_frozen(self):
        return self.frozen


@pytest.fixture
def local_risk(tmp_path):
    api = importlib.import_module("custos.local.reservations")
    events = ExecutionEventStore.initialize(tmp_path / "state.sqlite3")
    journal_id = uuid4()
    journal = tmp_path / "journal.identity"
    journal.write_text(f"{journal_id}:0")
    probe_hooks = []

    def identity_probe(path):
        for hook in probe_hooks:
            hook(path)
        return ExecutionEventStore.open(path).identity() if path == events.path else probe(path)

    registry = RuntimeAuthorityRegistry.initialize(
        tmp_path / "control", identity_probe=identity_probe
    )
    bindings = RuntimeStorageBindings(
        BoundRuntimeStore(events.path, events.store_id), BoundRuntimeStore(journal, journal_id)
    )
    key = Ed25519PrivateKey.generate()
    key_id = local_operator_key_id(key.public_key())
    authenticator = LocalPermitAuthenticator(
        {key_id: LocalOperatorAuthority("operator", key.public_key())}
    )
    clock = [NOW]
    leases = []
    alternate_authorization = None

    def make(*, previous=None, recover=False, blocker=None):
        nonlocal alternate_authorization
        manifest = (
            previous.manifest
            if previous and recover
            else parse_local_manifest(canonical_json_bytes(manifest_document()))
        )
        permit = (
            previous.permit
            if previous and recover
            else LocalRunPermitV1(
                schema_version=1,
                permit_id=uuid4(),
                run_id=manifest.run_id,
                manifest_digest=manifest.digest,
                account_scope=account().scope_digest,
                control_registry_id=registry.registry_id,
                authorization_epoch=1
                if previous is None
                else previous.permit.authorization_epoch + 1,
                operator_id="operator",
                key_id=key_id,
                issued_at_utc=utc(NOW),
                not_before_utc=utc(NOW),
                expires_at_utc=utc(NOW + timedelta(days=2)),
                exit_until_utc=utc(NOW + timedelta(days=3)),
            )
        )
        raw = sign_local_permit(permit, key)
        if previous is None:
            alternate_document = manifest.model_dump(mode="json")
            alternate_document["limits"].update(
                max_order_margin_usd="200", max_order_notional_usd="1000"
            )
            alternate_manifest = parse_local_manifest(canonical_json_bytes(alternate_document))
            alternate_permit_document = permit.model_dump(mode="json")
            alternate_permit_document.update(
                permit_id=str(uuid4()), manifest_digest=alternate_manifest.digest
            )
            alternate_permit = LocalRunPermitV1.model_validate(alternate_permit_document)
            alternate_raw = sign_local_permit(alternate_permit, key)
            with registry.account_guard(account()) as alternate_guard:
                alternate_checkpoint = alternate_guard.prepare_claim(
                    run_id=manifest.run_id,
                    permit_digest=hashlib.sha256(alternate_raw).hexdigest(),
                    expected_authorization_epoch=0,
                )
                alternate_authorization = authenticator.verify(
                    manifest_bytes=alternate_manifest.canonical_bytes(),
                    signed_permit_bytes=alternate_raw,
                    account=account(),
                    control_registry_id=registry.registry_id,
                    checkpoint=alternate_checkpoint,
                    now=NOW,
                )
        with registry.account_guard(account()) as guard:
            checkpoint = guard.prepare_claim(
                run_id=manifest.run_id,
                permit_digest=hashlib.sha256(raw).hexdigest(),
                expected_authorization_epoch=0
                if previous is None
                else previous.permit.authorization_epoch,
            )
            authorization = authenticator.verify(
                manifest_bytes=manifest.canonical_bytes(),
                signed_permit_bytes=raw,
                account=account(),
                control_registry_id=registry.registry_id,
                checkpoint=checkpoint,
                now=NOW,
            )
            lease = guard.claim(checkpoint, stores=bindings)
        leases.append(lease)
        blocker = blocker or _Blocker()
        backend = api.LocalReservationStore(
            store=events,
            lease=lease,
            authorization=authorization,
            risk_blocker=blocker,
            clock=lambda: clock[0],
        )
        return SimpleNamespace(
            store=backend,
            events=events,
            registry=registry,
            bindings=bindings,
            lease=lease,
            manifest=manifest,
            permit=permit,
            authorization=authorization,
            blocker=blocker,
            clock=clock,
            probe_hooks=probe_hooks,
            alternate_authorization=alternate_authorization,
        )

    first = make()
    first.make = make
    try:
        yield first
    finally:
        for lease in leases:
            lease.close()


def reserve(risk, order="entry", amount="500", event=None):
    return risk.store.reserve_order_notional_sync(
        event_id=event or "reserve-" + order,
        runtime_id=risk.manifest.run_id,
        client_order_id=order,
        policy_id=risk.permit.permit_id,
        requested_notional=Decimal(amount),
    )


def fill(risk, order="entry", *, trade="venue-1", amount="500", quantity="0.005"):
    token = hashlib.sha256(trade.encode()).hexdigest()
    return risk.store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{token}:{order}",
        runtime_id=risk.manifest.run_id,
        client_order_id=order,
        fill_notional=Decimal(amount),
        fill_quantity=Decimal(quantity),
        leaves_quantity=Decimal(0),
        position_id="position-" + order,
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="BUY",
    )


def snapshot(risk, order="entry"):
    return risk.store.load_order_reservation_sync(risk.manifest.run_id, order)


def test_local_reservation_has_no_platform_identity_and_uses_registered_sequence(local_risk):
    risk = local_risk
    result = reserve(risk)
    assert result.runtime_id == risk.manifest.run_id
    assert result.reserved_notional == Decimal("500")
    assert not hasattr(result, "deployment_instance_id")
    assert risk.events.identity().sequence == 1
    assert len(risk.events.risk_events(risk.manifest.run_id)) == 1
    assert not risk.blocker.is_frozen()


def test_replaying_same_reservation_does_not_create_capacity_or_events(local_risk):
    first = reserve(local_risk)
    assert reserve(local_risk) == first
    assert snapshot(local_risk) == first
    assert local_risk.events.identity().sequence == 1


@pytest.mark.parametrize("amount", ["501", "1000"])
def test_per_order_cap_refuses_before_reserving(local_risk, amount):
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk, amount=amount)
    assert not local_risk.store.has_order_reservation_sync(local_risk.manifest.run_id, "entry")


def test_account_aggregate_includes_all_outstanding_reservations(local_risk):
    for index in range(4):
        reserve(local_risk, order=str(index))
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk, order="fifth")
    assert [snapshot(local_risk, str(i)).reserved_notional for i in range(4)] == [
        Decimal("500")
    ] * 4
    exposure = asyncio.run(local_risk.store.load_runner_exposure(local_risk.permit.permit_id))
    assert exposure.total_exposure == Decimal("2000")


def test_unknown_terminal_result_never_releases_pending_risk(local_risk):
    original = reserve(local_risk)
    with pytest.raises(RuntimeAuthorityError):
        local_risk.store.release_order_reservation_sync(
            event_id="uncertain",
            runtime_id=local_risk.manifest.run_id,
            client_order_id="entry",
            reason="unknown",
        )
    assert snapshot(local_risk) == original


def test_confirmed_release_retains_filled_cost(local_risk):
    reserve(local_risk)
    fill(local_risk, amount="400")
    local_risk.store.release_order_reservation_sync(
        event_id="confirmed-cancel",
        runtime_id=local_risk.manifest.run_id,
        client_order_id="entry",
        reason="canceled",
    )
    result = snapshot(local_risk)
    assert result.reserved_notional == 0
    assert result.filled_exposure == Decimal("400")


def test_native_fill_replay_is_accounted_once_after_owner_restart(local_risk):
    risk = local_risk
    reserve(risk)
    first = fill(risk)
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    assert recovered.lease.fence.process_generation > risk.lease.fence.process_generation
    assert fill(recovered) == first
    assert snapshot(recovered).filled_exposure == Decimal("500")
    assert recovered.events.identity().sequence == 2


def test_expiry_refuses_reservation_but_keeps_late_executed_fact(local_risk):
    reserve(local_risk)
    local_risk.clock[0] = NOW + timedelta(days=2)
    with pytest.raises(RuntimeAuthorityError):
        reserve(local_risk, order="expired")
    fill(local_risk)
    assert snapshot(local_risk).filled_exposure == Decimal("500")


def test_overfilled_fact_and_risk_latch_survive_the_reported_error(local_risk):
    risk = local_risk
    reserve(risk)
    with pytest.raises(RuntimeAuthorityError):
        fill(risk, amount="600")
    assert snapshot(risk).filled_exposure == Decimal("600")
    assert risk.blocker.is_frozen()
    risk.lease.close()
    restored = risk.make(previous=risk, recover=True)
    assert restored.blocker.is_frozen()
    assert snapshot(restored).filled_exposure == Decimal("600")


def test_conflicting_venue_fact_preserves_original_and_persists_freeze(local_risk):
    reserve(local_risk)
    fill(local_risk)
    with pytest.raises(RuntimeAuthorityError):
        fill(local_risk, amount="499")
    assert snapshot(local_risk).filled_exposure == Decimal("500")
    local_risk.lease.close()
    restored = local_risk.make(previous=local_risk, recover=True)
    assert restored.blocker.is_frozen()


def test_old_run_fill_cannot_be_reapplied_under_a_fresh_permit(local_risk):
    risk = local_risk
    reserve(risk)
    fill(risk)
    risk.lease.retire()
    risk.lease.close()
    next_run = risk.make(previous=risk)
    with pytest.raises(RuntimeAuthorityError):
        fill(next_run)
    assert next_run.blocker.is_frozen()
    assert risk.events.identity().sequence >= 2
    with closing(sqlite3.connect(risk.events.path)) as connection:
        stored = connection.execute(
            "SELECT filled_exposure FROM local_order_reservation WHERE client_order_id='entry'"
        ).fetchall()
    assert stored == [("500",)]


@pytest.mark.parametrize("target", ["event", "risk"])
def test_failed_half_of_transaction_leaves_neither_event_nor_reservation(local_risk, target):
    table = "execution_event" if target == "event" else "local_order_reservation"
    with closing(sqlite3.connect(local_risk.events.path)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected write failure'); END"
        )
    with pytest.raises(RuntimeStorageUnavailable):
        reserve(local_risk)
    assert local_risk.events.identity().sequence == 0
    assert not local_risk.store.has_order_reservation_sync(local_risk.manifest.run_id, "entry")
    assert local_risk.blocker.is_frozen()


def test_watermark_failure_after_commit_cannot_undo_the_reservation(local_risk, monkeypatch):
    original = local_risk.lease.assert_current
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeStorageUnavailable("injected registry observation failure")
        original()

    with monkeypatch.context() as patch:
        patch.setattr(local_risk.lease, "assert_current", unavailable)
        with pytest.raises(RuntimeStorageUnavailable):
            reserve(local_risk)
    assert snapshot(local_risk).reserved_notional == Decimal("500")
    assert local_risk.events.identity().sequence == 1
    assert local_risk.blocker.is_frozen()


def test_registered_database_loss_is_never_repaired_with_an_empty_file(local_risk):
    local_risk.events.path.rename(local_risk.events.path.with_suffix(".retained"))
    with pytest.raises(RuntimeStorageUnavailable):
        reserve(local_risk)
    assert not local_risk.events.path.exists()
    assert local_risk.blocker.is_frozen()


def test_reservation_math_does_not_inherit_ambient_decimal_precision(local_risk):
    with localcontext() as context:
        context.prec = 1
        reserve(local_risk)
        fill(local_risk, amount="499")
    assert snapshot(local_risk).filled_exposure == Decimal("499")


@pytest.mark.parametrize("amount", [500.0, True, "500", Decimal("NaN"), Decimal("Infinity")])
def test_money_inputs_are_typed_finite_decimals(local_risk, amount):
    with pytest.raises((RuntimeAuthorityError, ValueError, TypeError)):
        local_risk.store.reserve_order_notional_sync(
            event_id="invalid",
            runtime_id=local_risk.manifest.run_id,
            client_order_id="invalid",
            policy_id=local_risk.permit.permit_id,
            requested_notional=amount,
        )
    assert local_risk.events.identity().sequence == 0


@pytest.mark.parametrize("damage", ["engine_identity", "float_amount"])
def test_unusable_fill_freezes_durably_without_inventing_an_execution(local_risk, damage):
    risk = local_risk
    reserve(risk)
    token = hashlib.sha256(b"unusable-trade").hexdigest()
    source = "engine" if damage == "engine_identity" else "venue"
    with pytest.raises(RuntimeAuthorityError):
        risk.store.record_order_fill_sync(
            event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:{source}:{token}:entry",
            runtime_id=risk.manifest.run_id,
            client_order_id="entry",
            fill_notional=500.0 if damage == "float_amount" else Decimal("500"),
            fill_quantity=Decimal("0.005"),
            leaves_quantity=Decimal(0),
            position_id="position-entry",
            instrument_id="BTC-USD-PERP.PAPERTRADE",
            side="BUY",
        )
    assert snapshot(risk).filled_exposure == 0
    assert risk.blocker.is_frozen()
    risk.lease.close()
    restored = risk.make(previous=risk, recover=True)
    assert restored.blocker.is_frozen()


def test_unapplied_partial_close_keeps_an_immutable_venue_fact(local_risk):
    risk = local_risk
    reserve(risk)
    fill(risk)
    token = hashlib.sha256(b"partial-close").hexdigest()
    fields = dict(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:position-entry",
        runtime_id=risk.manifest.run_id,
        position_id="position-entry",
        reduction_notional=Decimal("100"),
        reduction_quantity=Decimal("0.001"),
    )
    with pytest.raises(RuntimeAuthorityError):
        risk.store.record_position_reduction_fifo_sync(**fields)
    with pytest.raises(RuntimeAuthorityError):
        risk.store.record_position_reduction_fifo_sync(
            **{
                **fields,
                "reduction_notional": Decimal("500"),
                "reduction_quantity": Decimal("0.005"),
            }
        )
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert snapshot(risk).filled_quantity == Decimal("0.005")
    assert risk.blocker.is_frozen()


def test_unknown_order_fill_is_retained_as_pending_venue_evidence(local_risk):
    risk = local_risk
    with pytest.raises(RuntimeAuthorityError):
        fill(risk, order="unknown-order")
    with closing(sqlite3.connect(risk.events.path)) as connection:
        row = connection.execute(
            "SELECT original_run_id,outcome_json FROM local_venue_fill"
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert "error_code" in row[1]
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize("relaxed_lease", [False, True])
def test_competing_reservations_cannot_overbook_even_with_outer_fence_relaxed(
    local_risk, monkeypatch, relaxed_lease
):
    risk = local_risk
    if relaxed_lease:
        monkeypatch.setattr(risk.lease, "control_action", nullcontext)
        monkeypatch.setattr(risk.lease, "assert_current", lambda: None)

    def compete(index):
        try:
            reserve(risk, str(index))
        except RuntimeAuthorityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=6) as workers:
        outcomes = list(workers.map(compete, range(6)))
    assert sum(outcomes) == 4
    accepted = [
        snapshot(risk, str(index)).reserved_notional
        for index, result in enumerate(outcomes)
        if result
    ]
    assert sum(accepted) == Decimal("2000")


@pytest.mark.parametrize("exit_notional", ["400", "600"])
async def test_full_position_exit_releases_entry_cost_and_is_replay_safe(local_risk, exit_notional):
    risk = local_risk
    reserve(risk)
    fill(risk)
    token = hashlib.sha256(b"full-close").hexdigest()
    fields = dict(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill_reduce:fill-source-v1:venue:{token}:position-entry",
        runtime_id=risk.manifest.run_id,
        position_id="position-entry",
        reduction_notional=Decimal(exit_notional),
        reduction_quantity=Decimal("0.005"),
    )
    first = risk.store.record_position_reduction_fifo_sync(**fields)
    assert first.filled_exposure == 0
    assert first.filled_quantity == 0
    assert first.state == "closed"
    assert risk.store.record_position_reduction_fifo_sync(**fields) == first
    exposure = await risk.store.load_runner_exposure(risk.permit.permit_id)
    assert exposure.total_exposure == 0
    assert exposure.within_policy
    assert risk.events.identity().sequence == 3


def test_proportional_pending_reservation_rounds_conservatively(local_risk):
    risk = local_risk
    reserve(risk)
    token = hashlib.sha256(b"partial-opening").hexdigest()
    result = risk.store.record_order_fill_sync(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{token}:entry",
        runtime_id=risk.manifest.run_id,
        client_order_id="entry",
        fill_notional=Decimal("165"),
        fill_quantity=Decimal("1"),
        leaves_quantity=Decimal("2"),
        position_id="position-entry",
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="BUY",
    )
    with localcontext() as context:
        context.prec = 200
        exact = Decimal("1000") / Decimal(3)
        assert result.reserved_notional >= exact
        assert result.reserved_notional - exact < Decimal("1e-90")


def test_registry_probe_never_runs_while_risk_sqlite_transaction_is_held(local_risk):
    risk = local_risk
    observations = []

    def require_unlocked(path):
        if path == risk.events.path:
            with closing(sqlite3.connect(path, timeout=0)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            observations.append(path)

    risk.probe_hooks.append(require_unlocked)
    reserve(risk)
    fill(risk)
    assert len(observations) >= 4


@pytest.mark.parametrize(
    "table",
    ["local_risk_meta", "local_order_reservation", "local_venue_fill", "local_reduction_claim"],
)
def test_missing_risk_schema_is_refused_without_recreation(local_risk, table):
    from custos.core.execution_events import LocalExecutionRecorder

    risk = local_risk
    LocalExecutionRecorder(risk.events, risk.lease, configuration_revision=1).record_local_refusal(
        client_order_id="refused",
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="BUY",
        reason_code="test_refusal",
    )
    with closing(sqlite3.connect(risk.events.path)) as connection, connection:
        connection.execute(f"DROP TABLE {table}")
    fresh_blocker = _Blocker()
    with pytest.raises(RuntimeStorageUnavailable):
        importlib.import_module("custos.local.reservations").LocalReservationStore(
            store=risk.events,
            lease=risk.lease,
            authorization=risk.authorization,
            risk_blocker=fresh_blocker,
            clock=lambda: NOW,
        )
    assert fresh_blocker.is_frozen()
    assert len(risk.events.events(risk.manifest.run_id)) == 1
    with closing(sqlite3.connect(risk.events.path)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        )


def test_same_run_and_epoch_in_another_registry_cannot_use_the_permit(local_risk, tmp_path):
    risk = local_risk
    other = RuntimeAuthorityRegistry.initialize(
        tmp_path / "other-control",
        identity_probe=lambda path: (
            ExecutionEventStore.open(path).identity() if path == risk.events.path else probe(path)
        ),
    )
    blocker = _Blocker()
    with other.claim(
        account(),
        run_id=risk.manifest.run_id,
        permit_digest=risk.authorization.permit_digest,
        expected_authorization_epoch=0,
        stores=risk.bindings,
    ) as wrong_lease:
        with pytest.raises(RuntimeAuthorityError, match="actual lease"):
            importlib.import_module("custos.local.reservations").LocalReservationStore(
                store=risk.events,
                lease=wrong_lease,
                authorization=risk.authorization,
                risk_blocker=blocker,
                clock=lambda: NOW,
            )
    assert blocker.is_frozen()
    assert risk.events.identity().sequence == 0


def test_default_decimal_context_cannot_shrink_the_local_risk_domain():
    script = """
from decimal import DefaultContext, Overflow
DefaultContext.Emax = 2
DefaultContext.traps[Overflow] = False
import pytest
raise SystemExit(pytest.main(['tests/test_local_reservations.py::test_account_aggregate_includes_all_outstanding_reservations', '-q']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=40
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.filterwarnings("ignore:This process .*multi-threaded:DeprecationWarning")
def test_fork_copy_refuses_before_waiting_for_the_parent_action_lock(local_risk):
    from custos.core.runtime_authority import RuntimeFenceStale

    risk = local_risk
    entered, release = threading.Event(), threading.Event()

    def hold():
        with risk.lease.control_action():
            entered.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(3)
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        try:
            reserve(risk, "forked")
        except RuntimeFenceStale:
            os.write(writer, b"fenced")
        except Exception as error:
            os.write(writer, type(error).__name__.encode())
        else:
            os.write(writer, b"unfenced")
        os._exit(0)
    os.close(writer)
    try:
        ready, _, _ = select.select([reader], [], [], 3)
        assert ready, "fork copy waited for an inherited parent lock"
        assert os.read(reader, 128) == b"fenced"
    finally:
        release.set()
        worker.join(3)
        os.close(reader)
        waited, _ = os.waitpid(child, os.WNOHANG)
        if waited == 0:
            os.kill(child, 9)
            os.waitpid(child, 0)
    assert not risk.blocker.is_frozen()


def test_watermark_failure_after_fill_keeps_the_committed_venue_fact(local_risk, monkeypatch):
    risk = local_risk
    reserve(risk)
    original = risk.lease.assert_current
    calls = 0

    def unavailable():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeStorageUnavailable("injected registry observation failure")
        original()

    with monkeypatch.context() as patch:
        patch.setattr(risk.lease, "assert_current", unavailable)
        with pytest.raises(RuntimeStorageUnavailable):
            fill(risk)
    assert snapshot(risk).filled_exposure == Decimal("500")
    assert risk.events.identity().sequence == 2
    assert risk.blocker.is_frozen()
    risk.lease.close()
    recovered = risk.make(previous=risk, recover=True)
    fill(recovered)
    assert recovered.events.identity().sequence == 2


@pytest.mark.parametrize("metadata", ["bad\nposition", "x" * 513])
def test_unusable_fact_metadata_is_quarantined_without_copying_unsafe_values(local_risk, metadata):
    risk = local_risk
    reserve(risk)
    token = hashlib.sha256(b"unusable-metadata").hexdigest()
    with pytest.raises(RuntimeAuthorityError):
        risk.store.record_order_fill_sync(
            event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{token}:entry",
            runtime_id=risk.manifest.run_id,
            client_order_id="entry",
            fill_notional=Decimal("500"),
            fill_quantity=Decimal("0.005"),
            leaves_quantity=Decimal(0),
            position_id=metadata,
            instrument_id="BTC-USD-PERP.PAPERTRADE",
            side="BUY",
        )
    assert snapshot(risk).filled_exposure == 0
    records = risk.events.risk_events(risk.manifest.run_id)
    assert metadata not in " ".join(record.risk.payload() for record in records)
    assert risk.blocker.is_frozen()
    risk.lease.close()
    restored = risk.make(previous=risk, recover=True)
    assert restored.blocker.is_frozen()


def test_another_genuine_prepared_permit_cannot_replace_the_claimed_permit(local_risk):
    risk = local_risk
    other = risk.alternate_authorization
    assert other.manifest.run_id == risk.manifest.run_id
    assert other.permit.authorization_epoch == risk.permit.authorization_epoch
    assert other.permit.control_registry_id == risk.permit.control_registry_id
    assert other.permit_digest != risk.authorization.permit_digest
    assert other.manifest.limits.max_order_notional_usd == "1000"
    blocker = _Blocker()
    with pytest.raises(RuntimeAuthorityError, match="actual lease"):
        importlib.import_module("custos.local.reservations").LocalReservationStore(
            store=risk.events,
            lease=risk.lease,
            authorization=other,
            risk_blocker=blocker,
            clock=lambda: NOW,
        )
    assert blocker.is_frozen()
    assert risk.events.identity().sequence == 0


def test_lost_account_latch_cannot_be_recreated_as_unfrozen(local_risk):
    risk = local_risk
    reserve(risk)
    with pytest.raises(RuntimeAuthorityError):
        fill(risk, amount="600")
    assert risk.blocker.is_frozen()
    before = risk.events.identity()
    risk.lease.close()
    with closing(sqlite3.connect(risk.events.path)) as connection, connection:
        connection.execute("DELETE FROM local_account_risk")
    blocker = _Blocker()
    with pytest.raises(RuntimeStorageUnavailable):
        risk.make(previous=risk, recover=True, blocker=blocker)
    assert blocker.is_frozen()
    assert risk.events.identity() == before
    with closing(sqlite3.connect(risk.events.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_account_risk").fetchone()[0] == 0


def test_extreme_valid_fill_keeps_a_conservatively_representable_reservation(local_risk):
    risk = local_risk
    reserve(risk, amount="100")
    token = hashlib.sha256(b"extreme-quantity").hexdigest()
    fields = dict(
        event_id=f"runner-order-reservation:{risk.manifest.run_id}:fill:fill-source-v1:venue:{token}:entry",
        runtime_id=risk.manifest.run_id,
        client_order_id="entry",
        fill_notional=Decimal("1"),
        fill_quantity=Decimal("999999999999999999"),
        leaves_quantity=Decimal("0.000000000000000001"),
        position_id="position-entry",
        instrument_id="BTC-USD-PERP.PAPERTRADE",
        side="BUY",
    )
    result = risk.store.record_order_fill_sync(**fields)
    with localcontext() as context:
        context.prec = 200
        exact = (
            Decimal("100")
            * fields["leaves_quantity"]
            / (fields["leaves_quantity"] + fields["fill_quantity"])
        )
        assert result.reserved_notional >= exact
        assert result.reserved_notional - exact <= Decimal("1e-120")
    assert result.filled_exposure == Decimal("1")
    assert not risk.blocker.is_frozen()
    with pytest.raises(RuntimeAuthorityError):
        risk.store.record_order_fill_sync(**{**fields, "fill_quantity": Decimal("1")})
    assert snapshot(risk).filled_exposure == Decimal("1")
    assert risk.blocker.is_frozen()


@pytest.mark.parametrize("reader", ["has", "load", "exposure"])
def test_storage_read_failure_freezes_the_bound_native_blocker(local_risk, reader):
    risk = local_risk
    risk.events.path.rename(risk.events.path.with_suffix(".retained"))
    with pytest.raises(RuntimeStorageUnavailable):
        if reader == "has":
            risk.store.has_order_reservation_sync(risk.manifest.run_id, "entry")
        elif reader == "load":
            snapshot(risk)
        else:
            asyncio.run(risk.store.load_runner_exposure(risk.permit.permit_id))
    assert risk.blocker.is_frozen()
    assert not risk.events.path.exists()


def test_failure_after_economic_update_retains_pending_fact_without_partial_amounts(
    local_risk, monkeypatch
):
    risk = local_risk
    reserve(risk)
    observed = []

    def fail_after_update(row):
        observed.append((row["reserved_notional"], row["filled_exposure"]))
        assert row["filled_exposure"] == "500"
        raise RuntimeAuthorityError("injected semantic failure after economic update")

    with monkeypatch.context() as patch:
        patch.setattr(risk.store._ledger, "snapshot", fail_after_update)
        with pytest.raises(RuntimeAuthorityError, match="local_fact_application_unreliable"):
            fill(risk)
    assert observed == [("0", "500")]
    result = snapshot(risk)
    assert result.reserved_notional == Decimal("500")
    assert result.filled_exposure == 0
    assert result.filled_quantity == 0
    records = risk.events.risk_events(risk.manifest.run_id)
    assert len(records) == 2
    assert records[-1].risk.outcome["error_code"] == "local_fact_application_unreliable"
    with closing(sqlite3.connect(risk.events.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_venue_fill").fetchone()[0] == 1
        assert connection.execute("SELECT reason_code FROM local_account_risk").fetchone()[0]
    assert risk.blocker.is_frozen()
    with pytest.raises(RuntimeAuthorityError, match="local_fact_application_unreliable"):
        fill(risk)
    assert snapshot(risk) == result
    assert risk.events.identity().sequence == 2
