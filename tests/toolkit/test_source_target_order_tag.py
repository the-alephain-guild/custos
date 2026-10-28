"""An external target stays identifiable after the signal becomes an order."""

from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("nautilus_trader")

from _strategy_harness import Harness  # noqa: E402
from custos_toolkit.signals.types import Signal  # noqa: E402
from custos_toolkit_nautilus.adapter.coordinators import SignalExecutionCoordinator  # noqa: E402
from custos_toolkit_nautilus.adapter.signal_correlation import (  # noqa: E402
    extract_target_delivery_id_from_tags,
)


def test_source_target_identity_reaches_entry_order_without_changing_size() -> None:
    harness = Harness()
    target_id = "a" * 64
    signal = Signal.enter_long(price=100.0, pair="BTC-USDT", target_delivery_id=target_id)
    signal.metadata["_signal_id"] = "10000000-0000-4000-8000-000000000001"
    captured = []

    def create(**kwargs):
        captured.append(kwargs)
        return harness.entry_order(**kwargs)

    harness.ctx.execution_manager.create_entry_order = create
    plan = SignalExecutionCoordinator(harness)._plan_entry(
        harness.ctx, signal, Decimal("50"), NS(close=Decimal("100"))
    )
    assert plan is not None and plan.size == Decimal("50")
    assert captured[0]["tags"] == [
        "signal_id:10000000-0000-4000-8000-000000000001",
        f"target_delivery_id:{target_id}",
    ]


def test_malformed_target_identity_is_not_put_on_order() -> None:
    harness = Harness()
    signal = Signal.enter_long(price=100.0, pair="BTC-USDT", target_delivery_id="not-a-digest")
    captured = []

    def create(**kwargs):
        captured.append(kwargs)
        return harness.entry_order(**kwargs)

    harness.ctx.execution_manager.create_entry_order = create
    plan = SignalExecutionCoordinator(harness)._plan_entry(
        harness.ctx, signal, Decimal("50"), NS(close=Decimal("100"))
    )
    assert plan is None
    assert captured == []


def test_target_tag_reader_refuses_ambiguous_or_malformed_tags() -> None:
    target = "a" * 64
    assert extract_target_delivery_id_from_tags([f"target_delivery_id:{target}"]) == target
    assert extract_target_delivery_id_from_tags([f"target_delivery_id:{target}"] * 2) is None
    assert extract_target_delivery_id_from_tags(["target_delivery_id:not-a-digest"]) is None
