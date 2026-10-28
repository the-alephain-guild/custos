"""Independent futures REST observation includes ordinary and algo orders."""

from datetime import UTC, datetime

import pytest

from custos.engines.nautilus.binance_ledger import (
    BinanceVenueLedgerError,
    BinanceVenueLedgerSource,
)


def source() -> BinanceVenueLedgerSource:
    return BinanceVenueLedgerSource(
        spec={
            "trading_mode": "testnet",
            "connector": "binance_perpetual",
            "pairs": ["ETH-USDT"],
        },
        credential={"api_key": "test-key", "api_secret": "test-secret"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("positions", "orders", "algo_orders", "expected"),
    [
        ("0", [], [], (0, 0)),
        ("1", [], [], (1, 0)),
        ("0", [{"symbol": "ETHUSDT"}], [], (0, 1)),
        ("0", [], [{"symbol": "ETHUSDT"}], (0, 1)),
    ],
)
async def test_observe_flat_counts_positions_and_both_order_books(
    monkeypatch, positions, orders, algo_orders, expected
) -> None:
    calls = []

    def get(self, path, params):
        calls.append((path, params))
        if path == "/fapi/v3/account":
            return {"positions": [{"symbol": "ETHUSDT", "positionAmt": positions}]}
        if path == "/fapi/v3/positionRisk":
            return [{"symbol": "ETHUSDT", "positionAmt": positions}]
        if path == "/fapi/v1/openOrders":
            return orders
        if path == "/fapi/v1/openAlgoOrders":
            return algo_orders
        raise AssertionError(path)

    monkeypatch.setattr(BinanceVenueLedgerSource, "_signed_get", get)
    monkeypatch.setattr(BinanceVenueLedgerSource, "_server_time_ms", lambda self: 1_760_000_000_000)
    result = await source().observe_flat(connector="binance_perpetual", pair="ETH-USDT")
    assert (result.open_positions, result.open_orders) == expected
    assert result.observed_at == datetime.fromtimestamp(1_760_000_000, UTC)
    assert result.positions_complete and result.orders_complete and result.source == "venue_api"
    assert ("/fapi/v1/openAlgoOrders", {"symbol": "ETHUSDT"}) in calls


@pytest.mark.asyncio
async def test_observe_flat_rejects_mismatched_scope_or_position_views(monkeypatch) -> None:
    source_reader = source()
    with pytest.raises(BinanceVenueLedgerError, match="scope"):
        await source_reader.observe_flat(connector="okx_perpetual", pair="ETH-USDT")

    def get(self, path, params):
        if path == "/fapi/v3/account":
            return {"positions": [{"symbol": "ETHUSDT", "positionAmt": "0"}]}
        if path == "/fapi/v3/positionRisk":
            return [{"symbol": "ETHUSDT", "positionAmt": "1"}]
        return []

    monkeypatch.setattr(BinanceVenueLedgerSource, "_signed_get", get)
    with pytest.raises(BinanceVenueLedgerError, match="differs"):
        await source_reader.observe_flat(connector="binance_perpetual", pair="ETH-USDT")


@pytest.mark.asyncio
async def test_observe_flat_rejects_wrong_symbol_or_fill_between_reads(monkeypatch) -> None:
    reader = source()
    monkeypatch.setattr(BinanceVenueLedgerSource, "_server_time_ms", lambda self: 1_760_000_000_000)

    def wrong_symbol(self, path, params):
        if path == "/fapi/v3/account":
            return {"positions": []}
        if path == "/fapi/v3/positionRisk":
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        return []

    monkeypatch.setattr(BinanceVenueLedgerSource, "_signed_get", wrong_symbol)
    with pytest.raises(BinanceVenueLedgerError, match="symbol"):
        await reader.observe_flat(connector="binance_perpetual", pair="ETH-USDT")

    reads = 0

    def fills_during_orders(self, path, params):
        nonlocal reads
        if path == "/fapi/v3/account":
            reads += 1
            return {"positions": [{"symbol": "ETHUSDT", "positionAmt": "0" if reads == 1 else "1"}]}
        if path == "/fapi/v3/positionRisk":
            return [{"symbol": "ETHUSDT", "positionAmt": "0" if reads == 1 else "1"}]
        return []

    monkeypatch.setattr(BinanceVenueLedgerSource, "_signed_get", fills_during_orders)
    with pytest.raises(BinanceVenueLedgerError, match="changed"):
        await reader.observe_flat(connector="binance_perpetual", pair="ETH-USDT")
