from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from custos.core.runner_fact import RunnerFactAuthority, RunnerFactContractError
from custos.core.runner_fact_producer import (
    RunnerFactDeployment,
    RunnerFactEventBridge,
    strategy_signal_metadata,
)


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
        self.fact_batches: list[tuple[object, ...]] = []
        self.remembered: dict[tuple[str, str], tuple[str, str]] = {}

    def remember_order(self, *, deployment_instance_id, client_order_id, direction, order_role):
        self.remembered[(deployment_instance_id, client_order_id)] = (direction, order_role)

    def recall_orders(self, deployment_instance_id):
        return {
            order_id: value
            for (instance, order_id), value in self.remembered.items()
            if instance == deployment_instance_id
        }

    def forget_order(self, *, deployment_instance_id, client_order_id):
        self.remembered.pop((deployment_instance_id, client_order_id), None)

    def emit_strategy_signal_sync(self, authority, **signal):
        assert authority == _authority()
        self.signals.append(signal)
        return signal["fact_id"]

    def emit_sync(self, authority, facts):
        assert authority == _authority()
        self.fact_batches.append(tuple(facts))
        return tuple(facts)


class _RuntimeLogEmitter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit_sync(self, authority, **event):
        assert authority == _authority()
        self.events.append(event)
        return event["correlation_id"]


class _OrderEvent:
    @staticmethod
    def to_dict(event) -> dict[str, object]:
        return dict(event.values)

    def __init__(
        self,
        *,
        event_id: str = "60000000-0000-4000-8000-000000000001",
        reduce_only: bool = False,
        order_side: str = "BUY",
        order_type: str = "MARKET",
        quantity: str = "0.007",
    ) -> None:
        self.values = {
            "event_id": event_id,
            "client_order_id": "supertrend-entry-1",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "order_side": order_side,
            "order_type": order_type,
            "quantity": quantity,
            "reduce_only": reduce_only,
            "ts_event": 1_786_586_400_000_000_000,
        }


class OrderInitialized(_OrderEvent):
    pass


class OrderSubmitted(_OrderEvent):
    pass


class OrderCanceled(_OrderEvent):
    def __init__(self) -> None:
        super().__init__(event_id="90000000-0000-4000-8000-000000000001")


class OrderRejected(_OrderEvent):
    def __init__(self) -> None:
        super().__init__(event_id="70000000-0000-4000-8000-000000000001")
        self.values["reason"] = "custos_runner_notional_policy_rejected"


class OrderFilled(_OrderEvent):
    def __init__(
        self,
        client_order_id: str,
        *,
        event_id: str = "80000000-0000-4000-8000-000000000001",
        trade_id: str = "trade-1",
        last_qty: str = "0.007",
    ) -> None:
        super().__init__(event_id=event_id)
        self.values.update(
            {
                "client_order_id": client_order_id,
                "trade_id": trade_id,
                "venue_order_id": "venue-1",
                "commission": "0.01 USDT",
                "last_qty": last_qty,
                "last_px": "63833.60",
                "order_type": "MARKET",
                "liquidity_side": "TAKER",
            }
        )


def _deployment() -> RunnerFactDeployment:
    return RunnerFactDeployment(
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


def test_strategy_signal_metadata_uses_verified_runtime_bar_type() -> None:
    strategy = SimpleNamespace(
        config=SimpleNamespace(
            platforms=SimpleNamespace(
                nautilus=SimpleNamespace(bar_type="1-MINUTE"),
            )
        )
    )

    _, timeframe = strategy_signal_metadata(
        {"strategy_version": "v2", "strategy_config": {}, "nautilus_config": {}},
        runtime_strategy=strategy,
    )

    assert timeframe == "1-MINUTE"


def test_strategy_signal_metadata_rejects_declared_runtime_bar_type_drift() -> None:
    strategy = SimpleNamespace(
        config=SimpleNamespace(
            platforms=SimpleNamespace(
                nautilus=SimpleNamespace(bar_type="1-MINUTE"),
            )
        )
    )

    with pytest.raises(RunnerFactContractError, match="timeframe differs"):
        strategy_signal_metadata(
            {
                "strategy_version": "v2",
                "strategy_config": {"timeframe": "5-MINUTE"},
            },
            runtime_strategy=strategy,
        )


def test_order_initialization_emits_one_signal_before_submission_outcome() -> None:
    emitter = _Emitter()
    runtime_logs = _RuntimeLogEmitter()
    bridge = RunnerFactEventBridge(
        emitter=emitter,
        deployment=_deployment(),
        runtime_log_emitter=runtime_logs,
    )

    bridge._on_order_event(OrderInitialized())  # noqa: SLF001
    bridge._on_order_event(OrderSubmitted())  # noqa: SLF001

    assert len(emitter.signals) == 1
    first = emitter.signals[0]
    assert first["instrument"] == "BTCUSDT-PERP.BINANCE"
    assert first["client_order_id"] == "supertrend-entry-1"
    assert first["direction"] == "long"
    assert first["strategy_version"] == "v2"
    assert first["timeframe"] == "1-MINUTE"
    assert len(str(first["input_digest"])) == 64
    json.dumps(first, default=str)
    assert runtime_logs.events[0]["message"] == "order_initialized"
    assert runtime_logs.events[0]["structured_fields"] == {
        "client_order_id": "supertrend-entry-1",
        "instrument": "BTCUSDT-PERP.BINANCE",
        "side": "long",
        "lifecycle": "initialized",
        "order_role": "strategy_entry",
        "order_type": "market",
        "quantity": "0.007",
    }
    assert runtime_logs.events[1]["message"] == "order_submitted"
    assert runtime_logs.events[1]["structured_fields"] == {
        "client_order_id": "supertrend-entry-1",
        "instrument": "BTCUSDT-PERP.BINANCE",
        "side": "long",
        "lifecycle": "submitted",
        "order_role": "strategy_entry",
    }


def test_protective_stop_is_owned_without_polluting_strategy_signals() -> None:
    emitter = _Emitter()
    runtime_logs = _RuntimeLogEmitter()

    RunnerFactEventBridge(
        emitter=emitter,
        deployment=_deployment(),
        runtime_log_emitter=runtime_logs,
    )._on_order_event(
        OrderInitialized(reduce_only=True, order_side="SELL", order_type="STOP_MARKET")
    )

    assert emitter.signals == []
    assert runtime_logs.events[0]["structured_fields"] == {
        "client_order_id": "supertrend-entry-1",
        "instrument": "BTCUSDT-PERP.BINANCE",
        "side": "sell",
        "lifecycle": "initialized",
        "order_role": "protective_stop",
        "order_type": "stop_market",
        "quantity": "0.007",
    }


def test_reduce_only_strategy_exit_remains_a_flat_signal() -> None:
    emitter = _Emitter()

    RunnerFactEventBridge(emitter=emitter, deployment=_deployment())._on_order_event(
        OrderInitialized(reduce_only=True, order_side="SELL")
    )

    assert emitter.signals[0]["direction"] == "flat"


def test_local_order_rejection_emits_structured_signed_lifecycle_fact() -> None:
    emitter = _Emitter()
    runtime_logs = _RuntimeLogEmitter()
    bridge = RunnerFactEventBridge(
        emitter=emitter,
        deployment=_deployment(),
        runtime_log_emitter=runtime_logs,
    )

    bridge._on_order_event(OrderInitialized())  # noqa: SLF001
    bridge._on_order_event(OrderRejected())  # noqa: SLF001

    rejection = runtime_logs.events[1]
    assert rejection["level"] == "WARN"
    assert rejection["component"] == "custos.execution.order"
    assert rejection["message"] == "order_rejected"
    assert rejection["structured_fields"] == {
        "client_order_id": "supertrend-entry-1",
        "instrument": "BTCUSDT-PERP.BINANCE",
        "side": "long",
        "lifecycle": "rejected",
        "order_role": "strategy_entry",
        "reason_code": "custos_runner_notional_policy_rejected",
    }


def test_foreign_order_events_do_not_enter_this_instance_fact_stream() -> None:
    emitter = _Emitter()
    runtime_logs = _RuntimeLogEmitter()
    bridge = RunnerFactEventBridge(
        emitter=emitter,
        deployment=_deployment(),
        runtime_log_emitter=runtime_logs,
    )

    bridge._on_order_event(OrderSubmitted())  # noqa: SLF001
    bridge._on_order_event(OrderFilled("foreign-order"))  # noqa: SLF001

    assert emitter.signals == []
    assert emitter.fact_batches == []
    assert runtime_logs.events == []


def test_owned_partial_fills_remain_separate_execution_and_settlement_facts() -> None:
    emitter = _Emitter()
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())
    bridge._on_order_event(OrderInitialized())  # noqa: SLF001

    bridge._on_order_event(  # noqa: SLF001
        OrderFilled(
            "supertrend-entry-1",
            trade_id="trade-part-1",
            last_qty="0.0031",
        )
    )
    bridge._on_order_event(  # noqa: SLF001
        OrderFilled(
            "supertrend-entry-1",
            event_id="80000000-0000-4000-8000-000000000002",
            trade_id="trade-part-2",
            last_qty="0.0039",
        )
    )

    assert len(emitter.fact_batches) == 2
    first_execution, first_settlement, _first_fee = emitter.fact_batches[0]
    second_execution, second_settlement, _second_fee = emitter.fact_batches[1]
    assert first_execution["venue_trade_id"] == "trade-part-1"
    assert first_execution["quantity"] == "0.0031"
    assert second_execution["venue_trade_id"] == "trade-part-2"
    assert second_execution["quantity"] == "0.0039"
    assert first_settlement["fill_id"] != second_settlement["fill_id"]


@pytest.mark.parametrize("kind", ["fill", "position", "initialized", "submitted"])
def test_audit_failure_reaches_host_without_interrupting_strategy(kind) -> None:
    from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder

    class BrokenEmitter(_Emitter):
        def emit_sync(self, *args, **kwargs):
            raise OSError("fixture disk full")

        def emit_strategy_signal_sync(self, *args, **kwargs):
            raise OSError("fixture disk full")

    class Strategy:
        def __init__(self):
            self.events = []

        def on_order_event(self, event):
            self.events.append(event)

        def on_position_event(self, event):
            self.events.append(event)

    class PositionClosed:
        @staticmethod
        def to_dict(event):
            return {
                "event_id": "80000000-0000-4000-8000-000000000001",
                "position_id": "position-1",
                "realized_pnl": "10 USDT",
                "ts_opened": 1_000_000_000,
                "ts_closed": 2_000_000_000,
            }

    broken = BrokenEmitter()
    bridge = RunnerFactEventBridge(
        emitter=broken, deployment=_deployment(), runtime_log_emitter=broken
    )
    bridge._owned_order_ids.add("supertrend-entry-1")
    failures = []
    forwarder = StrategyEventForwarder(
        deployment_instance_id=_deployment().deployment_instance_id,
        on_sink_failure=lambda *args: failures.append(args),
    )
    bridge.bootstrap(forwarder)
    strategy = Strategy()
    forwarder.install(strategy)
    event = {
        "fill": OrderFilled("supertrend-entry-1"),
        "position": PositionClosed(),
        "initialized": OrderInitialized(),
        "submitted": OrderSubmitted(),
    }[kind]
    if kind == "position":
        strategy.on_position_event(event)
    else:
        strategy.on_order_event(event)
    assert strategy.events == [event]
    assert len(failures) == 1
    assert failures[0][1].endswith(":OSError")


def test_native_position_closed_event_emits_a_durable_fact_without_to_dict() -> None:
    pytest.importorskip("nautilus_trader")
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import (
        AccountId,
        ClientOrderId,
        CryptoPerpetual,
        Currency,
        InstrumentId,
        LiquiditySide,
        Money,
        OrderSide,
        OrderType,
        Position,
        PositionId,
        Price,
        Quantity,
        StrategyId,
        Symbol,
        TradeId,
        TraderId,
        VenueOrderId,
    )
    from nautilus_trader.model import (
        OrderFilled as NativeOrderFilled,
    )
    from nautilus_trader.model import (
        PositionClosed as NativePositionClosed,
    )

    instrument = CryptoPerpetual(
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
        Symbol("BTCUSDT"),
        Currency.from_str("BTC"),
        Currency.from_str("USDT"),
        Currency.from_str("USDT"),
        False,
        2,
        3,
        Price.from_str("0.01"),
        Quantity.from_str("0.001"),
        0,
        0,
    )

    def fill(side, price: str, fee: str, sequence: int):
        return NativeOrderFilled(
            TraderId("TRADER-001"),
            StrategyId("TEST-001"),
            instrument.id,
            ClientOrderId(f"order-{sequence}"),
            VenueOrderId(str(sequence)),
            AccountId("BINANCE-001"),
            TradeId(str(sequence)),
            side,
            OrderType.MARKET,
            Quantity.from_str("1.000"),
            Price.from_str(price),
            Currency.from_str("USDT"),
            LiquiditySide.TAKER,
            UUID4(),
            sequence * 1_000_000_000,
            sequence * 1_000_000_000,
            False,
            position_id=PositionId("P-001"),
            commission=Money.from_str(f"{fee} USDT"),
        )

    position = Position(instrument, fill(OrderSide.BUY, "100.00", "1", 1))
    closing = fill(OrderSide.SELL, "110.00", "1", 2)
    position.apply(closing)
    event = NativePositionClosed.create(position, closing, UUID4(), 2_000_000_000)
    emitter = _Emitter()
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())

    bridge._on_position_event(event)

    assert len(emitter.fact_batches) == 1
    (fact,) = emitter.fact_batches[0]
    assert fact["kind"] == "position_closed"
    assert fact["realized_pnl"] == "8"
    assert fact["currency"] == "USDT"
