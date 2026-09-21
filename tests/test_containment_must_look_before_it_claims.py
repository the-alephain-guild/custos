"""Containment asked the venue to close, then declared victory without looking.

``flatten_positions`` sent cancels and closes and logged ``positions_flattened``
on the way out. Whether any of it reached the venue was never checked, and the
one log line that spoke about confirmation -- ``nt_flatten_containment_unconfirmed``
-- fired when nothing was visible at all, which is a different question.

The machinery to confirm already existed on the shutdown path
(``_flatten_and_confirm_shutdown``): poll the venue cache until it agrees, retry
what is still outstanding, give up on a deadline. Containment simply never used it.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from custos.engines.nautilus import host as nautilus_host
from custos.engines.nautilus.host import NtTradingNodeHost
from tests.fixtures.fake_live_node import FakeLiveNodeType
from tests.test_nt_trading_node_host import (
    _Artifact,
    _credential,
    _ShutdownAwareStrategy,
    _spec,
    _VenueOrder,
    _VenuePosition,
)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    built = NtTradingNodeHost()
    # Same dials the shutdown-path tests use: the loop is real, the waiting is not.
    # The loop is real; only the waiting is compressed. Ten polls inside the
    # budget is enough to observe a retry and to observe a deadline being kept.
    built._shutdown_poll_secs = 0.01  # noqa: SLF001 - fixture wiring
    built._containment_confirm_secs = 0.1  # noqa: SLF001 - fixture wiring
    built._containment_retry_secs = 0  # noqa: SLF001 - fixture wiring
    return built


async def _deployed(host: NtTradingNodeHost, label: str):
    strategy = _ShutdownAwareStrategy()
    spec = _spec(label, trading_mode="testnet")
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    instance = spec["deployment_instance_id"]
    strategy.node = host._active_nodes[instance].node  # noqa: SLF001 - fixture wiring
    return strategy, instance


@pytest.mark.asyncio
async def test_containment_reads_the_venue_back_before_it_claims(host) -> None:
    """The close worked, and saying so requires having looked."""
    strategy, instance = await _deployed(host, "contain-confirm")
    strategy.node.cache.positions.append(_VenuePosition())

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    names = [event["event"] for event in events]
    assert "nt_containment_confirmed" in names
    assert "nt_flatten_containment_unconfirmed" not in names, (
        "a successful containment must stop reporting itself as unconfirmed"
    )


@pytest.mark.asyncio
async def test_a_position_that_will_not_close_is_asked_again(host) -> None:
    """Retry is the half of the acceptance that a single request cannot satisfy.

    The strategy here swallows the first close -- the venue rejected it, the
    position is still there. One request and a log line would have called that
    contained.
    """
    strategy, instance = await _deployed(host, "contain-retry")
    strategy.node.cache.positions.append(_VenuePosition())
    attempts: list[str] = []
    original = strategy.close_all_positions_with_fallback

    def flaky(instrument_id: str) -> None:
        attempts.append(instrument_id)
        if len(attempts) == 1:
            return  # The venue did not take it; the position stays open.
        original(instrument_id)

    strategy.close_all_positions_with_fallback = flaky

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    assert len(attempts) >= 2, "an unclosed position must be asked again"
    assert not strategy.node.cache.positions
    assert "nt_containment_confirmed" in [event["event"] for event in events]


@pytest.mark.asyncio
async def test_a_position_that_never_closes_ends_loudly_and_on_time(host) -> None:
    """Bounded, and it must not raise.

    Raising would travel up ``EngineSafetySupervisor._tick``'s tripped branch into
    the supervision task, and the daemon fails the process when a long-running
    task exits. One slow venue would take down supervision for every instance.
    """
    strategy, instance = await _deployed(host, "contain-timeout")
    strategy.node.cache.positions.append(_VenuePosition())
    strategy.close_all_positions_with_fallback = lambda instrument_id: None

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    failures = [event for event in events if event["event"] == "nt_containment_not_confirmed"]
    assert failures, "an unconfirmed containment must say so"
    assert failures[0]["position_count"] == 1
    assert strategy.node.cache.positions, "the fixture's position really never closed"


@pytest.mark.asyncio
async def test_a_risk_increasing_order_that_survives_blocks_confirmation(host) -> None:
    """A flat position with a live entry order is not contained.

    That order is the way straight back into the exposure the breaker tripped on,
    which is the whole reason fix 26 cancels it. Confirmation has to include it,
    or the retry never notices the cancel was refused.
    """
    strategy, instance = await _deployed(host, "contain-order-survives")
    strategy.node.cache.positions.append(_VenuePosition())
    strategy.node.cache.orders.append(_VenueOrder(is_reduce_only=False, client_order_id="O-risk"))
    strategy.cancel_order = lambda client_order_id: None  # The venue refuses the cancel.

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    failures = [event for event in events if event["event"] == "nt_containment_not_confirmed"]
    assert failures, "a surviving risk-increasing order must block confirmation"
    assert failures[0]["risk_increasing_order_count"] == 1


@pytest.mark.asyncio
async def test_protective_orders_do_not_block_confirmation(host) -> None:
    """Reduce-only orders stay, and staying is not a failure.

    fix 26 decided protection survives containment. If confirmation counted it as
    leftover, this fix would quietly reverse that decision every time.
    """
    strategy, instance = await _deployed(host, "contain-protection")
    strategy.node.cache.positions.append(_VenuePosition())
    strategy.node.cache.orders.append(_VenueOrder(is_reduce_only=True, client_order_id="O-stop"))

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    names = [event["event"] for event in events]
    assert "nt_containment_confirmed" in names
    assert "nt_containment_not_confirmed" not in names
    assert strategy.node.cache.orders, "the protective order was not cancelled"


@pytest.mark.asyncio
async def test_an_unreadable_venue_is_not_confirmation_and_does_not_wedge(host) -> None:
    """Cannot see is not the same as nothing there, and neither is a reason to hang."""
    strategy, instance = await _deployed(host, "contain-unreadable")
    strategy.node.cache.positions.append(_VenuePosition())
    closed: list[str] = []

    def close(instrument_id: str) -> None:
        closed.append(instrument_id)
        # Only once the close has been requested does the view go dark, so the
        # containment request itself is still made.
        strategy.node.cache.positions_open = _refuse

    def _refuse():
        raise RuntimeError("venue cache is unavailable")

    strategy.close_all_positions_with_fallback = close

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    names = [event["event"] for event in events]
    assert closed, "the close was still requested"
    assert "nt_containment_state_unreadable" in names
    assert "nt_containment_confirmed" not in names, "an unreadable venue confirms nothing"


@pytest.mark.asyncio
async def test_seeing_nothing_without_having_asked_stays_unconfirmed(host) -> None:
    """The startup case this log line was written for, kept intact.

    At startup reconciliation may not have delivered the account's positions yet.
    Nothing visible then means nothing known -- not success.
    """
    _strategy, instance = await _deployed(host, "contain-nothing-seen")

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    names = [event["event"] for event in events]
    assert "nt_flatten_containment_unconfirmed" in names
    assert "nt_containment_confirmed" not in names


@pytest.mark.asyncio
async def test_a_contained_instance_stops_crying_unconfirmed_every_tick(host) -> None:
    """A drawdown breach keeps tripping after the position is gone.

    ``tripped`` is recomputed from the current numbers each tick, and equity does
    not recover just because the position closed -- so the supervisor calls
    containment again, sees nothing open, and logs the unconfirmed error. Forever.
    An error that is always on says nothing; the operator cannot tell it apart
    from the startup case it was written for.
    """
    strategy, instance = await _deployed(host, "contain-repeat")
    strategy.node.cache.positions.append(_VenuePosition())
    await host.flatten_positions(instance, "drawdown_breach")

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    names = [event["event"] for event in events]
    assert "nt_containment_still_clear" in names
    assert "nt_flatten_containment_unconfirmed" not in names


@pytest.mark.asyncio
async def test_a_redeployed_instance_does_not_inherit_the_old_confirmation(host) -> None:
    """The instance id outlives the node; what was contained does not.

    A new generation starts with an account whose positions have not been
    delivered yet -- exactly the startup case. Carrying the previous node's
    confirmation across would answer that question with the wrong node's evidence.
    """
    strategy, instance = await _deployed(host, "contain-redeploy")
    strategy.node.cache.positions.append(_VenuePosition())
    await host.flatten_positions(instance, "drawdown_breach")

    await host.stop(instance)
    replacement = _ShutdownAwareStrategy()
    spec = _spec("contain-redeploy", trading_mode="testnet")
    await host.deploy(spec, _credential(), _Artifact(strategy=replacement))

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    names = [event["event"] for event in events]
    assert "nt_flatten_containment_unconfirmed" in names
    assert "nt_containment_still_clear" not in names
