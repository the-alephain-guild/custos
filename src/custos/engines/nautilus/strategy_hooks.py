"""Installing host-owned behaviour onto a strategy instance, verifiably.

Two things the runner needs are only reachable through the strategy in
NautilusTrader 2.0: execution events, which arrive at its typed callbacks and
nowhere else, and outbound orders, which leave through its submit methods and
have no client-side seat to sit in any more.

Both are installed the same way and for the same reason. The strategy arrives
from a signed artifact and satisfies no base class -- there is no ``issubclass``
check anywhere in this runner -- so behaviour that lived in a toolkit base class
would silently not exist for an artifact that did not inherit it. The host
therefore installs it on the instance it is about to hand to the node, and
verifies afterwards that it is reachable rather than trusting the assignment: a
strategy with ``__slots__``, a custom ``__setattr__``, or a property of that name
would each leave it installed in name only.

A strategy that cannot carry a hook is refused before the node is built.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["StrategyHookUnsupported", "install_hook"]


class StrategyHookUnsupported(RuntimeError):
    """The strategy cannot carry a hook the runner requires."""


def install_hook(
    strategy: Any,
    method_name: str,
    wrap: Callable[[Callable[..., Any]], Callable[..., Any]],
) -> Callable[..., Any]:
    """Replace one of the strategy's methods with a host-owned wrapper.

    ``wrap`` receives the method as it is now and returns the replacement, so a
    caller decides what happens around the original without having to repeat the
    lookup, the assignment or the verification.

    Returns the installed wrapper, which lets a caller assert identity later.
    """
    original = getattr(strategy, method_name, None)
    if not callable(original):
        raise StrategyHookUnsupported(
            f"strategy {type(strategy).__name__} has no callable {method_name!r}; "
            "the runner cannot install its own behaviour on it"
        )

    wrapper = wrap(original)
    try:
        setattr(strategy, method_name, wrapper)
    except (AttributeError, TypeError) as exc:
        raise StrategyHookUnsupported(
            f"strategy {type(strategy).__name__} does not accept a runner hook on {method_name!r}"
        ) from exc
    if getattr(strategy, method_name, None) is not wrapper:
        raise StrategyHookUnsupported(
            f"the runner hook on {method_name!r} did not take effect for strategy "
            f"{type(strategy).__name__}"
        )
    return wrapper
