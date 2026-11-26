"""Behavior of the local Host's Papertrade routing and simulation boundary."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.model import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    OrderSide,
    OrderType,
    PositionId,
    Quantity,
    TimeInForce,
)

from custos.engines.nautilus.strategy_hooks import install_hook


def test_host_maps_strategy_market_order_to_the_signed_quote_lot():
    from custos.engines.nautilus.local_papertrade_orders import PapertradeOrderRouting

    submit = Mock(return_value=True)
    strategy = SimpleNamespace(submit_order=submit, order_factory=Mock())
    instrument = InstrumentId.from_str("BTC-USD-PERP.PAPERTRADE")
    original = SimpleNamespace(
        instrument_id=instrument,
        order_type=OrderType.MARKET,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.IOC,
        is_quote_quantity=False,
        is_reduce_only=False,
        quantity=Quantity.from_str("0.002"),
        exec_algorithm_id=None,
        tags=["signal"],
        client_order_id=ClientOrderId("entry"),
    )
    routing = PapertradeOrderRouting(
        strategy, Mock(), AccountId("PAPERTRADE-001"), (str(instrument),), "500"
    )
    install_hook(strategy, "submit_order", routing.wrap)
    assert strategy.submit_order(original)
    kwargs = strategy.order_factory.market.call_args.kwargs
    assert kwargs["quantity"].as_decimal() == 500
    assert kwargs["quote_quantity"] is True and kwargs["time_in_force"] == TimeInForce.GTC
    assert kwargs["client_order_id"] == original.client_order_id
    submit.assert_called_once_with(strategy.order_factory.market.return_value)


def test_host_binds_a_strategy_full_close_to_the_single_held_position():
    from custos.engines.nautilus.local_papertrade_orders import PapertradeOrderRouting

    submit = Mock(return_value=True)
    strategy = SimpleNamespace(submit_order=submit, order_factory=Mock())
    account = AccountId("PAPERTRADE-001")
    instrument = InstrumentId.from_str("BTC-USD-PERP.PAPERTRADE")
    position = SimpleNamespace(
        id=PositionId("PT-9001"),
        account_id=account,
        is_long=True,
        instrument_id=instrument,
        quantity=Quantity.from_str("0.005"),
    )
    cache = Mock()
    cache.positions_open.return_value = [position]
    order = SimpleNamespace(
        instrument_id=instrument,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        side=OrderSide.SELL,
        is_reduce_only=True,
        is_quote_quantity=False,
        exec_algorithm_id=None,
        quantity=position.quantity,
        tags=[],
        client_order_id=ClientOrderId("close"),
    )
    routing = PapertradeOrderRouting(strategy, cache, account, (str(instrument),), "500")
    install_hook(strategy, "submit_order", routing.wrap)
    assert strategy.submit_order(order)
    submit.assert_called_once_with(
        strategy.order_factory.market.return_value, position_id=position.id
    )
    submit.reset_mock()
    cache.positions_open.return_value = [position, position]
    assert strategy.submit_order(order) is False
    submit.assert_not_called()


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:9999/",
        "http://localhost:9999/",
        "http://127.0.0.1.example:9999/",
        "http://127.0.0.1:9999/api",
        "http://127.0.0.1:9999/?test=1",
        "http://127.0.0.1:0/",
    ],
)
def test_local_simulation_refuses_noncanonical_loopback_origins(origin):
    from custos.engines.nautilus.local_host import LoopbackPapertrade

    with pytest.raises(ValueError, match="loopback"):
        LoopbackPapertrade(origin)


@pytest.mark.asyncio
async def test_cancellation_waits_for_native_before_returning_control():
    from custos.engines.nautilus.local_host import wait_for_native_stop

    finished = asyncio.Event()
    node = asyncio.create_task(finished.wait())
    stopping = asyncio.create_task(wait_for_native_stop(node, lambda _: None))
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done()
    assert not node.done()
    finished.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert node.done()


def test_papertrade_cutoff_preserves_native_signing_lifetime_and_stop_confirmation():
    from custos.engines.nautilus.local_host import papertrade_stop_cutoff

    assert papertrade_stop_cutoff(20_000, 20_000, 30) == 16_369
    assert papertrade_stop_cutoff(20_000, 23_600, 30) == 19_969
    assert papertrade_stop_cutoff(20_000, 23_631, 30) == 20_000


@pytest.mark.asyncio
async def test_strategy_stopping_itself_switches_to_host_drain(monkeypatch):
    from custos.engines.nautilus import local_host

    stopped = asyncio.Event()
    statuses = []
    node = SimpleNamespace(
        handle=lambda: SimpleNamespace(stop=stopped.set),
        run_async=stopped.wait,
    )
    strategy = SimpleNamespace(is_running=Mock(side_effect=(True, False)), is_ready=lambda: True)
    supervisor = Mock()
    supervisor.refresh.return_value = SimpleNamespace(state="ready")

    async def flatten(**kwargs):
        assert supervisor.close.called
        assert "strategy_stopped" in statuses
        return True

    monkeypatch.setattr(local_host, "_flatten", flatten)
    assert await local_host._drive(
        node=node,
        strategy=strategy,
        cache=Mock(),
        account_id=Mock(),
        store=Mock(),
        storage=Mock(),
        supervisor=supervisor,
        submit=Mock(),
        stop_requested=asyncio.Event(),
        fault=threading.Event(),
        emit=statuses.append,
        recovering=False,
        stop_by=2**63 - 1,
    )
    assert statuses == ["running", "strategy_stopped"]
