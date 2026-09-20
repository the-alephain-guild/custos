"""A replacement bridge must still know which orders belong to this instance.

The fact bridge only signs facts for orders it initialized, because a live venue
stream is account-wide and carries sibling instances' and manual orders too. That
ownership used to live in three process-local dictionaries, so a rebuilt bridge
started empty and silently dropped every later report for an order that was still
working -- an entry's remaining fill, a stop that was already resting. These tests
require the ownership to be durable, instance-scoped, and loaded before the first
report arrives, without letting anyone else's orders in.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custos.core.runner_fact import (
    RunnerFactEmitter,
    RunnerFactIdentity,
    RunnerFactOutbox,
)
from custos.core.runner_fact_producer import RunnerFactEventBridge
from tests.test_strategy_signal_bridge import (
    OrderCanceled,
    OrderFilled,
    OrderInitialized,
    _deployment,
)

_OTHER_INSTANCE = UUID("20000000-0000-4000-8000-0000000000ff")


def _emitter(path: Path) -> RunnerFactEmitter:
    outbox = RunnerFactOutbox(path)
    return RunnerFactEmitter(
        outbox,
        RunnerFactIdentity(Ed25519PrivateKey.generate(), "attribution-test-key"),
        lambda: None,
    )


def _forwarder() -> SimpleNamespace:
    return SimpleNamespace(
        add_order_sink=lambda _name, _sink: None,
        add_position_sink=lambda _name, _sink: None,
    )


def _bridge(emitter: RunnerFactEmitter) -> RunnerFactEventBridge:
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())
    bridge.bootstrap(_forwarder())
    return bridge


def _restarted(path: Path) -> tuple[RunnerFactEmitter, RunnerFactEventBridge]:
    """A fresh emitter over the same database -- what a process restart looks like.

    Reusing the first emitter object would prove nothing: its own memory would
    carry the answer. The attribution has to come back out of the file.
    """
    emitter = _emitter(path)
    return emitter, _bridge(emitter)


def _late_fill(client_order_id: str) -> OrderFilled:
    return OrderFilled(
        client_order_id,
        trade_id="trade-2",
        event_id=str(uuid4()),
        last_qty="0.004",
    )


@pytest.mark.asyncio
async def test_a_rebuilt_bridge_still_owns_an_order_it_did_not_initialize(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attribution.sqlite3"
    emitter = _emitter(path)
    first = _bridge(emitter)
    first._on_order_event(OrderInitialized())
    first._on_order_event(OrderFilled("supertrend-entry-1", last_qty="0.003"))
    before = len(await emitter._outbox.pending(limit=100))
    assert before > 0

    restarted_emitter, replacement = _restarted(path)
    replacement._on_order_event(_late_fill("supertrend-entry-1"))

    assert len(await restarted_emitter._outbox.pending(limit=100)) > before


@pytest.mark.asyncio
async def test_a_rebuilt_bridge_still_refuses_an_order_nobody_here_initialized(
    tmp_path: Path,
) -> None:
    """The filter is account-level isolation, not bookkeeping; it has to survive too."""
    emitter = _emitter(tmp_path / "isolation.sqlite3")
    bridge = _bridge(emitter)

    bridge._on_order_event(_late_fill("someone-elses-order"))

    assert await emitter._outbox.pending(limit=100) == []


@pytest.mark.asyncio
async def test_another_instances_order_does_not_leak_across_the_store(
    tmp_path: Path,
) -> None:
    """One database can hold several instances; ownership is scoped to one of them."""
    path = tmp_path / "shared.sqlite3"
    owner = _bridge(_emitter(path))
    owner._on_order_event(OrderInitialized())
    emitter = _emitter(path)

    original = _deployment()
    neighbour = RunnerFactEventBridge(
        emitter=emitter,
        deployment=replace(
            original,
            deployment_instance_id=str(_OTHER_INSTANCE),
            authority=replace(original.authority, deployment_instance_id=_OTHER_INSTANCE),
        ),
    )
    neighbour.bootstrap(_forwarder())
    before = len(await emitter._outbox.pending(limit=100))

    neighbour._on_order_event(_late_fill("supertrend-entry-1"))

    assert len(await emitter._outbox.pending(limit=100)) == before


@pytest.mark.asyncio
async def test_a_terminal_order_is_forgotten_everywhere(tmp_path: Path) -> None:
    path = tmp_path / "terminal.sqlite3"
    bridge = _bridge(_emitter(path))
    bridge._on_order_event(OrderInitialized())
    bridge._on_order_event(OrderCanceled())

    restarted_emitter, replacement = _restarted(path)
    before = len(await restarted_emitter._outbox.pending(limit=100))
    replacement._on_order_event(_late_fill("supertrend-entry-1"))

    assert len(await restarted_emitter._outbox.pending(limit=100)) == before


@pytest.mark.asyncio
async def test_a_resting_stop_still_belongs_to_the_instance_after_a_rebuild(
    tmp_path: Path,
) -> None:
    """A protective order emits no signal of its own, so it is easy to forget."""
    path = tmp_path / "stop.sqlite3"
    emitter = _emitter(path)
    first = _bridge(emitter)
    first._on_order_event(
        OrderInitialized(reduce_only=True, order_side="SELL", order_type="STOP_MARKET")
    )
    before = len(await emitter._outbox.pending(limit=100))

    restarted_emitter, replacement = _restarted(path)
    replacement._on_order_event(_late_fill("supertrend-entry-1"))

    assert len(await restarted_emitter._outbox.pending(limit=100)) > before
