"""Normal OKX collection reaches the real signed-period transaction boundary."""

import io
import json
import threading
import urllib.parse
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from custos.core.credential_resolver import VaultRunnerCredentialResolverV1
from custos.core.runner_fact import RunnerFactEmitter
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactProductionLoop
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource
from tests.engines.nautilus.test_okx_bill_window import END, START, START_MS
from tests.engines.nautilus.test_okx_cash_flow_mapping import transfer
from tests.test_okx_ledger_binding import ScopeVault, running_store
from tests.test_runner_fact_store import _runner_authority


class NormalOpener:
    def __init__(self):
        self.calls = []
        self.thread_ids = []
        self.bills = [transfer(), transfer("9007199254740994", type="4", subType="999")]

    def open(self, request, *, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, params))
        self.thread_ids.append(threading.get_ident())
        if parsed.path.endswith("/time"):
            data = [{"ts": str(START_MS + 2000)}]
        elif parsed.path.endswith("/config"):
            data = [{"uid": "42", "mainUid": "42", "posMode": "net_mode"}]
        elif parsed.path.endswith("/balance"):
            data = [{"details": [{"ccy": "USDT", "cashBal": "100", "availBal": "100"}]}]
        elif parsed.path.endswith("/ticker"):
            data = [{"instId": params["instId"], "bidPx": "100", "askPx": "100"}]
        elif parsed.path.endswith("/bills-archive"):
            data = self.bills
        elif parsed.path.endswith("/fills-history"):
            data = []
        else:
            raise AssertionError(f"unexpected normal collection request: {parsed.path}")
        return io.BytesIO(json.dumps({"code": "0", "data": data}).encode())


class NormalHost(NtTradingNodeHost):
    async def runner_fact_cash_snapshot(self, instance, currency):
        assert currency == "USDT"
        return Decimal("100"), ({"asset": "USDT", "quantity": "100", "mark_price": "1"},)


async def normal_fixture(path):
    outbox, store, engine_authority, verified = await running_store(path)
    authority = replace(_runner_authority(verified), strategy_id=verified.command.strategy_id)
    deployment = RunnerFactDeployment(
        authority=authority,
        deployment_instance_id=str(authority.deployment_instance_id),
        deployment_spec_id=str(authority.deployment_spec_id),
        deployment_spec_digest=authority.deployment_spec_digest,
        venue="OKX",
        connector="okx",
        currency="USDT",
        reconciliation_available=True,
        valuation_checkpoint_available=True,
        strategy_version="fixture-release",
        timeframe="1m",
    )
    emitter = RunnerFactEmitter(outbox, store._identity, lambda: None)
    host = NormalHost()
    loop = RunnerFactProductionLoop(
        host=host, emitter=emitter, snapshot_interval_secs=1, period_secs=60, period_retry_secs=1
    )
    return SimpleNamespace(
        outbox=outbox,
        store=store,
        engine_authority=engine_authority,
        verified=verified,
        authority=authority,
        deployment=deployment,
        emitter=emitter,
        host=host,
        loop=loop,
        spec=verified.command.to_runtime_spec().model_dump(mode="python"),
    )


async def owned_normal_fixture(path):
    from custos.engines.nautilus.okx_ledger import OwnedOkxVenueLedgerSource

    fixture = await normal_fixture(path)
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    fixture.resolver = VaultRunnerCredentialResolverV1(
        ScopeVault(scope.scope_id, scope.scope_digest)
    )
    fixture.resolved = await fixture.host.replace_okx_ledger_owner(
        fixture.store, fixture.engine_authority, fixture.resolver
    )
    fixture.owner = fixture.resolved.owner
    fixture.source = OwnedOkxVenueLedgerSource(
        fixture.spec, fixture.resolved.credential, ledger_owner=fixture.owner
    )
    fixture.opener = NormalOpener()
    fixture.source._http._opener = fixture.opener
    fixture.source._http._minimum_interval = 0
    fixture.host._runner_fact_contexts[fixture.deployment.deployment_instance_id] = (
        fixture.deployment,
        fixture.source,
    )
    return fixture


@pytest.mark.parametrize("boundary", ["host", "loop"])
async def test_unowned_normal_okx_collect_cannot_sign_a_period(tmp_path, boundary):
    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    source = OkxVenueLedgerSource(
        fixture.spec,
        {
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            "api_passphrase": "fixture-pass",
        },
    )
    opener = NormalOpener()
    source._http._opener = opener
    source._http._minimum_interval = 0
    fixture.host._runner_fact_contexts[fixture.deployment.deployment_instance_id] = (
        fixture.deployment,
        source,
    )
    if boundary == "loop":

        async def unchecked_host(_instance, start, end):
            return await source.collect(start, end)

        fixture.host.runner_fact_venue_ledger = unchecked_host
    closed = await fixture.loop._close_reconciliation_period(fixture.deployment, START, END)
    assert opener.calls
    assert not closed
    assert await fixture.outbox.pending() == []


async def test_owned_normal_collect_signs_one_partial_group_and_retains_raw_and_unmapped(tmp_path):
    from custos.engines.nautilus import okx_ledger

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    resolved = await fixture.host.replace_okx_ledger_owner(
        fixture.store,
        fixture.engine_authority,
        VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest)),
    )
    source = okx_ledger.OwnedOkxVenueLedgerSource(
        fixture.spec, resolved.credential, ledger_owner=resolved.owner
    )
    opener = NormalOpener()
    source._http._opener = opener
    source._http._minimum_interval = 0
    fixture.host._runner_fact_contexts[fixture.deployment.deployment_instance_id] = (
        fixture.deployment,
        source,
    )
    actual_collect = fixture.host.runner_fact_venue_ledger
    observations = []

    async def observed_collect(*args):
        evidence = await actual_collect(*args)
        observations.append(evidence)
        return evidence

    fixture.host.runner_fact_venue_ledger = observed_collect
    assert await fixture.loop._close_reconciliation_period(fixture.deployment, START, END)
    assert len(observations) == 1
    owned = observations[0]
    assert owned.owner is resolved.owner
    assert owned.mapping.raw_window is owned.raw_window
    assert len(owned.raw_window.raw_bills) == 2 and len(owned.mapping.unmapped) == 1
    assert len(owned.evidence.cash_flows) == 1
    assert owned.evidence.completeness["cash_flows_complete"] is False
    assert sum(path.endswith("/bills-archive") for path, _ in opener.calls) == 1
    assert len(set(opener.thread_ids)) == 1
    assert opener.thread_ids[0] != threading.get_ident()
    pending = await fixture.outbox.pending()
    docs = [json.loads(row.payload) for row in pending]
    chunks = [fact for doc in docs for fact in doc["facts"] if "cash_flows" in fact]
    assert chunks[0]["cash_flows"][0]["amount"] == "100.000000000000000001"
    assert [doc["source_seq_start"] for doc in docs] == [1, 2, 3, 4]


async def test_each_host_deploy_uses_one_fresh_credential_for_native_validation_and_provider(
    tmp_path, monkeypatch
):
    from custos.engines.nautilus import venue_okx

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope

    class RotatingVault(ScopeVault):
        def decrypt(self, reference):
            credential = super().decrypt(reference)
            credential["api_key"] = f"resolved-key-{len(self.calls)}"
            return credential

    resolver = VaultRunnerCredentialResolverV1(RotatingVault(scope.scope_id, scope.scope_digest))
    host = NtTradingNodeHost(state_store=fixture.store, ledger_credential_resolver=resolver)
    seen = []
    native_config_type = venue_okx.OKXExecutionClientConfig

    def recorded_native_config(**kwargs):
        seen.append(("native", kwargs["api_key"]))
        return native_config_type(**kwargs)

    def validate_account(source):
        seen.append(("account", source._credential["api_key"]))

    async def inspect_native_inputs(spec, credential, artifact, ledger_slot):
        config, _, _ = host._build_exec_plan("testnet", spec, credential, venue_okx)
        assert type(config) is native_config_type
        await venue_okx.validate_account_configuration(spec, credential)
        provider = venue_okx.venue_ledger_source(spec, credential, ledger_owner=ledger_slot.current)
        seen.append(("provider", provider._credential["api_key"]))
        assert provider._ledger_owner is ledger_slot.current
        return "engineering-host-inputs-only"

    monkeypatch.setattr(OkxVenueLedgerSource, "validate_account", validate_account)
    monkeypatch.setattr(venue_okx, "OKXExecutionClientConfig", recorded_native_config)
    monkeypatch.setattr(host, "_deploy", inspect_native_inputs)
    runtime_credential = await resolver.resolve(fixture.verified, scope)
    await host.deploy(fixture.spec, runtime_credential, object())
    instance = fixture.deployment.deployment_instance_id
    first_slot = host._okx_ledger_slots[instance]
    first_owner = first_slot.current

    async def completed_run():
        return None

    await host._run_node_for_opening(instance, SimpleNamespace(run_async=completed_run), first_slot)
    assert first_owner.retired
    await host.deploy(fixture.spec, runtime_credential, object())
    assert host._okx_ledger_slots[instance] is not first_slot
    assert seen == [
        (consumer, f"resolved-key-{round_number}")
        for round_number in (2, 3)
        for consumer in ("native", "account", "provider")
    ]
    await host.close()


async def test_daemon_build_host_injects_the_actual_store_and_same_resolver(tmp_path):
    from argparse import Namespace

    from custos.cli._daemon import _build_host

    fixture = await normal_fixture(tmp_path / "runner.sqlite3")
    scope = fixture.verified.command.to_runtime_spec().credential_scope
    resolver = VaultRunnerCredentialResolverV1(ScopeVault(scope.scope_id, scope.scope_digest))
    host = _build_host(
        Namespace(engine="nautilus", tenant_id="acme", runner_id=str(fixture.authority.runner_id)),
        state_store=fixture.store,
        ledger_credential_resolver=resolver,
    )
    assert host._state_store is fixture.store
    assert host._ledger_credential_resolver is resolver
