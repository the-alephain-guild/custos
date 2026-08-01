"""The guard does not question a deployment the engine has not finished starting.

Real testnet evidence, 2026-08-01, timed to the millisecond:

    02:30:03.556  NautilusTrader config: reconciliation_startup_delay_secs = 10.0
    02:30:03.570  ExecEngine: "Awaiting startup reconciliation completion"
    02:30:05.846  breaker fail-closed: portfolio_equity_missing:USDT
    02:30:05.962  Portfolio: Updated AccountState        <- 116 ms too late
    02:30:08.460  ExecEngine: "Startup reconciliation completed"

The guard asked at 2.3 seconds and the balance arrived at 2.4. Nothing was wrong with
the account or the snapshot: the balance can only arrive with reconciliation, and
NautilusTrader says up front that it intends to take about ten seconds over it. No
amount of computing the snapshot more correctly would help, because the data genuinely
did not exist yet.

Owner chose B1-b over a grace window (2026-08-01). A window would need a number, and
picking one is guessing; readiness is a fact the engine can be asked for. It only became
askable today: ``EngineReadinessChecks`` now reads ``trader.is_running``, and per
``NautilusKernel.start_async`` a started trader means startup reconciliation was passed.

Two things this must not become:

* **Blind forever.** An engine that never reports ready would never be guarded at all.
  So the wait is bounded, and past the bound the guard evaluates anyway -- which lands
  on an unreliable snapshot and fails closed. That is the old behaviour, merely delayed,
  and it is the right direction to fail in.
* **Silently skipped.** An engine that cannot answer readiness is evaluated immediately,
  exactly as before -- but it says so, once, rather than leaving a safety check quietly
  disabled.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from structlog.testing import capture_logs

from custos.core.engine_protocol import EngineStatus
from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.offline.safety import OfflineExposureGuard

_SPEC = "supertrend-testnet"
_INSTANCE = "instance-1"


def _limits() -> FallbackBreakerConfig:
    return FallbackBreakerConfig.strictest_local_fallback("testnet")


def _healthy_status() -> EngineStatus:
    return EngineStatus(
        phase="running",
        position_count=0,
        order_count=0,
        open_notional=Decimal("0"),
        peak_equity=Decimal("1000"),
        current_equity=Decimal("1000"),
        drawdown_pct=Decimal("0"),
    )


class _Engine:
    """An engine that reports ready only once told to."""

    def __init__(self, *, ready: bool = False) -> None:
        self.ready = ready
        self.status_calls = 0
        self.readiness_calls = 0
        self.flattened: list[str] = []

    async def deployment_ready(self, deployment_instance_id: str) -> bool:
        self.readiness_calls += 1
        return self.ready

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.status_calls += 1
        return _healthy_status()

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        self.flattened.append(reason)


class _EngineWithoutReadiness:
    """An engine predating the readiness query -- it must keep working unchanged."""

    def __init__(self) -> None:
        self.status_calls = 0
        self.flattened: list[str] = []

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.status_calls += 1
        return _healthy_status()

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        self.flattened.append(reason)


def _guard(engine) -> OfflineExposureGuard:
    guard = OfflineExposureGuard(engine=engine)
    guard.watch(_SPEC, _INSTANCE, _limits())
    return guard


async def test_a_deployment_that_is_not_ready_is_not_questioned() -> None:
    """The whole point: no status is read while the engine is still starting."""
    engine = _Engine(ready=False)
    guard = _guard(engine)

    await guard.evaluate_once()

    assert engine.status_calls == 0, "the guard asked about exposure before the engine was up"
    assert engine.flattened == []
    assert guard.allows_new_generations(), "waiting is not tripping"


async def test_it_is_questioned_once_the_engine_is_ready() -> None:
    engine = _Engine(ready=False)
    guard = _guard(engine)

    await guard.evaluate_once()
    engine.ready = True
    await guard.evaluate_once()

    assert engine.status_calls == 1


async def test_readiness_is_not_re_asked_once_it_has_been_reached() -> None:
    """Readiness is a boundary crossed, not a state to keep polling."""
    engine = _Engine(ready=True)
    guard = _guard(engine)

    await guard.evaluate_once()
    await guard.evaluate_once()
    await guard.evaluate_once()

    assert engine.status_calls == 3
    assert engine.readiness_calls == 1


async def test_an_engine_that_cannot_answer_is_questioned_immediately_and_says_so() -> None:
    """Backwards compatible, but not silent: a disabled safety check has to be visible."""
    engine = _EngineWithoutReadiness()
    guard = _guard(engine)

    with capture_logs() as logs:
        await guard.evaluate_once()
        await guard.evaluate_once()

    assert engine.status_calls == 2
    events = [entry["event"] for entry in logs]
    assert events.count("offline_exposure_readiness_unknown") == 1, (
        "the engine's inability to report readiness must be recorded exactly once, "
        "not per tick and not never"
    )


async def test_waiting_forever_is_not_an_option() -> None:
    """Past the bound the guard evaluates regardless, rather than staying blind.

    An engine stuck half-started would otherwise never be guarded at all -- the fail-open
    hole that makes waiting on readiness dangerous if left unbounded.
    """
    engine = _Engine(ready=False)
    guard = OfflineExposureGuard(engine=engine, readiness_timeout=0.0)
    guard.watch(_SPEC, _INSTANCE, _limits())

    with capture_logs() as logs:
        await guard.evaluate_once()

    assert engine.status_calls == 1, "the bound expired, so the guard must guard"
    assert "offline_exposure_readiness_timeout" in [entry["event"] for entry in logs]


async def test_the_wait_is_visible_in_the_record() -> None:
    """A guard that is deliberately not guarding yet has to say so."""
    engine = _Engine(ready=False)
    guard = _guard(engine)

    with capture_logs() as logs:
        await guard.evaluate_once()
        await guard.evaluate_once()

    events = [entry["event"] for entry in logs]
    assert events.count("offline_exposure_awaiting_readiness") == 1, (
        "say it once when the wait starts -- every tick would be noise"
    )


async def test_becoming_ready_is_recorded_too() -> None:
    engine = _Engine(ready=False)
    guard = _guard(engine)

    with capture_logs() as logs:
        await guard.evaluate_once()
        engine.ready = True
        await guard.evaluate_once()

    assert "offline_exposure_guard_evaluating" in [entry["event"] for entry in logs]


async def test_the_run_loop_does_not_exit_while_waiting_to_become_ready() -> None:
    """``run`` stops when everything has latched. Waiting is not latching."""
    engine = _Engine(ready=False)
    guard = _guard(engine)
    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(guard.run(stop), _stop_soon())

    assert engine.status_calls == 0
    assert guard.allows_new_generations()
