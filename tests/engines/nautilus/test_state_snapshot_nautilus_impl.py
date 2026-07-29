"""NtTradingNodeHost Tier-2 implementations against a stubbed NT node.

These drive the real host methods with a lightweight fake node/kernel/cache so
the position/connectivity/flatten mappings are exercised without a running
TradingNode. Money is asserted Decimal end to end (red line 0.4).
"""

from __future__ import annotations

from decimal import Decimal

from custos.engines.nautilus.host import NtTradingNodeHost, SandboxSimulationHost


class _FakeInstrumentId(str):
    @property
    def venue(self) -> str:
        return "BINANCE"


class _FakeMoney:
    def __init__(self, value: str) -> None:
        self._value = Decimal(value)

    def as_decimal(self) -> Decimal:
        return self._value


class _FakePortfolio:
    def __init__(self, equity: str) -> None:
        self._equity = equity

    def missing_price_instruments(self, venue: str) -> list:
        return []

    def equity(self, venue: str) -> dict[str, _FakeMoney]:
        return {"USDT": _FakeMoney(self._equity)}


class _FakePosition:
    def __init__(self, quantity: str, avg_px_open: str) -> None:
        self.instrument_id = _FakeInstrumentId(f"TEST-{avg_px_open}.BINANCE")
        self.quantity = quantity
        self.avg_px_open = avg_px_open
        self.settlement_currency = "USDT"
        self.is_short = Decimal(quantity) < 0

    def unrealized_pnl(self, mark_price: str) -> _FakeMoney:
        return _FakeMoney("0")


class _FakeCache:
    def __init__(self, positions: list[_FakePosition]) -> None:
        self._positions = positions

    def positions_open(self) -> list[_FakePosition]:
        return self._positions

    def instrument_ids(self) -> list[_FakeInstrumentId]:
        return [position.instrument_id for position in self._positions]

    def mark_price(self, instrument_id: _FakeInstrumentId) -> str:
        position = next(p for p in self._positions if p.instrument_id == instrument_id)
        return position.avg_px_open


class _FakeKernel:
    def __init__(self, cache: _FakeCache) -> None:
        self.cache = cache
        self.portfolio = _FakePortfolio("250")


class _FakeNode:
    def __init__(self, positions: list[_FakePosition]) -> None:
        self.kernel = _FakeKernel(_FakeCache(positions))


def _host() -> NtTradingNodeHost:
    return NtTradingNodeHost(tenant_id="t", runner_id="r")


async def test_nt_host_get_open_notional_sums_positions_decimal() -> None:
    host = _host()
    node = _FakeNode([_FakePosition("2", "100"), _FakePosition("-1", "50")])
    host._active_nodes["spec-1"] = (node, None)

    value = await host.get_open_notional("spec-1")

    # 2 * 100 + abs(-1) * 50 = 250, gross exposure over both legs.
    assert value == Decimal("250")
    assert isinstance(value, Decimal)


async def test_nt_host_get_open_notional_unknown_spec_zero() -> None:
    assert await _host().get_open_notional("nope") == Decimal("0")


# -- flatten_positions → NT close_all_positions mapping -------------------


class _FakeStrategy:
    def __init__(self) -> None:
        self.closed: list = []

    def close_all_positions(self, instrument_id) -> None:
        self.closed.append(instrument_id)


class _FakeInstPosition:
    def __init__(self, instrument_id: str) -> None:
        self.instrument_id = instrument_id


class _FakeTrader:
    def __init__(self, strategies: list) -> None:
        self._strategies = strategies

    def strategies(self) -> list:
        return self._strategies


class _FakeFlattenCache:
    def __init__(self, positions: list) -> None:
        self._positions = positions

    def positions_open(self) -> list:
        return self._positions


class _FakeFlattenKernel:
    def __init__(self, positions: list, strategies: list) -> None:
        self.cache = _FakeFlattenCache(positions)
        self.trader = _FakeTrader(strategies)


class _FakeFlattenNode:
    def __init__(self, positions: list, strategies: list) -> None:
        self.kernel = _FakeFlattenKernel(positions, strategies)


async def test_flatten_positions_maps_to_close_all() -> None:
    host = _host()
    strategy = _FakeStrategy()
    node = _FakeFlattenNode(
        [_FakeInstPosition("BTCUSDT"), _FakeInstPosition("ETHUSDT")], [strategy]
    )
    host._active_nodes["spec-1"] = (node, None)

    await host.flatten_positions("spec-1", "notional_breach")

    # The engine-neutral flatten maps to NT's per-instrument close_all_positions.
    assert sorted(strategy.closed) == ["BTCUSDT", "ETHUSDT"]


async def test_flatten_positions_sandbox_simulation_host_noop() -> None:
    # A paper host flatten is a logged no-op — no exception, no state.
    await SandboxSimulationHost().flatten_positions("spec-1", "notional_breach")


# -- T2.1 snapshot Tier-2 NT-side impl tests ------------------------------


class _FakeSnapshotPosition:
    def __init__(
        self,
        instrument_id: str,
        quantity: str,
        avg_px_open: str,
        unrealized_pnl: str = "0",
    ) -> None:
        self.instrument_id = _FakeInstrumentId(instrument_id)
        self.quantity = quantity
        self.avg_px_open = avg_px_open
        self.settlement_currency = "USDT"
        self.is_short = Decimal(quantity) < 0
        self._unrealized_pnl = unrealized_pnl

    def unrealized_pnl(self, mark_price: str) -> _FakeMoney:
        return _FakeMoney(self._unrealized_pnl)


class _FakeSnapshotOrder:
    def __init__(
        self,
        client_order_id: str,
        instrument_id: str,
        side: str,
        quantity: str,
        price: str,
        status: str,
    ) -> None:
        self.client_order_id = client_order_id
        self.instrument_id = instrument_id
        self.side = side
        self.quantity = quantity
        self.price = price
        self.status = status


class _FakeSnapshotCache:
    def __init__(self, positions: list, orders: list) -> None:
        self._positions = positions
        self._orders = orders

    def positions_open(self) -> list:
        return self._positions

    def orders_open(self) -> list:
        return self._orders

    def instrument_ids(self) -> list[_FakeInstrumentId]:
        return [position.instrument_id for position in self._positions]

    def mark_price(self, instrument_id: _FakeInstrumentId) -> str:
        position = next(p for p in self._positions if p.instrument_id == instrument_id)
        return position.avg_px_open


class _FakeSnapshotKernel:
    def __init__(self, positions: list, orders: list, equity: str) -> None:
        self.cache = _FakeSnapshotCache(positions, orders)
        self.portfolio = _FakePortfolio(equity)


class _FakeSnapshotNode:
    def __init__(self, positions: list, orders: list, equity: str = "1000") -> None:
        self.kernel = _FakeSnapshotKernel(positions, orders, equity)


async def test_nt_host_get_positions_returns_decimal_snapshots() -> None:
    host = _host()
    node = _FakeSnapshotNode(
        [
            _FakeSnapshotPosition("BTCUSDT", "2", "100", "5"),
            _FakeSnapshotPosition("ETHUSDT", "-1", "50", "-2.5"),
        ],
        orders=[],
    )
    host._active_nodes["spec-1"] = (node, None)

    positions = await host.get_positions("spec-1")

    assert len(positions) == 2
    # Every money field lands as Decimal end-to-end (red line 0.4).
    for snapshot in positions:
        assert isinstance(snapshot.quantity, Decimal)
        assert isinstance(snapshot.avg_px, Decimal)
        assert isinstance(snapshot.unrealized_pnl, Decimal)
        assert isinstance(snapshot.notional, Decimal)
    # The fake trusted mark equals avg_px, so current marked notional matches it.
    by_instrument = {p.instrument_id: p for p in positions}
    assert by_instrument["BTCUSDT"].notional == Decimal("200")
    assert by_instrument["ETHUSDT"].notional == Decimal("50")


async def test_nt_host_get_positions_unknown_spec_empty() -> None:
    assert await _host().get_positions("nope") == []


async def test_nt_host_get_orders_returns_decimal_snapshots() -> None:
    host = _host()
    node = _FakeSnapshotNode(
        positions=[],
        orders=[
            _FakeSnapshotOrder("c1", "BTCUSDT", "BUY", "1", "100", "ACCEPTED"),
            _FakeSnapshotOrder("c2", "ETHUSDT", "SELL", "2", "50", "SUBMITTED"),
        ],
    )
    host._active_nodes["spec-1"] = (node, None)

    orders = await host.get_orders("spec-1")

    assert len(orders) == 2
    for snapshot in orders:
        assert isinstance(snapshot.quantity, Decimal)
        assert isinstance(snapshot.price, Decimal)


async def test_nt_host_get_orders_unknown_spec_empty() -> None:
    assert await _host().get_orders("nope") == []


class _FakeMarketOrder:
    """A market order the way Nautilus models one: no ``price`` attribute at all.

    ``MarketOrder`` does not merely leave ``price`` unset — the attribute is absent,
    so anything reaching for it raises ``AttributeError``. Every other fake order here
    carries a price, which is why the snapshot path could assume one indefinitely.
    """

    def __init__(self, client_order_id: str, instrument_id: str, side: str) -> None:
        self.client_order_id = client_order_id
        self.instrument_id = instrument_id
        self.side = side
        self.quantity = "1"
        self.status = "ACCEPTED"


async def test_nt_host_get_orders_reports_market_orders_without_a_price() -> None:
    """A market order has no limit price, so the snapshot reports absence, not zero.

    Zero would be indistinguishable from a genuine limit at zero; the field is
    optional precisely so a reader cannot mistake one for the other.
    """

    host = _host()
    node = _FakeSnapshotNode(
        positions=[],
        orders=[
            _FakeMarketOrder("c1", "BTCUSDT", "BUY"),
            _FakeSnapshotOrder("c2", "ETHUSDT", "SELL", "2", "50", "SUBMITTED"),
        ],
    )
    host._active_nodes["spec-1"] = (node, None)

    orders = await host.get_orders("spec-1")

    assert [snapshot.price for snapshot in orders] == [None, Decimal("50")]
    assert all(isinstance(snapshot.quantity, Decimal) for snapshot in orders)


async def test_nt_host_get_engine_status_decimal_and_tracks_peak() -> None:
    host = _host()
    # First tick: peak = current (initial exposure).
    node1 = _FakeSnapshotNode(
        [_FakeSnapshotPosition("BTCUSDT", "1", "100", "10")],
        orders=[],
        equity="1000",
    )
    host._active_nodes["spec-1"] = (node1, None)

    status1 = await host.get_engine_status("spec-1")
    assert status1.phase == "running"
    assert status1.position_count == 1
    assert status1.order_count == 0
    assert status1.open_notional == Decimal("100")
    # Equity comes from the Nautilus portfolio ledger, never a position proxy.
    assert status1.current_equity == Decimal("1000")
    assert status1.peak_equity == Decimal("1000")
    assert status1.drawdown_pct == Decimal("0")

    # Second tick: equity drops → drawdown_pct > 0 while peak stays.
    node2 = _FakeSnapshotNode(
        [_FakeSnapshotPosition("BTCUSDT", "1", "50", "-10")],
        orders=[],
        equity="400",
    )
    host._active_nodes["spec-1"] = (node2, None)

    status2 = await host.get_engine_status("spec-1")
    assert status2.current_equity == Decimal("400")
    assert status2.peak_equity == Decimal("1000")
    # drawdown_pct = (1000 - 400) / 1000 * 100.
    assert isinstance(status2.drawdown_pct, Decimal)
    assert status2.drawdown_pct == Decimal("60")


async def test_nt_host_get_engine_status_unknown_spec_zero() -> None:
    status = await _host().get_engine_status("nope")
    assert status.phase == "unknown"
    assert status.position_count == 0
    assert status.order_count == 0
    assert status.open_notional == Decimal("0")
    assert status.current_equity == Decimal("0")
    assert status.peak_equity == Decimal("0")
    assert status.drawdown_pct == Decimal("0")
