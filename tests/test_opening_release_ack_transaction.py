"""Owner-only semantic fixtures; no installed complete native model is claimed."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.core.execution_reservation_store import PlatformOpeningGate, RunnerOpeningGateClosed
from custos.core.opening_inventory_contract import require_opening_inventory_support
from custos.core.opening_observation import AdmittedNativeCashSample, OpeningObservationBundle
from custos.core.platform_process_fence import PlatformProcessRegistry
from custos.core.runner_fact import _canonical_json_bytes, runner_fact_capability_shape
from custos.core.runner_fact_producer import RunnerFactObservationToken
from tests.test_runner_fact_opening_bundle import _future_native_bundle
from tests.test_runner_fact_store import _runner_authority, _runner_fact_store, _verified_command
from tests.test_runner_opening_release_command import local_release, signed


def signed_generation(generation=1, lifecycle="running", issued_at=None):
    from custos.contracts.deployment import canonical_deployment_spec_digest

    def mutate(event):
        payload = event["payload"]
        payload["deployment_spec"]["execution_config"]["connector"] = "binance"
        payload["deployment_spec_digest"] = canonical_deployment_spec_digest(
            payload["deployment_spec"]
        )
        payload["generation"] = event["aggregate_version"] = generation
        payload["lifecycle_state"] = lifecycle
        if issued_at is not None:
            payload["issued_at"] = event["occurred_at"] = issued_at

    return _verified_command(mutate_event=mutate)[2]


@pytest.fixture
async def opening(tmp_path, request):
    from decimal import Decimal

    from custos.core.runner_fact import _signed_decimal
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

    _, store = _runner_fact_store(tmp_path / "runner.sqlite3")
    verified = signed_generation()
    await store.record_desired_command(
        command=verified.command,
        command_fingerprint=verified.command_fingerprint,
        verification_receipt=verified.verification_receipt,
    )
    context = store.platform_process_snapshot(
        EngineLifecycleAuthority.from_verified_command(verified)
    ).context
    account = resolve_platform_account_identity(
        context, build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
    )
    desired = await store.load_durable_desired_command(
        account.context.authority.deployment_instance_id
    )
    projectors, contracts = runner_fact_capability_shape(opening_release=True)
    contracts["risk"]["opening_inventory"] = {
        "schema_revision": 1,
        "source_models": ["unavailable", "sandbox-account-state-v1"],
        "capture_apis": ["execution-account-capture-v1"],
        "mapping_revisions": ["native_currency_codes_v1"],
        "runtime_artifact_digests": ["e" * 64],
        "complete_models": ["sandbox-account-state-v1"],
    }
    manifest = {"fact_kind_projectors": projectors, "runner_fact_contracts": contracts}
    digest = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    store._authority_resolver = lambda command: replace(
        _runner_authority(command),
        capability_manifest_digest=digest,
        strategy_id=command.command.strategy_id,
    )
    authority = store._authority_resolver(desired)
    sample = _future_native_bundle(authority=authority)
    inventory = dict(sample.inventory)
    inventory["scope"]["connector"] = account.context.spec["connector"]
    amount_input = request.param if hasattr(request, "param") else "1"
    amount = _signed_decimal(Decimal(amount_input), "fixture equity", bounded=True)
    inventory["equity_binding"]["amount"] = amount
    inventory["observations"]["balances"]["rows"][0].update(total=amount, available=amount)
    require_opening_inventory_support(inventory, manifest)
    bundle = OpeningObservationBundle(
        authority,
        RunnerFactObservationToken(desired.command_fingerprint, 1, 1),
        sample.independent,
        sample.trading_gate,
        inventory,
        AdmittedNativeCashSample.from_inventory(inventory, authority, "USDT", manifest),
        sample.currency,
    )
    object.__setattr__(bundle.native, "equity", Decimal(amount_input))
    authenticator, subject, event = local_release(desired)
    event["occurred_at"] = event["payload"]["issued_at"] = "2026-11-04T00:00:00Z"
    event["payload"]["basis_amount"] = amount
    event["payload"]["account_identity"] = bundle.inventory["observations"]["identity"]["rows"][0]
    await store.record_opening_release(
        authenticator, subject=subject, signed_envelope=signed(subject, event)
    )
    registry = PlatformProcessRegistry.initialize(tmp_path / "authority", host_scope_id=uuid4())
    registry.register_store(store)
    lease = registry.claim_closed(store, account)
    gate = PlatformOpeningGate(store, EngineLifecycleAuthority.from_verified_command(desired))
    gate.bind_process_lease(lease)
    import asyncio
    from types import SimpleNamespace

    from custos.engines.nautilus.host import NtTradingNodeHost

    host = NtTradingNodeHost()
    instance = str(authority.deployment_instance_id)
    host._active_nodes[instance] = SimpleNamespace(task=asyncio.Future())
    host._runner_fact_contexts[instance] = (
        SimpleNamespace(
            authority=authority,
            connector=inventory["scope"]["connector"],
            currency="USDT",
            deployment_instance_id=instance,
        ),
        None,
    )
    host._lifecycle_authorities[instance] = gate.authority
    host._runner_safety_boundaries[instance] = SimpleNamespace(platform_opening_gate=gate)
    host._platform_process_leases[instance] = lease
    bundle = OpeningObservationBundle(
        authority,
        await host.runner_fact_observation_token(authority),
        sample.independent,
        sample.trading_gate,
        inventory,
        bundle.native,
        sample.currency,
    )
    store._test_host = host
    store._test_observation_owner = await host.runner_fact_opening_observation_owner(
        authority, bundle.token
    )
    try:
        yield store, registry, lease, gate, authenticator, event, manifest, bundle
    finally:
        lease.close()
        registry.close()


async def test_successful_ack_commits_signed_bytes_before_the_outbound_gate_opens(opening):
    store, _, lease, gate, auth, event, manifest, bundle = opening
    result = await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    batch_id, payload = result
    fact = json.loads(payload)["facts"][0]
    assert fact["outcome"] == "acknowledged"
    assert fact["reason"] is None
    assert fact["seq"] == 1
    assert str(batch_id) == json.loads(payload)["batch_id"]
    with gate.control_action():
        gate.assert_released()
    assert (
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
        == result
    )


async def test_a_process_retired_during_signing_cannot_commit_an_ack(opening, monkeypatch):
    store, _, lease, gate, auth, event, manifest, bundle = opening
    original = store._identity.sign_batch_payload

    def sign(payload):
        lease.close()
        return original(payload)

    monkeypatch.setattr(store._identity, "sign_batch_payload", sign)
    with pytest.raises(Exception, match="stale"):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    with store._outbox._connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM runner_opening_acknowledgements").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 0
    with pytest.raises(RunnerOpeningGateClosed):
        gate.assert_released()


async def test_the_existing_observation_loop_commits_a_matching_release(opening):
    from types import SimpleNamespace

    from custos.core.runner_fact_producer import observe_pending_opening_release

    store, _, lease, gate, auth, _, manifest, bundle = opening

    class Host:
        async def deployment_ready(self, instance):
            return True

        async def runner_fact_observation_token(self, authority):
            return bundle.token

        async def runner_fact_opening_bundle(self, instance, currency):
            return bundle

        async def runner_fact_opening_process_lease(self, authority, token):
            assert authority == bundle.authority and token == bundle.token
            return lease

        async def runner_fact_opening_observation_owner(self, authority, token):
            return store._test_observation_owner

    await observe_pending_opening_release(
        SimpleNamespace(
            authority=bundle.authority,
            deployment_instance_id=str(bundle.authority.deployment_instance_id),
            currency="USDT",
            connector=bundle.inventory["scope"]["connector"],
        ),
        host=Host(),
        state_store=store,
        authenticator=auth,
        manifest=manifest,
    )
    with gate.control_action():
        gate.assert_released()


async def test_commit_time_failure_rolls_back_without_poisoning_the_held_lease(
    opening, monkeypatch
):
    import sqlite3

    store, _, lease, gate, auth, event, manifest, bundle = opening
    with store._outbox._connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_ack_commit AFTER INSERT ON runner_opening_acknowledgements BEGIN INSERT INTO runner_opening_permissions VALUES(NEW.instance_id,NEW.generation,'unbound-owner',1,NEW.command_fingerprint,'missing-release','{}',0); END"
        )
    connect = store._outbox._connect

    def deferred_connection():
        connection = connect()
        connection.execute("PRAGMA defer_foreign_keys=ON")
        return connection

    monkeypatch.setattr(store._outbox, "_connect", deferred_connection)
    before = lease._sequence
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    assert lease._sequence == before
    with store._outbox._connect() as connection:
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM runner_opening_permissions").fetchone()[0] == 0
        )
        connection.execute("DROP TRIGGER fail_ack_commit")
    await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    with gate.control_action():
        gate.assert_released()


async def test_same_generation_host_retirement_during_signing_rejects_the_observation(
    opening, monkeypatch
):
    store, _, lease, _, auth, event, manifest, bundle = opening
    host = store._test_host
    instance = str(bundle.authority.deployment_instance_id)
    original = store._identity.sign_batch_payload

    def sign(payload):
        host._retire_opening_observation(instance)
        return original(payload)

    monkeypatch.setattr(store._identity, "sign_batch_payload", sign)
    with pytest.raises(Exception, match="observation.*retired"):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    with store._outbox._connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM runner_opening_acknowledgements").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["signature", "permission"])
async def test_signing_or_permission_failure_keeps_sequence_and_gate_closed(
    opening, monkeypatch, fault
):
    store, _, lease, gate, auth, event, manifest, bundle = opening
    if fault == "signature":

        def failed_sign(_payload):
            raise RuntimeError("injected signing failure")

        monkeypatch.setattr(store._identity, "sign_batch_payload", failed_sign)
    else:
        with store._outbox._connect() as connection:
            connection.execute(
                "CREATE TRIGGER fail_permission BEFORE INSERT ON runner_opening_permissions BEGIN SELECT RAISE(ABORT,'injected permission failure'); END"
            )
    with pytest.raises((RuntimeError, sqlite3.IntegrityError), match="injected"):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    with store._outbox._connect() as connection:
        for table in (
            "runner_fact_stream",
            "runner_fact_seen_event",
            "runner_fact_outbox",
            "runner_opening_acknowledgements",
            "runner_opening_permissions",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(RunnerOpeningGateClosed):
        gate.assert_released()


@pytest.mark.parametrize("lifecycle", ["paused", "stopped", "archived"])
async def test_signed_nonrunning_command_wins_before_ack_and_cannot_be_reopened(opening, lifecycle):
    from custos.core.runner_fact import RunnerStateAuthorityError

    store, _, lease, gate, auth, event, manifest, bundle = opening
    next_command = signed_generation(2, lifecycle)
    await store.record_desired_command(
        command=next_command.command,
        command_fingerprint=next_command.command_fingerprint,
        verification_receipt=next_command.verification_receipt,
    )
    with pytest.raises(RunnerStateAuthorityError):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    with pytest.raises(RunnerOpeningGateClosed):
        gate.assert_released()


async def test_trade_fact_before_ack_is_retained_and_prevents_initial_release(opening):
    from custos.core.runner_fact import RunnerStateAuthorityError, execution_fill

    store, _, lease, gate, auth, event, manifest, bundle = opening
    trade = execution_fill(
        event_id=uuid4(),
        venue="BINANCE",
        venue_trade_id="early-trade",
        venue_order_id="early-order",
        instrument="BTCUSDT",
        side="BUY",
        quantity="1",
        price="1",
        fee="0",
        currency="USDT",
        occurred_at="2026-11-04T00:00:00Z",
    )
    store._outbox.enqueue_sync(bundle.authority, store._identity, [trade])
    with pytest.raises(RunnerStateAuthorityError, match="traded before"):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    with store._outbox._connect() as connection:
        assert connection.execute("SELECT count(*) FROM runner_fact_outbox").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM runner_opening_acknowledgements").fetchone()[0]
            == 0
        )
    with pytest.raises(RunnerOpeningGateClosed):
        gate.assert_released()


@pytest.mark.parametrize("field", ["generation", "deployment_spec_id", "deployment_spec_digest"])
async def test_bad_composed_authority_is_rejected_before_signing_or_sequence_allocation(
    opening, monkeypatch, field
):
    from types import SimpleNamespace

    from custos.core.runner_fact import RunnerStateAuthorityError

    store, _, lease, _, auth, event, manifest, bundle = opening
    wrong = replace(
        bundle.authority,
        **{
            field: (
                bundle.authority.generation + 1
                if field == "generation"
                else uuid4()
                if field == "deployment_spec_id"
                else "f" * 64
            )
        },
    )
    store._authority_resolver = lambda _: wrong
    inventory = dict(bundle.inventory)
    inventory["scope"][field] = (
        str(getattr(wrong, field))
        if isinstance(getattr(wrong, field), UUID)
        else getattr(wrong, field)
    )
    host = store._test_host
    instance = str(bundle.authority.deployment_instance_id)
    original_context = host._runner_fact_contexts[instance]
    host._runner_fact_contexts[instance] = (SimpleNamespace(authority=wrong), None)
    token = await host.runner_fact_observation_token(wrong)
    owner = await host.runner_fact_opening_observation_owner(wrong, token)
    observation = OpeningObservationBundle(
        wrong,
        token,
        bundle.independent,
        "awaiting_release",
        inventory,
        AdmittedNativeCashSample.from_inventory(inventory, wrong, "USDT", manifest),
        "USDT",
    )
    signed_payloads = []
    sign = store._identity.sign_batch_payload

    def tracked_sign(payload):
        signed_payloads.append(payload)
        return sign(payload)

    monkeypatch.setattr(store._identity, "sign_batch_payload", tracked_sign)
    with pytest.raises(RunnerStateAuthorityError, match="authority"):
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=observation,
            observation_owner=owner,
        )
    assert signed_payloads == []
    with store._outbox._connect() as connection:
        assert connection.execute("SELECT count(*) FROM runner_fact_stream").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM runner_opening_acknowledgements").fetchone()[0]
            == 0
        )
    store._authority_resolver = lambda _: bundle.authority
    host._runner_fact_contexts[instance] = original_context
    correct_owner = await host.runner_fact_opening_observation_owner(bundle.authority, bundle.token)
    _, payload = await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=correct_owner,
    )
    assert json.loads(payload)["generation"] == bundle.authority.generation


async def test_released_equity_can_follow_the_committed_ack_in_the_real_outbox(opening):
    from custos.core.opening_observation import ObservabilityFactPlan
    from custos.core.runner_fact import equity_snapshot

    store, _, lease, _, auth, event, manifest, bundle = opening
    _, ack = await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    fact = equity_snapshot(
        event_id=uuid4(),
        amount="1",
        currency="USDT",
        observed_at=bundle.native.observed_at,
        trading_gate="released",
        account_inventory=bundle.inventory,
    )
    batch_id = await store._outbox.enqueue_observability(
        bundle.authority,
        store._identity,
        ObservabilityFactPlan.from_facts([fact], None),
        bundle.token.command_fingerprint,
        opening_owner=store._test_observation_owner,
    )
    with store._outbox._connect() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM runner_fact_outbox WHERE batch_id=?", (str(batch_id),)
            ).fetchone()[0]
        )
    assert payload["facts"][0]["trading_gate"] == "released"
    assert payload["facts"][0]["seq"] > json.loads(ack)["facts"][0]["seq"]


@pytest.mark.parametrize(
    "opening", ["0.00000001", "1.2300", "0.0000000000000000000000000001"], indirect=True
)
async def test_actual_signed_ack_serializes_exact_canonical_decimal_values(opening):
    store, _, lease, _, auth, event, manifest, bundle = opening
    _, payload = await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    fact = json.loads(payload)["facts"][0]
    assert fact["amount"] == event["payload"]["basis_amount"]
    assert "E" not in fact["amount"] and "e" not in fact["amount"]


async def test_retired_owner_cannot_sign_released_equity_using_historical_permission(opening):
    from custos.core.opening_observation import ObservabilityFactPlan
    from custos.core.runner_fact import RunnerStateAuthorityError, equity_snapshot

    store, _, lease, _, auth, event, manifest, bundle = opening
    await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    lease.close()
    fact = equity_snapshot(
        event_id=uuid4(),
        amount="1",
        currency="USDT",
        observed_at=bundle.native.observed_at,
        trading_gate="released",
        account_inventory=bundle.inventory,
    )
    with pytest.raises(RunnerStateAuthorityError):
        await store._outbox.enqueue_observability(
            bundle.authority,
            store._identity,
            ObservabilityFactPlan.from_facts([fact], None),
            bundle.token.command_fingerprint,
        )


@pytest.mark.parametrize("reduce_only", [True, False])
async def test_first_dispatch_and_synchronous_trade_callback_use_the_existing_safety_port(
    opening, monkeypatch, reduce_only
):
    from custos.cli._daemon import _build_runner_safety_boundary_factory
    from custos.core.runner_fact import execution_fill
    from custos.engines.nautilus.runner_safety import RunnerSafetyOrderGate
    from tests.cli.test_runner_safety_daemon_composition import _Resolver
    from tests.engines.nautilus.test_runner_safety_execution_boundary import _order, _Semantics

    store, _, lease, _, auth, event, manifest, bundle = opening
    _, ack = await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    desired = await store.load_durable_desired_command(bundle.authority.deployment_instance_id)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tests.test_runner_policy_runtime import _verified_policy

    policy = _verified_policy(
        Ed25519PrivateKey.from_private_bytes(bytes([91]) * 32),
        tenant_id=bundle.authority.tenant_id,
        runner_id=bundle.authority.runner_id,
        expires_at="2030-01-01T00:00:00Z",
    )
    await store.record_verified_runner_safety_policy(policy)
    boundary = await _build_runner_safety_boundary_factory(
        state_store=store, safety_policy_resolver=_Resolver(policy_id=policy.policy.policy_id)
    )(desired.command.to_runtime_spec().model_dump(mode="python"))
    boundary.platform_opening_gate.bind_process_lease(lease)
    boundary.bind_runtime(semantics=_Semantics())
    refusals, sent = [], []
    gate = RunnerSafetyOrderGate(
        boundary=boundary, client_order_id_len_limit=None, on_refusal=refusals.append
    )
    connect = store._outbox._connect

    def fast_failure_connection():
        connection = connect()
        connection.execute("PRAGMA busy_timeout=50")
        return connection

    monkeypatch.setattr(store._outbox, "_connect", fast_failure_connection)

    def execution_port(order):
        sent.append(order.client_order_id)
        if not reduce_only:
            from decimal import Decimal

            assert store.has_order_reservation_sync(
                bundle.authority.deployment_instance_id, order.client_order_id
            )
            store.record_order_fill_sync(
                event_id="synchronous-first-fill",
                deployment_instance_id=bundle.authority.deployment_instance_id,
                client_order_id=order.client_order_id,
                fill_notional=Decimal("1"),
                fill_quantity=Decimal("1"),
                leaves_quantity=Decimal("0"),
            )
        fact = execution_fill(
            event_id=uuid4(),
            venue="BINANCE",
            venue_trade_id="first-trade",
            venue_order_id="first-order",
            instrument="BTCUSDT",
            side="BUY",
            quantity="1",
            price="1",
            fee="0",
            currency="USDT",
            occurred_at="2026-11-04T00:00:02Z",
        )
        store._outbox.enqueue_sync(bundle.authority, store._identity, [fact])

    assert gate.submit_order(execution_port, _order("first-order", reduce_only=reduce_only)) is True
    assert sent == ["first-order"] and refusals == []
    with store._outbox._connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM runner_fact_outbox ORDER BY source_seq_start"
        ).fetchall()
    trade = [
        fact
        for row in rows
        for fact in json.loads(row[0])["facts"]
        if fact["kind"] == "execution_fill"
    ]
    assert len(trade) == 1 and trade[0]["seq"] > json.loads(ack)["facts"][0]["seq"]
    if not reduce_only:
        reservation = store.load_order_reservation_sync(
            bundle.authority.deployment_instance_id, "first-order"
        )
        assert reservation.reserved_notional == 0 and reservation.filled_exposure == 1


async def test_same_generation_new_owner_rebinds_through_the_actual_host_read_chain(
    opening, monkeypatch
):
    import asyncio
    from decimal import Decimal
    from types import SimpleNamespace

    from custos.core.runner_fact_producer import observe_pending_opening_release
    from custos.engines.nautilus.native_opening_inventory import NativeOpeningAccountInventory
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox

    store, registry, first, _, auth, event, manifest, bundle = opening
    await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=first,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    first.close()
    host = store._test_host
    instance = str(bundle.authority.deployment_instance_id)
    context = store.platform_process_snapshot(first.authority).context
    account = resolve_platform_account_identity(
        context, build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
    )
    second = registry.claim_closed(store, account)
    gate = PlatformOpeningGate(store, first.authority)
    gate.bind_process_lease(second)
    host._platform_process_leases[instance] = second
    host._runner_safety_boundaries[instance] = SimpleNamespace(platform_opening_gate=gate)
    host._capability_receipt = SimpleNamespace(capability_manifest=manifest)

    async def ready(_instance):
        return True

    async def risk(_instance, _currency):
        return Decimal("1"), []

    monkeypatch.setattr(host, "deployment_ready", ready)
    monkeypatch.setattr(host, "runner_fact_risk_snapshot", risk)
    token = await host.runner_fact_observation_token(bundle.authority)

    def captured(fresh=False):
        from custos.core.utc_time import render_utc_nanos
        from tests.test_opening_inventory_contract import _retoken

        inventory = bundle.inventory
        if fresh:
            inventory["native_capture"]["observed_at_ns"] += 1_000_000_000
            inventory["native_capture"]["capture_sequence"] += 1
            at = render_utc_nanos(inventory["native_capture"]["observed_at_ns"])
            for read in inventory["observations"].values():
                read["source_time"] = at
            inventory["equity_binding"]["observed_at"] = at
            _retoken(inventory)
        future = asyncio.Future()
        future.set_result(NativeOpeningAccountInventory(None, inventory))
        host._opening_fact_reads[instance] = (token, future)

    try:
        captured()
        observed = await host.runner_fact_opening_bundle(instance, "USDT")
        assert observed.trading_gate == "awaiting_release"
        captured()
        await observe_pending_opening_release(
            host._runner_fact_contexts[instance][0],
            host=host,
            state_store=store,
            authenticator=auth,
            manifest=manifest,
        )
        with pytest.raises(RunnerOpeningGateClosed):
            gate.assert_released()
        captured(fresh=True)
        await observe_pending_opening_release(
            host._runner_fact_contexts[instance][0],
            host=host,
            state_store=store,
            authenticator=auth,
            manifest=manifest,
        )
        with gate.control_action():
            gate.assert_released()
    finally:
        second.close()


@pytest.mark.parametrize("rollback", [False, True])
async def test_all_synchronous_reservation_writes_and_reads_share_only_the_outer_transaction(
    opening, rollback, monkeypatch
):
    from decimal import Decimal

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from custos.core.runner_fact import RunnerStateAuthorityError
    from tests.test_runner_policy_runtime import _verified_policy

    store, _, lease, gate, auth, event, manifest, bundle = opening
    await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    policy = _verified_policy(
        Ed25519PrivateKey.from_private_bytes(bytes([91]) * 32),
        tenant_id=bundle.authority.tenant_id,
        runner_id=bundle.authority.runner_id,
        expires_at="2030-01-01T00:00:00Z",
    )
    await store.record_verified_runner_safety_policy(policy)
    instance = bundle.authority.deployment_instance_id
    connect = store._outbox._connect

    def fast_connection():
        value = connect()
        value.execute("PRAGMA busy_timeout=50")
        return value

    monkeypatch.setattr(store._outbox, "_connect", fast_connection)
    try:
        with gate.control_action():
            gate.assert_released()
            store.reserve_order_notional_sync(
                event_id="scope-reserve",
                deployment_instance_id=instance,
                client_order_id="scope-order",
                policy_id=policy.policy.policy_id,
                requested_notional=Decimal("25"),
            )
            assert store.has_order_reservation_sync(instance, "scope-order")
            store.replace_order_reservation_sync(
                event_id="scope-replace",
                deployment_instance_id=instance,
                client_order_id="scope-order",
                new_reserved_notional=Decimal("20"),
            )
            assert (
                store.load_order_reservation_sync(instance, "scope-order").reserved_notional == 20
            )
            with pytest.raises(RunnerStateAuthorityError, match="scope"):
                store.reserve_order_notional_sync(
                    event_id="scope-foreign",
                    deployment_instance_id=uuid4(),
                    client_order_id="foreign-order",
                    policy_id=policy.policy.policy_id,
                    requested_notional=Decimal("1"),
                )
            store.record_order_fill_sync(
                event_id="scope-fill",
                deployment_instance_id=instance,
                client_order_id="scope-order",
                fill_notional=Decimal("10"),
                fill_quantity=Decimal("1"),
                leaves_quantity=Decimal("0"),
                position_id="position-1",
                instrument_id="BTCUSDT",
                side="buy",
            )
            store.release_order_reservation_sync(
                event_id="scope-release",
                deployment_instance_id=instance,
                client_order_id="scope-order",
                reason="canceled",
            )
            store.record_position_reduction_fifo_sync(
                event_id="scope-fifo",
                deployment_instance_id=instance,
                position_id="position-1",
                reduction_notional=Decimal("5"),
                reduction_quantity=Decimal("0.5"),
            )
            store.record_position_reduction_sync(
                event_id="scope-reduce",
                deployment_instance_id=instance,
                client_order_id="scope-order",
                reduction_notional=Decimal("5"),
                reduction_quantity=Decimal("0.5"),
            )
            store._outbox.remember_order_sync(
                deployment_instance_id=str(instance),
                client_order_id="scope-order",
                direction="long",
                order_role="entry",
            )
            assert "scope-order" in store._outbox.recall_orders_sync(str(instance))
            store._outbox.release_order_sync(
                deployment_instance_id=str(instance),
                client_order_id="scope-order",
                released_at_ns=1,
            )
            assert store._outbox.prune_released_orders_sync(
                deployment_instance_id=str(instance), released_before_ns=2
            ) == ("scope-order",)
            store.record_breaker_state_sync(
                deployment_instance_id=instance,
                peak_equity=Decimal("2"),
                frozen=True,
                reason_code="fixture_freeze",
            )
            assert store.load_breaker_state_sync(instance).frozen
            store._outbox.enqueue_strategy_signal_sync(
                bundle.authority,
                store._identity,
                fact_id=uuid4(),
                instrument="BTCUSDT",
                timeframe="1m",
                direction="long",
                occurred_at="2026-11-04T00:00:02Z",
                input_digest="f" * 64,
                strategy_version="fixture-v1",
                trace_id=uuid4(),
            )
            with sqlite3.connect(store._outbox.path) as other:
                assert other.execute("SELECT count(*) FROM order_reservation").fetchone()[0] == 0
            if rollback:
                raise RuntimeError("outer transaction failure")
    except RuntimeError as error:
        assert rollback and str(error) == "outer transaction failure"
    if rollback:
        assert not store.has_order_reservation_sync(instance, "scope-order")
        assert store.load_breaker_state_sync(instance) is None
    else:
        result = store.load_order_reservation_sync(instance, "scope-order")
        assert (
            result.reserved_notional == 0
            and result.filled_exposure == 0
            and result.state == "closed"
        )


async def test_synchronous_overfill_persists_its_latch_without_an_inner_commit(opening):
    from decimal import Decimal

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from custos.core.runner_fact import RunnerPostTradeRiskBreach
    from tests.test_runner_policy_runtime import _verified_policy

    store, _, lease, gate, auth, event, manifest, bundle = opening
    await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    policy = _verified_policy(
        Ed25519PrivateKey.from_private_bytes(bytes([91]) * 32),
        tenant_id=bundle.authority.tenant_id,
        runner_id=bundle.authority.runner_id,
        expires_at="2030-01-01T00:00:00Z",
    )
    await store.record_verified_runner_safety_policy(policy)
    instance = bundle.authority.deployment_instance_id
    with gate.control_action():
        store.reserve_order_notional_sync(
            event_id="overfill-reserve",
            deployment_instance_id=instance,
            client_order_id="overfilled",
            policy_id=policy.policy.policy_id,
            requested_notional=Decimal("95"),
        )
        with pytest.raises(RunnerPostTradeRiskBreach):
            store.record_order_fill_sync(
                event_id="actual-overfill",
                deployment_instance_id=instance,
                client_order_id="overfilled",
                fill_notional=Decimal("110"),
                fill_quantity=Decimal("1"),
            )
        assert store.load_order_reservation_sync(instance, "overfilled").filled_exposure == 110
        with sqlite3.connect(store._outbox.path) as other:
            assert other.execute("SELECT count(*) FROM order_reservation").fetchone()[0] == 0
    assert store.load_order_reservation_sync(instance, "overfilled").filled_exposure == 110


async def test_pause_resume_requires_account_continuity_and_keeps_traded_positions(opening):
    import asyncio
    from decimal import Decimal
    from types import SimpleNamespace

    from custos.core.opening_observation import IndependentRiskSample, RiskSampleUnavailable
    from custos.core.runner_fact import RunnerStateAuthorityError
    from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
    from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox
    from tests.test_opening_inventory_contract import _retoken

    store, registry, old_lease, old_gate, auth, event, manifest, bundle = opening
    original = await store.commit_acknowledged_opening_release(
        auth,
        release_id=UUID(event["payload"]["release_id"]),
        process_lease=old_lease,
        capability_manifest=manifest,
        observation=bundle,
        observation_owner=store._test_observation_owner,
    )
    old_lease.close()
    for generation, lifecycle in [(2, "paused"), (3, "running")]:
        current = signed_generation(generation, lifecycle, "2026-11-05T00:00:00Z")
        await store.record_desired_command(
            command=current.command,
            command_fingerprint=current.command_fingerprint,
            verification_receipt=current.verification_receipt,
        )
    authority = EngineLifecycleAuthority.from_verified_command(current)
    context = store.platform_process_snapshot(authority).context
    account = resolve_platform_account_identity(
        context, build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
    )
    lease = registry.claim_closed(store, account)
    gate = PlatformOpeningGate(store, authority)
    gate.bind_process_lease(lease)
    host = store._test_host
    instance = str(authority.deployment_instance_id)
    current_fact_authority = store._authority_resolver(current)
    with host._opening_observation_lock:
        host._retire_opening_observation(instance)
        host._active_nodes[instance] = SimpleNamespace(task=asyncio.Future())
        host._runner_fact_contexts[instance] = (
            SimpleNamespace(authority=current_fact_authority),
            None,
        )
        host._lifecycle_authorities[instance] = authority
        host._runner_safety_boundaries[instance] = SimpleNamespace(platform_opening_gate=gate)
        host._platform_process_leases[instance] = lease
    token = await host.runner_fact_observation_token(current_fact_authority)
    owner = await host.runner_fact_opening_observation_owner(current_fact_authority, token)
    try:
        with pytest.raises(RunnerOpeningGateClosed):
            old_gate.assert_released()
        with pytest.raises(RunnerOpeningGateClosed):
            gate.assert_released()
        inventory = dict(bundle.inventory)
        inventory["scope"]["generation"] = 3
        inventory["native_capture"]["capture_sequence"] += 1
        from datetime import UTC, datetime

        inventory["native_capture"]["observed_at_ns"] = (
            int(datetime(2026, 11, 6, tzinfo=UTC).timestamp()) * 1_000_000_000
        )
        for read in inventory["observations"].values():
            read["source_time"] = "2026-11-06T00:00:00Z"
        inventory["observations"]["positions"]["rows"] = [
            {
                "position_id": "position-1",
                "instrument": "BTCUSDT",
                "quantity": "1",
                "active": True,
                "position_side": "LONG",
                "average_entry_price": None,
                "settlement_currency": "USDT",
            }
        ]
        inventory["equity_binding"] = {
            "status": "unproven",
            "capture_token": None,
            "currency": None,
            "amount": None,
            "observed_at": None,
            "valuation_rule": None,
            "reason": "independent_risk_observation",
        }
        inventory["inventory_complete"] = False
        _retoken(inventory)
        proof = OpeningObservationBundle(
            current_fact_authority,
            token,
            RiskSampleUnavailable("2026-11-06T00:00:00Z", "fixture"),
            "awaiting_release",
            inventory,
            None,
            "USDT",
        )
        with pytest.raises(RunnerStateAuthorityError, match="independently observed risk equity"):
            await store.rebind_opening_release(
                process_lease=lease,
                capability_manifest=manifest,
                observation=proof,
                observation_owner=owner,
            )
        proof = OpeningObservationBundle(
            current_fact_authority,
            token,
            IndependentRiskSample(
                Decimal("2"),
                "USDT",
                (
                    {
                        "instrument": "BTCUSDT",
                        "quantity": "1",
                        "mark_price": "1",
                        "currency": "USDT",
                    },
                ),
                "2026-11-06T00:00:00Z",
                "continuity-fixture",
                "portfolio",
            ),
            "awaiting_release",
            inventory,
            None,
            "USDT",
        )
        import copy

        stale = copy.deepcopy(inventory)
        stale["native_capture"]["observed_at_ns"] = bundle.inventory["native_capture"][
            "observed_at_ns"
        ]
        for read in stale["observations"].values():
            read["source_time"] = bundle.native.observed_at
        _retoken(stale)
        stale_observation = OpeningObservationBundle(
            current_fact_authority,
            token,
            proof.independent,
            "awaiting_release",
            stale,
            None,
            "USDT",
        )
        with pytest.raises(RunnerStateAuthorityError, match="continuity"):
            await store.rebind_opening_release(
                process_lease=lease,
                capability_manifest=manifest,
                observation=stale_observation,
                observation_owner=owner,
            )
        assert (
            await store.rebind_opening_release(
                process_lease=lease,
                capability_manifest=manifest,
                observation=proof,
                observation_owner=owner,
            )
            == event["payload"]["release_id"]
        )
        with gate.control_action():
            gate.assert_released()
        assert (
            await store.commit_acknowledged_opening_release(
                auth,
                release_id=UUID(event["payload"]["release_id"]),
                process_lease=None,
                capability_manifest=None,
                observation=None,
            )
            == original
        )
    finally:
        lease.close()
