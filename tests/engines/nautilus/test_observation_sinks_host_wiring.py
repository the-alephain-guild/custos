"""The host hands every order and position event to observation sinks, when given some.

The offline lane publishes fills and closed positions from these for the
operator's own tools. They are registered after the safety and fact sinks and
receive the instance an event belongs to, since one host runs several.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nautilus_trader")

from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402


class _Strategy:
    def on_order_event(self, event) -> None: ...

    def on_position_event(self, event) -> None: ...


def test_without_observation_sinks_nothing_extra_is_forwarded() -> None:
    host = NtTradingNodeHost()
    strategy = _Strategy()

    host._attach_runtime_bridges("instance-1", strategy, object(), None, None)
    strategy.on_order_event("filled")


def test_observation_sinks_receive_each_event_with_its_instance() -> None:
    host = NtTradingNodeHost()
    seen: list[tuple[str, str, object]] = []
    host.add_observation_sinks(
        order=lambda instance, event: seen.append(("order", instance, event)),
        position=lambda instance, event: seen.append(("position", instance, event)),
    )
    first, second = _Strategy(), _Strategy()

    host._attach_runtime_bridges("instance-1", first, object(), None, None)
    host._attach_runtime_bridges("instance-2", second, object(), None, None)
    first.on_order_event("filled")
    second.on_position_event("closed")

    assert seen == [("order", "instance-1", "filled"), ("position", "instance-2", "closed")]
