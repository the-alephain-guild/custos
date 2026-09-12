"""The runner's event forwarding is installed by the host, not by the strategy.

2.0 delivers order and position events only to the strategy's typed callbacks, and
the strategy arrives from a signed artifact with no base class to rely on. So the
host installs the forwarding on the instance and refuses a strategy that cannot
carry it -- these tests are the contract for both halves.

Failure-mode contract:
- strategy without the callback            -> refused, no node is built
- strategy that will not accept the wrapper -> refused (__slots__, read-only attr)
- a wrapper that does not take effect       -> refused (the verification is live)
- a sink that raises                        -> other sinks and the strategy still run,
                                               the host is told, and the log carries no
                                               exception text (red line 0.1)
"""

from __future__ import annotations

import pytest
import structlog

from custos.engines.nautilus.strategy_event_forwarding import (
    ORDER_EVENT_CALLBACK,
    POSITION_EVENT_CALLBACK,
    StrategyEventForwarder,
)
from custos.engines.nautilus.strategy_hooks import StrategyHookUnsupported

_INSTANCE = "11111111-1111-4111-8111-111111111111"


class _Strategy:
    """Stands in for an artifact's strategy: the two callbacks, recording calls.

    ``record_into`` lets a test share one list with the sinks, which is the only way
    to assert that the sinks ran first rather than merely that both ran.
    """

    def __init__(self, record_into: list[str] | None = None) -> None:
        self.seen: list[tuple[str, object]] = []
        self._record_into = record_into

    def _note(self, kind: str) -> None:
        if self._record_into is not None:
            self._record_into.append(f"strategy:{kind}")

    def on_order_event(self, event: object) -> None:
        self._note("order")
        self.seen.append(("order", event))

    def on_position_event(self, event: object) -> None:
        self._note("position")
        self.seen.append(("position", event))


class _OrderEvent:
    pass


class _PositionEvent:
    pass


def _forwarder(failures: list[tuple[str, str]] | None = None) -> StrategyEventForwarder:
    recorded = failures if failures is not None else []
    return StrategyEventForwarder(
        deployment_instance_id=_INSTANCE,
        on_sink_failure=lambda sink, reason: recorded.append((sink, reason)),
    )


def test_an_order_event_reaches_every_sink_before_the_strategy() -> None:
    # One list for both, so the ordering between them is what is asserted rather
    # than each side's contents in isolation.
    order: list[str] = []
    strategy = _Strategy(record_into=order)
    forwarder = _forwarder()
    forwarder.add_order_sink("reservations", lambda _event: order.append("reservations"))
    forwarder.add_order_sink("facts", lambda _event: order.append("facts"))
    forwarder.install(strategy)

    event = _OrderEvent()
    strategy.on_order_event(event)

    # Sinks first, and in registration order: a reservation has to be released and
    # the fact recorded before the strategy can act on the event and submit again.
    assert order == ["reservations", "facts", "strategy:order"]
    assert strategy.seen == [("order", event)]


def test_a_position_event_reaches_its_own_sinks() -> None:
    seen: list[object] = []
    strategy = _Strategy()
    forwarder = _forwarder()
    forwarder.add_position_sink("facts", seen.append)
    forwarder.install(strategy)

    event = _PositionEvent()
    strategy.on_position_event(event)

    assert seen == [event]
    assert strategy.seen == [("position", event)]


def test_order_and_position_sinks_do_not_cross() -> None:
    orders: list[object] = []
    positions: list[object] = []
    strategy = _Strategy()
    forwarder = _forwarder()
    forwarder.add_order_sink("orders", orders.append)
    forwarder.add_position_sink("positions", positions.append)
    forwarder.install(strategy)

    strategy.on_order_event(_OrderEvent())

    assert len(orders) == 1
    assert positions == []


def test_a_strategy_without_the_callback_is_refused() -> None:
    class _NoCallbacks:
        pass

    with pytest.raises(StrategyHookUnsupported, match=ORDER_EVENT_CALLBACK):
        _forwarder().install(_NoCallbacks())


def test_a_strategy_missing_only_the_position_callback_is_refused() -> None:
    class _OrdersOnly:
        def on_order_event(self, event: object) -> None: ...

    with pytest.raises(StrategyHookUnsupported, match=POSITION_EVENT_CALLBACK):
        _forwarder().install(_OrdersOnly())


def test_a_strategy_that_will_not_accept_the_wrapper_is_refused() -> None:
    """__slots__ leaves nowhere to put the wrapper, and setattr says so."""

    class _Slotted:
        __slots__ = ()

        def on_order_event(self, event: object) -> None: ...
        def on_position_event(self, event: object) -> None: ...

    with pytest.raises(StrategyHookUnsupported, match="does not accept"):
        _forwarder().install(_Slotted())


def test_a_wrapper_that_does_not_take_effect_is_refused() -> None:
    """The verification after the assignment is a live guard, not a formality.

    A strategy whose __setattr__ swallows the assignment would otherwise be
    accepted with no forwarding on it at all -- which is the exact failure the
    host-installed design exists to prevent.
    """

    class _SwallowsAssignment:
        def __setattr__(self, name: str, value: object) -> None:
            pass

        def on_order_event(self, event: object) -> None: ...
        def on_position_event(self, event: object) -> None: ...

    with pytest.raises(StrategyHookUnsupported, match="did not take effect"):
        _forwarder().install(_SwallowsAssignment())


def test_a_failing_sink_does_not_stop_the_others_or_the_strategy() -> None:
    reached: list[str] = []
    failures: list[tuple[str, str]] = []
    strategy = _Strategy()
    forwarder = _forwarder(failures)

    def _explode(_event: object) -> None:
        raise ValueError("sink is broken")

    forwarder.add_order_sink("reservations", _explode)
    forwarder.add_order_sink("facts", lambda _event: reached.append("facts"))
    forwarder.install(strategy)

    strategy.on_order_event(_OrderEvent())

    assert reached == ["facts"]
    assert len(strategy.seen) == 1
    assert failures == [("reservations", "reservations:ValueError")]


def test_a_failing_sink_is_logged_without_its_exception_text() -> None:
    """Red line 0.1: an adapter exception can carry credential material.

    The rust dispatch discards what this callback raises, so the log is the only
    place the failure appears -- which makes it the one place a raw key could reach
    a log through this path.
    """
    strategy = _Strategy()
    forwarder = _forwarder()

    def _explode(_event: object) -> None:
        raise ValueError("api_key=super-secret-material")

    forwarder.add_order_sink("facts", _explode)
    forwarder.install(strategy)

    with structlog.testing.capture_logs() as logs:
        strategy.on_order_event(_OrderEvent())

    failures = [entry for entry in logs if entry["event"] == "runner_event_forwarding_failed"]
    assert len(failures) == 1
    assert failures[0]["sink"] == "facts"
    assert failures[0]["error_type"] == "ValueError"
    assert failures[0]["deployment_instance_id"] == _INSTANCE
    assert "super-secret-material" not in repr(failures[0])


def test_installation_is_announced_with_the_sinks_it_wired() -> None:
    strategy = _Strategy()
    forwarder = _forwarder()
    forwarder.add_order_sink("reservations", lambda _event: None)
    forwarder.add_position_sink("facts", lambda _event: None)

    with structlog.testing.capture_logs() as logs:
        forwarder.install(strategy)

    installed = [entry for entry in logs if entry["event"] == "runner_event_forwarding_installed"]
    assert len(installed) == 1
    assert installed[0]["order_sinks"] == ["reservations"]
    assert installed[0]["position_sinks"] == ["facts"]


def test_a_forwarder_with_no_sinks_still_leaves_the_strategy_working() -> None:
    """The host wires nothing when neither bridge is configured, and the strategy
    must behave exactly as it would have without the host in the way."""
    strategy = _Strategy()
    forwarder = _forwarder()
    forwarder.install(strategy)

    event = _OrderEvent()
    strategy.on_order_event(event)

    assert forwarder.sink_count == 0
    assert strategy.seen == [("order", event)]
