"""Readiness checks the engine's actual state, not the mode it was asked to run in.

``EngineReadinessChecks`` calls itself "evidence that a created task has crossed every
mandatory ready boundary". Three of its seven fields were not evidence of anything:

* ``reconciliation_initialized`` was ``trading_mode == "sandbox"`` -- **inverted**.
  Sandbox is the one mode where reconciliation is switched off (it fills locally against
  live prices; there is no exchange account to reconcile against, `_build_exec_plan`
  returns ``False``), while testnet and live are the modes where it actually runs. So the
  field was true exactly when nothing was reconciled, and false whenever something was --
  which made ``ready`` unreachable on testnet and live.
* ``portfolio_initialized`` was ``getattr(kernel, "portfolio", None) is not None``: a
  kernel always has one, so this was a constant.
* ``strategy_accepting_lifecycle`` was ``not task.done()`` -- character for character the
  same expression as ``node_task_alive`` two lines above.

Fixing only the first would have been worse than leaving all three. ``ready`` would have
gone from never true to true on two constants and a duplicate: a gate that never passes
is at least loud, and one that always passes is silent. So all three are replaced by
something the engine can actually be asked.

What makes the reconciliation check possible is the kernel's own start sequence
(``NautilusKernel.start_async``): engines start, clients connect, and then, if
reconciliation is enabled, ``_await_execution_reconciliation()`` runs -- and on failure
the method **returns without ever starting the trader**. A running trader is therefore
proof that the reconciliation step was passed, which is the closest thing to a completion
signal NautilusTrader offers; there is no ``reconciliation_complete`` flag to read.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("nautilus_trader")

from custos.core.engine_protocol import EngineLifecycleAuthority
from custos.engines.nautilus.host import NtTradingNodeHost


class _Task:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _node(
    *,
    connected: bool = True,
    reconciliation: bool = True,
    portfolio_initialized: bool = True,
    trader_running: bool = True,
    strategies_running: tuple[bool, ...] = (True,),
) -> SimpleNamespace:
    strategies = [SimpleNamespace(is_running=running) for running in strategies_running]
    kernel = SimpleNamespace(
        data_engine=SimpleNamespace(check_connected=lambda: connected),
        exec_engine=SimpleNamespace(
            check_connected=lambda: connected, reconciliation=reconciliation
        ),
        portfolio=SimpleNamespace(initialized=portfolio_initialized),
        trader=SimpleNamespace(is_running=trader_running, strategies=lambda: strategies),
    )
    return SimpleNamespace(kernel=kernel)


def _authority(trading_mode: str) -> EngineLifecycleAuthority:
    return EngineLifecycleAuthority.from_spec(
        {
            "deployment_instance_id": str(uuid4()),
            "deployment_spec_id": str(uuid4()),
            "deployment_spec_digest": "d" * 64,
            "generation": 1,
            "trading_mode": trading_mode,
        }
    )


def _host_with(authority: EngineLifecycleAuthority, node, task=None) -> NtTradingNodeHost:
    host = NtTradingNodeHost(tenant_id="tenant", runner_id="runner")
    instance = str(authority.deployment_instance_id)
    host._lifecycle_authorities[instance] = authority
    host._active_nodes[instance] = (node, task or _Task())
    return host


async def _ready(authority, node, task=None) -> bool:
    """True when readiness is reached inside a deliberately tiny window."""
    host = _host_with(authority, node, task)
    try:
        await host.wait_ready(authority, timeout_secs=0.05)
    except TimeoutError:
        return False
    return True


# ---------------------------------------------------------------------------
# reconciliation_initialized
# ---------------------------------------------------------------------------


async def test_a_testnet_node_can_become_ready_at_all() -> None:
    """The regression the old expression made impossible.

    With ``reconciliation_initialized = trading_mode == "sandbox"``, this could never
    pass -- ``ready`` requires every field, so the offline lane's readiness was
    permanently false on the only modes that reach a venue.
    """
    authority = _authority("testnet")

    assert await _ready(authority, _node())


async def test_readiness_waits_for_the_trader_to_start() -> None:
    """A trader that has not started is a kernel that has not cleared reconciliation."""
    authority = _authority("testnet")

    assert not await _ready(authority, _node(trader_running=False))


async def test_reconciliation_switched_off_on_a_real_venue_is_not_ready() -> None:
    """Testnet and live reconcile against exchange state; that is not optional there.

    Without this the check would pass on a misconfigured deployment purely because the
    trader started -- which it does whether reconciliation ran or was skipped.
    """
    authority = _authority("testnet")

    assert not await _ready(authority, _node(reconciliation=False))


async def test_sandbox_is_ready_although_reconciliation_never_runs() -> None:
    """Sandbox has no exchange account, so there is nothing to reconcile and that is fine.

    This is the one thing the old expression got right, and it has to keep working.
    """
    authority = _authority("sandbox")

    assert await _ready(authority, _node(reconciliation=False))


# ---------------------------------------------------------------------------
# portfolio_initialized
# ---------------------------------------------------------------------------


async def test_an_uninitialised_portfolio_is_not_ready() -> None:
    """The old check asked whether the attribute existed, which it always does."""
    authority = _authority("testnet")

    assert not await _ready(authority, _node(portfolio_initialized=False))


# ---------------------------------------------------------------------------
# strategy_accepting_lifecycle
# ---------------------------------------------------------------------------


async def test_a_strategy_that_is_not_running_is_not_ready() -> None:
    """Distinct from node_task_alive, which it used to duplicate exactly.

    The task is alive here, so the old expression said yes while nothing was trading.
    """
    authority = _authority("testnet")

    assert not await _ready(authority, _node(strategies_running=(False,)), _Task(done=False))


async def test_one_stopped_strategy_among_several_is_not_ready() -> None:
    authority = _authority("testnet")

    assert not await _ready(authority, _node(strategies_running=(True, False)))


async def test_a_node_with_no_strategies_is_not_ready() -> None:
    """Nothing is accepting lifecycle commands if nothing was added."""
    authority = _authority("testnet")

    assert not await _ready(authority, _node(strategies_running=()))


# ---------------------------------------------------------------------------
# The receipt still reports what was checked
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The same answer, asked without waiting
#
# The offline lane's exposure guard runs on its own clock and cannot block on
# ``wait_ready``; it needs to poll. Both answers come from one computation on purpose --
# a guard that decided readiness differently from the lifecycle wait would be a second
# opinion about the same fact, and the two would drift.
# ---------------------------------------------------------------------------


async def test_a_ready_deployment_answers_the_poll() -> None:
    authority = _authority("testnet")
    host = _host_with(authority, _node())

    assert await host.deployment_ready(str(authority.deployment_instance_id)) is True


async def test_a_deployment_still_starting_answers_no() -> None:
    authority = _authority("testnet")
    host = _host_with(authority, _node(trader_running=False))

    assert await host.deployment_ready(str(authority.deployment_instance_id)) is False


async def test_an_unknown_deployment_is_not_ready() -> None:
    """Fail closed on the readiness side too: unknown is not ready."""
    authority = _authority("testnet")
    host = _host_with(authority, _node())

    assert await host.deployment_ready("no-such-instance") is False


async def test_the_poll_and_the_wait_agree() -> None:
    """Same node, same verdict -- they must not be two opinions."""
    authority = _authority("testnet")
    host = _host_with(authority, _node(portfolio_initialized=False))

    assert await host.deployment_ready(str(authority.deployment_instance_id)) is False
    try:
        await host.wait_ready(authority, timeout_secs=0.05)
    except TimeoutError:
        return
    raise AssertionError("wait_ready said ready while the poll said not ready")


async def test_the_sandbox_host_is_ready_as_soon_as_it_has_deployed() -> None:
    """The simulation is in-process: there is no venue state to wait for.

    It still has to answer, or the guard would treat the whole sandbox lane as an engine
    that cannot report readiness and log that on every deployment.
    """
    from uuid import uuid4

    from custos.engines.nautilus.host import SandboxSimulationHost

    host = SandboxSimulationHost()
    instance = str(uuid4())
    assert await host.deployment_ready(instance) is False

    await host.deploy(
        {
            "deployment_instance_id": instance,
            "deployment_spec_id": str(uuid4()),
            "deployment_spec_digest": "d" * 64,
            "generation": 1,
            "trading_mode": "sandbox",
            "pairs": ["BTC-USDT"],
        },
        {},
        SimpleNamespace(activation_id="activation-test", strategy=object()),
    )

    assert await host.deployment_ready(instance) is True


async def test_the_receipt_carries_the_checks_that_passed() -> None:
    authority = _authority("testnet")
    host = _host_with(authority, _node())

    receipt = await host.wait_ready(authority, timeout_secs=0.05)

    assert receipt.checks.ready
    assert receipt.checks.reconciliation_initialized
    assert receipt.checks.portfolio_initialized
    assert receipt.checks.strategy_accepting_lifecycle
