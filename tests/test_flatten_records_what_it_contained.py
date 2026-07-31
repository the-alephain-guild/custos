"""A flatten that had nothing to act on must not be recorded as containment.

Real evidence, 2026-07-30: the fallback breaker fail-closed at startup and the flatten
ran while the portfolio still reported zero instruments; the account's existing short
arrived through reconciliation 2.5 seconds later and was never flattened. The record
said ``positions_flattened``, which reads as though the exposure had been dealt with.

C9 already asks for the opposite -- "the record must say containment was not confirmed,
not write it as though it was contained" -- and the lane's timeout path already honours
that with ``offline_exposure_containment_unconfirmed``. This is the same discipline on
the path that reaches the engine and finds nothing.
"""

from __future__ import annotations

from structlog.testing import capture_logs

from custos.engines.nautilus.host import NtTradingNodeHost


class _Cache:
    def __init__(self, positions: list) -> None:
        self._positions = positions

    def positions_open(self):
        return self._positions


class _Strategy:
    def __init__(self) -> None:
        self.closed: list = []

    def close_all_positions(self, instrument_id) -> None:
        self.closed.append(instrument_id)


class _Trader:
    def __init__(self, strategies: list) -> None:
        self._strategies = strategies

    def strategies(self):
        return self._strategies


class _Node:
    def __init__(self, positions: list, strategy: _Strategy) -> None:
        self.kernel = type(
            "Kernel",
            (),
            {"cache": _Cache(positions), "trader": _Trader([strategy])},
        )()


class _Position:
    def __init__(self, instrument_id: str) -> None:
        self.instrument_id = instrument_id


def _host_with(node: _Node) -> NtTradingNodeHost:
    host = NtTradingNodeHost(tenant_id="tenant", runner_id="runner")
    host._active_nodes["instance"] = (node, None)
    return host


async def test_a_flatten_with_nothing_open_is_recorded_as_unconfirmed() -> None:
    """Zero instruments means nothing was contained, and the record has to say so."""
    strategy = _Strategy()
    host = _host_with(_Node([], strategy))

    with capture_logs() as logs:
        await host.flatten_positions("instance", "portfolio_equity_ambiguous")

    events = [entry["event"] for entry in logs]
    assert "nt_flatten_containment_unconfirmed" in events
    assert "positions_flattened" not in events, (
        "an empty flatten must not be recorded with the event that means it contained something"
    )
    assert strategy.closed == [], "there was nothing to close"

    unconfirmed = next(e for e in logs if e["event"] == "nt_flatten_containment_unconfirmed")
    assert unconfirmed["reason"] == "portfolio_equity_ambiguous"


async def test_a_flatten_that_closed_something_still_says_so() -> None:
    """The honest record cuts both ways: real containment keeps its own event."""
    strategy = _Strategy()
    host = _host_with(_Node([_Position("BTCUSDT-PERP.BINANCE")], strategy))

    with capture_logs() as logs:
        await host.flatten_positions("instance", "max_notional_exceeded")

    events = [entry["event"] for entry in logs]
    assert "positions_flattened" in events
    assert "nt_flatten_containment_unconfirmed" not in events
    assert strategy.closed == ["BTCUSDT-PERP.BINANCE"]

    flattened = next(e for e in logs if e["event"] == "positions_flattened")
    assert flattened["instrument_count"] == 1
