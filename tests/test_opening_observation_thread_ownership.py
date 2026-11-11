"""Native lifetime tests; account evidence remains the owner-only kernel fixture."""

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from decimal import Decimal
from functools import partial
from types import FrameType, TracebackType
from uuid import UUID, uuid4

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.adapters.sandbox import SandboxExecutionClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.live import LiveNode, NodeState
from nautilus_trader.model import TraderId

from custos.core import opening_release_state
from custos.core.execution_reservation_store import PlatformOpeningGate
from custos.core.opening_observation import (
    AdmittedNativeCashSample,
    IndependentRiskSample,
    ObservabilityFactPlan,
    OpeningObservationBundle,
)
from custos.core.runner_fact import equity_snapshot
from custos.core.runner_fact_producer import RunnerFactObservationRetired
from custos.core.utc_time import render_utc_nanos
from custos.engines.nautilus.host import _NodeRuntime
from custos.engines.nautilus.platform_account_identity import resolve_platform_account_identity
from custos.engines.nautilus.venue_binance import build_exec_client_config_sandbox
from tests.test_opening_inventory_contract import _retoken
from tests.test_opening_release_ack_transaction import opening

__all__ = ["opening"]


def _native_owner_references(value):
    pending, seen, forbidden = [value], set(), set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        kind = type(current)
        if kind.__module__.startswith("nautilus_trader") or kind.__name__ in {
            "NtTradingNodeHost",
            "_NodeRuntime",
            "RunnerReservationBoundary",
        }:
            forbidden.add(kind.__module__ + "." + kind.__name__)
            continue
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (tuple, list, set, frozenset)):
            pending.extend(current)
        elif isinstance(current, TracebackType):
            pending.extend([current.tb_frame, current.tb_next])
        elif isinstance(current, FrameType):
            pending.extend(current.f_locals.values())
        elif kind.__module__.startswith(("custos.", "tests.")):
            pending.extend(getattr(current, "__dict__", {}).values())
            for slot in getattr(kind, "__slots__", ()):
                if not slot.startswith("__") and hasattr(current, slot):
                    pending.append(getattr(current, slot))
    return sorted(forbidden)


@pytest.mark.parametrize("operation", ["ack", "rebind", "released_equity"])
@pytest.mark.parametrize("outcome", ["success", "exception", "cancel"])
async def test_native_wrappers_stay_on_the_event_loop_after_worker_and_stop(
    opening, monkeypatch, operation, outcome
):
    store, registry, lease, _, auth, event, manifest, bundle = opening
    host = store._test_host
    instance = str(bundle.authority.deployment_instance_id)
    if operation != "ack":
        await store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=store._test_observation_owner,
        )
    if operation == "rebind":
        lease.close()
        context = store.platform_process_snapshot(lease.authority).context
        account = resolve_platform_account_identity(
            context, build_exec_client_config_sandbox(context.spec, {}, ["1 USDT"])
        )
        lease = registry.claim_closed(store, account)
        gate = PlatformOpeningGate(store, lease.authority)
        gate.bind_process_lease(lease)
        host._retire_opening_observation(instance)
        host._platform_process_leases[instance] = lease
        host._runner_safety_boundaries[instance].platform_opening_gate = gate
        inventory = bundle.inventory
        inventory["native_capture"]["capture_sequence"] += 1
        inventory["native_capture"]["observed_at_ns"] += 1_000_000_000
        observed = render_utc_nanos(inventory["native_capture"]["observed_at_ns"])
        inventory["equity_binding"]["observed_at"] = observed
        for value in inventory["observations"].values():
            value["source_time"] = observed
        _retoken(inventory)
        independent = IndependentRiskSample(
            Decimal(inventory["equity_binding"]["amount"]),
            "USDT",
            [],
            observed,
            "fresh-risk",
            "portfolio",
        )
    else:
        inventory = bundle.inventory
        independent = bundle.independent
    builder = LiveNode.builder("native-lifetime", TraderId("CUSTOS-LIFETIME"), Environment.SANDBOX)
    builder = (
        builder.with_reconciliation(False).with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
    )
    config = build_exec_client_config_sandbox({"connector": "binance"}, {}, ["10000 USDT"])
    builder = builder.add_simulated_exec_client("BINANCE", SandboxExecutionClientFactory(), config)
    node = builder.build()
    handle, cache, portfolio = node.handle(), node.cache, node.portfolio
    run = getattr(host, "_run_node_for_opening", lambda _instance, node: node.run_async())
    native_task = asyncio.create_task(run(instance, node))
    for _ in range(100):
        if handle.state == NodeState.RUNNING:
            break
        await asyncio.sleep(0.01)
    assert handle.state == NodeState.RUNNING
    runtime = _NodeRuntime(node, native_task, handle, cache, portfolio, (), False)
    host._retire_opening_observation(instance)
    host._active_nodes[instance] = runtime
    bundle = OpeningObservationBundle(
        bundle.authority,
        await host.runner_fact_observation_token(bundle.authority),
        independent,
        bundle.trading_gate,
        inventory,
        bundle.native
        if operation != "rebind"
        else AdmittedNativeCashSample.from_inventory(inventory, bundle.authority, "USDT", manifest),
        bundle.currency,
    )
    owner = await host.runner_fact_opening_observation_owner(bundle.authority, bundle.token)
    del store._test_host, store._test_observation_owner
    owner_references = _native_owner_references(owner)
    entered, may_return = threading.Event(), threading.Event()
    unraisable, error_references = [], []
    previous_hook = sys.unraisablehook

    def capture_unraisable(error):
        unraisable.append((str(error.exc_value), threading.get_ident()))
        previous_hook(error)

    monkeypatch.setattr(sys, "unraisablehook", capture_unraisable)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opening-lifetime")

    async def to_thread(function, *args, **kwargs):
        return await loop.run_in_executor(
            executor, partial(copy_context().run, function, *args, **kwargs)
        )

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    if operation == "ack":
        target, name = opening_release_state, "commit_acknowledged"
    elif operation == "rebind":
        target, name = opening_release_state, "rebind"
    else:
        target, name = store._outbox, "_enqueue_observability"
    original = getattr(target, name)

    def parked(*args):
        try:
            result = original(*args)
        except BaseException as error:
            error_references.extend(_native_owner_references(error.__traceback__))
            entered.set()
            assert may_return.wait(5)
            raise
        entered.set()
        assert may_return.wait(5)
        return result

    monkeypatch.setattr(target, name, parked)
    if outcome == "exception":
        host._retire_opening_observation(instance)
    if operation == "ack":
        job = store.commit_acknowledged_opening_release(
            auth,
            release_id=UUID(event["payload"]["release_id"]),
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=owner,
        )
    elif operation == "rebind":
        job = store.rebind_opening_release(
            process_lease=lease,
            capability_manifest=manifest,
            observation=bundle,
            observation_owner=owner,
        )
    else:
        fact = equity_snapshot(
            event_id=uuid4(),
            amount="1",
            currency="USDT",
            observed_at=bundle.native.observed_at,
            trading_gate="released",
            account_inventory=bundle.inventory,
        )
        job = store._outbox.enqueue_observability(
            bundle.authority,
            store._identity,
            ObservabilityFactPlan.from_facts([fact], None),
            bundle.token.command_fingerprint,
            opening_owner=owner,
        )
    waiter = asyncio.create_task(job)
    try:
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        if outcome == "cancel":
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        await host.stop(instance)
        assert instance not in host._active_nodes and native_task.done()
        del owner, runtime, node, handle, cache, portfolio, builder
        may_return.set()
        if outcome == "success":
            assert await waiter is not None
        elif outcome == "exception":
            with pytest.raises(RunnerFactObservationRetired):
                await waiter
        # FIFO executor ordering proves the old WorkItem and its fn/args have
        # already been destroyed before this sentinel runs. No GC or delay.
        await loop.run_in_executor(executor, lambda: None)
        assert owner_references == []
        assert error_references == []
        assert unraisable == []
    finally:
        may_return.set()
        if instance in host._active_nodes:
            await host.stop(instance)
        executor.shutdown(wait=True)
        lease.close()


@pytest.mark.parametrize("outcome", ["success", "exception", "cancel"])
async def test_run_exit_retires_observation_before_task_completion(opening, outcome):
    from types import SimpleNamespace

    store, _, _, _, _, _, _, bundle = opening
    host = store._test_host
    instance = str(bundle.authority.deployment_instance_id)
    finish = asyncio.Event()

    class Run:
        async def run_async(self):
            await finish.wait()
            if outcome == "exception":
                raise ValueError("native run failed")

    task = asyncio.create_task(host._run_node_for_opening(instance, Run()))
    host._retire_opening_observation(instance)
    host._active_nodes[instance] = SimpleNamespace(task=task)
    token = await host.runner_fact_observation_token(bundle.authority)
    owner = await host.runner_fact_opening_observation_owner(bundle.authority, token)
    observed = []

    def done(_task):
        with pytest.raises(RunnerFactObservationRetired):
            owner.assert_current()
        observed.append(True)

    task.add_done_callback(done)
    # Let the hosted coroutine enter before cancellation, so its finally runs.
    await asyncio.sleep(0)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        finish.set()
        if outcome == "exception":
            with pytest.raises(ValueError, match="native run failed"):
                await task
        else:
            await task
    assert observed == [True]
