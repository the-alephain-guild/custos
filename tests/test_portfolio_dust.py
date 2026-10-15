"""Dust counts as flat for safety and stays unsigned while it has no price.

Dust is a holding no order can take off the book: below the pair's minimum order
quantity, or -- when a price is known -- worth less than its minimum notional. The
judgement is the toolkit's (``custos_toolkit.position.dust``); these tests are the
runner's reading of it, against the same boundary vectors the toolkit reads.

Two views of one snapshot. The safety view (breaker, readiness, containment,
engine status) leaves unpriced dust out of equity and exposure and stays
reliable: nothing the breaker could do would change a holding no order can
reach. The signed view (RunnerFact valuations, the stop boundary) refuses to
value without it, as before, and a signed runtime log says why. A real holding
without a price fails closed in both.

The sample is account 60366 on SoDEX spot testnet: a 0.00000935 vBTC balance that
NautilusTrader rounds to a 0.00001 position, on a pair whose step and minimum
quantity are 0.00001 and whose minimum notional is 5 vUSDC.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from custos.core.fallback_breaker import FallbackBreakerConfig
from custos.engines.nautilus import host as nautilus_host
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.portfolio_snapshot import NautilusPortfolioSnapshotProvider
from custos.offline.safety import OfflineExposureGuard
from tests.fixtures.fake_live_node import FakeLiveNodeType
from tests.test_nt_trading_node_host import _Artifact, _credential, _ShutdownAwareStrategy, _spec

_VECTORS = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "dust_vectors.json").read_text()
)

VBTC = "vBTC_vUSDC.SODEX_SPOT"
VETH = "vETH_vUSDC.SODEX_SPOT"


class _Value:
    """A Quantity, Price or Money: exact, and only ever handed out as a Decimal."""

    def __init__(self, value: str, currency: str | None = None) -> None:
        self._value = value
        self.currency = currency

    def as_decimal(self) -> Decimal:
        return Decimal(self._value)

    def __str__(self) -> str:
        return self._value


class _InstrumentId:
    def __init__(self, value: str) -> None:
        self._value = value
        self.venue = value.rsplit(".", 1)[1]

    def __str__(self) -> str:
        return self._value

    def __eq__(self, other: object) -> bool:
        return str(other) == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass
class _Pair:
    instrument_id: str
    base: str
    quote: str
    size_increment: str
    min_quantity: str | None
    min_notional: str | None

    def instrument(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=_InstrumentId(self.instrument_id),
            base_currency=self.base,
            quote_currency=self.quote,
            instrument_class="SPOT",
            size_increment=_Value(self.size_increment),
            min_quantity=None if self.min_quantity is None else _Value(self.min_quantity),
            min_notional=(
                None if self.min_notional is None else _Value(self.min_notional, self.quote)
            ),
        )


SODEX_VBTC = _Pair(VBTC, "VBTC", "VUSDC", "0.00001", "0.00001", "5")
SODEX_VETH = _Pair(VETH, "VETH", "VUSDC", "0.0001", "0.0001", "5")


class _Position:
    def __init__(self, instrument_id: str, quantity: str) -> None:
        self.instrument_id = _InstrumentId(instrument_id)
        self.quantity = _Value(quantity)
        self.settlement_currency = "VUSDC"
        self.is_short = False
        self.is_long = True
        self.is_closed = False
        self.strategy_id = "S-1"
        self.avg_px_open = _Value("80000")

    def unrealized_pnl(self, mark: object) -> _Value:
        return _Value("0")


class _Account:
    def __init__(self, account_type: str, balances: dict[str, str]) -> None:
        self.account_type = account_type
        self._balances = balances

    def balance_total(self, currency: object) -> _Value | None:
        amount = self._balances.get(str(currency).upper())
        return None if amount is None else _Value(amount, str(currency).upper())


class _DustyCache:
    """The parts of the Nautilus cache a valuation reads, with nothing priced unless told."""

    def __init__(
        self,
        *,
        pairs: tuple[_Pair, ...],
        positions: list[_Position],
        account: _Account,
        prices: dict[str, str] | None = None,
    ) -> None:
        self.pairs = {pair.instrument_id: pair for pair in pairs}
        self.positions = positions
        self.orders: list = []
        self.account = account
        self.prices = dict(prices or {})

    def positions_open(self, instrument_id=None) -> list:
        if instrument_id is None:
            return list(self.positions)
        return [item for item in self.positions if item.instrument_id == instrument_id]

    def orders_open(self, instrument_id=None) -> list:
        return list(self.orders)

    def instrument_ids(self) -> list[_InstrumentId]:
        return [_InstrumentId(value) for value in self.pairs]

    def instrument(self, instrument_id) -> SimpleNamespace | None:
        pair = self.pairs.get(str(instrument_id))
        return None if pair is None else pair.instrument()

    def account_for_venue(self, venue) -> _Account:
        return self.account

    def mark_price(self, instrument_id):
        return None

    def price(self, instrument_id, price_type):
        price = self.prices.get(str(instrument_id))
        return None if price is None else _Value(price)

    def quote(self, instrument_id):
        return None

    def trade(self, instrument_id):
        return None


class _Portfolio:
    """Equity is the balances; unpriced open positions are reported, as the engine does."""

    def __init__(self, cache: _DustyCache, balances: dict[str, str]) -> None:
        self._cache = cache
        self._balances = balances
        self.initialized = True
        self.is_initialized = True

    def equity(self, venue) -> dict[str, _Value]:
        return {currency: _Value(amount) for currency, amount in self._balances.items()}

    def missing_price_instruments(self, venue) -> list[_InstrumentId]:
        return [
            position.instrument_id
            for position in self._cache.positions
            if str(position.instrument_id) not in self._cache.prices
        ]


@dataclass
class _Runtime:
    cache: _DustyCache
    portfolio: _Portfolio
    strategies: tuple = ()


def _account_60366(
    *,
    price: str | None = None,
    position: str = "0.00001",
    balance: str = "0.00000935",
    extra: dict[str, str] | None = None,
    extra_positions: tuple[_Position, ...] = (),
    pairs: tuple[_Pair, ...] = (SODEX_VBTC,),
    account_type: str = "CASH",
) -> _Runtime:
    balances = {"VUSDC": "1000", "VBTC": balance, **(extra or {})}
    positions = [_Position(VBTC, position), *extra_positions] if position is not None else []
    prices = {} if price is None else {VBTC: price}
    cache = _DustyCache(
        pairs=pairs,
        positions=positions,
        account=_Account(account_type, balances),
        prices=prices,
    )
    return _Runtime(cache=cache, portfolio=_Portfolio(cache, balances))


def _provider() -> NautilusPortfolioSnapshotProvider:
    return NautilusPortfolioSnapshotProvider(
        price_type_mid="MID", price_type_last="LAST", dust_counts_as_flat=lambda venue: True
    )


# --------------------------------------------------------------------------- vectors


def _vector_runtime(vector: dict) -> _Runtime:
    spec = _VECTORS["instruments"][vector["instrument"]]
    pair = _Pair(
        "PAIR_QUOTE.SODEX_SPOT",
        "BASE",
        "QUOTE",
        spec["size_increment"],
        spec["min_quantity"],
        spec["min_notional"],
    )
    balance = vector["base_balance"]
    balances = {"QUOTE": "1000"} if balance is None else {"QUOTE": "1000", "BASE": balance}
    cache = _DustyCache(
        pairs=(pair,),
        positions=[_Position(pair.instrument_id, vector["position_quantity"])],
        account=_Account("MARGIN" if balance is None else "CASH", balances),
        prices={} if vector["price"] is None else {pair.instrument_id: vector["price"]},
    )
    return _Runtime(cache=cache, portfolio=_Portfolio(cache, balances))


@pytest.mark.parametrize("vector", _VECTORS["dust"], ids=lambda vector: vector["name"])
def test_the_runner_reads_the_shared_dust_vectors(vector: dict) -> None:
    """The runner's reading of the cache reaches the toolkit's verdict on every vector."""
    runtime = _vector_runtime(vector)

    verdicts = _provider().position_dust(runtime)

    if not vector["dust"]:
        assert verdicts == {}, f"a real holding was called dust: {verdicts}"
        return
    (holding,) = verdicts.values()
    assert holding.quantity_source == vector["expected_quantity_source"]
    assert holding.reason == vector["reason"]
    assert holding.threshold == Decimal(vector["threshold"])
    assert holding.threshold_source == vector["threshold_source"]
    side = vector["expected_quantity_source"]
    assert holding.quantity == Decimal(
        vector["position_quantity" if side == "position" else "base_balance"]
    )
    assert holding.price == (None if vector["price"] is None else Decimal(vector["price"]))


# --------------------------------------------------------------------------- snapshot


def test_unpriced_dust_leaves_the_safety_view_reliable() -> None:
    """The account 60366 remainder with no price at all."""
    snapshot = _provider().snapshot(_account_60366(), currency="VUSDC")

    assert snapshot.reliable, snapshot.unreliable_reason
    assert snapshot.equity == Decimal("1000"), "unpriced dust is left out of equity"
    assert snapshot.open_notional == Decimal("0"), "and out of exposure"
    assert snapshot.positions == ()
    (holding,) = snapshot.dust
    assert holding.instrument_id == VBTC
    assert holding.asset == "VBTC"
    assert holding.quantity == Decimal("0.00000935")
    assert holding.quantity_source == "balance"
    assert holding.threshold == Decimal("0.00001")
    assert holding.threshold_source == "size_increment"
    assert holding.price is None
    assert snapshot.signable is False, "a valuation without it cannot be signed"


def test_an_unpriced_real_holding_still_fails_closed() -> None:
    """Half a vBTC without a price is not dust, so it stops everything as before."""
    snapshot = _provider().snapshot(_account_60366(position="0.5", balance="0.5"), currency="VUSDC")

    assert snapshot.reliable is False
    assert snapshot.unreliable_reason.startswith(
        ("portfolio_prices_missing:", "mark_price_unavailable:", "asset_conversion_unavailable:")
    )
    assert VBTC in snapshot.unreliable_reason
    assert snapshot.dust == ()


def test_an_unpriced_real_balance_names_the_conversion_it_lacks() -> None:
    """The same for inventory alone: a balance with no position and no price."""
    snapshot = _provider().snapshot(_account_60366(position=None, balance="0.5"), currency="VUSDC")

    assert snapshot.reliable is False
    assert snapshot.unreliable_reason == "asset_conversion_unavailable:VBTC/VUSDC"


def test_a_quantity_of_exactly_the_minimum_is_not_dust() -> None:
    """A balance and a position of exactly the minimum are real holdings."""
    snapshot = _provider().snapshot(
        _account_60366(position="0.00001", balance="0.00001"), currency="VUSDC"
    )

    assert snapshot.reliable is False
    assert snapshot.dust == ()


def test_a_dust_position_beside_real_inventory_values_the_inventory() -> None:
    """A position rounded to nothing is dust; the account's real inventory is not."""
    unpriced = _provider().snapshot(_account_60366(position="0", balance="0.5"), currency="VUSDC")
    assert unpriced.reliable is False, "the inventory still needs a price"
    assert unpriced.unreliable_reason == "asset_conversion_unavailable:VBTC/VUSDC"

    priced = _provider().snapshot(
        _account_60366(position="0", balance="0.5", price="82962"), currency="VUSDC"
    )
    assert priced.reliable, priced.unreliable_reason
    assert priced.equity == Decimal("1000") + Decimal("0.5") * Decimal("82962")
    (holding,) = priced.dust
    assert holding.quantity_source == "position"
    assert holding.quantity == Decimal("0")


def test_priced_dust_is_valued_and_listed() -> None:
    """A price makes dust ordinary to value, and it is still named as dust."""
    snapshot = _provider().snapshot(_account_60366(price="82962"), currency="VUSDC")

    assert snapshot.reliable, snapshot.unreliable_reason
    assert snapshot.signable is True
    assert snapshot.equity == Decimal("1000") + Decimal("0.00000935") * Decimal("82962")
    (position,) = snapshot.positions
    assert position.quantity == Decimal("0.00001")
    assert position.mark_price == Decimal("82962")
    (holding,) = snapshot.dust
    assert holding.price == Decimal("82962")
    assert holding.reason == "notional"
    assert {row["asset"] for row in snapshot.cash_inventory} == {"VBTC", "VUSDC"}


def test_a_holding_worth_less_than_the_minimum_notional_is_dust() -> None:
    """The decided minNotional case: 0.00004 vBTC at 82962 is 3.32 vUSDC, under 5."""
    priced = _provider().snapshot(
        _account_60366(position="0.00004", balance="0.00004", price="82962"), currency="VUSDC"
    )
    assert priced.reliable, priced.unreliable_reason
    (holding,) = priced.dust
    assert holding.reason == "notional"
    assert holding.quantity == Decimal("0.00004")

    unpriced = _provider().snapshot(
        _account_60366(position="0.00004", balance="0.00004"), currency="VUSDC"
    )
    assert unpriced.reliable is False, "without a price only the quantity counts"
    assert unpriced.dust == ()


def test_dust_beside_an_unpriced_real_holding_still_fails_closed() -> None:
    """Unpriced dust excuses itself and nothing else: the real holding is named."""
    runtime = _account_60366(
        extra={"VETH": "2"},
        extra_positions=(_Position(VETH, "2"),),
        pairs=(SODEX_VBTC, SODEX_VETH),
    )

    snapshot = _provider().snapshot(runtime, currency="VUSDC")

    assert snapshot.reliable is False
    assert snapshot.unreliable_reason == f"portfolio_prices_missing:{VETH}"
    assert [holding.instrument_id for holding in snapshot.dust] == [VBTC]


def test_a_venue_that_keeps_dust_real_fails_closed_on_it() -> None:
    """OKX: one minimum order is worth up to 32 USDT there, too much to wave through."""
    provider = NautilusPortfolioSnapshotProvider(
        price_type_mid="MID", price_type_last="LAST", dust_counts_as_flat=lambda venue: False
    )

    snapshot = provider.snapshot(_account_60366(), currency="VUSDC")

    assert snapshot.reliable is False
    assert snapshot.dust == ()
    assert provider.position_dust(_account_60366()) == {}


def test_venues_declare_whether_dust_counts_as_flat() -> None:
    from custos.engines.nautilus import venue_binance, venue_okx, venue_sodex

    assert venue_sodex.dust_counts_as_flat() is True
    assert venue_binance.dust_counts_as_flat() is True
    assert venue_okx.dust_counts_as_flat() is False


# --------------------------------------------------------------------------- host


def _registered(host: NtTradingNodeHost, instance: str, runtime: _Runtime) -> None:
    host._active_nodes[instance] = nautilus_host._NodeRuntime(  # noqa: SLF001
        node=None,
        task=asyncio.get_event_loop().create_future(),
        handle=None,
        cache=runtime.cache,
        portfolio=runtime.portfolio,
        strategies=runtime.strategies,
        reconciliation_enabled=True,
    )
    host._settlement_currencies[instance] = "VUSDC"  # noqa: SLF001


async def test_the_engine_status_counts_dust_and_stays_reliable() -> None:
    """At the host: reliable, the position counted, the dust carried in the status."""
    host = NtTradingNodeHost()
    _registered(host, "dusty", _account_60366())

    status = await host.get_engine_status("dusty")

    assert status.reliable, status.unreliable_reason
    assert status.position_count == 1
    assert [holding.quantity for holding in status.dust] == [Decimal("0.00000935")]
    assert await host.get_open_notional("dusty") == Decimal("0")


async def test_readiness_finds_the_valuation_ready_beside_unpriced_dust() -> None:
    """The valuation readiness waits for does not wait on dust."""
    from nautilus_trader.live import NodeState

    from tests.engines.nautilus.test_readiness_checks_what_it_claims import _authority, _Task

    host = NtTradingNodeHost()
    authority = _authority("testnet")
    instance = str(authority.deployment_instance_id)
    dusty = _account_60366()
    runtime = nautilus_host._NodeRuntime(  # noqa: SLF001
        node=None,
        task=_Task(),
        handle=SimpleNamespace(state=NodeState.RUNNING),
        cache=dusty.cache,
        portfolio=dusty.portfolio,
        strategies=(SimpleNamespace(is_running=True),),
        reconciliation_enabled=True,
    )
    host._active_nodes[instance] = runtime  # noqa: SLF001
    host._settlement_currencies[instance] = "VUSDC"  # noqa: SLF001

    checks = await host._readiness_checks(authority, runtime)  # noqa: SLF001

    assert checks.portfolio_valuation_ready is True


async def test_an_unreliable_status_still_counts_the_positions_the_cache_holds() -> None:
    """A status that cannot be valued reports what is open, not zero."""
    host = NtTradingNodeHost()
    _registered(host, "real", _account_60366(position="0.5", balance="0.5"))

    status = await host.get_engine_status("real")

    assert status.reliable is False
    assert status.position_count == 1


async def test_the_signed_view_refuses_unpriced_dust() -> None:
    """The three RunnerFact snapshots and the stop boundary still refuse to value it."""
    host = NtTradingNodeHost()
    _registered(host, "dusty", _account_60366())
    host._runner_fact_contexts["dusty"] = (object(), None)  # noqa: SLF001

    for read in (
        host.runner_fact_risk_snapshot,
        host.runner_fact_valuation_snapshot,
        host.runner_fact_cash_snapshot,
    ):
        with pytest.raises(RuntimeError, match="portfolio_dust_unpriced"):
            await read("dusty", "VUSDC")

    valuation, failure = host._value_stop_boundary(  # noqa: SLF001
        "dusty",
        host._active_nodes["dusty"],
        "VUSDC",  # noqa: SLF001
    )
    assert valuation is None
    assert failure == "valuation_unreliable"


async def test_the_signed_view_values_priced_dust() -> None:
    host = NtTradingNodeHost()
    _registered(host, "priced", _account_60366(price="82962"))
    host._runner_fact_contexts["priced"] = (object(), None)  # noqa: SLF001

    equity, rows = await host.runner_fact_risk_snapshot("priced", "VUSDC")

    assert equity == Decimal("1000") + Decimal("0.00000935") * Decimal("82962")
    assert [row["quantity"] for row in rows] == ["0.00001"]


@dataclass
class _RuntimeLogs:
    emitted: list[dict] = field(default_factory=list)

    def emit_sync(self, authority, **event) -> None:
        self.emitted.append({"authority": authority, **event})


async def test_unpriced_dust_is_signed_once_per_change() -> None:
    """The signed view says why it went quiet, once for each new set of dust."""
    host = NtTradingNodeHost()
    runtime = _account_60366()
    _registered(host, "dusty", runtime)
    authority = SimpleNamespace(stream_key="stream", deployment_instance_id="dusty")
    host._runner_fact_contexts["dusty"] = (SimpleNamespace(authority=authority), None)  # noqa: SLF001
    logs = _RuntimeLogs()
    host._runtime_logs = logs  # noqa: SLF001

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await host.runner_fact_risk_snapshot("dusty", "VUSDC")
    runtime.cache.account._balances["VBTC"] = "0.000005"
    runtime.portfolio._balances["VBTC"] = "0.000005"
    with pytest.raises(RuntimeError):
        await host.runner_fact_risk_snapshot("dusty", "VUSDC")

    assert [event["message"] for event in logs.emitted] == [
        "portfolio_dust_unpriced",
        "portfolio_dust_unpriced",
    ]
    first = logs.emitted[0]
    assert first["authority"] is authority
    assert first["level"] == "WARN"
    assert first["structured_fields"]["holdings"] == [
        {
            "instrument_id": VBTC,
            "asset": "VBTC",
            "quantity": "0.00000935",
            "quantity_source": "balance",
            "threshold": "0.00001",
            "threshold_source": "size_increment",
        }
    ]
    assert logs.emitted[1]["structured_fields"]["holdings"][0]["quantity"] == "0.000005"


async def test_the_guard_does_not_trip_on_unpriced_dust() -> None:
    """Within its ceilings, an account holding only dust stays running."""
    host = NtTradingNodeHost()
    _registered(host, "dusty", _account_60366())
    guard = OfflineExposureGuard(engine=host, readiness_timeout=0)
    guard.watch("spec", "dusty", FallbackBreakerConfig.strictest_local_fallback("testnet"))

    (tick,) = await guard.evaluate_once()

    assert tick.status.reliable, tick.status.unreliable_reason
    assert not tick.verdict.tripped
    assert guard.allows_new_generations()


async def test_the_guard_still_trips_on_an_unpriced_real_holding() -> None:
    """Without a price, a real holding still trips the guard."""
    host = NtTradingNodeHost()
    host._containment_confirm_secs = 0.02  # noqa: SLF001
    host._shutdown_poll_secs = 0.01  # noqa: SLF001
    _registered(host, "real", _account_60366(position="0.5", balance="0.5"))
    guard = OfflineExposureGuard(engine=host, readiness_timeout=0)
    guard.watch("spec", "real", FallbackBreakerConfig.strictest_local_fallback("testnet"))

    (tick,) = await guard.evaluate_once()

    assert tick.verdict.tripped
    assert not guard.allows_new_generations()


# --------------------------------------------------------------------------- containment


@pytest.fixture
def deployed_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nautilus_host, "LiveNode", FakeLiveNodeType)
    built = NtTradingNodeHost()
    built._shutdown_poll_secs = 0.01  # noqa: SLF001 - fixture wiring
    built._containment_confirm_secs = 0.1  # noqa: SLF001 - fixture wiring
    built._containment_retry_secs = 0  # noqa: SLF001 - fixture wiring
    return built


async def _deploy_into(host: NtTradingNodeHost, label: str, runtime: _Runtime):
    """Deploy a strategy, then give its node the dusty account as its cache."""
    strategy = _ShutdownAwareStrategy()
    spec = _spec(label, trading_mode="testnet")
    await host.deploy(spec, _credential(), _Artifact(strategy=strategy))
    instance = spec["deployment_instance_id"]
    node = host._active_nodes[instance].node  # noqa: SLF001
    cache = node.cache
    for name in (
        "instrument_ids",
        "instrument",
        "account_for_venue",
        "mark_price",
        "price",
        "quote",
        "trade",
    ):
        setattr(cache, name, getattr(runtime.cache, name))
    cache.positions[:] = runtime.cache.positions
    strategy.node = node
    return strategy, instance


async def test_containment_leaves_unpriced_dust_alone_and_confirms(deployed_host) -> None:
    """No close is sent for dust, and containment does not wait for it to go."""
    strategy, instance = await _deploy_into(deployed_host, "dust-contain", _account_60366())

    with capture_logs() as events:
        await deployed_host.flatten_positions(instance, "drawdown_breach")

    assert strategy.closed == []
    names = [event["event"] for event in events]
    assert "nt_containment_confirmed" in names
    assert "nt_containment_not_confirmed" not in names


async def test_containment_leaves_priced_dust_alone_too(deployed_host) -> None:
    """A price does not make dust closable."""
    strategy, instance = await _deploy_into(
        deployed_host, "dust-priced", _account_60366(price="82962")
    )

    with capture_logs() as events:
        await deployed_host.flatten_positions(instance, "drawdown_breach")

    assert strategy.closed == []
    assert "nt_containment_confirmed" in [event["event"] for event in events]


async def test_containment_still_closes_a_real_holding_beside_dust(deployed_host) -> None:
    runtime = _account_60366(
        extra={"VETH": "2"},
        extra_positions=(_Position(VETH, "2"),),
        pairs=(SODEX_VBTC, SODEX_VETH),
    )
    strategy, instance = await _deploy_into(deployed_host, "dust-and-real", runtime)

    await deployed_host.flatten_positions(instance, "drawdown_breach")

    assert [str(instrument) for instrument in strategy.closed] == [VETH]


async def test_containment_closes_a_dust_position_beside_real_inventory_never(
    deployed_host,
) -> None:
    """The position is dust even though the account holds real inventory."""
    strategy, instance = await _deploy_into(
        deployed_host, "dust-inventory", _account_60366(position="0", balance="0.5")
    )

    await deployed_host.flatten_positions(instance, "drawdown_breach")

    assert strategy.closed == []


async def test_a_flatten_stop_leaves_dust_alone_and_confirms(deployed_host) -> None:
    """The flatten shutdown counts dust as flat and the node comes down."""
    strategy, instance = await _deploy_into(deployed_host, "dust-flatten", _account_60366())
    deployed_host._shutdown_policies[instance] = nautilus_host._ShutdownPolicy(  # noqa: SLF001
        position_policy="flatten", confirmation_timeout_secs=1.0
    )

    with capture_logs() as events:
        await deployed_host.stop(instance)

    assert strategy.closed == []
    assert "nt_shutdown_flatten_confirmed" in [event["event"] for event in events]
    assert not deployed_host.attached(instance)
    assert [holding["quantity"] for holding in deployed_host.stop_dust(instance)] == ["0.00000935"]


async def test_a_venue_that_keeps_dust_real_closes_it(deployed_host, monkeypatch) -> None:
    """OKX keeps no exemption: containment asks for the close as before."""
    from custos.engines.nautilus import venue_sodex

    monkeypatch.setattr(venue_sodex, "dust_counts_as_flat", lambda: False)
    strategy, instance = await _deploy_into(deployed_host, "dust-okx", _account_60366())

    await deployed_host.flatten_positions(instance, "drawdown_breach")

    assert [str(instrument) for instrument in strategy.closed] == [VBTC]
