from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from custos.core.engine_protocol import EngineStatus
from custos.core.engine_safety import EngineSafetySupervisor
from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.portfolio_snapshot import (
    NautilusPortfolioPosition,
    NautilusPortfolioSnapshot,
    NautilusPortfolioSnapshotProvider,
)


class _DecimalValue:
    def __init__(self, value: str) -> None:
        self._value = value

    def as_decimal(self) -> Decimal:
        return Decimal(self._value)

    def __str__(self) -> str:
        return self._value


class _InstrumentId:
    venue = "BINANCE"

    def __init__(self, value: str = "BTC-USDT.BINANCE") -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


class _Position:
    def __init__(self) -> None:
        self.instrument_id = _InstrumentId()
        self.quantity = _DecimalValue("2")
        self.avg_px_open = _DecimalValue("90")
        self.settlement_currency = "USDT"
        self.is_short = False
        self.mark_arguments: list[object] = []

    def unrealized_pnl(self, mark_price: object) -> _DecimalValue:
        self.mark_arguments.append(mark_price)
        return _DecimalValue("20")


class _Cache:
    def __init__(self, *, mark_price: object | None) -> None:
        self.position = _Position()
        self._mark_price = mark_price

    def positions_open(self):
        return [self.position]

    def instrument_ids(self):
        return [self.position.instrument_id]

    def mark_price(self, instrument_id):
        return self._mark_price

    def price(self, instrument_id, price_type):
        return None

    def orders_open(self):
        return []


class _Portfolio:
    def __init__(self, equities: dict | None = None, missing: tuple = ()) -> None:
        self._equities = equities if equities is not None else {"USDT": _DecimalValue("1000")}
        self._missing = missing
        self.venues: list[object] = []

    def equity(self, venue):
        self.venues.append(venue)
        return self._equities

    def missing_price_instruments(self, venue):
        return self._missing


class _Node:
    def __init__(self, *, mark_price: object | None, portfolio: _Portfolio | None = None) -> None:
        self.kernel = type(
            "Kernel",
            (),
            {
                "cache": _Cache(mark_price=mark_price),
                "portfolio": portfolio or _Portfolio(),
            },
        )()


def test_provider_calls_unrealized_pnl_with_trusted_mark_without_conversion_syntax() -> None:
    mark = _DecimalValue("100")
    node = _Node(mark_price=mark)
    snapshot = NautilusPortfolioSnapshotProvider(price_type_mid="MID").snapshot(
        node,
        currency="USDT",
    )

    assert snapshot.reliable is True
    assert snapshot.equity == Decimal("1000")
    assert snapshot.open_notional == Decimal("200")
    assert snapshot.positions[0].unrealized_pnl == Decimal("20")
    assert node.kernel.cache.position.mark_arguments == [mark]


def test_a_real_mark_price_update_is_unwrapped_before_it_is_priced_with() -> None:
    """The cache's two price sources return different types, and only one is a price.

    Real testnet evidence, 2026-08-01: with a position open the breaker fail-closed on
    ``portfolio_snapshot_invalid:TypeError`` while ``mark_price_unavailable`` never
    appeared -- so a mark price *was* found, and using it is what threw.

    ``Cache.mark_price()`` returns a ``MarkPriceUpdate``; the fallback
    ``Cache.price(..., MID)`` returns a ``Price``. The snapshot treated both the same and
    handed the wrapper to ``Position.unrealized_pnl()``, which wants a ``Price``.

    The branch had never run: nothing in the tree subscribed mark prices until the fix
    for that landed, so the code always fell through to the ``Price`` branch. This test
    uses the **real** ``MarkPriceUpdate`` deliberately -- the previous fake returned a
    decimal-like object, which is to say it was shaped like the code's assumption rather
    than like NautilusTrader, and no amount of it passing could have found this.
    """
    import pytest

    pytest.importorskip("nautilus_trader")
    from nautilus_trader.model.data import MarkPriceUpdate
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.objects import Price

    price = Price.from_str("100.00")
    update = MarkPriceUpdate(
        instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
        value=price,
        ts_event=1,
        ts_init=1,
    )
    node = _Node(mark_price=update)

    snapshot = NautilusPortfolioSnapshotProvider(price_type_mid="MID").snapshot(
        node,
        currency="USDT",
    )

    assert snapshot.reliable is True, (
        f"snapshot came back unreliable: {snapshot.unreliable_reason} -- the mark price "
        "was found and then could not be used"
    )
    assert snapshot.positions[0].mark_price == Decimal("100.00")
    assert node.kernel.cache.position.mark_arguments == [price], (
        "the Price inside the update must be what prices the position, not the update itself"
    )


def test_missing_mark_or_equity_returns_typed_unreliable_snapshot() -> None:
    missing_mark = NautilusPortfolioSnapshotProvider(price_type_mid="MID").snapshot(
        _Node(mark_price=None),
        currency="USDT",
    )
    missing_equity = NautilusPortfolioSnapshotProvider(price_type_mid="MID").snapshot(
        _Node(mark_price=_DecimalValue("100"), portfolio=_Portfolio(equities={})),
        currency="USDT",
    )

    assert missing_mark.reliable is False
    assert missing_mark.unreliable_reason == "mark_price_unavailable:BTC-USDT.BINANCE"
    assert missing_equity.reliable is False
    assert missing_equity.unreliable_reason == "portfolio_equity_missing:USDT"


@dataclass
class _RecordingProvider:
    value: NautilusPortfolioSnapshot

    def __post_init__(self) -> None:
        self.calls: list[str | None] = []

    def snapshot(self, node: object, *, currency: str | None = None) -> NautilusPortfolioSnapshot:
        self.calls.append(currency)
        return self.value


async def test_host_status_breaker_inputs_and_runner_facts_share_one_provider() -> None:
    position = NautilusPortfolioPosition(
        instrument_id="BTC-USDT.BINANCE",
        settlement_currency="USDT",
        quantity=Decimal("2"),
        avg_px=Decimal("90"),
        mark_price=Decimal("100"),
        unrealized_pnl=Decimal("20"),
        notional=Decimal("200"),
    )
    snapshot = NautilusPortfolioSnapshot(
        venue="BINANCE",
        currency="USDT",
        equity=Decimal("1000"),
        positions=(position,),
        reliable=True,
        unreliable_reason=None,
    )
    provider = _RecordingProvider(snapshot)
    host = NtTradingNodeHost(
        tenant_id="tenant",
        runner_id="runner",
        portfolio_snapshot_provider=provider,
    )
    node = _Node(mark_price=_DecimalValue("100"))
    host._active_nodes["instance"] = (node, None)
    host._runner_fact_contexts["instance"] = (object(), None)
    # deploy registers this beside the node; the guard paths read it so equity resolves
    # on an account that holds more than one currency.
    host._settlement_currencies["instance"] = "USDT"

    assert await host.get_open_notional("instance") == Decimal("200")
    assert (await host.get_positions("instance"))[0].unrealized_pnl == Decimal("20")
    status = await host.get_engine_status("instance")
    equity, rows = await host.runner_fact_risk_snapshot("instance", "USDT")

    assert status.reliable is True
    assert status.current_equity == Decimal("1000")
    assert equity == Decimal("1000")
    assert rows == [
        {
            "instrument": "BTC-USDT.BINANCE",
            "quantity": "2",
            "mark_price": "100",
            "currency": "USDT",
        }
    ]
    # All four declare the currency: the three guard paths take it from what deploy
    # registered, and the RunnerFact path takes it from its caller. None of them may
    # ask an account holding several currencies for "the" equity.
    assert provider.calls == ["USDT", "USDT", "USDT", "USDT"]


async def test_host_marks_inactive_or_unreliable_status_fail_closed() -> None:
    host = NtTradingNodeHost(tenant_id="tenant", runner_id="runner")
    inactive = await host.get_engine_status("missing")
    assert inactive.reliable is False
    assert inactive.unreliable_reason == "deployment_not_active"

    provider = _RecordingProvider(NautilusPortfolioSnapshot.unreliable("portfolio_equity_invalid"))
    host = NtTradingNodeHost(
        tenant_id="tenant",
        runner_id="runner",
        portfolio_snapshot_provider=provider,
    )
    host._active_nodes["instance"] = (_Node(mark_price=None), None)
    status = await host.get_engine_status("instance")
    assert status.phase == "degraded"
    assert status.reliable is False
    assert status.unreliable_reason == "portfolio_equity_invalid"


class _SafetyEngine:
    def __init__(self, status: EngineStatus) -> None:
        self.status = status
        self.status_calls = 0
        self.flattened: list[tuple[str, str]] = []

    async def get_engine_status(self, deployment_instance_id: str) -> EngineStatus:
        self.status_calls += 1
        return self.status

    async def flatten_positions(self, deployment_instance_id: str, reason: str) -> None:
        self.flattened.append((deployment_instance_id, reason))


def _safety_breaker() -> FallbackBreaker:
    return FallbackBreaker(
        FallbackBreakerConfig(
            max_notional=Decimal("100"),
            max_drawdown_pct=Decimal("10"),
        )
    )


async def test_safety_tick_uses_one_status_snapshot_and_flattens_a_breach() -> None:
    engine = _SafetyEngine(
        EngineStatus(
            phase="running",
            position_count=1,
            order_count=0,
            open_notional=Decimal("101"),
            peak_equity=Decimal("1000"),
            current_equity=Decimal("1000"),
            drawdown_pct=Decimal("0"),
        )
    )
    supervisor = EngineSafetySupervisor(engine=engine, breaker=_safety_breaker())

    tick = await supervisor.evaluate_once("instance-1")

    assert engine.status_calls == 1
    assert tick.verdict.reason == "notional_breach"
    assert engine.flattened == [("instance-1", "notional_breach")]


async def test_safety_tick_fails_closed_on_an_unreliable_status() -> None:
    engine = _SafetyEngine(
        EngineStatus(
            phase="degraded",
            position_count=0,
            order_count=0,
            open_notional=Decimal("0"),
            peak_equity=Decimal("0"),
            current_equity=Decimal("0"),
            drawdown_pct=Decimal("0"),
            reliable=False,
            unreliable_reason="portfolio_equity_missing:USDT",
        )
    )
    supervisor = EngineSafetySupervisor(engine=engine, breaker=_safety_breaker())

    tick = await supervisor.evaluate_once("instance-2")

    assert tick.verdict.reason == "portfolio_equity_missing:USDT"
    assert supervisor.breaker.frozen is True
    assert engine.flattened == [("instance-2", "portfolio_equity_missing:USDT")]


# ---------------------------------------------------------------------------
# A funded account holds more than one currency, and equity has to say which one.
#
# Real evidence, 2026-07-30: a testnet account holding USDT + USDC + BTC with
# base_currency=None made every guard that does not declare a currency unreliable,
# the fallback breaker fail closed at startup, and the exposure guard latch. Funding
# the account in more currencies makes this worse, not better.
# ---------------------------------------------------------------------------


def _multi_currency_portfolio() -> _Portfolio:
    """What a funded futures account actually looks like."""
    return _Portfolio(
        equities={
            "USDT": _DecimalValue("4488.58845941"),
            "USDC": _DecimalValue("5000"),
            "BTC": _DecimalValue("0.01"),
        }
    )


def test_equity_is_ambiguous_on_a_multi_currency_account_when_none_is_declared() -> None:
    """The cause, pinned: with no currency asked for, more than one entry cannot resolve."""
    node = _Node(mark_price=_DecimalValue("100"), portfolio=_multi_currency_portfolio())

    snapshot = NautilusPortfolioSnapshotProvider(price_type_mid="MID").snapshot(node)

    assert snapshot.reliable is False
    assert snapshot.unreliable_reason == "portfolio_equity_ambiguous"


def test_declaring_the_currency_resolves_the_same_account() -> None:
    """Same account, same provider -- naming the currency is the whole difference."""
    node = _Node(mark_price=_DecimalValue("100"), portfolio=_multi_currency_portfolio())

    snapshot = NautilusPortfolioSnapshotProvider(price_type_mid="MID").snapshot(
        node, currency="USDT"
    )

    assert snapshot.reliable is True
    assert snapshot.equity == Decimal("4488.58845941")


async def test_the_guard_paths_stay_reliable_on_a_multi_currency_account() -> None:
    """get_open_notional / get_positions / get_engine_status must not go unreliable
    merely because the account is funded in several currencies.

    These three are what the notional cap, the snapshot publisher and the fallback
    breaker read, so an ambiguous answer here is what tripped the breaker at startup.
    """
    node = _Node(mark_price=_DecimalValue("100"), portfolio=_multi_currency_portfolio())
    host = NtTradingNodeHost(
        tenant_id="tenant",
        runner_id="runner",
        portfolio_snapshot_provider=NautilusPortfolioSnapshotProvider(price_type_mid="MID"),
    )
    host._active_nodes["instance"] = (node, None)
    host._settlement_currencies["instance"] = "USDT"

    assert await host.get_open_notional("instance") == Decimal("200")
    assert len(await host.get_positions("instance")) == 1
    status = await host.get_engine_status("instance")

    assert status.reliable is True
    assert status.current_equity == Decimal("4488.58845941")


# ---------------------------------------------------------------------------
# Where the declared currency comes from: the spec's pairs, which are required and
# non-empty, so it is derivable even with no open position -- which is exactly the
# moment the startup guards need it.
# ---------------------------------------------------------------------------


def test_settlement_currency_is_derived_from_the_pairs() -> None:
    from custos.engines.nautilus.settlement import settlement_currency_for_pairs

    assert settlement_currency_for_pairs(["BTC-USDT"]) == "USDT"
    assert settlement_currency_for_pairs(["BTC/USDT", "ETH-USDT"]) == "USDT"


def test_a_deployment_spanning_settlement_currencies_is_refused_by_name() -> None:
    """Equity has no single answer here, and the error must say that rather than
    borrow the word 'ambiguous', which already means something else in this code."""
    import pytest

    from custos.engines.nautilus.settlement import (
        SettlementCurrencyError,
        settlement_currency_for_pairs,
    )

    with pytest.raises(SettlementCurrencyError) as excinfo:
        settlement_currency_for_pairs(["BTC-USDT", "ETH-USDC"])

    message = str(excinfo.value)
    assert "settlement currenc" in message.lower()
    assert "ambiguous" not in message.lower()

    with pytest.raises(SettlementCurrencyError):
        settlement_currency_for_pairs([])
