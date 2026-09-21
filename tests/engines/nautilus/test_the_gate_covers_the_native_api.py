"""The execution gate has to cover the venue-reaching API that actually exists.

``install_order_gate`` says it puts the gate "in front of every way this strategy
can reach the venue". That is red line 0.2, and it was not true: ``modify_orders``
-- batch modify -- builds its command natively and never passes the ``modify_order``
wrapper, so a frozen breaker could still watch a resting order's quantity go from 1
to 200.

Two further defects live in the same place, and both were hidden by doubles that
were more forgiving than NautilusTrader: the notional calculation hands a Decimal to
``instrument.notional_value``, which requires a native ``Price``; and the wrappers
read ``.client_order_id`` off their first argument, while 2.0's ``modify_order`` and
``cancel_order`` are given a ``ClientOrderId`` to begin with.

The coverage check here is derived from the live Strategy class rather than written
down, so a new outbound method in a future NautilusTrader turns it red instead of
passing quietly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    install_order_gate,
)

nautilus_trading = pytest.importorskip("nautilus_trader.trading")

# Outbound actions the gate deliberately does not stand in front of, each with the
# reason. Anything the enumeration finds that is neither wrapped nor listed here is
# a hole, and the test says so.
_DELIBERATELY_UNGATED = {
    # Cancelling removes working orders; it cannot add exposure, and refusing it
    # could strand a position with orders the strategy is trying to withdraw.
    "cancel_order": "cancelling cannot increase exposure",
    "cancel_orders": "cancelling cannot increase exposure",
    "cancel_all_orders": "cancelling cannot increase exposure",
    "cancel_gtd_expiry": "withdraws a local expiry timer, never reaches the venue",
    # Queries, not actions.
    "query_order": "read-only",
    "query_account": "read-only",
}


def _native_outbound_actions() -> set[str]:
    """Every public Strategy method that can put an instruction on the wire.

    Derived from the class, not from a list someone maintained: a hardcoded list can
    only prove that somebody once wrote it down, and it goes stale in exactly the
    way this finding did.
    """
    strategy = nautilus_trading.Strategy
    verbs = ("submit_", "modify_", "cancel_", "close_", "market_exit", "query_")
    found = set()
    for name in dir(strategy):
        if name.startswith("_") or name.startswith("on_") or name.startswith("post_"):
            # ``on_*`` and ``post_*`` are callbacks NautilusTrader makes into the
            # strategy -- post_market_exit is fired by finalize_market_exit, it is
            # not a way out.
            continue
        if any(name.startswith(verb) or name == verb for verb in verbs):
            found.add(name)
    return found


def test_the_enumeration_finds_the_methods_we_know_about() -> None:
    """A control: if the derivation broke, every other check here would pass vacuously."""
    found = _native_outbound_actions()

    for expected in ("submit_order", "modify_order", "modify_orders", "market_exit"):
        assert expected in found, f"{expected} missing: the derivation is not working"


def test_every_native_outbound_action_is_gated_or_declared() -> None:
    from custos.engines.nautilus import runner_safety

    gated = {
        runner_safety.SUBMIT_ORDER,
        runner_safety.SUBMIT_ORDER_LIST,
        runner_safety.MODIFY_ORDER,
        runner_safety.MODIFY_ORDERS,
        runner_safety.MARKET_EXIT,
        runner_safety.CLOSE_POSITION,
        runner_safety.CLOSE_ALL_POSITIONS,
    }

    uncovered = _native_outbound_actions() - gated - set(_DELIBERATELY_UNGATED)

    assert not uncovered, (
        f"these reach the venue without passing the gate: {sorted(uncovered)}. "
        "Wrap them, or add them to _DELIBERATELY_UNGATED with the reason."
    )


def test_the_declared_coverage_is_what_actually_gets_installed() -> None:
    """The constants are a claim; this checks the installer acts on all of them.

    Without it the enumeration above could stay green while a method was quietly
    dropped from the install list -- the constant would still be there.
    """
    from custos.engines.nautilus import runner_safety
    from tests.engines.nautilus.test_runner_safety_execution_boundary import (
        _boundary,
        _gate,
        _GatedStrategy,
        _Store,
    )

    declared = {
        runner_safety.SUBMIT_ORDER,
        runner_safety.SUBMIT_ORDER_LIST,
        runner_safety.MODIFY_ORDER,
        runner_safety.MODIFY_ORDERS,
        runner_safety.MARKET_EXIT,
        runner_safety.CLOSE_POSITION,
        runner_safety.CLOSE_ALL_POSITIONS,
    }
    strategy = _GatedStrategy()
    # install_hook does setattr on the instance, so a hooked method shows up in
    # __dict__ while an unhooked one is still found on the class. Comparing the bound
    # methods would prove nothing: getattr builds a fresh one every time, so `is` is
    # False whether or not anything was installed.
    assert not set(vars(strategy)) & declared, "precondition: nothing is hooked yet"

    install_order_gate(strategy, _gate(_boundary(_Store([]))))

    unhooked = declared - set(vars(strategy))
    assert not unhooked, f"declared as gated but never hooked: {sorted(unhooked)}"


def test_batch_modify_is_refused_rather_than_silently_allowed() -> None:
    """Until multi-leg reservations are atomic, the honest answer is no."""
    from tests.engines.nautilus.test_runner_safety_execution_boundary import (
        _boundary,
        _gate,
        _GatedStrategy,
        _Store,
    )

    strategy = _GatedStrategy()
    install_order_gate(strategy, _gate(_boundary(_Store([]))))

    result = strategy.modify_orders([("order-1", 200)])

    assert result is None
    assert strategy.submitted == [], "a batch modify must not reach the venue"


# --------------------------------------------------------------- native arguments


def _instrument():
    from nautilus_trader.testkit.providers import TestInstrumentProvider

    return TestInstrumentProvider.btcusdt_perp_binance()


def test_a_real_instrument_rejects_a_decimal_price() -> None:
    """The fact that made this defect invisible: our doubles were more forgiving."""
    instrument = _instrument()

    assert instrument.notional_value(
        instrument.make_qty(1), instrument.make_price(100)
    ).as_decimal() == Decimal("100")
    with pytest.raises(TypeError, match="Price"):
        instrument.notional_value(instrument.make_qty(1), Decimal("100"))


def test_a_real_limit_order_can_be_valued() -> None:
    """order_notional against a real instrument and a real order, not a stand-in."""
    from nautilus_trader.model import LimitOrder, OrderSide

    instrument = _instrument()

    class _Cache:
        @staticmethod
        def instrument(_instrument_id):
            return instrument

    order = _limit_order(instrument, LimitOrder, OrderSide.BUY, "1", "100")

    notional = NautilusCachedOrderSemantics(_Cache()).order_notional(order)

    assert notional == Decimal("100")


def _limit_order(instrument, limit_order_cls, side, quantity: str, price: str):
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import ClientOrderId, StrategyId, TimeInForce, TraderId

    return limit_order_cls(
        trader_id=TraderId("TESTER-000"),
        strategy_id=StrategyId("S-001"),
        instrument_id=instrument.id,
        client_order_id=ClientOrderId("O-1"),
        order_side=side,
        quantity=instrument.make_qty(Decimal(quantity)),
        price=instrument.make_price(Decimal(price)),
        init_id=UUID4(),
        ts_init=0,
        time_in_force=TimeInForce.GTC,
        post_only=False,
        reduce_only=False,
        quote_quantity=False,
    )


def test_the_modify_wrapper_accepts_a_client_order_id() -> None:
    """2.0 hands modify_order a ClientOrderId; the wrapper used to want an Order."""
    from nautilus_trader.model import ClientOrderId

    from tests.engines.nautilus.test_runner_safety_execution_boundary import (
        _boundary,
        _gate,
        _GatedStrategy,
        _order,
        _Store,
    )

    strategy = _GatedStrategy()
    boundary = _boundary(_Store([]))
    resting = _order("order-1", reduce_only=True)
    boundary._semantics.cached_orders["order-1"] = resting  # noqa: SLF001 - harness wiring
    install_order_gate(strategy, _gate(boundary))

    client_order_id = ClientOrderId("order-1")
    strategy.modify_order(client_order_id, 1)

    assert strategy.submitted == [client_order_id], (
        "the modification must reach the venue, identified the way 2.0 identifies it"
    )
