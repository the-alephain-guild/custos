"""Preserve Native submit targets and prove a full hedged position before a close."""

from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")
from nautilus_trader.model import ClientId, PositionId, Quantity

from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.engines.nautilus.runner_safety import (
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)

INSTRUMENT = "BTC-USD-PERP.PAPERTRADE"
ACCOUNT = "PAPERTRADE-001"


def order(*, side="SELL", quantity=None):
    return SimpleNamespace(
        client_order_id="close-7",
        strategy_id="LOCAL-001",
        instrument_id=INSTRUMENT,
        side=side,
        quantity=quantity or Quantity.from_str("0.005"),
        is_quote_quantity=False,
        is_reduce_only=True,
        emulation_trigger=None,
        exec_algorithm_id=None,
    )


def position(identifier="PT-7", *, long=True):
    return SimpleNamespace(
        id=PositionId(identifier),
        account_id=ACCOUNT,
        instrument_id=INSTRUMENT,
        quantity=Quantity.from_str("0.005"),
        is_long=long,
        is_short=not long,
        is_closed=False,
        strategy_id="LOCAL-001",
    )


def target(positions=None, *, position_id=None, params=None, close=None, client_id=None):
    positions = [position(), position("PT-8", long=False)] if positions is None else positions
    cache = SimpleNamespace(positions_open=lambda **kwargs: positions)
    intent = SimpleNamespace(
        order=close or order(), position_id=position_id, params=params, client_id=client_id
    )
    return NautilusCachedOrderSemantics(cache).papertrade_reduction_target(
        intent, account_id=ACCOUNT, client_id="PAPERTRADE"
    )


def test_explicit_and_parameter_target_select_one_of_two_hedged_positions():
    result = target(position_id=PositionId("PT-7"), params={"papertrade_position_id": "7"})
    assert result.position_id == "PT-7"
    assert result.account_id == ACCOUNT
    assert result.quantity == Decimal("0.005")
    assert result.side == "SELL"
    assert result.instrument_id == INSTRUMENT
    assert result.client_id == "PAPERTRADE"
    assert len(result.params_digest) == 64


def test_short_position_can_be_closed_without_netting_the_two_sides():
    result = target(position_id=PositionId("PT-8"), close=order(side="BUY"))
    assert result.position_id == "PT-8" and result.side == "BUY"


@pytest.mark.parametrize("raw", ["8", "007", "-1", True, 7, None, "PT-7", str(2**256)])
def test_conflicting_or_noncanonical_parameter_target_is_refused(raw):
    with pytest.raises(ValueError):
        target(position_id=PositionId("PT-7"), params={"papertrade_position_id": raw})


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "instrument",
        "side",
        "partial",
        "float",
        "quote",
        "not_reduce",
        "closed",
        "missing",
        "duplicate",
        "client",
    ],
)
def test_target_requires_current_account_direction_and_the_complete_exact_position(change):
    pos, close = position(), order()
    client_id = None
    positions = [pos]
    if change == "account":
        pos.account_id = "PAPERTRADE-OTHER"
    elif change == "instrument":
        pos.instrument_id = "ETH-USD-PERP.PAPERTRADE"
    elif change == "side":
        close.side = "BUY"
    elif change == "partial":
        close.quantity = Quantity.from_str("0.004")
    elif change == "float":
        close.quantity = 0.005
    elif change == "quote":
        close.is_quote_quantity = True
    elif change == "not_reduce":
        close.is_reduce_only = False
    elif change == "closed":
        pos.is_closed = True
    elif change == "missing":
        positions = []
    elif change == "duplicate":
        positions.append(position())
    elif change == "client":
        client_id = ClientId("OTHER")
    with pytest.raises(ValueError):
        target(positions, position_id=PositionId("PT-7"), close=close, client_id=client_id)


class Boundary:
    def __init__(self, callback):
        self.callback = callback
        self.fallback_breaker = FallbackBreaker(
            FallbackBreakerConfig(max_notional=Decimal("2000"), max_drawdown_pct=Decimal("10"))
        )
        self.calls = 0

    def dispatch_control(self):
        return nullcontext()

    def assert_control_current(self):
        pass

    def before_submit_order(self, intent):
        self.calls += 1
        self.callback(intent)
        return ()

    def rollback_submit(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("positional", [False, True])
def test_gate_checks_and_forwards_the_same_detached_submit_context(positional):
    original = {"papertrade_position_id": "7", "extra": {"tags": ["original"]}}
    position_id, client_id = PositionId("PT-7"), ClientId("PAPERTRADE")
    seen = []

    def check(intent):
        assert intent.position_id == position_id and intent.client_id == client_id
        assert intent.params == {"papertrade_position_id": "7", "extra": {"tags": ["original"]}}
        original["papertrade_position_id"] = "8"
        original["extra"]["tags"].append("changed")

    boundary = Boundary(check)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)

    def submit(item, *args, **kwargs):
        values = args if args else (kwargs["position_id"], kwargs["client_id"], kwargs["params"])
        seen.append(values)

    if positional:
        accepted = gate.submit_order(submit, order(), position_id, client_id, original)
    else:
        accepted = gate.submit_order(
            submit, order(), position_id=position_id, client_id=client_id, params=original
        )
    assert accepted is True
    assert seen[0][0:2] == (position_id, client_id)
    assert seen[0][2] == {"papertrade_position_id": "7", "extra": {"tags": ["original"]}}


def test_duplicate_submit_target_arguments_are_refused_before_reservation():
    boundary = Boundary(lambda intent: None)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    sent = []
    assert (
        gate.submit_order(
            lambda *args, **kwargs: sent.append(True),
            order(),
            PositionId("PT-7"),
            position_id=PositionId("PT-7"),
        )
        is False
    )
    assert not sent and boundary.calls == 0


@pytest.mark.parametrize("location", ["root", "nested_dict", "nested_list"])
def test_custom_copy_hooks_cannot_preserve_caller_owned_submit_parameters(location):
    class AliasedDict(dict):
        def __deepcopy__(self, memo):
            return self

    class AliasedList(list):
        def __deepcopy__(self, memo):
            return self

    params = {"papertrade_position_id": "7", "extra": {"tags": ["original"]}}
    if location == "root":
        params = AliasedDict(params)
    elif location == "nested_dict":
        params["extra"] = AliasedDict(params["extra"])
    else:
        params["extra"]["tags"] = AliasedList(params["extra"]["tags"])
    boundary = Boundary(lambda intent: None)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    sent = []
    assert gate.submit_order(lambda *args, **kw: sent.append(kw), order(), params=params) is False
    assert not sent and boundary.calls == 0


@pytest.mark.parametrize("extra", [float("nan"), float("inf"), {1: "value"}, Decimal("1")])
def test_non_json_values_are_refused_before_reservation(extra):
    boundary = Boundary(lambda intent: None)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    assert gate.submit_order(lambda *args, **kw: None, order(), params={"extra": extra}) is False
    assert boundary.calls == 0


def test_cyclic_parameters_are_refused_before_reservation():
    params = {}
    params["cycle"] = params
    boundary = Boundary(lambda intent: None)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    assert gate.submit_order(lambda *args, **kw: None, order(), params=params) is False
    assert boundary.calls == 0


def test_plain_native_json_metadata_remains_supported():
    params = {"extra": (None, True, 2, 1.25, "value")}
    boundary = Boundary(lambda intent: None)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    sent = []
    assert gate.submit_order(lambda *args, **kw: sent.append(kw), order(), params=params) is True
    assert sent == [{"params": {"extra": [None, True, 2, 1.25, "value"]}}]


def test_a_parameter_only_target_is_normalized_to_the_native_cache_identity():
    result = target(params={"papertrade_position_id": "7"})
    assert result.position_id == "PT-7"


def test_no_target_is_not_inferred_from_the_net_position():
    with pytest.raises(ValueError):
        target()


@pytest.mark.parametrize("kwargs", [{"unknown": 1}, {"params": []}, {"params": "7"}])
def test_non_native_submit_arguments_never_reach_the_reservation_or_dispatch(kwargs):
    boundary = Boundary(lambda intent: None)
    gate = RunnerSafetyOrderGate(boundary=boundary, client_order_id_len_limit=None)
    sent = []
    assert gate.submit_order(lambda *args, **kw: sent.append(True), order(), **kwargs) is False
    assert not sent and boundary.calls == 0


def test_a_target_snapshot_does_not_change_when_the_cache_object_changes():
    from dataclasses import FrozenInstanceError

    pos = position()
    result = target([pos], position_id=PositionId("PT-7"))
    pos.quantity = Quantity.from_str("0.001")
    assert result.quantity == Decimal("0.005")
    with pytest.raises(FrozenInstanceError):
        result.quantity = Decimal("0.001")
