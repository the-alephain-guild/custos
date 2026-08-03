"""Where the offline lane's exposure ceilings come from, and what refuses them.

The lane has no signed owner policy and never will, so the strictest non-live
fallback is the floor it starts from. A spec may name its own ceilings because the
lane already takes the strategy, the venue and the credential from that same
unsigned spec — but a ceiling that cannot be read is refused rather than quietly
replaced by the default, which would leave an operator believing a limit is in
force that never was.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

from custos.contracts import TradingMode
from custos.core.engine_protocol import EngineStatus
from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.core.local_cap import RunnerSafetyPolicyUnavailableError
from custos.offline.mode_guard import OfflineModeRefused
from custos.offline.safety import OfflineExposureGuard, resolve_breaker_config
from custos.offline.spec import OfflineDeploymentSpec

STRICTEST_NOTIONAL = Decimal("200")
STRICTEST_DRAWDOWN_PCT = Decimal("10")
INSTANCE = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
_STILL_HELD = "offline_exposure_latched_deployment_stopping"


def _spec(**overrides: Any) -> OfflineDeploymentSpec:
    document: dict[str, Any] = {
        "spec_id": "supertrend-sandbox",
        "generation": 1,
        "trading_mode": "sandbox",
        "lifecycle_state": "running",
        "strategy_path": "/opt/ps/trend/supertrend",
        "provenance_ref": {"credential_id": "binance-supertrend"},
        "connector": "binance_perpetual",
        "pairs": ["BTC-USDT"],
        "leverage": 3,
        "sandbox": {"starting_balances": ["10_000 USDT"]},
    }
    document.update(overrides)
    return OfflineDeploymentSpec.model_validate(document)


def test_a_spec_that_names_no_limits_gets_the_strictest_non_live_ceilings() -> None:
    config = resolve_breaker_config(_spec())

    assert config.max_notional == STRICTEST_NOTIONAL
    assert config.max_drawdown_pct == STRICTEST_DRAWDOWN_PCT
    assert config.owner_policy is False


def test_the_default_ceilings_are_the_shared_strictest_fallback_not_a_local_copy() -> None:
    """Two sources of truth for the same number drift; this asserts there is one."""

    assert resolve_breaker_config(_spec()) == FallbackBreakerConfig.strictest_local_fallback(
        "sandbox"
    )


def test_a_spec_may_raise_the_ceilings_above_the_default() -> None:
    """The consumer funds sandbox runs with 10,000 USDT; $200 would trip on entry."""

    config = resolve_breaker_config(
        _spec(risk_config={"max_total_notional": "25000", "max_drawdown_pct": "35"})
    )

    assert config.max_notional == Decimal("25000")
    assert config.max_drawdown_pct == Decimal("35")
    assert config.source != FallbackBreakerConfig.strictest_local_fallback("sandbox").source


def test_a_spec_may_also_lower_the_ceilings() -> None:
    """A tighter limit is strictly safer, so refusing it would be perverse."""

    config = resolve_breaker_config(_spec(risk_config={"max_total_notional": "25"}))

    assert config.max_notional == Decimal("25")
    assert config.max_drawdown_pct == STRICTEST_DRAWDOWN_PCT


def test_naming_one_ceiling_leaves_the_other_at_the_default() -> None:
    config = resolve_breaker_config(_spec(risk_config={"max_drawdown_pct": "40"}))

    assert config.max_notional == STRICTEST_NOTIONAL
    assert config.max_drawdown_pct == Decimal("40")


@pytest.mark.parametrize("value", ["0", "-1", "-0.5"])
def test_a_ceiling_that_is_not_positive_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": value}))


@pytest.mark.parametrize("value", ["", "lots", "1,000", None, True, ["200"]])
def test_a_ceiling_that_cannot_be_read_is_refused_rather_than_defaulted(value: object) -> None:
    """Falling back silently would report a limit the operator never set."""

    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": value}))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_a_ceiling_decimal_accepts_but_arithmetic_cannot_is_refused(value: str) -> None:
    """These parse. Comparing exposure against them is the part that does not work."""

    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": value}))


def test_a_float_ceiling_is_refused_because_money_is_not_binary_fractions() -> None:
    with pytest.raises(ValueError, match="max_total_notional"):
        resolve_breaker_config(_spec(risk_config={"max_total_notional": 25000.0}))


def test_an_integer_ceiling_is_read_exactly() -> None:
    config = resolve_breaker_config(_spec(risk_config={"max_total_notional": 25000}))

    assert config.max_notional == Decimal("25000")


def test_a_misspelled_ceiling_is_refused_rather_than_ignored() -> None:
    """An ignored typo reads, from the operator's side, exactly like a raised limit."""

    with pytest.raises(ValueError, match="max_notional"):
        resolve_breaker_config(_spec(risk_config={"max_notional": "25000"}))


def test_live_is_refused_by_the_lane_before_any_ceiling_is_resolved() -> None:
    live = _spec().model_copy(update={"trading_mode": TradingMode.LIVE})

    with pytest.raises(OfflineModeRefused, match="live"):
        resolve_breaker_config(live)


def test_the_shared_fallback_refuses_live_on_its_own_account() -> None:
    """Proves the inner layer is a live guard, not a branch the lane's guard shadows."""

    with pytest.raises(RunnerSafetyPolicyUnavailableError):
        FallbackBreakerConfig.strictest_local_fallback("live")


def _status(**overrides: Any) -> EngineStatus:
    document: dict[str, Any] = {
        "phase": "running",
        "position_count": 1,
        "order_count": 0,
        "open_notional": Decimal("100"),
        "peak_equity": Decimal("10000"),
        "current_equity": Decimal("10000"),
        "drawdown_pct": Decimal("0"),
    }
    document.update(overrides)
    return EngineStatus(**document)


class _SafetyEngine:
    """An engine that answers with one snapshot, or refuses to answer at all.

    It holds a deployment from the moment it is watched until a stop succeeds, so
    ``attached`` answers about the engine rather than about a record of it.
    """

    def __init__(self, snapshot: EngineStatus | Exception | None = None) -> None:
        self.snapshot = snapshot if snapshot is not None else _status()
        self.asked: list[str] = []
        self.flattened: list[tuple[str, str]] = []
        self.flatten_error: Exception | None = None
        self.stopped: list[str] = []
        self.stop_error: Exception | None = None
        self._released: set[str] = set()

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.asked.append(deployment_instance_id)
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        self.flattened.append((deployment_instance_id, reason))
        if self.flatten_error is not None:
            raise self.flatten_error

    async def stop(self, deployment_instance_id: str) -> None:
        self.stopped.append(deployment_instance_id)
        if self.stop_error is not None:
            raise self.stop_error
        self._released.add(deployment_instance_id)

    def attached(self, deployment_instance_id: str) -> bool:
        return deployment_instance_id not in self._released


def _guard(engine: _SafetyEngine, **overrides: Any) -> OfflineExposureGuard:
    return OfflineExposureGuard(engine=engine, interval=0.001, **overrides)


def _watch(guard: OfflineExposureGuard, spec: OfflineDeploymentSpec) -> None:
    guard.watch(spec.spec_id, INSTANCE, resolve_breaker_config(spec))


async def test_nothing_is_evaluated_before_a_deployment_is_watched() -> None:
    engine = _SafetyEngine()

    assert await _guard(engine).evaluate_once() == []
    assert engine.asked == []


async def test_a_snapshot_within_the_ceiling_leaves_the_position_alone() -> None:
    engine = _SafetyEngine()
    guard = _guard(engine)
    _watch(guard, _spec(risk_config={"max_total_notional": "25000"}))

    await guard.evaluate_once()

    assert engine.asked == [INSTANCE]
    assert engine.flattened == []
    assert guard.allows_new_generations()


async def test_a_snapshot_beyond_the_ceiling_flattens_and_latches() -> None:
    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    await guard.evaluate_once()

    assert engine.flattened == [(INSTANCE, "notional_breach")]
    assert not guard.allows_new_generations()


async def test_an_unreadable_snapshot_fails_closed_rather_than_assuming_safety() -> None:
    engine = _SafetyEngine(RuntimeError("the engine is not answering"))
    guard = _guard(engine)
    _watch(guard, _spec())

    await guard.evaluate_once()

    assert [instance for instance, _ in engine.flattened] == [INSTANCE]
    assert not guard.allows_new_generations()


async def test_an_untrustworthy_snapshot_fails_closed_too() -> None:
    engine = _SafetyEngine(_status(reliable=False, unreliable_reason="no_mark_for_position"))
    guard = _guard(engine)
    _watch(guard, _spec())

    await guard.evaluate_once()

    assert engine.flattened == [(INSTANCE, "no_mark_for_position")]


async def test_a_latched_guard_does_not_flatten_the_same_position_every_tick() -> None:
    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    await guard.evaluate_once()
    await guard.evaluate_once()

    assert len(engine.flattened) == 1


async def test_the_ceiling_the_spec_named_is_the_one_enforced() -> None:
    """The same exposure that breaches the default is within a raised ceiling."""

    engine = _SafetyEngine(_status(open_notional=Decimal("9000")))
    guard = _guard(engine)
    _watch(guard, _spec(risk_config={"max_total_notional": "25000"}))

    await guard.evaluate_once()

    assert engine.flattened == []


async def test_a_stopped_deployment_is_no_longer_evaluated() -> None:
    """A stopped instance answers nothing, and failing closed on that would be noise."""

    engine = _SafetyEngine()
    guard = _guard(engine)
    spec = _spec()
    _watch(guard, spec)

    guard.release(spec.spec_id)
    await guard.evaluate_once()

    assert engine.asked == []


async def test_the_tick_keeps_evaluating_until_it_is_stopped() -> None:
    engine = _SafetyEngine()
    guard = _guard(engine)
    _watch(guard, _spec(risk_config={"max_total_notional": "25000"}))
    stop = asyncio.Event()

    async def stop_after_three() -> None:
        while len(engine.asked) < 3:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(asyncio.gather(guard.run(stop), stop_after_three()), timeout=2)

    assert len(engine.asked) >= 3


async def test_the_tick_does_not_end_once_every_watched_deployment_is_latched() -> None:
    """This used to assert the opposite -- that the loop returned here.

    "Nothing is left to evaluate" was true only of the exposure number. The
    deployment was still running, and on real hardware it opened four more
    positions, each over twice the ceiling that had latched, with nothing
    watching. Standing down is the defect; the loop now runs until it is stopped.
    """

    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(guard.run(asyncio.Event()), timeout=0.05)

    assert len(engine.flattened) == 1
    assert engine.stopped == [INSTANCE]


class _WedgedEngine(_SafetyEngine):
    """An engine that accepts the question and never answers it."""

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.asked.append(deployment_instance_id)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_an_engine_that_never_answers_does_not_hold_the_tick_forever() -> None:
    engine = _WedgedEngine()
    guard = _guard(engine, deadline=0.02)
    _watch(guard, _spec())

    await asyncio.wait_for(guard.evaluate_once(), timeout=2)

    assert engine.asked == [INSTANCE]


async def test_an_engine_that_never_answers_fails_closed() -> None:
    """Silence is not evidence of safety, and it is the one case with no exception."""

    engine = _WedgedEngine()
    guard = _guard(engine, deadline=0.02)
    _watch(guard, _spec())

    await guard.evaluate_once()

    assert not guard.allows_new_generations()


async def test_a_wedged_engine_is_recorded_as_containment_not_confirmed() -> None:
    """Nothing flattened the position, so the log must not read as if something had."""

    engine = _WedgedEngine()
    guard = _guard(engine, deadline=0.02)
    _watch(guard, _spec())

    with capture_logs() as logs:
        await guard.evaluate_once()

    assert any(entry["event"] == "offline_exposure_containment_unconfirmed" for entry in logs)
    assert engine.flattened == []


async def test_the_tick_does_not_end_against_an_engine_that_never_answers() -> None:
    """Also a superseded contract: the loop used to return once this latched.

    An engine too wedged to answer is not an engine that has stopped trading, so
    the deployment still has to be ended.
    """

    engine = _WedgedEngine()
    guard = _guard(engine, deadline=0.02)
    _watch(guard, _spec())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(guard.run(asyncio.Event()), timeout=0.2)

    assert not guard.allows_new_generations()
    assert engine.stopped == [INSTANCE]


async def test_the_tick_does_not_swallow_a_failure_to_flatten() -> None:
    """If containment itself fails, the lane must hear about it, not tick on."""

    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    engine.flatten_error = RuntimeError("the venue refused the close")
    guard = _guard(engine)
    _watch(guard, _spec())

    with pytest.raises(RuntimeError, match="refused the close"):
        await asyncio.wait_for(guard.run(asyncio.Event()), timeout=2)


class _UnstoppableEngine(_SafetyEngine):
    """It accepts the stop and goes on holding the deployment anyway."""

    async def stop(self, deployment_instance_id: str) -> None:
        self.stopped.append(deployment_instance_id)


class _StopWedgedEngine(_SafetyEngine):
    """It accepts the stop and never returns from it."""

    async def stop(self, deployment_instance_id: str) -> None:
        self.stopped.append(deployment_instance_id)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _UnheldEngine(_SafetyEngine):
    """It holds nothing -- a deployment that latched before it ever ran."""

    def attached(self, deployment_instance_id: str) -> bool:
        return False


async def test_a_latched_deployment_is_stopped_rather_than_left_trading() -> None:
    """Flattening removes the exposure that tripped it; the strategy opens the next.

    Measured on real hardware: four entries over the following 2h40m, each more
    than twice the ceiling that had latched, because nothing ever touched the
    strategy.
    """

    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    await guard.evaluate_once()

    assert engine.flattened == [(INSTANCE, "notional_breach")]
    assert engine.stopped == [INSTANCE]


async def test_stopping_the_deployment_does_not_unlatch_the_generation_gate() -> None:
    """The gate reads the watched entries, so dropping one would re-open it.

    Ending the deployment and refusing the next generation are two separate
    promises, and stopping must not quietly retract the second.
    """

    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    await guard.evaluate_once()
    await guard.evaluate_once()

    assert engine.stopped == [INSTANCE]
    assert not guard.allows_new_generations()


async def test_a_deployment_that_will_not_stop_is_reported_on_every_tick() -> None:
    """One shout and then silence would recreate the blind spot being removed."""

    engine = _UnstoppableEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    with capture_logs() as logs:
        for _ in range(3):
            await guard.evaluate_once()

    alarms = [entry for entry in logs if entry["event"] == _STILL_HELD]
    assert len(alarms) == 3
    assert engine.stopped == [INSTANCE, INSTANCE, INSTANCE]


async def test_a_deployment_that_did_stop_is_not_stopped_again() -> None:
    """Falsifies the test above: the repetition must come from the engine still
    holding it, not from the guard repeating itself regardless."""

    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    with capture_logs() as logs:
        for _ in range(3):
            await guard.evaluate_once()

    assert engine.stopped == [INSTANCE]
    assert len([entry for entry in logs if entry["event"] == _STILL_HELD]) == 1


async def test_a_stop_that_never_returns_is_recorded_rather_than_assumed() -> None:
    """Same shape as the wedged status query: no exception to catch, so bound it."""

    engine = _StopWedgedEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine, deadline=0.02)
    _watch(guard, _spec())

    with capture_logs() as logs:
        await asyncio.wait_for(guard.evaluate_once(), timeout=2)

    assert any(entry["event"] == "offline_exposure_stop_unconfirmed" for entry in logs)


async def test_the_tick_does_not_swallow_a_failure_to_stop() -> None:
    """Same contract as a failure to flatten: containment failing is not a detail."""

    engine = _SafetyEngine(_status(open_notional=Decimal("10000")))
    engine.stop_error = RuntimeError("the engine refused to stop")
    guard = _guard(engine)
    _watch(guard, _spec())

    with pytest.raises(RuntimeError, match="refused to stop"):
        await asyncio.wait_for(guard.run(asyncio.Event()), timeout=2)


async def test_a_latched_deployment_the_engine_never_held_is_left_alone() -> None:
    """Nothing to end, so stopping and shouting would both be noise."""

    engine = _UnheldEngine(_status(open_notional=Decimal("10000")))
    guard = _guard(engine)
    _watch(guard, _spec())

    with capture_logs() as logs:
        await guard.evaluate_once()

    assert engine.stopped == []
    assert not any(entry["event"] == _STILL_HELD for entry in logs)
    assert not guard.allows_new_generations()


async def test_an_engine_that_never_answers_is_stopped_too() -> None:
    """It latched by failing closed rather than by breaching, and that is still a
    deployment nobody is watching."""

    engine = _WedgedEngine()
    guard = _guard(engine, deadline=0.02)
    _watch(guard, _spec())

    await guard.evaluate_once()

    assert engine.stopped == [INSTANCE]
