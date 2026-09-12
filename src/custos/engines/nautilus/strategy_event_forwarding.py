"""Host-installed forwarding from a strategy's typed callbacks to the runner's sinks.

NautilusTrader 2.0 removed python access to the internal message bus. Order and
position events reach python only through ``Strategy.on_order_event`` and
``Strategy.on_position_event``, so both runner bridges that used to subscribe to
the bus -- signed RunnerFact emission and the order reservation boundary -- now
hang off those callbacks.

The strategy comes from a signed artifact and satisfies no base class (there is
no ``issubclass`` check anywhere in this runner), so a bridge that lived in a
toolkit base class would silently stop existing the first time an artifact did
not inherit it. Forwarding is therefore installed here, by the host, on the
instance it is about to hand to the node, and a strategy that cannot carry it is
refused before the node ever sees it.

Two properties of the 2.0 dispatch shape what this module does:

- The rust side discards whatever a python callback raises. Nothing upstream
  will notice a sink that failed, so a failure has to be turned into a signal
  here -- otherwise "the reconciliation is never silent" becomes silent by
  construction.
- Dispatch is gated on the strategy being ``Running``. Events that arrive after
  it stops are logged by nautilus but not delivered, which is a narrower window
  than the 1.x subscription had. Nothing here can widen it; it is recorded in
  the plan and in the red-line table rather than papered over.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from custos.core.log import get_logger
from custos.engines.nautilus.strategy_hooks import install_hook

__all__ = [
    "ORDER_EVENT_CALLBACK",
    "POSITION_EVENT_CALLBACK",
    "StrategyEventForwarder",
]

_log = get_logger("custos.nautilus_host.event_forwarding")

# The 2.0 callbacks every order / position event passes through. Nautilus calls
# the specific handler first (on_order_filled and friends) and then these two, so
# subscribing here is equivalent to the 1.x wildcard topics the bridges used.
ORDER_EVENT_CALLBACK = "on_order_event"
POSITION_EVENT_CALLBACK = "on_position_event"


class StrategyEventForwarder:
    """Runs the runner's sinks on every order / position event a strategy receives.

    Sinks run before the strategy's own handler: a released reservation and a
    recorded fact both have to be true of the event before the strategy can act on
    it and submit the next order.
    """

    def __init__(
        self,
        *,
        deployment_instance_id: str,
        on_sink_failure: Callable[[str, str], None],
    ) -> None:
        self._deployment_instance_id = deployment_instance_id
        self._on_sink_failure = on_sink_failure
        self._order_sinks: list[tuple[str, Callable[[Any], None]]] = []
        self._position_sinks: list[tuple[str, Callable[[Any], None]]] = []

    def add_order_sink(self, name: str, sink: Callable[[Any], None]) -> None:
        self._order_sinks.append((name, sink))

    def add_position_sink(self, name: str, sink: Callable[[Any], None]) -> None:
        self._position_sinks.append((name, sink))

    @property
    def sink_count(self) -> int:
        return len(self._order_sinks) + len(self._position_sinks)

    def install(self, strategy: Any) -> None:
        """Wrap the strategy's typed callbacks, or refuse the strategy."""
        self._install_one(strategy, ORDER_EVENT_CALLBACK, self._order_sinks)
        self._install_one(strategy, POSITION_EVENT_CALLBACK, self._position_sinks)
        _log.info(
            "runner_event_forwarding_installed",
            deployment_instance_id=self._deployment_instance_id,
            strategy=type(strategy).__name__,
            order_sinks=[name for name, _ in self._order_sinks],
            position_sinks=[name for name, _ in self._position_sinks],
        )

    def _install_one(
        self,
        strategy: Any,
        callback_name: str,
        sinks: list[tuple[str, Callable[[Any], None]]],
    ) -> None:
        def wrap(handler: Callable[[Any], None]) -> Callable[[Any], None]:
            def forwarding(event: Any) -> None:
                for sink_name, sink in sinks:
                    try:
                        sink(event)
                    except Exception as exc:  # noqa: BLE001 - one sink must not silence others
                        self._record_sink_failure(sink_name, callback_name, event, exc)
                handler(event)

            return forwarding

        install_hook(strategy, callback_name, wrap)

    def _record_sink_failure(
        self,
        sink_name: str,
        callback_name: str,
        event: Any,
        exc: Exception,
    ) -> None:
        reason = f"{sink_name}:{type(exc).__name__}"
        _log.error(
            "runner_event_forwarding_failed",
            deployment_instance_id=self._deployment_instance_id,
            sink=sink_name,
            callback=callback_name,
            event_type=type(event).__name__,
            error_type=type(exc).__name__,
        )
        # The rust dispatch discards what this callback raises, so re-raising would
        # lose the failure entirely. Hand it to the host instead, which degrades the
        # deployment's reported status until someone looks.
        self._on_sink_failure(sink_name, reason)
