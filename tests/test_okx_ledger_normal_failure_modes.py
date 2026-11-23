"""Normal OKX periods cannot evade source retirement or the owned signing path."""

import asyncio
import io
import json
import threading
import urllib.parse

import pytest

from custos.core.okx_ledger_binding import OkxLedgerOwnerRetired
from custos.core.runner_fact import RunnerFactContractError
from tests.test_okx_ledger_group import ledger_fixture
from tests.test_okx_ledger_normal import (
    END,
    START,
    NormalOpener,
    normal_fixture,
    owned_normal_fixture,
)


async def test_capability_short_name_is_not_a_valuation_fact_kind(tmp_path):
    fixture = await ledger_fixture(tmp_path / "runner.sqlite3")
    batches = fixture.batches[:-1] + [[{"kind": "valuation_checkpoint"}]] + fixture.batches[-1:]
    with pytest.raises(RunnerFactContractError, match="closed ledger observation group"):
        fixture.owner.freeze_group(
            fixture.authority, fixture.account_binding, fixture.raw.content_digest, batches
        )


async def test_normal_uid_is_rechecked_after_legacy_economic_collection(tmp_path):
    fixture = await owned_normal_fixture(tmp_path / "runner.sqlite3")

    class ChangedAccountOpener(NormalOpener):
        def __init__(self):
            super().__init__()
            self.economic_collection_started = False

        def open(self, request, *, timeout):
            path = urllib.parse.urlsplit(request.full_url).path
            if path == "/api/v5/account/balance":
                self.economic_collection_started = True
            if self.economic_collection_started and path == "/api/v5/account/config":
                return io.BytesIO(b'{"code":"0","data":[{"uid":"43","mainUid":"43"}]}')
            return super().open(request, timeout=timeout)

    opener = ChangedAccountOpener()
    fixture.source._http._opener = opener
    try:
        with pytest.raises(OkxLedgerOwnerRetired):
            await fixture.source.collect(START, END)
    finally:
        assert opener.economic_collection_started
    assert await fixture.outbox.pending() == []


async def test_final_period_owner_retirement_rejects_all_rows_without_legacy_group_fallback(
    tmp_path, monkeypatch
):
    fixture = await owned_normal_fixture(tmp_path / "runner.sqlite3")
    before = fixture.outbox.platform_storage_snapshot()
    original = fixture.emitter.emit_ledger_group
    captures = []
    legacy_calls = []

    async def retire_before_writer(authority, capture):
        captures.append(capture)
        kinds = [fact["kind"] for batch in json.loads(capture.facts_bytes) for fact in batch]
        assert "RunnerValuationCheckpointFact.v1" in kinds
        fixture.owner.retire("stopped")
        return await original(authority, capture)

    async def reject_legacy(*args):
        legacy_calls.append(args)
        raise AssertionError("normal OKX must not use the unowned group writer")

    monkeypatch.setattr(fixture.emitter, "emit_ledger_group", retire_before_writer)
    monkeypatch.setattr(fixture.emitter, "emit_group", reject_legacy)
    assert not await fixture.loop._close_reconciliation_period(fixture.deployment, START, END)
    assert len(captures) == 1 and not legacy_calls
    assert await fixture.outbox.pending() == []
    assert fixture.outbox.platform_storage_snapshot().mutation_seq == before.mutation_seq


@pytest.mark.parametrize("phase", ["in_flight", "rate_wait"])
@pytest.mark.parametrize("retire", [False, True])
async def test_cancelled_or_retired_normal_worker_blocks_new_http_and_keeps_lock_until_exit(
    tmp_path, monkeypatch, retire, phase
):
    fixture = await owned_normal_fixture(tmp_path / "runner.sqlite3")
    in_flight = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    original = fixture.source._collect_owned

    class HeldOpener(NormalOpener):
        def open(self, request, *, timeout):
            if "/bills-archive?" in request.full_url:
                in_flight.set()
                assert release.wait(3)
            return super().open(request, timeout=timeout)

    opener = HeldOpener()
    fixture.source._http._opener = opener
    if phase == "rate_wait":
        import time

        from custos.engines.nautilus import ledger_http

        def wait_before_request(_delay):
            in_flight.set()
            assert release.wait(3)

        fixture.source._http._minimum_interval = 1
        fixture.source._http._last_request = time.monotonic()
        monkeypatch.setattr(ledger_http.time, "sleep", wait_before_request)

    def tracked_worker(*args):
        try:
            return original(*args)
        finally:
            exited.set()

    monkeypatch.setattr(fixture.source, "_collect_owned", tracked_worker)
    task = asyncio.create_task(fixture.source.collect(START, END))
    try:
        assert await asyncio.to_thread(in_flight.wait, 3)
        assert fixture.source._collection_lock.locked()
        if retire:
            await fixture.host.stop(fixture.deployment.deployment_instance_id)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not exited.is_set()
        assert fixture.source._collection_lock.locked()
        count = len(opener.calls)
        release.set()
        if retire:
            with pytest.raises(OkxLedgerOwnerRetired):
                await asyncio.wait_for(task, 3)
        assert await asyncio.to_thread(exited.wait, 3)
        assert len(opener.calls) == count + (1 if phase == "in_flight" else 0)
        assert fixture.source._http.before_request is None
        assert not fixture.source._collection_lock.locked()
        assert await fixture.outbox.pending() == []
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert await asyncio.to_thread(exited.wait, 3)


@pytest.mark.parametrize("change", ["missing_dependencies", "connector", "scope", "generation"])
async def test_host_rejects_unbound_or_stale_runtime_spec_before_native_inputs(
    tmp_path, monkeypatch, change
):
    import copy

    from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.test_okx_ledger_binding import ScopeVault

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    host = NtTradingNodeHost(
        state_store=fixture.store if change != "missing_dependencies" else None,
        ledger_credential_resolver=resolver if change != "missing_dependencies" else None,
    )
    spec = copy.deepcopy(fixture.spec)
    if change == "connector":
        spec["connector"] = "okx_perpetual"
    elif change == "scope":
        spec["credential_scope"]["scope_digest"] = "b" * 64
    elif change == "generation":
        spec["generation"] += 1
    native_calls = []

    async def refuse_native(*args):
        native_calls.append(args)
        raise AssertionError("stale input reached native assembly")

    monkeypatch.setattr(host, "_deploy", refuse_native)
    with pytest.raises(RuntimeError):
        await host.deploy(spec, {"api_key": "stale-runtime-input"}, object())
    assert not native_calls
    assert fixture.deployment.deployment_instance_id not in host._okx_ledger_slots


async def test_late_deploy_binding_failure_does_not_retire_a_newer_source_slot(
    tmp_path, monkeypatch
):
    from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.test_okx_ledger_binding import ScopeVault

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    host = NtTradingNodeHost(state_store=fixture.store, ledger_credential_resolver=resolver)
    original_replace = host.replace_okx_ledger_owner
    bound = asyncio.Event()
    release = asyncio.Event()

    async def delayed_binding(*args):
        resolved = await original_replace(*args)
        bound.set()
        await release.wait()
        return resolved

    async def forbidden_native(*args):
        raise AssertionError("superseded binding reached native assembly")

    monkeypatch.setattr(host, "replace_okx_ledger_owner", delayed_binding)
    monkeypatch.setattr(host, "_deploy", forbidden_native)
    old_deploy = asyncio.create_task(host.deploy(fixture.spec, {}, object()))
    try:
        await asyncio.wait_for(bound.wait(), 3)
        newer = await original_replace(fixture.store, fixture.engine_authority, resolver)
        instance = fixture.deployment.deployment_instance_id
        current_slot = host._okx_ledger_slots[instance]
        release.set()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(old_deploy, 3)
        assert host._okx_ledger_slots[instance] is current_slot
        assert current_slot.current is newer.owner
        newer.owner.assert_current()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(old_deploy, return_exceptions=True), 3)


async def test_source_retired_while_waiting_for_platform_start_lock_never_deploys(
    tmp_path, monkeypatch
):
    from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.test_okx_ledger_binding import ScopeVault

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    host = NtTradingNodeHost(
        state_store=fixture.store,
        ledger_credential_resolver=resolver,
        platform_process_fence=object(),
    )
    lock = asyncio.Lock()
    await lock.acquire()
    waiting = asyncio.Event()

    class ObservedStartLock:
        async def __aenter__(self):
            waiting.set()
            await lock.acquire()

        async def __aexit__(self, *_args):
            lock.release()

    native_calls = []

    async def forbidden_native(*args):
        native_calls.append(args)
        raise AssertionError("retired source reached native assembly")

    monkeypatch.setattr(host, "_platform_start_lock", ObservedStartLock())
    monkeypatch.setattr(host, "_deploy", forbidden_native)
    deploy = asyncio.create_task(host.deploy(fixture.spec, {}, object()))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        await host.stop(fixture.deployment.deployment_instance_id)
        lock.release()
        with pytest.raises(OkxLedgerOwnerRetired):
            await asyncio.wait_for(deploy, 3)
        assert not native_calls
    finally:
        if lock.locked():
            lock.release()
        await asyncio.wait_for(asyncio.gather(deploy, return_exceptions=True), 3)


async def test_owned_binding_preserves_real_native_unsupported_account_identity(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
    from custos.core.engine_protocol import EngineDeploymentRefused
    from custos.engines.nautilus import venue_okx
    from custos.engines.nautilus.host import NtTradingNodeHost
    from custos.engines.nautilus.platform_account_identity import PlatformExecutionProcessFence
    from tests.test_nt_trading_node_host import _StrategyDouble
    from tests.test_okx_ledger_binding import ScopeVault

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    host = NtTradingNodeHost(
        state_store=fixture.store,
        ledger_credential_resolver=resolver,
        platform_process_fence=PlatformExecutionProcessFence(object(), fixture.store),
    )
    http_calls = []

    async def reject_http(*args):
        http_calls.append(args)
        raise AssertionError("unsupported Native identity must fail before account HTTP")

    monkeypatch.setattr(venue_okx, "validate_account_configuration", reject_http)
    artifact = SimpleNamespace(
        activation_id="fixture-native-refusal", strategy=_StrategyDouble(fixture.spec)
    )
    with pytest.raises(EngineDeploymentRefused) as caught:
        await host.deploy(fixture.spec, {}, artifact)
    assert caught.value.reason_code == "unsupported_account_identity"
    assert not http_calls
    assert fixture.deployment.deployment_instance_id not in host._okx_ledger_slots


async def test_cancelled_old_worker_cannot_clear_replacement_owner_or_http_guard(
    tmp_path, monkeypatch
):
    from custos.engines.nautilus.okx_ledger import OwnedOkxVenueLedgerSource

    fixture = await owned_normal_fixture(tmp_path / "runner.sqlite3")
    old_entered, old_release, old_exited = (threading.Event() for _ in range(3))
    new_entered, new_release = threading.Event(), threading.Event()
    original_worker = fixture.source._collect_owned

    class OldOpener(NormalOpener):
        def open(self, request, *, timeout):
            if "/bills-archive?" in request.full_url:
                old_entered.set()
                assert old_release.wait(3)
            return super().open(request, timeout=timeout)

    class NewOpener(NormalOpener):
        def open(self, request, *, timeout):
            if not new_entered.is_set():
                new_entered.set()
                assert new_release.wait(3)
            return super().open(request, timeout=timeout)

    def tracked_old_worker(*args):
        try:
            return original_worker(*args)
        finally:
            old_exited.set()

    monkeypatch.setattr(fixture.source, "_collect_owned", tracked_old_worker)
    fixture.source._http._opener = OldOpener()
    old = asyncio.create_task(fixture.source.collect(START, END))
    tasks = [old]
    try:
        assert await asyncio.to_thread(old_entered.wait, 3)
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old
        replacement = await fixture.host.replace_okx_ledger_owner(
            fixture.store, fixture.engine_authority, fixture.resolver
        )
        current_slot = fixture.host._okx_ledger_slots[fixture.deployment.deployment_instance_id]
        source = OwnedOkxVenueLedgerSource(
            fixture.spec, replacement.credential, ledger_owner=replacement.owner
        )
        source._http._opener = NewOpener()
        source._http._minimum_interval = 0
        new = asyncio.create_task(source.collect(START, END))
        tasks.append(new)
        assert await asyncio.to_thread(new_entered.wait, 3)
        new_guard = source._http.before_request
        assert new_guard is not None
        old_release.set()
        assert await asyncio.to_thread(old_exited.wait, 3)
        assert source._http.before_request is new_guard
        assert source._collection_lock.locked()
        assert current_slot.current is replacement.owner
        new_release.set()
        result = await asyncio.wait_for(new, 3)
        assert result.owner is replacement.owner and len(result.evidence.cash_flows) == 1
    finally:
        old_release.set()
        new_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert await asyncio.to_thread(old_exited.wait, 3)


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
async def test_final_http_hook_rechecks_budget_after_waiting_for_real_owner_lock(
    tmp_path, monkeypatch, ending
):
    import time

    from custos.engines.nautilus import okx_ledger
    from custos.engines.nautilus.ledger_http import VenueLedgerError

    fixture = await owned_normal_fixture(tmp_path / "runner.sqlite3")
    budget = okx_ledger.FundingWindowBudget(label="ledger")
    monkeypatch.setattr(okx_ledger, "FundingWindowBudget", lambda **_: budget)
    in_http = threading.local()
    final_hook, attempt_owner, owner_waiting, owner_checked, worker_exited = (
        threading.Event() for _ in range(5)
    )
    original_request = fixture.source._http._request
    original_current = type(fixture.owner).assert_current
    original_worker = fixture.source._collect_owned
    intercepted = False

    def actual_request(*args, **kwargs):
        in_http.active = True
        try:
            return original_request(*args, **kwargs)
        finally:
            in_http.active = False

    def observed_current(owner):
        nonlocal intercepted
        if owner is fixture.owner and getattr(in_http, "active", False) and not intercepted:
            intercepted = True
            final_hook.set()
            assert attempt_owner.wait(3)
            owner_waiting.set()
            original_current(owner)
            owner_checked.set()
        else:
            original_current(owner)

    def tracked_worker(*args):
        try:
            return original_worker(*args)
        finally:
            worker_exited.set()

    monkeypatch.setattr(fixture.source._http, "_request", actual_request)
    monkeypatch.setattr(type(fixture.owner), "assert_current", observed_current)
    monkeypatch.setattr(fixture.source, "_collect_owned", tracked_worker)
    collect = asyncio.create_task(fixture.source.collect(START, END))
    held = False
    try:
        assert await asyncio.to_thread(final_hook.wait, 3)
        fixture.owner._lock.acquire()
        held = True
        attempt_owner.set()
        assert await asyncio.to_thread(owner_waiting.wait, 3)
        assert not await asyncio.to_thread(owner_checked.wait, 0.05)
        if ending == "cancel":
            collect.cancel()
            with pytest.raises(asyncio.CancelledError):
                await collect
            assert budget.cancelled.is_set()
        else:
            budget.deadline = time.monotonic() - 1
        fixture.owner._lock.release()
        held = False
        if ending == "deadline":
            with pytest.raises(VenueLedgerError, match="time budget"):
                await asyncio.wait_for(collect, 3)
        assert await asyncio.to_thread(worker_exited.wait, 3)
        assert owner_checked.is_set()
        assert fixture.opener.calls == []
        assert fixture.source._http.before_request is None
        assert not fixture.source._collection_lock.locked()
    finally:
        attempt_owner.set()
        if held:
            fixture.owner._lock.release()
        await asyncio.wait_for(asyncio.gather(collect, return_exceptions=True), 3)
        assert await asyncio.to_thread(worker_exited.wait, 3)


async def test_lifecycle_logs_redact_the_hosts_fresh_unlabelled_passphrase(tmp_path, monkeypatch):
    from structlog.testing import capture_logs

    from custos.artifacts.runtime import ArtifactRuntimeCapabilityV1
    from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
    from custos.core.engine_lifecycle import (
        EngineLifecycleConfig,
        EngineLifecycleQuarantined,
        EngineLifecycleSupervisor,
    )
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.test_engine_lifecycle import _Artifact
    from tests.test_okx_ledger_binding import ScopeVault

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    fresh = "lilac-river-472938"

    class FreshVault(ScopeVault):
        def decrypt(self, reference):
            credential = super().decrypt(reference)
            credential["api_passphrase"] = fresh
            return credential

    old = await VaultRunnerCredentialResolverV1(
        ScopeVault(scope.scope_id, scope.scope_digest)
    ).resolve(fixture.verified, scope)
    host = NtTradingNodeHost(
        state_store=fixture.store,
        ledger_credential_resolver=VaultRunnerCredentialResolverV1(
            FreshVault(scope.scope_id, scope.scope_digest)
        ),
    )

    async def failed_start(*args):
        raise RuntimeError(f"venue refused login {fresh}")

    monkeypatch.setattr(host, "_deploy", failed_start)
    supervisor = EngineLifecycleSupervisor(
        engine=host,
        state_store=fixture.store,
        artifact_capability=ArtifactRuntimeCapabilityV1.production_ready(),
        config=EngineLifecycleConfig(restart_budget=0),
    )
    with capture_logs() as events, pytest.raises(EngineLifecycleQuarantined):
        await supervisor._start_with_budget(
            delivery_id="fresh-okx-credential-log",
            verified=fixture.verified,
            runtime_spec=fixture.spec,
            credential=old,
            artifact=_Artifact(),
            authority=fixture.engine_authority,
            artifact_activation_id=None,
            artifact_policy_id=None,
            restart_count=0,
        )
    failures = [event for event in events if event["event"] == "engine_start_attempt_failed"]
    assert len(failures) == 1
    assert fresh not in json.dumps(events, default=str)
    assert failures[0]["error"] == "venue refused login <redacted>"


@pytest.mark.parametrize("kind", ["ordinary", "refused", "dependency", "timeout", "cancel"])
async def test_fresh_credential_error_boundary_preserves_classification_without_raw_cause(
    tmp_path, monkeypatch, kind
):
    import traceback

    from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
    from custos.core.engine_protocol import EngineDependencyUnavailable, EngineDeploymentRefused
    from custos.engines.nautilus.host import NtTradingNodeHost
    from tests.test_okx_ledger_binding import ScopeVault

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    fresh = "lilac-river-472938"

    class FreshVault(ScopeVault):
        def decrypt(self, reference):
            credential = super().decrypt(reference)
            credential["api_passphrase"] = fresh
            return credential

    host = NtTradingNodeHost(
        state_store=fixture.store,
        ledger_credential_resolver=VaultRunnerCredentialResolverV1(
            FreshVault(scope.scope_id, scope.scope_digest)
        ),
    )
    errors = {
        "ordinary": ValueError(f"venue refused login {fresh}"),
        "refused": EngineDeploymentRefused(
            "unsupported_account_identity", f"venue refused login {fresh}"
        ),
        "dependency": EngineDependencyUnavailable(f"venue refused login {fresh}"),
        "timeout": TimeoutError(f"venue refused login {fresh}"),
        "cancel": asyncio.CancelledError("fixture cancellation"),
    }
    original = errors[kind]

    async def failed_start(*args):
        raise original

    monkeypatch.setattr(host, "_deploy", failed_start)
    expected = RuntimeError if kind == "ordinary" else type(original)
    with pytest.raises(expected) as caught:
        await host.deploy(fixture.spec, {}, object())
    if kind == "cancel":
        assert caught.value is original
    else:
        assert caught.value is not original
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert fresh not in "".join(traceback.format_exception(caught.value))
        if kind == "refused":
            assert caught.value.reason_code == original.reason_code
