"""The sandbox host's close behaviour is unchanged by the reduce-only escape hatch.

``SandboxSimulationHost`` is an explicit local-simulation boundary: it settles nothing at
a venue, holds no positions, and so can never produce the rejection the hatch keys on.
Its flatten is a logged no-op and must stay one.

This is a no-regression pin, not "sandbox falls back too". It exists because the boundary
is the kind of thing a later change tidies away — someone unifying the two hosts would
make sandbox reach for a strategy and close positions it does not have, and every
sandbox test would still pass while the simulation quietly stopped being a simulation.
Plans 26 and 27 asked for the same pin for the same reason.
"""

from __future__ import annotations

from decimal import Decimal

from structlog.testing import capture_logs

from custos.engines.nautilus.host import SandboxSimulationHost


async def test_the_sandbox_flatten_is_a_logged_no_op() -> None:
    host = SandboxSimulationHost()

    with capture_logs() as logs:
        await host.flatten_positions("instance", "max_notional_exceeded")

    events = [entry["event"] for entry in logs]
    assert events == ["sandbox_simulation_positions_flattened"], (
        "the sandbox flatten records the breaker's trip and does nothing else"
    )
    assert logs[0]["reason"] == "max_notional_exceeded"


async def test_the_sandbox_host_holds_nothing_to_close() -> None:
    """The premise of the no-op: there is no position for any close path to act on."""
    host = SandboxSimulationHost()

    assert await host.get_positions("instance") == []
    assert await host.get_orders("instance") == []
    assert await host.get_open_notional("instance") == Decimal("0")


def test_the_sandbox_host_does_not_carry_the_escape_hatch() -> None:
    """It has no venue to be refused by, so it must not grow the strategy-side close path.

    If this ever fails, the question to ask is not "add the method" but "why is the
    simulation boundary being asked to close a real position".
    """
    assert not hasattr(SandboxSimulationHost, "close_all_positions_with_fallback")
