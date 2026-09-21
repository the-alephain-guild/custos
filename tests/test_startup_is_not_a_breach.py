"""A deployment that has not finished starting is not a deployment in breach.

The signed lane's supervision began evaluating the moment a node registered, before
reconciliation had delivered the account balance. A snapshot with no equity yet is
reported as unreliable, the supervisor reads unreliable as "we have lost a
trustworthy view of a running deployment" and fails closed -- and fail_closed does
not heal: the balance arrives, the snapshot becomes reliable, and the breaker stays
frozen.

The offline lane already solved this, with the measurement to go with it: on
2026-08-01 its exposure guard tripped on portfolio_equity_missing 116ms before the
balance landed, while NautilusTrader was still inside the startup reconciliation it
announces in advance. What it does -- wait, bounded, and evaluate anyway past the
bound -- is what the signed lane needed too.

Since the freeze became durable, a startup trip also survives restarts and needs an
operator to lift, so the cost of getting this wrong went up.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from custos.cli._daemon import _run_signed_safety_supervision
from custos.core.engine_protocol import EngineStatus
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.core.order_reservation_boundary import RunnerReservationBoundary

INSTANCE = UUID("11111111-1111-4111-8111-111111111111")
POLICY = UUID("22222222-2222-4222-8222-222222222222")


def _boundary() -> RunnerReservationBoundary:
    return RunnerReservationBoundary(
        store=NS(),
        deployment_instance_id=INSTANCE,
        policy_id=POLICY,
        fallback_breaker=FallbackBreaker(
            FallbackBreakerConfig(
                max_notional=Decimal("1000"),
                max_drawdown_pct=Decimal("10"),
            )
        ),
        trading_mode="testnet",
    )


class _Host:
    """A host that starts unready and answers with no equity until it is."""

    def __init__(self, *, ready: bool, reliable: bool = False) -> None:
        self._ready = ready
        self._reliable = reliable
        self.status_calls = 0
        self.flattened: list[str] = []
        self.readiness_calls = 0

    def runner_fact_deployments(self):
        return (NS(deployment_instance_id=str(INSTANCE)),)

    async def deployment_ready(self, deployment_instance_id: str) -> bool:
        self.readiness_calls += 1
        return self._ready

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.status_calls += 1
        if self._reliable:
            return EngineStatus(
                phase="running",
                position_count=0,
                order_count=0,
                open_notional=Decimal("0"),
                peak_equity=Decimal("1000"),
                current_equity=Decimal("1000"),
                drawdown_pct=Decimal("0"),
                reliable=True,
            )
        return EngineStatus(
            phase="starting",
            position_count=0,
            order_count=0,
            open_notional=Decimal("0"),
            peak_equity=Decimal("0"),
            current_equity=Decimal("0"),
            drawdown_pct=Decimal("0"),
            reliable=False,
            unreliable_reason="portfolio_equity_missing:USDT",
        )

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        self.flattened.append(reason)


class _HostWithoutProbe(_Host):
    """An older engine: it cannot be asked whether it has finished starting."""

    deployment_ready = None

    def __getattribute__(self, name):
        if name == "deployment_ready":
            raise AttributeError(name)
        return super().__getattribute__(name)


async def _one_round(host: _Host, boundary: RunnerReservationBoundary, **kwargs) -> None:
    stop = asyncio.Event()

    async def stop_after_a_moment() -> None:
        await asyncio.sleep(0.02)
        stop.set()

    stopper = asyncio.create_task(stop_after_a_moment())
    await _run_signed_safety_supervision(
        stop,
        host=host,
        boundaries={str(INSTANCE): boundary},
        interval_secs=0.001,
        **kwargs,
    )
    await stopper


@pytest.mark.asyncio
async def test_a_deployment_that_is_still_starting_is_not_frozen() -> None:
    host = _Host(ready=False)
    boundary = _boundary()

    await _one_round(host, boundary)

    assert boundary.fallback_breaker.frozen is False, (
        "an account balance that has not arrived yet is not a lost view of a running deployment"
    )
    assert host.status_calls == 0, "an unready deployment must not even be asked"
    assert host.flattened == []


@pytest.mark.asyncio
async def test_once_it_is_ready_it_is_evaluated_normally() -> None:
    """The control: waiting must not become never looking."""
    host = _Host(ready=True, reliable=True)
    boundary = _boundary()

    await _one_round(host, boundary)

    assert host.status_calls > 0
    assert boundary.fallback_breaker.frozen is False


@pytest.mark.asyncio
async def test_losing_the_view_of_a_ready_deployment_still_fails_closed() -> None:
    """The other control, and the one that matters: this must not weaken containment."""
    host = _Host(ready=True, reliable=False)
    boundary = _boundary()

    await _one_round(host, boundary)

    assert boundary.fallback_breaker.frozen is True
    assert host.flattened, "fail-closed must still flatten"


@pytest.mark.asyncio
async def test_an_engine_that_never_reports_ready_is_guarded_anyway() -> None:
    """Never blind: past the bound it is evaluated regardless, which fails closed."""
    host = _Host(ready=False)
    boundary = _boundary()

    with capture_logs() as events:
        await _one_round(host, boundary, readiness_timeout_secs=0.0)

    assert boundary.fallback_breaker.frozen is True, (
        "a half-started engine must end up guarded, not silently exempt"
    )
    assert any(event["event"] == "signed_supervision_readiness_timeout" for event in events)


@pytest.mark.asyncio
async def test_a_host_without_the_probe_is_evaluated_as_before_and_says_so() -> None:
    """A safety check that is quietly not running is worse than one that is loudly not."""
    host = _HostWithoutProbe(ready=False)
    boundary = _boundary()

    with capture_logs() as events:
        await _one_round(host, boundary)

    assert boundary.fallback_breaker.frozen is True
    assert any(event["event"] == "signed_supervision_readiness_unknown" for event in events)


@pytest.mark.asyncio
async def test_a_startup_trip_does_not_become_a_durable_freeze(tmp_path) -> None:
    """Since fix 20 a freeze outlives the process, so a spurious one needs an operator.

    This is the interaction, not a restatement: without the readiness wait the trip
    above would be written to runner_breaker_state and survive every restart until
    somebody ran ``arx-runner breaker clear``.
    """
    from tests.test_order_reservation import INSTANCE_A, _store

    store = _store(tmp_path / "startup.sqlite3")
    breaker = FallbackBreaker(
        FallbackBreakerConfig(max_notional=Decimal("1000"), max_drawdown_pct=Decimal("10")),
        on_state_change=lambda peak, frozen, reason: store.record_breaker_state_sync(
            deployment_instance_id=INSTANCE_A,
            peak_equity=peak,
            frozen=frozen,
            reason_code=reason,
        ),
    )
    boundary = RunnerReservationBoundary(
        store=NS(),
        deployment_instance_id=INSTANCE,
        policy_id=POLICY,
        fallback_breaker=breaker,
        trading_mode="testnet",
    )

    await _one_round(_Host(ready=False), boundary)

    assert store.load_breaker_state_sync(INSTANCE_A) is None, (
        "a deployment that never started must not leave a freeze an operator has to lift"
    )


class _RestartingHost(_Host):
    """A node that was ready, and was then replaced by one that is still starting.

    Nothing about the deployment changed: the id is the deployment, not the run. NT
    restarts the node under it, and the replacement has its own startup reconciliation
    to finish -- with an account balance that has not arrived yet, which is the same
    unready engine the wait exists for, arriving by a different door.
    """

    def __init__(self) -> None:
        super().__init__(ready=True, reliable=True)

    async def deployment_ready(self, deployment_instance_id: str) -> bool:
        self.readiness_calls += 1
        if self.readiness_calls >= 2:
            self._ready = False
            self._reliable = False
        return self._ready


@pytest.mark.asyncio
async def test_a_restart_under_the_same_id_is_asked_again() -> None:
    """FR-4: the first node's answer was latched and never revisited."""
    host = _RestartingHost()
    boundary = _boundary()

    await _one_round(host, boundary)

    assert host.readiness_calls > 1, "readiness is a current fact, not a one-off gate"
    assert host.status_calls == 1, "the replacement node must not be evaluated mid-startup"
    assert boundary.fallback_breaker.frozen is False
    assert host.flattened == []


@pytest.mark.asyncio
async def test_a_restart_that_never_finishes_is_still_guarded() -> None:
    """Waiting is not exempting, and reopening the window must not change that."""
    host = _RestartingHost()
    boundary = _boundary()

    with capture_logs() as events:
        await _one_round(host, boundary, readiness_timeout_secs=0.0)

    assert boundary.fallback_breaker.frozen is True
    names = [event["event"] for event in events]
    assert "signed_supervision_restart_awaiting_readiness" in names
    assert names.count("signed_supervision_readiness_timeout") == 1, (
        "an error repeated every round is one nobody can read"
    )


@pytest.mark.asyncio
async def test_a_deployment_that_goes_away_leaves_no_spent_window_behind() -> None:
    """An id that returns is a new deployment and gets its own bounded wait.

    Carrying the previous occupant's exhausted window across would evaluate the new
    one from its first round -- the startup trip this whole wait exists to avoid,
    handed to it by its predecessor.
    """
    from custos.cli._daemon import _SupervisionStartups

    startups = _SupervisionStartups(0.01)
    host = _Host(ready=False)

    assert await startups.may_evaluate(host, str(INSTANCE)) is False
    await asyncio.sleep(0.02)
    assert await startups.may_evaluate(host, str(INSTANCE)) is True, "past the bound"

    startups.retain(set())

    assert await startups.may_evaluate(host, str(INSTANCE)) is False


class _ChurningHost(_Host):
    """The deployment leaves the registry and comes back under the same id."""

    def __init__(self) -> None:
        super().__init__(ready=True, reliable=True)
        self.listings = 0

    def runner_fact_deployments(self):
        self.listings += 1
        return () if self.listings % 2 else (NS(deployment_instance_id=str(INSTANCE)),)


@pytest.mark.asyncio
async def test_the_supervision_loop_forgets_deployments_that_left() -> None:
    """Wiring: ``retain`` changes nothing if nobody calls it each round."""
    host = _ChurningHost()
    boundary = _boundary()

    with capture_logs() as events:
        await _one_round(host, boundary)

    starts = [event for event in events if event["event"] == "signed_supervision_evaluating"]
    assert len(starts) > 1, (
        "a deployment that left and returned is a new one, and starts being evaluated again"
    )
