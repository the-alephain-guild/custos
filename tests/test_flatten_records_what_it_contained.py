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


# ---------------------------------------------------------------------------
# Which close the flatten uses.
#
# Containment goes through NT's own ``close_all_positions``, whose ``reduce_only``
# defaults to True -- so on a venue refusing that form (Binance's demo engine does, see
# plan 28) the breaker's flatten cannot close anything, at the moment it most needs to.
# Toolkit strategies carry a close path that drops reduce-only on evidence of exactly
# that refusal, and the flatten should use it when the strategy has one.
#
# The preference is duck-typed rather than an isinstance check: this host runs whatever
# strategy the deployment names, the toolkit is not a requirement, and a strategy without
# the method must keep working unchanged.
# ---------------------------------------------------------------------------


class _ToolkitStrategy(_Strategy):
    def __init__(self) -> None:
        super().__init__()
        self.closed_with_fallback: list = []

    def close_all_positions_with_fallback(self, instrument_id) -> None:
        self.closed_with_fallback.append(instrument_id)


async def test_the_flatten_uses_the_toolkit_close_path_when_there_is_one() -> None:
    strategy = _ToolkitStrategy()
    host = _host_with(_Node([_Position("BTCUSDT-PERP.BINANCE")], strategy))

    await host.flatten_positions("instance", "max_notional_exceeded")

    assert strategy.closed_with_fallback == ["BTCUSDT-PERP.BINANCE"]
    assert strategy.closed == [], (
        "the plain reduce-only-only close must not also run -- two closes is one too many"
    )


async def test_a_strategy_without_the_toolkit_close_path_is_unaffected() -> None:
    strategy = _Strategy()
    host = _host_with(_Node([_Position("BTCUSDT-PERP.BINANCE")], strategy))

    await host.flatten_positions("instance", "max_notional_exceeded")

    assert strategy.closed == ["BTCUSDT-PERP.BINANCE"]
