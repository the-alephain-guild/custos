from __future__ import annotations

import json
from uuid import UUID

from custos.core.runner_fact import RunnerFactAuthority
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactMessageBusBridge


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="tenant-a",
        trading_mode="testnet",
        runner_id=UUID("10000000-0000-4000-8000-000000000001"),
        deployment_instance_id=UUID("20000000-0000-4000-8000-000000000001"),
        deployment_spec_id=UUID("30000000-0000-4000-8000-000000000001"),
        deployment_spec_digest="a" * 64,
        generation=3,
        strategy_id=UUID("40000000-0000-4000-8000-000000000001"),
        capability_version_id=UUID("50000000-0000-4000-8000-000000000001"),
        capability_version=2,
        capability_manifest_digest="b" * 64,
    )


class _Emitter:
    def __init__(self) -> None:
        self.signals: list[dict[str, object]] = []

    def emit_strategy_signal_sync(self, authority, **signal):
        assert authority == _authority()
        self.signals.append(signal)
        return signal["fact_id"]


class OrderSubmitted:
    @staticmethod
    def to_dict(event) -> dict[str, object]:
        return dict(event.values)

    def __init__(self, *, reduce_only: bool = False) -> None:
        self.values = {
            "event_id": "60000000-0000-4000-8000-000000000001",
            "client_order_id": "supertrend-entry-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "order_side": "BUY",
            "reduce_only": reduce_only,
            "ts_event": 1_786_586_400_000_000_000,
        }


def test_order_submission_emits_one_deterministic_strategy_signal() -> None:
    emitter = _Emitter()
    deployment = RunnerFactDeployment(
        authority=_authority(),
        deployment_instance_id="20000000-0000-4000-8000-000000000001",
        deployment_spec_id="30000000-0000-4000-8000-000000000001",
        deployment_spec_digest="a" * 64,
        venue="BINANCE",
        currency="USDT",
        reconciliation_available=True,
        strategy_version="v2",
        timeframe="1-MINUTE",
    )
    bridge = RunnerFactMessageBusBridge(emitter=emitter, deployment=deployment)

    bridge._on_order_event(OrderSubmitted())  # noqa: SLF001
    bridge._on_order_event(OrderSubmitted())  # noqa: SLF001

    assert len(emitter.signals) == 2
    first, replay = emitter.signals
    assert first["fact_id"] == replay["fact_id"]
    assert first["trace_id"] == replay["trace_id"]
    assert first["instrument"] == "BTCUSDT-PERP.BINANCE"
    assert first["client_order_id"] == "supertrend-entry-1"
    assert first["direction"] == "long"
    assert first["strategy_version"] == "v2"
    assert first["timeframe"] == "1-MINUTE"
    assert len(str(first["input_digest"])) == 64
    json.dumps(first, default=str)


def test_reduce_only_submission_is_a_flat_signal() -> None:
    emitter = _Emitter()
    deployment = RunnerFactDeployment(
        authority=_authority(),
        deployment_instance_id="20000000-0000-4000-8000-000000000001",
        deployment_spec_id="30000000-0000-4000-8000-000000000001",
        deployment_spec_digest="a" * 64,
        venue="BINANCE",
        currency="USDT",
        reconciliation_available=True,
        strategy_version="v2",
        timeframe="1-MINUTE",
    )

    RunnerFactMessageBusBridge(emitter=emitter, deployment=deployment)._on_order_event(
        OrderSubmitted(reduce_only=True)
    )

    assert emitter.signals[0]["direction"] == "flat"
