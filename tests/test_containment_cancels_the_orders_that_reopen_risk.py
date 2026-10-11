"""Flattening a position does not contain anything while its orders are still live.

The breaker's freeze stops new submissions, but an order the venue already accepted
is past that gate. ``flatten_positions`` closed positions and left those orders
resting, so a risk-increasing order could fill again straight after the flatten and
reopen the exposure the breaker had just tripped on -- with nothing to notice until
the next supervision tick.

Cancelling comes first: doing it after the close would leave the same window open,
just narrower.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

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


@dataclass
class _Cancels(_ShutdownAwareStrategy):
    pass


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    return NtTradingNodeHost()


async def _deployed(host: NtTradingNodeHost, label: str):
    strategy = _ShutdownAwareStrategy()
    spec = _spec(label, trading_mode="testnet")
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    instance = spec["deployment_instance_id"]
    strategy.node = host._active_nodes[instance].node  # noqa: SLF001 - fixture wiring
    return strategy, instance


@pytest.mark.asyncio
async def test_a_resting_risk_increasing_order_is_cancelled_by_containment(host) -> None:
    strategy, instance = await _deployed(host, "contain-cancel")
    node = strategy.node
    node.cache.positions.append(_VenuePosition())
    working = _VenueOrder(is_reduce_only=False, client_order_id="O-risk")
    node.cache.orders.append(working)

    await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancelled == ["O-risk"], (
        "an order the venue already accepted can refill after the flatten; the "
        "freeze does not reach it"
    )


@pytest.mark.asyncio
async def test_protective_orders_survive_containment(host) -> None:
    """The control: containment must not strip the position's own protection."""
    strategy, instance = await _deployed(host, "contain-keep")
    node = strategy.node
    node.cache.positions.append(_VenuePosition())
    protection = _VenueOrder(is_reduce_only=True, client_order_id="O-stop")
    node.cache.orders.append(protection)

    await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancelled == [], "a reduce-only order is what containment wants kept"


@pytest.mark.asyncio
async def test_cancelling_happens_before_the_close(host) -> None:
    """Order matters: closing first leaves the same window, only shorter."""
    strategy, instance = await _deployed(host, "contain-order")
    node = strategy.node
    node.cache.positions.append(_VenuePosition())
    node.cache.orders.append(_VenueOrder(is_reduce_only=False, client_order_id="O-risk"))
    sequence: list[str] = []
    original_cancel = strategy.cancel_order
    original_close = strategy.close_all_positions_with_fallback

    def record_cancel(client_order_id):
        sequence.append("cancel")
        return original_cancel(client_order_id)

    def record_close(instrument_id):
        sequence.append("close")
        return original_close(instrument_id)

    strategy.cancel_order = record_cancel
    strategy.close_all_positions_with_fallback = record_close

    await host.flatten_positions(instance, "drawdown_breach")

    assert sequence[0] == "cancel", f"cancel must come first, got {sequence}"
    assert "close" in sequence


@pytest.mark.asyncio
async def test_containment_still_closes_the_position(host) -> None:
    """The regression: cancelling is added to the flatten, it does not replace it."""
    strategy, instance = await _deployed(host, "contain-still-closes")
    node = strategy.node
    node.cache.positions.append(_VenuePosition())

    await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.closed, "the position itself must still be closed"


@pytest.mark.asyncio
async def test_an_unknown_instance_is_still_a_logged_no_op(host) -> None:
    await host.flatten_positions("00000000-0000-4000-8000-000000000000", "drawdown_breach")


@pytest.mark.asyncio
async def test_an_unreadable_order_list_does_not_stop_the_close(host) -> None:
    """Containment's first duty is the position; the order sweep must not block it.

    Adding the sweep introduced a way for the whole flatten to fail on the part meant
    to make it more thorough. This is the test that says it does not.
    """
    from structlog.testing import capture_logs

    strategy, instance = await _deployed(host, "contain-unreadable")
    node = strategy.node
    node.cache.positions.append(_VenuePosition())

    def refuse():
        raise RuntimeError("venue cache is not answering")

    node.cache.orders_open = refuse

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.closed, "the position must still be closed"
    assert any(event["event"] == "nt_containment_orders_unreadable" for event in events), (
        "and it must not be silent about what it could not do"
    )


@dataclass
class _SpotPosition:
    """A long the venue is slow to close: the close is asked for, the position stays."""

    quantity: str = "0.0070"
    instrument_id: str = "vBTC_vUSDC.SODEX_SPOT"
    strategy_id: str = "S-1"
    is_long: bool = True
    is_short: bool = False
    is_closed: bool = False


@dataclass
class _PlainOrder:
    """An exit as a spot venue takes it: a plain sell, with no reduce-only flag."""

    client_order_id: str
    quantity: str = "0.0070"
    side: str = "OrderSide.SELL"
    is_reduce_only: bool = False
    is_quote_quantity: bool = False
    instrument_id: str = "vBTC_vUSDC.SODEX_SPOT"
    strategy_id: str = "S-1"


class _SlowClose(_ShutdownAwareStrategy):
    def close_all_positions_with_fallback(self, instrument_id: str) -> None:
        self.closed.append(instrument_id)


async def _holding_long(host: NtTradingNodeHost, label: str, *orders: _PlainOrder, **spec):
    strategy = _SlowClose()
    deployment = _spec(label, trading_mode="testnet", **spec)
    await host.deploy(deployment, _credential(), _Artifact(strategy=strategy))
    instance = deployment["deployment_instance_id"]
    strategy.node = host._active_nodes[instance].node  # noqa: SLF001 - fixture wiring
    strategy.node.cache.positions.append(_SpotPosition())
    strategy.node.cache.orders.extend(orders)
    return strategy, instance


@pytest.mark.asyncio
async def test_a_plain_exit_within_the_position_survives_containment(host) -> None:
    """Judged by what the order does to the position, not by a flag spot venues refuse.

    A spot exit carries no reduce-only flag, because the venue rejects one. Judged by
    the flag it was a risk-increasing order, and containment withdrew the exit it was
    there to complete.
    """
    host._containment_confirm_secs = 0.05  # noqa: SLF001 - fixture wiring
    strategy, instance = await _holding_long(host, "contain-plain-exit", _PlainOrder("exit"))

    await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancelled == []


@pytest.mark.asyncio
async def test_a_plain_sell_larger_than_the_position_is_still_cancelled(host) -> None:
    """Past the position it opens the other side, so it is risk-increasing."""
    host._containment_confirm_secs = 0.05  # noqa: SLF001 - fixture wiring
    strategy, instance = await _holding_long(
        host, "contain-oversize", _PlainOrder("reverse", quantity="0.0071")
    )

    await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancelled == ["reverse"]


@pytest.mark.asyncio
async def test_two_full_size_exits_leave_one_standing(host) -> None:
    """Each judged against what the others have not already claimed.

    Judged one at a time against the whole position, both read as reducing and both
    stay -- and if both fill, the second opens a short of the same size.
    """
    host._containment_confirm_secs = 0.05  # noqa: SLF001 - fixture wiring
    strategy, instance = await _holding_long(
        host, "contain-two-exits", _PlainOrder("first"), _PlainOrder("second")
    )

    await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancelled == ["second"]


@pytest.mark.asyncio
async def test_a_preserve_stop_keeps_a_plain_exit_within_the_position(host) -> None:
    host._shutdown_poll_secs = 0  # noqa: SLF001 - fixture wiring
    strategy, instance = await _holding_long(
        host,
        "preserve-plain-exit",
        _PlainOrder("exit"),
        _PlainOrder("reverse", quantity="0.0071"),
        shutdown_policy={
            "schema_version": 1,
            "position_policy": "preserve",
            "confirmation_timeout_secs": 5,
        },
    )

    await host.stop(instance)

    assert strategy.cancelled == ["reverse"]
    assert strategy.prepared == ["preserve"]
    assert not host.attached(instance)


@pytest.mark.asyncio
async def test_an_order_that_cannot_be_judged_is_treated_as_risk_increasing(host) -> None:
    """Containment cancels what it cannot show to be reducing, and says it could not."""
    from structlog.testing import capture_logs

    host._containment_confirm_secs = 0.05  # noqa: SLF001 - fixture wiring
    strategy, instance = await _holding_long(
        host, "contain-unjudged", _PlainOrder("odd", quantity="not a number")
    )

    with capture_logs() as events:
        await host.flatten_positions(instance, "drawdown_breach")

    assert strategy.cancelled == ["odd"]
    assert any(event["event"] == "nt_order_risk_unjudged" for event in events)
