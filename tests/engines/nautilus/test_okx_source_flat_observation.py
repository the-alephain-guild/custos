"""Independent OKX REST observation includes standard and pending algo orders."""

from datetime import UTC, datetime

import pytest

from custos.engines.nautilus.ledger_http import VenueLedgerError
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource


def source() -> OkxVenueLedgerSource:
    return OkxVenueLedgerSource(
        {
            "connector": "okx_perpetual",
            "trading_mode": "testnet",
            "pairs": ["ETH-USDT"],
            "leverage": 3,
        },
        {"api_key": "key", "api_secret": "secret", "api_passphrase": "phrase"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position", "ordinary", "algo_type", "expected"),
    [
        ("0", [], None, (0, 0)),
        ("2", [], None, (1, 0)),
        ("0", [{"instId": "ETH-USDT-SWAP"}], None, (0, 1)),
        ("0", [], "conditional", (0, 1)),
        ("0", [], "oco", (0, 1)),
        ("0", [], "trigger", (0, 1)),
        ("0", [], "move_order_stop", (0, 1)),
    ],
)
async def test_observe_flat_checks_all_pending_order_types(
    monkeypatch, position, ordinary, algo_type, expected
) -> None:
    calls = []

    def get(self, path, params, *, private=True):
        calls.append((path, params, private))
        if path == "/api/v5/account/positions":
            return [{"instId": "ETH-USDT-SWAP", "pos": position}]
        if path == "/api/v5/trade/orders-pending":
            return ordinary
        if path == "/api/v5/trade/orders-algo-pending":
            return [{"instId": "ETH-USDT-SWAP"}] if params["ordType"] == algo_type else []
        if path == "/api/v5/public/time":
            return [{"ts": "1760000000000"}]
        raise AssertionError(path)

    monkeypatch.setattr(OkxVenueLedgerSource, "_get", get)
    result = await source().observe_flat(connector="okx_perpetual", pair="ETH-USDT")
    assert (result.open_positions, result.open_orders) == expected
    assert result.observed_at == datetime.fromtimestamp(1_760_000_000, UTC)
    assert result.positions_complete and result.orders_complete and result.source == "venue_api"
    assert [call[1]["ordType"] for call in calls if call[0].endswith("algo-pending")] == [
        "conditional",
        "oco",
        "trigger",
        "move_order_stop",
    ]


@pytest.mark.asyncio
async def test_observe_flat_rejects_wrong_scope_and_cross_instrument_rows(monkeypatch) -> None:
    reader = source()
    with pytest.raises(VenueLedgerError, match="scope"):
        await reader.observe_flat(connector="binance_perpetual", pair="ETH-USDT")

    def get(self, path, params, *, private=True):
        if path == "/api/v5/account/positions":
            return [{"instId": "BTC-USDT-SWAP", "pos": "0"}]
        return []

    monkeypatch.setattr(OkxVenueLedgerSource, "_get", get)
    with pytest.raises(VenueLedgerError, match="instrument"):
        await reader.observe_flat(connector="okx_perpetual", pair="ETH-USDT")


@pytest.mark.asyncio
async def test_observe_flat_refuses_position_change_during_order_reads(monkeypatch) -> None:
    reader = source()
    positions_read = 0

    def get(self, path, params, *, private=True):
        nonlocal positions_read
        if path == "/api/v5/account/positions":
            positions_read += 1
            return [{"instId": "ETH-USDT-SWAP", "pos": "0" if positions_read == 1 else "2"}]
        if path == "/api/v5/public/time":
            return [{"ts": "1760000000000"}]
        return []

    monkeypatch.setattr(OkxVenueLedgerSource, "_get", get)
    with pytest.raises(VenueLedgerError, match="changed"):
        await reader.observe_flat(connector="okx_perpetual", pair="ETH-USDT")
