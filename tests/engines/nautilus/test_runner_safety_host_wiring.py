"""The host puts the safety gate in front of the strategy, or refuses to deploy.

In 1.x this wired a wrapped execution client into the node. 2.0 has no seat there
for python -- no execution-client base to subclass, and a factory registry that
refuses anything it does not already know -- so the boundary is installed on the
strategy instead, next to the event forwarding and for the same reason: the
strategy comes from a signed artifact and nothing constrains its ancestry.

Failure-mode contract:
- no boundary configured        -> nothing is installed, the strategy is untouched
- boundary but no fact stream   -> deploy is refused rather than gating silently
- boundary and fact stream      -> the gate is installed and refusals are recorded
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader")

from custos.engines.nautilus.host import NtTradingNodeHost  # noqa: E402
from custos.engines.nautilus.runner_safety import (  # noqa: E402
    OrderRefusal,
    RunnerSafetyOrderGate,
    install_order_gate,
)

_GATED_METHODS = ("submit_order", "submit_order_list", "modify_order", "market_exit")


class _Boundary:
    def __init__(self) -> None:
        self.forwarder = None
        self.semantics = None

    def bootstrap(self, forwarder) -> None:
        self.forwarder = forwarder

    def bind_runtime(self, *, semantics) -> None:
        self.semantics = semantics


class _Strategy:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            manage_contingent_orders=False,
            manage_gtd_expiry=False,
            manage_stop=False,
        )

    def on_order_event(self, event) -> None: ...

    def on_position_event(self, event) -> None: ...

    def submit_order(self, order, *args, **kwargs) -> None: ...

    def submit_order_list(self, order_list, *args, **kwargs) -> None: ...

    def modify_order(self, client_order_id, *args, **kwargs) -> None: ...

    def modify_orders(self, updates, *args, **kwargs) -> None: ...

    def market_exit(self, *args, **kwargs) -> None: ...

    def close_position(self, position, *args, **kwargs) -> None: ...

    def close_all_positions(self, *args, **kwargs) -> None: ...


class _FactBridge:
    def __init__(self) -> None:
        self.refusals: list[dict] = []

    def bootstrap(self, forwarder) -> None: ...

    def record_local_refusal(self, **fields) -> None:
        self.refusals.append(fields)


def _host_with(boundary) -> NtTradingNodeHost:
    return NtTradingNodeHost(runner_safety_boundary_factory=lambda _spec: boundary)


async def test_the_boundary_factory_is_asked_for_this_deployment() -> None:
    boundary = _Boundary()
    host = _host_with(boundary)

    selected = await host._build_runner_safety_boundary({"deployment_instance_id": "instance-1"})

    assert selected is boundary


async def test_no_boundary_factory_means_no_boundary() -> None:
    host = NtTradingNodeHost()

    assert await host._build_runner_safety_boundary({"deployment_instance_id": "i"}) is None


def test_without_a_boundary_the_strategy_is_left_alone() -> None:
    """Only the event forwarding is installed; nothing gates the orders."""
    host = NtTradingNodeHost()
    strategy = _Strategy()

    host._attach_runtime_bridges("instance-1", strategy, object(), None, None)

    for method in _GATED_METHODS:
        assert method not in vars(strategy), f"{method} was gated with no boundary configured"


def test_a_gate_with_nowhere_to_record_a_refusal_is_refused() -> None:
    """Red line: containment without evidence is not containment we accept.

    A refused order produces no nautilus event at all -- 2.0 publishes an order's
    initialized event inside submit, which a refusal never reaches -- so the fact
    stream is the only place the refusal can appear. Deploying a gate that cannot
    reach it would silently drop that record for every refusal.
    """
    host = _host_with(_Boundary())
    strategy = _Strategy()

    with pytest.raises(RuntimeError, match="signed fact stream"):
        host._attach_runtime_bridges("instance-1", strategy, object(), None, _Boundary())


def test_the_installed_gate_records_its_refusals_as_facts() -> None:
    """End to end: an order the boundary rejects becomes a fact, not just a log."""
    bridge = _FactBridge()
    strategy = _Strategy()

    class _RefusingBoundary:
        def before_submit_order(self, _intent):
            raise RuntimeError("over the cap")

    gate = RunnerSafetyOrderGate(
        boundary=_RefusingBoundary(),
        client_order_id_len_limit=None,
        on_refusal=lambda refusal: bridge.record_local_refusal(
            client_order_id=refusal.client_order_id,
            instrument_id=refusal.instrument_id,
            side=refusal.side,
            reason_code=refusal.reason_code,
        ),
    )
    install_order_gate(strategy, gate)

    strategy.submit_order(
        SimpleNamespace(
            client_order_id="O-1",
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="OrderSide.SELL",
            emulation_trigger=None,
            exec_algorithm_id=None,
        )
    )

    assert bridge.refusals == [
        {
            "client_order_id": "O-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "side": "sell",
            "reason_code": "custos_runner_safety_boundary_unavailable",
        }
    ]


def test_a_refusal_carries_what_an_operator_needs_to_find_the_order() -> None:
    refusal = OrderRefusal(
        client_order_id="O-2",
        instrument_id="ETHUSDT-PERP.BINANCE",
        side="buy",
        reason_code="custos_runner_notional_policy_rejected",
    )

    assert refusal.client_order_id and refusal.instrument_id and refusal.reason_code
