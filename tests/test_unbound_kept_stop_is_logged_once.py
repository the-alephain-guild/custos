"""A kept stop that no capability binds is logged once, not at every restart.

A stop for an instance the runner's capability never bound is acknowledged and
kept; the instance was never started by this process. If the capability never
binds it (an instance stopped before it was ever bound is not bound afterwards),
the command stays kept for good, and every restart used to log one
``durable_command_recovery_skipped`` line for it. The first restart that finds
it still unbound logs it; later restarts do not, until the command or the reason
it is unbound changes. A kept start keeps being logged at every restart: that
one is an instance an operator expects to run.
"""

from __future__ import annotations

import pytest

from tests.test_runner_command_runtime_failure_modes import (
    UNBOUND_LIFECYCLE,
    _deferred,
    _InstanceCapability,
    _lifecycle_command,
    _lifecycle_facts,
    _restart,
)


def _skipped(restarted) -> list[tuple[str, int, str, str]]:
    return [
        (
            log["deployment_instance_id"],
            log["generation"],
            log["lifecycle_state"],
            log["binding_gap"],
        )
        for log in restarted.logs
        if log["event"] == "durable_command_recovery_skipped"
    ]


class _GapCapability:
    """Binds nothing, for a reason the test chooses."""

    def __init__(self, gap: str) -> None:
        self.gap = gap

    def require_scope_bindings(self, **kwargs):
        from custos.core.runner_fact import RunnerFactContractError

        raise RunnerFactContractError(self.gap)


@pytest.mark.parametrize("lifecycle_state", ["stopped", "paused", "archived"])
@pytest.mark.asyncio
async def test_an_unbound_kept_non_running_command_is_logged_at_the_first_restart_only(
    tmp_path, lifecycle_state
) -> None:
    from tests.test_runner_fact_store import _runner_fact_store

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    kept = _lifecycle_command(lifecycle_state, 2)
    await _deferred(store, kept)
    instance = str(kept.command.deployment_instance_id)

    logged = [_skipped(await _restart(store, _InstanceCapability({}))) for _ in range(3)]

    assert logged == [[(instance, 2, lifecycle_state, UNBOUND_LIFECYCLE)], [], []]
    # Still kept, and still reported once a capability binds it.
    assert (await store.load_engine_lifecycle_state(kept)).desired_status == "recorded"
    assert _lifecycle_facts(database) == []


@pytest.mark.asyncio
async def test_a_newer_unbound_kept_stop_is_logged_again(tmp_path) -> None:
    from tests.test_runner_fact_store import _runner_fact_store

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    instance = None
    for generation in (2, 3):
        kept = _lifecycle_command("stopped", generation)
        instance = str(kept.command.deployment_instance_id)
        await _deferred(store, kept)
        first = await _restart(store, _InstanceCapability({}))
        again = await _restart(store, _InstanceCapability({}))

        assert _skipped(first) == [(instance, generation, "stopped", UNBOUND_LIFECYCLE)]
        assert _skipped(again) == []


@pytest.mark.asyncio
async def test_an_unbound_kept_stop_is_logged_again_when_the_reason_changes(tmp_path) -> None:
    from tests.test_runner_fact_store import _runner_fact_store

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    kept = _lifecycle_command("stopped", 2)
    await _deferred(store, kept)
    instance = str(kept.command.deployment_instance_id)
    other_gap = "capability has no unique settlement binding for DeploymentInstance"

    first = await _restart(store, _InstanceCapability({}))
    changed = await _restart(store, _GapCapability(other_gap))
    unchanged = await _restart(store, _GapCapability(other_gap))

    assert _skipped(first) == [(instance, 2, "stopped", UNBOUND_LIFECYCLE)]
    assert _skipped(changed) == [(instance, 2, "stopped", other_gap)]
    assert _skipped(unchanged) == []


@pytest.mark.asyncio
async def test_an_unbound_kept_start_is_logged_at_every_restart(tmp_path) -> None:
    from tests.test_runner_fact_store import _runner_fact_store

    database = tmp_path / "runner-state.sqlite3"
    _outbox, store = _runner_fact_store(database)
    kept = _lifecycle_command("running", 2)
    await _deferred(store, kept)
    instance = str(kept.command.deployment_instance_id)

    logged = [_skipped(await _restart(store, _InstanceCapability({}))) for _ in range(2)]

    assert logged == [[(instance, 2, "running", UNBOUND_LIFECYCLE)]] * 2
