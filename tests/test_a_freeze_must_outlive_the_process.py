"""A tripped breaker stays tripped until someone says otherwise.

The breaker's own contract is "freezes further orders until an operator
intervenes". It used to live entirely in memory, so a daemon restart built a
fresh one with `frozen = False` and `peak_equity = 0` -- every restart was an
unsigned release, and the drawdown high-water mark restarted from whatever the
equity happened to be. These tests pin the freeze and the high-water mark to the
runner state database, keyed by deployment instance, and require an explicit
release to be the only way out.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from tests.test_order_reservation import INSTANCE_A, INSTANCE_B, POLICY_ID, _store


def _config(max_drawdown_pct: str = "10") -> FallbackBreakerConfig:
    return FallbackBreakerConfig(
        max_notional=Decimal("10000"),
        max_drawdown_pct=Decimal(max_drawdown_pct),
        policy_id=POLICY_ID,
        policy_digest="d" * 64,
        owner_policy=True,
        source="verified_crucible_runner_policy",
    )


def _persisting_breaker(store, instance, *, config: FallbackBreakerConfig | None = None):
    """A breaker wired the way the daemon wires it."""
    return FallbackBreaker(
        config or _config(),
        on_state_change=lambda peak_equity, frozen, reason_code: store.record_breaker_state_sync(
            deployment_instance_id=instance,
            peak_equity=peak_equity,
            frozen=frozen,
            reason_code=reason_code,
        ),
    )


def _restored_breaker(store, instance, *, config: FallbackBreakerConfig | None = None):
    """What a restart does: build a fresh breaker, then load the durable state."""
    breaker = _persisting_breaker(store, instance, config=config)
    state = store.load_breaker_state_sync(instance)
    if state is not None:
        breaker.restore(peak_equity=state.peak_equity, frozen=state.frozen)
    return breaker


def _trip_on_drawdown(breaker) -> None:
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("800"))
    assert breaker.frozen


def test_a_restart_keeps_both_the_freeze_and_the_high_water_mark(tmp_path: Path) -> None:
    store = _store(tmp_path / "breaker.sqlite3")
    _trip_on_drawdown(_persisting_breaker(store, INSTANCE_A))

    restarted = _restored_breaker(store, INSTANCE_A)

    assert restarted.frozen is True
    assert restarted.allows_new_orders() is False
    # The mark is what makes the drawdown visible at all; losing it hides the fall.
    assert restarted.peak_equity == Decimal("1000")


def test_the_high_water_mark_survives_even_without_a_trip(tmp_path: Path) -> None:
    """A restart mid-fall must not re-baseline the drawdown to the lower equity."""
    store = _store(tmp_path / "peak.sqlite3")
    breaker = _persisting_breaker(store, INSTANCE_A, config=_config(max_drawdown_pct="50"))
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("900"))
    assert breaker.frozen is False

    restarted = _restored_breaker(store, INSTANCE_A, config=_config(max_drawdown_pct="50"))
    verdict = restarted.evaluate(open_notional=Decimal("0"), current_equity=Decimal("400"))

    assert restarted.peak_equity == Decimal("1000")
    assert verdict.drawdown_pct == Decimal("60")
    assert verdict.tripped is True


def test_the_freeze_is_scoped_to_one_deployment_instance(tmp_path: Path) -> None:
    store = _store(tmp_path / "scope.sqlite3")
    _trip_on_drawdown(_persisting_breaker(store, INSTANCE_A))

    neighbour = _restored_breaker(store, INSTANCE_B)

    assert neighbour.frozen is False
    assert store.load_breaker_state_sync(INSTANCE_B) is None


def test_a_config_refresh_changes_the_ceilings_and_nothing_else(tmp_path: Path) -> None:
    store = _store(tmp_path / "hot.sqlite3")
    breaker = _persisting_breaker(store, INSTANCE_A)
    _trip_on_drawdown(breaker)

    changed = breaker.apply_config(_config(max_drawdown_pct="40"))

    assert changed is True
    assert breaker.frozen is True
    state = store.load_breaker_state_sync(INSTANCE_A)
    assert state is not None
    assert state.frozen is True
    assert state.released_at_ns is None


def test_a_day_boundary_is_not_a_release(tmp_path: Path) -> None:
    """The breaker has no daily semantics; the daily reset lives in the risk controller."""
    store = _store(tmp_path / "rollover.sqlite3")
    _trip_on_drawdown(_persisting_breaker(store, INSTANCE_A))
    before = store.load_breaker_state_sync(INSTANCE_A)
    assert before is not None and before.frozen_at_ns is not None

    tomorrow = _restored_breaker(store, INSTANCE_A)

    assert tomorrow.frozen is True
    after = store.load_breaker_state_sync(INSTANCE_A)
    assert after is not None
    assert after.frozen_at_ns == before.frozen_at_ns


def test_a_still_frozen_breaker_keeps_the_moment_it_first_tripped(tmp_path: Path) -> None:
    """Re-recording must not look like a fresh trip, or "how long" is unanswerable."""
    store = _store(tmp_path / "moment.sqlite3")
    breaker = _persisting_breaker(store, INSTANCE_A)
    _trip_on_drawdown(breaker)
    first = store.load_breaker_state_sync(INSTANCE_A)
    assert first is not None and first.frozen_at_ns is not None

    # Equity recovers past the old peak: a new high-water mark is written while the
    # breaker is still frozen, which is the only path that re-records a frozen row.
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1200"))
    again = store.load_breaker_state_sync(INSTANCE_A)

    assert again is not None
    assert again.peak_equity == Decimal("1200")
    assert again.frozen is True
    assert again.frozen_at_ns == first.frozen_at_ns
    assert again.reason_code == first.reason_code


def test_only_an_explicit_release_unfreezes_and_it_is_attributed(tmp_path: Path) -> None:
    store = _store(tmp_path / "release.sqlite3")
    _trip_on_drawdown(_persisting_breaker(store, INSTANCE_A))

    released = store.release_breaker_sync(
        deployment_instance_id=INSTANCE_A,
        operator="wukai",
        reason="reviewed the drawdown, funding restored",
    )

    assert released.frozen is False
    assert released.released_by == "wukai"
    assert released.release_reason == "reviewed the drawdown, funding restored"
    assert released.released_at_ns is not None
    assert _restored_breaker(store, INSTANCE_A).allows_new_orders() is True


def test_a_release_is_not_a_pardon_for_a_breach_that_is_still_there(tmp_path: Path) -> None:
    store = _store(tmp_path / "repeat.sqlite3")
    _trip_on_drawdown(_persisting_breaker(store, INSTANCE_A))
    store.release_breaker_sync(
        deployment_instance_id=INSTANCE_A,
        operator="wukai",
        reason="too hasty",
    )

    resumed = _restored_breaker(store, INSTANCE_A)
    verdict = resumed.evaluate(open_notional=Decimal("0"), current_equity=Decimal("800"))

    assert verdict.tripped is True
    assert resumed.frozen is True
    state = store.load_breaker_state_sync(INSTANCE_A)
    assert state is not None and state.frozen is True
    # The row must not read as frozen and released at the same time; the stale
    # release would otherwise look like it still stands.
    assert state.released_at_ns is None
    assert state.released_by is None
    assert state.release_reason is None


def test_releasing_a_breaker_that_was_never_frozen_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path / "noop.sqlite3")

    with pytest.raises(Exception, match="not frozen"):
        store.release_breaker_sync(
            deployment_instance_id=INSTANCE_A,
            operator="wukai",
            reason="nothing to do",
        )


def test_releasing_twice_is_refused_and_does_not_rewrite_the_first_release(
    tmp_path: Path,
) -> None:
    """A row that exists but is unfrozen must be refused as loudly as a missing one.

    Without this the refusal could be passing only because no row was there at all,
    which is a different reason than the one being claimed.
    """
    store = _store(tmp_path / "twice.sqlite3")
    breaker = _persisting_breaker(store, INSTANCE_A)
    breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
    assert store.load_breaker_state_sync(INSTANCE_A) is not None, "an unfrozen row exists"

    with pytest.raises(Exception, match="not frozen"):
        store.release_breaker_sync(
            deployment_instance_id=INSTANCE_A,
            operator="wukai",
            reason="there is nothing to lift",
        )

    _trip_on_drawdown(breaker)
    first = store.release_breaker_sync(
        deployment_instance_id=INSTANCE_A, operator="wukai", reason="reviewed"
    )
    with pytest.raises(Exception, match="not frozen"):
        store.release_breaker_sync(
            deployment_instance_id=INSTANCE_A, operator="someone-else", reason="again"
        )

    still = store.load_breaker_state_sync(INSTANCE_A)
    assert still is not None
    assert still.released_by == "wukai"
    assert still.released_at_ns == first.released_at_ns


def test_a_failure_to_persist_keeps_the_freeze_and_says_so() -> None:
    """Containment must not depend on the database answering."""

    def refuse(peak_equity, frozen, reason_code):
        raise RuntimeError("state database is locked")

    breaker = FallbackBreaker(_config(), on_state_change=refuse)

    with capture_logs() as events:
        breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
        verdict = breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("800"))

    assert verdict.tripped is True
    assert breaker.frozen is True
    refusals = [
        event for event in events if event["event"] == "fallback_breaker_state_persist_failed"
    ]
    assert refusals, "a freeze that could not be written down must not be silent"
    assert refusals[-1]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_the_daemon_loads_the_freeze_before_it_hands_out_a_boundary(
    tmp_path: Path,
) -> None:
    """The reviewer's shape: a new registry over the same store must not forget."""
    from custos.cli._daemon import _build_runner_safety_boundary_factory

    store = _store(tmp_path / "daemon.sqlite3")
    resolver = AsyncMock()
    resolver.resolve.return_value = type(
        "Limits", (), {"owner_policy": True, "policy_id": POLICY_ID, "breaker": _config()}
    )()
    spec = {"trading_mode": "sandbox", "deployment_instance_id": str(INSTANCE_A)}

    build = _build_runner_safety_boundary_factory(
        state_store=store, safety_policy_resolver=resolver, boundaries={}
    )
    first = await build(spec)
    _trip_on_drawdown(first.fallback_breaker)

    # A daemon restart: new registry, same durable store.
    rebuild = _build_runner_safety_boundary_factory(
        state_store=store, safety_policy_resolver=resolver, boundaries={}
    )
    after = await rebuild(spec)

    assert after.fallback_breaker.frozen is True
    assert after.fallback_breaker.allows_new_orders() is False
    assert after.fallback_breaker.peak_equity == Decimal("1000")


def test_the_operator_surface_reports_and_lifts_a_real_freeze(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The release path has to work through the command an operator actually runs."""
    from custos.cli.subcommands import main

    database = tmp_path / "cli.sqlite3"
    store = _store(database)
    _trip_on_drawdown(_persisting_breaker(store, INSTANCE_A))
    argv = [
        "breaker",
        "status",
        "--deployment-instance-id",
        str(INSTANCE_A),
        "--runner-fact-outbox",
        str(database),
    ]

    assert main(argv) == 1, "a frozen breaker is not a healthy exit code"
    assert "frozen since" in capsys.readouterr().out

    assert (
        main(
            [
                "breaker",
                "clear",
                "--deployment-instance-id",
                str(INSTANCE_A),
                "--runner-fact-outbox",
                str(database),
                "--operator",
                "wukai",
                "--reason",
                "reviewed the drawdown",
            ]
        )
        == 0
    )
    assert "released by wukai" in capsys.readouterr().out
    assert main(argv) == 0
    assert "not frozen" in capsys.readouterr().out
    assert _restored_breaker(store, INSTANCE_A).allows_new_orders() is True


def test_clearing_a_breaker_that_is_not_frozen_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from custos.cli.subcommands import main

    database = tmp_path / "cli-noop.sqlite3"
    _store(database)

    exit_code = main(
        [
            "breaker",
            "clear",
            "--deployment-instance-id",
            str(INSTANCE_A),
            "--runner-fact-outbox",
            str(database),
            "--operator",
            "wukai",
            "--reason",
            "nothing to do",
        ]
    )

    assert exit_code == 1
    assert "not frozen" in capsys.readouterr().err


class _FlakyState:
    """A state sink that refuses the first ``failures`` freezes and then works.

    Only the freeze is refused: the pre-freeze high-water mark has to land, because
    what this is about is a store that took everything up to the freeze and then
    missed exactly the row that matters.
    """

    def __init__(self, failures: int) -> None:
        self._remaining = failures
        self.written: list[tuple[Decimal, bool, str | None]] = []
        self.attempts = 0

    def __call__(self, peak_equity: Decimal, frozen: bool, reason_code: str | None) -> None:
        self.attempts += 1
        if frozen and self._remaining > 0:
            self._remaining -= 1
            raise OSError("state database is temporarily unavailable")
        self.written.append((peak_equity, frozen, reason_code))


class TestAFreezeThatCouldNotBeWrittenIsWrittenLater:
    """FR-6: a single transient write failure made the freeze memory-only forever.

    The failure was caught and logged, and nothing remembered it. Every later
    publication is conditional -- ``fail_closed`` writes only on the transition,
    ``evaluate`` only on a new trip or a new high -- so the store kept the
    pre-freeze row, and the next restart rebuilt a breaker that let orders through
    while nobody had lifted anything.
    """

    @staticmethod
    def _tripped(sink: _FlakyState) -> FallbackBreaker:
        breaker = FallbackBreaker(_config(), on_state_change=sink)
        breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
        breaker.fail_closed("execution_accounting_error")
        return breaker

    def test_the_next_evaluation_writes_what_the_failed_one_could_not(self):
        sink = _FlakyState(failures=1)
        breaker = self._tripped(sink)
        assert sink.written == [(Decimal("1000"), False, None)], "only the pre-freeze row landed"

        breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))

        assert sink.written[-1][1] is True

    def test_the_retry_carries_the_reason_the_freeze_was_reached_under(self):
        """This tick froze nothing; explaining the freeze with its reason is wrong."""
        sink = _FlakyState(failures=1)
        breaker = self._tripped(sink)

        breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))

        assert sink.written[-1][2] == "execution_accounting_error"

    def test_a_restart_after_the_retry_still_refuses_orders(self, tmp_path: Path):
        """The consequence, through the durable store the daemon actually uses."""
        store = _store(tmp_path / "flaky.sqlite3")
        failures = {"left": 1}

        def write(peak_equity, frozen, reason_code):
            if frozen and failures["left"]:
                failures["left"] -= 1
                raise OSError("state database is temporarily unavailable")
            store.record_breaker_state_sync(
                deployment_instance_id=INSTANCE_A,
                peak_equity=peak_equity,
                frozen=frozen,
                reason_code=reason_code,
            )

        breaker = FallbackBreaker(_config(), on_state_change=write)
        breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))
        breaker.fail_closed("execution_accounting_error")
        breaker.fail_closed("execution_accounting_error")

        assert _restored_breaker(store, INSTANCE_A).allows_new_orders() is False

    def test_a_sink_that_never_works_keeps_trying_and_keeps_saying_so(self):
        sink = _FlakyState(failures=99)

        with capture_logs() as events:
            breaker = self._tripped(sink)
            for _ in range(3):
                breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))

        failures = [
            event for event in events if event["event"] == "fallback_breaker_state_persist_failed"
        ]
        assert len(failures) == 4, "each attempt is reported; a silent retry is not a retry"
        assert [event["attempt"] for event in failures] == [1, 2, 3, 4]
        assert breaker.frozen is True, "the in-memory freeze stands regardless"

    def test_a_write_that_succeeded_is_not_written_again(self):
        """The control: the retry must not turn every tick into a write."""
        sink = _FlakyState(failures=0)
        breaker = self._tripped(sink)
        before = sink.attempts

        for _ in range(3):
            breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("1000"))

        assert sink.attempts == before


def test_a_stale_durable_row_cannot_lift_a_freeze_that_is_already_held() -> None:
    """Restoring is loading, and loading must not be a release.

    A freeze is lifted by an operator. A row that says otherwise is a row that was
    written before the freeze -- which is exactly the row a failed write leaves
    behind.
    """
    breaker = FallbackBreaker(_config())
    breaker.fail_closed("execution_accounting_error")

    breaker.restore(peak_equity=Decimal("500"), frozen=False)

    assert breaker.frozen is True
    assert breaker.peak_equity == Decimal("500"), "the durable mark is still adopted"
