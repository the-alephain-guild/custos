from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custos.core.runner_fact import execution_fill, settlement_fee
from custos.engines.nautilus.settlement import settlement_currency_for_pairs


def test_spot_fill_keeps_quote_currency_separate_from_fee_currency() -> None:
    fact = execution_fill(
        event_id=uuid4(),
        venue="OKX",
        venue_trade_id="trade",
        venue_order_id="order",
        instrument="BTC-USDT.OKX",
        side="buy",
        quantity="0.1",
        price="50000",
        fee="0.0001",
        currency="USDT",
        fee_currency="BTC",
        occurred_at="2026-09-19T00:00:00Z",
    )
    assert fact["currency"] == "USDT"
    assert fact["fee_currency"] == "BTC"


def test_fee_rebate_retains_its_sign() -> None:
    fact = settlement_fee(
        event_id=uuid4(),
        fill_id=uuid4(),
        amount="-0.01",
        currency="USDT",
        assessed_at="2026-09-19T00:00:00Z",
    )
    assert fact["amount"] == "-0.01"


def test_native_spot_symbol_does_not_become_an_invented_currency() -> None:
    assert settlement_currency_for_pairs(["vBTC_vUSDC"]) == "VUSDC"
    assert settlement_currency_for_pairs(["BTC-USDT-SWAP"]) == "USDT"


def test_contract_notional_is_converted_using_the_instrument_multiplier() -> None:
    pytest.importorskip("nautilus_trader")
    from custos_toolkit_nautilus.adapter.sizing import quantity_from_notional

    instrument = SimpleNamespace(multiplier=Decimal("0.01"), is_inverse=False)
    assert quantity_from_notional(instrument, Decimal("1000"), Decimal("50000")) == Decimal("2")


def test_sodex_closed_position_retains_the_distinct_asset_code():
    from unittest.mock import Mock

    from custos.core.runner_fact_producer import RunnerFactEventBridge

    class PositionClosed:
        @staticmethod
        def to_dict(_event):
            return {
                "event_id": str(uuid4()),
                "position_id": "position-1",
                "realized_pnl": "2.50 vUSDC",
                "ts_opened": 1_000_000_000,
                "ts_closed": 2_000_000_000,
            }

    emitter = Mock()
    bridge = RunnerFactEventBridge(
        emitter=emitter,
        deployment=SimpleNamespace(authority=SimpleNamespace(stream_key="scope"), currency="VUSDC"),
    )
    bridge._on_position_event(PositionClosed())
    emitter.emit_sync.assert_called_once()
    fact = emitter.emit_sync.call_args.args[1][0]
    assert fact["currency"] == "VUSDC"
    assert fact["realized_pnl"] == "2.5"
