"""On a cash account every close and protective order goes out without reduce_only.

Reduce-only exists for one hazard: a close re-sent while the cache still lags a fill
that already closed the position must be refused instead of opening the reverse
position. A cash account cannot sell what it does not hold, so that hazard does not
exist there -- and spot adapters refuse the flag outright. SoDEX spot denies every
reduce-only order locally ("reduce-only on spot is not supported by SoDEX"), and the
Binance spot adapter does the same (``UnsupportedReduceOnly``). Each refused close was
re-sent after the in-flight window, which is how one probe run on the SoDEX spot
testnet turned into about fifteen thousand cancel attempts.

On a margin account nothing changes: reduce-only stays the protective default and the
escape hatch keeps its one plain attempt.

The account type is read from the cache at the moment the order is built. An account
that cannot be read keeps the protective form -- the worst a refused reduce-only order
can do is not close -- and leaves a warning behind.

Closes that leave the protective form must be submitted as orders rather than through
``close_position``: the runner's safety gate refuses a plain ``close_position`` and only
recognises a plain close that was submitted with a quantity within the position.
"""

from __future__ import annotations

import ast
import sys
import types
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

pytest.importorskip("nautilus_trader")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _strategy_harness import Harness, scaled_monitor  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import (  # noqa: E402
    SignalExecutionCoordinator,
)
from custos_toolkit_nautilus.adapter.execution import ExecutionManager  # noqa: E402
from custos_toolkit_nautilus.adapter.orders import OrderTracker  # noqa: E402
from custos_toolkit_nautilus.adapter.sltp_mode import SLTPMode  # noqa: E402
from custos_toolkit_nautilus.adapter.strategy_core import NautilusStrategyCore  # noqa: E402
from custos_toolkit_nautilus.adapter.tick_monitor import TickMonitorManager  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    Currency,
    CurrencyPair,
    InstrumentId,
    Money,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    Symbol,
    TimeInForce,
)

_ONE_SEC_NS = 1_000_000_000

_SPOT = CurrencyPair(
    InstrumentId.from_str("BTCUSDT.BINANCE"),
    Symbol("BTCUSDT"),
    Currency.from_str("BTC"),
    Currency.from_str("USDT"),
    2,
    5,
    Price.from_str("0.01"),
    Quantity.from_str("0.00001"),
    0,
    0,
)


class _Accounts:
    """The cache's account lookup: answers with one account, or with nothing.

    ``raises`` stands for a cache read that fails outright, which must be treated the
    same as an account that is not there.
    """

    def __init__(self, account_type: AccountType | None, *, raises: bool = False) -> None:
        self._account_type = account_type
        self._raises = raises
        self.asked: list[object] = []

    def __call__(self, venue: object) -> object | None:
        self.asked.append(venue)
        if self._raises:
            raise RuntimeError("account cache unavailable")
        if self._account_type is None:
            return None
        # More base than any position here, so a cash exit is never capped by it.
        held = lambda currency: Money(Decimal("1"), currency)  # noqa: E731
        return NS(
            account_type=self._account_type,
            id=f"{venue}-001",
            balance_free=held,
            balance_total=held,
        )


def _recording_factory(created: list[dict]) -> NS:
    """An order factory that keeps the arguments of every order it builds."""

    def build(order_type: OrderType):
        def make(**kwargs):
            created.append({"order_type": order_type, **kwargs})
            return NS(
                client_order_id=f"O-{len(created)}",
                order_type=order_type,
                quantity=kwargs.get("quantity"),
                side=kwargs.get("order_side"),
                is_reduce_only=kwargs.get("reduce_only", False),
                time_in_force=kwargs.get("time_in_force"),
                instrument_id=kwargs.get("instrument_id"),
                is_open=True,
                is_closed=False,
                ts_init=0,
                tags=kwargs.get("tags"),
            )

        return make

    return NS(
        market=build(OrderType.MARKET),
        limit=build(OrderType.LIMIT),
        stop_market=build(OrderType.STOP_MARKET),
        trailing_stop_market=build(OrderType.TRAILING_STOP_MARKET),
    )


# ---------------------------------------------------------------------------
# Bar exit: SignalExecutionCoordinator.execute_exit_for_pair
# ---------------------------------------------------------------------------


def _bar_exit_strategy(accounts: _Accounts, quantity: str = "0.00630") -> NS:
    created: list[dict] = []
    submitted: list = []
    position = NS(
        is_closed=False,
        is_long=True,
        is_short=False,
        quantity=Quantity.from_str(quantity),
        instrument_id=_SPOT.id,
    )
    strat = NS(
        now_ns=1_000,
        created=created,
        submitted=submitted,
        _position=position,
        _order_signal_map={},
        log=MagicMock(),
    )
    strat.cache = NS(
        positions_open=lambda instrument_id=None: [] if position.is_closed else [position],
        instrument=lambda _instrument_id: _SPOT,
        account_for_venue=accounts,
    )
    strat.clock = NS(timestamp_ns=lambda: strat.now_ns)
    strat.submit_order = lambda order: submitted.append(order)
    strat.order_factory = _recording_factory(created)
    return strat


def _bar_exit_ctx(strat: NS) -> NS:
    return NS(
        instrument_id=_SPOT.id,
        pair="BTC-USDT",
        order_tracker=OrderTracker(),
        execution_manager=ExecutionManager(strat.order_factory, strat.cache, strat.log),
        spot_reserved_remainder=None,
    )


def _exit_long() -> Signal:
    return Signal.exit_long(Decimal("95569"), pair="BTC-USDT")


def test_a_cash_account_exit_carries_no_reduce_only() -> None:
    accounts = _Accounts(AccountType.CASH)
    strat = _bar_exit_strategy(accounts)
    ctx = _bar_exit_ctx(strat)

    SignalExecutionCoordinator(strat).execute_exit_for_pair(ctx, _exit_long(), bar=object())

    assert len(strat.created) == 1, "one exit order must be built"
    order = strat.created[0]
    assert order["order_type"] is OrderType.MARKET
    assert order["time_in_force"] is TimeInForce.IOC
    assert order["order_side"] is OrderSide.SELL
    assert order["quantity"] == Quantity.from_str("0.00630"), "the size comes from the position"
    assert order["reduce_only"] is False, "a spot venue refuses reduce-only on a cash account"
    assert len(strat.submitted) == 1
    assert accounts.asked, "the decision must come from the account, not from the venue name"


def test_a_margin_account_exit_keeps_reduce_only() -> None:
    strat = _bar_exit_strategy(_Accounts(AccountType.MARGIN))
    ctx = _bar_exit_ctx(strat)

    SignalExecutionCoordinator(strat).execute_exit_for_pair(ctx, _exit_long(), bar=object())

    assert [order["reduce_only"] for order in strat.created] == [True]


def test_a_margin_account_keeps_its_one_plain_attempt() -> None:
    """The escape hatch on a margin account behaves exactly as before."""
    strat = _bar_exit_strategy(_Accounts(AccountType.MARGIN))
    ctx = _bar_exit_ctx(strat)
    ctx.order_tracker.record_reduce_only_refusal()
    coordinator = SignalExecutionCoordinator(strat)

    coordinator.execute_exit_for_pair(ctx, _exit_long(), bar=object())
    strat.now_ns += 6 * _ONE_SEC_NS
    coordinator.execute_exit_for_pair(ctx, _exit_long(), bar=object())

    assert [order["reduce_only"] for order in strat.created] == [False]
    assert ctx.order_tracker.plain_close_submitted is True


def test_an_unreadable_account_keeps_the_protective_form() -> None:
    for accounts in (_Accounts(None), _Accounts(AccountType.CASH, raises=True)):
        strat = _bar_exit_strategy(accounts)
        ctx = _bar_exit_ctx(strat)

        SignalExecutionCoordinator(strat).execute_exit_for_pair(ctx, _exit_long(), bar=object())

        assert [order["reduce_only"] for order in strat.created] == [True], (
            "without an account the protective form is the safe one"
        )


def test_an_unreadable_account_leaves_a_warning() -> None:
    for accounts in (_Accounts(None), _Accounts(AccountType.CASH, raises=True)):
        strat = _bar_exit_strategy(accounts)
        ctx = _bar_exit_ctx(strat)

        SignalExecutionCoordinator(strat).execute_exit_for_pair(ctx, _exit_long(), bar=object())

        warnings = [str(call.args[0]) for call in strat.log.warning.call_args_list]
        assert any("account" in line.lower() for line in warnings), (
            f"a close sent without knowing the account type must say so; got {warnings}"
        )


def test_a_cash_exit_in_flight_is_not_sent_twice() -> None:
    accounts = _Accounts(AccountType.CASH)
    strat = _bar_exit_strategy(accounts)
    ctx = _bar_exit_ctx(strat)
    coordinator = SignalExecutionCoordinator(strat)

    coordinator.execute_exit_for_pair(ctx, _exit_long(), bar=object())
    strat.now_ns += _ONE_SEC_NS  # inside the in-flight window
    coordinator.execute_exit_for_pair(ctx, _exit_long(), bar=object())

    assert len(strat.created) == 1, "a plain close in flight must not be followed by another"
    assert len(strat.submitted) == 1


# ---------------------------------------------------------------------------
# Tick exits: ExecutionCoordinator partial exits and full closes
# ---------------------------------------------------------------------------


def _tick_harness(account_type: AccountType, monitor: TickMonitorManager) -> Harness:
    harness = Harness(mode=SLTPMode.TICK, monitor=monitor)
    harness.accounts = _Accounts(account_type)
    harness.cache.account_for_venue = harness.accounts
    return harness


def test_a_cash_partial_exit_carries_no_reduce_only() -> None:
    monitor = scaled_monitor(1)
    monitor.init_position(Decimal("100"), True)
    harness = _tick_harness(AccountType.CASH, monitor)

    harness.tick("103")

    assert len(harness.sent) == 1, "the scaled level must send its lot"
    assert harness.sent[0].is_reduce_only is False


def test_a_margin_partial_exit_keeps_reduce_only() -> None:
    monitor = scaled_monitor(1)
    monitor.init_position(Decimal("100"), True)
    harness = _tick_harness(AccountType.MARGIN, monitor)

    harness.tick("103")

    assert [order.is_reduce_only for order in harness.sent] == [True]


def _fixed_take_profit() -> TickMonitorManager:
    monitor = TickMonitorManager(mode="tick", tp_method="fixed", tp_fixed_pct=Decimal("0.02"))
    monitor.init_position(Decimal("100"), True)
    return monitor


def test_a_cash_full_close_is_submitted_not_closed_natively() -> None:
    harness = _tick_harness(AccountType.CASH, _fixed_take_profit())

    harness.tick("103")

    assert harness.closed == [], "close_position on a cash account sends the refused form"
    assert len(harness.sent) == 1, "the full close must be submitted as an order"
    order = harness.sent[0]
    assert order.order_type is OrderType.MARKET
    assert order.side is OrderSide.SELL
    assert order.quantity == harness.instrument.make_qty(Decimal("1"))
    assert order.is_reduce_only is False
    assert harness.ctx.order_tracker.can_submit_close(harness.now_ns) is False, (
        "the close is in flight"
    )


def test_a_margin_full_close_still_closes_natively() -> None:
    harness = _tick_harness(AccountType.MARGIN, _fixed_take_profit())

    harness.tick("103")

    assert len(harness.closed) == 1
    _args, kwargs = harness.closed[0]
    assert kwargs.get("reduce_only") is True
    assert kwargs.get("time_in_force") is TimeInForce.IOC


# ---------------------------------------------------------------------------
# Containment: strategy_core close_all_positions_with_fallback
# ---------------------------------------------------------------------------


class _CoreStub(NS):
    """The core's own methods bound onto a plain object.

    ``NautilusStrategyCore`` extends a type that will not instantiate without an engine,
    which is why the sibling tests bind methods one by one. Binding on lookup instead
    keeps this stub working when the close path grows helpers of its own.
    """

    def __getattr__(self, name: str) -> object:
        attribute = getattr(NautilusStrategyCore, name)
        if callable(attribute):
            return types.MethodType(attribute, self)
        return attribute


def _core(account_type: AccountType | None, tracker: OrderTracker | None = None) -> _CoreStub:
    created: list[dict] = []
    position = NS(
        instrument_id=_SPOT.id,
        quantity=Quantity.from_str("0.00630"),
        is_long=True,
        is_short=False,
        is_closed=False,
        side="LONG",
    )
    stub = _CoreStub(
        created=created,
        submitted=[],
        closed=[],
        position=position,
        log=MagicMock(),
        order_factory=_recording_factory(created),
    )
    stub.cache = NS(
        positions_open=lambda instrument_id=None: [position],
        instrument=lambda _instrument_id: _SPOT,
        account_for_venue=_Accounts(account_type),
    )
    stub.submit_order = lambda order, *args, **kwargs: stub.submitted.append(order)
    stub.close_position = lambda *args, **kwargs: stub.closed.append((args, kwargs))
    stub.cancel_all_orders = MagicMock()
    stub.order_tracker_for = lambda _instrument_id: tracker
    return stub


def test_a_cash_containment_close_is_submitted_not_closed_natively() -> None:
    stub = _core(AccountType.CASH)

    stub.close_all_positions_with_fallback(_SPOT.id)

    assert stub.closed == [], "close_position on a cash account sends the refused form"
    assert len(stub.submitted) == 1
    order = stub.submitted[0]
    assert order.order_type is OrderType.MARKET
    assert order.side is OrderSide.SELL
    assert order.quantity == Quantity.from_str("0.00630")
    assert order.time_in_force is TimeInForce.IOC
    assert order.is_reduce_only is False


def test_a_margin_containment_close_still_closes_natively() -> None:
    stub = _core(AccountType.MARGIN)

    stub.close_all_positions_with_fallback(_SPOT.id)

    assert stub.submitted == []
    assert len(stub.closed) == 1
    assert stub.closed[0][1].get("reduce_only") is True


def test_the_containment_hatch_submits_its_plain_close() -> None:
    """The runner's gate refuses a plain close_position; a plain submitted order passes."""
    tracker = OrderTracker()
    tracker.record_reduce_only_refusal()
    stub = _core(AccountType.MARGIN, tracker)

    stub.close_all_positions_with_fallback(_SPOT.id)

    assert stub.closed == [], "a plain close_position is refused by the runner's safety gate"
    assert len(stub.submitted) == 1
    order = stub.submitted[0]
    assert order.is_reduce_only is False
    assert order.quantity == Quantity.from_str("0.00630")
    assert tracker.plain_close_submitted is True, "the one plain attempt is spent"


# ---------------------------------------------------------------------------
# Static guard: no order site decides reduce_only on its own
# ---------------------------------------------------------------------------

_ADAPTER = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "custos-strategy-toolkit-nautilus"
    / "src"
    / "custos_toolkit_nautilus"
    / "adapter"
)

# Functions allowed to spell the flag out: the one place that decides it.
_DECISION_FUNCTIONS: frozenset[str] = frozenset({"decide_order_form"})


def _is_bool_literal(node: ast.AST) -> bool:
    """A literal True or False; a Protocol stub's ``...`` default decides nothing."""
    return isinstance(node, ast.Constant) and isinstance(node.value, bool)


def _literal_sites(source: str, filename: str) -> tuple[list[str], set[str]]:
    """Where ``reduce_only`` is spelt out as a literal, and which functions exist.

    Two shapes count: a keyword argument ``reduce_only=True`` / ``False`` in a call, and
    a parameter named ``reduce_only`` whose default is such a literal -- a default lets a
    caller skip the decision without writing anything at all.
    """
    tree = ast.parse(source, filename=filename)
    sites: list[str] = []
    defined: set[str] = set()

    def visit(node: ast.AST, owner: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            owner = node.name
            if owner not in _DECISION_FUNCTIONS:
                arguments = node.args
                positional = arguments.posonlyargs + arguments.args
                pairs = list(
                    zip(
                        positional[len(positional) - len(arguments.defaults) :],
                        arguments.defaults,
                        strict=True,
                    )
                )
                pairs += [
                    (arg, default)
                    for arg, default in zip(
                        arguments.kwonlyargs, arguments.kw_defaults, strict=True
                    )
                    if default is not None
                ]
                for arg, default in pairs:
                    if arg.arg == "reduce_only" and _is_bool_literal(default):
                        sites.append(f"{filename}:{default.lineno} default in {owner}")
        if isinstance(node, ast.Call) and owner not in _DECISION_FUNCTIONS:
            for keyword in node.keywords:
                if keyword.arg == "reduce_only" and _is_bool_literal(keyword.value):
                    sites.append(f"{filename}:{keyword.value.lineno} in {owner}")
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, None)
    return sites, defined


def _adapter_literal_sites() -> list[str]:
    sites: list[str] = []
    defined: set[str] = set()
    for path in sorted(_ADAPTER.rglob("*.py")):
        found, names = _literal_sites(
            path.read_text(encoding="utf-8"), str(path.relative_to(_ADAPTER))
        )
        sites += found
        defined |= names
    missing = _DECISION_FUNCTIONS - defined
    assert not missing, f"allow-listed decision functions no longer exist: {sorted(missing)}"
    return sites


def test_no_order_site_hardcodes_reduce_only() -> None:
    sites = _adapter_literal_sites()

    assert sites == [], (
        "every order site must take reduce_only from the one decision; "
        f"these spell it out themselves: {sites}"
    )


def test_the_static_guard_sees_the_shapes_it_is_meant_to_see() -> None:
    """The guard must be able to fail, and must not fail on what decides nothing."""
    source = (
        "def literal(factory):\n"
        "    return factory.market(reduce_only=True)\n"
        "def defaulted(size, reduce_only=False):\n"
        "    return size\n"
        "def decided(factory, form):\n"
        "    return factory.market(reduce_only=form.reduce_only)\n"
        "class Stub:\n"
        "    def market(self, reduce_only: bool = ...): ...\n"
    )

    sites, defined = _literal_sites(source, "sample.py")

    assert sites == ["sample.py:2 in literal", "sample.py:3 default in defaulted"]
    assert {"literal", "defaulted", "decided", "market"} <= defined
