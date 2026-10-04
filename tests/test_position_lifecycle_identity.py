"""A signed ``position_closed`` names one position lifecycle, not one engine slot.

Under NETTING, NautilusTrader keeps a single position per instrument and strategy and
reuses its id every time the position is reopened. Crucible keeps one closed position
per ``position_id``, so a slot-derived identity makes the second close of the same slot
collide with the first and the settlement projection of the instance stops.

These tests drive real engine runs through the production event forwarding and the
production RunnerFact bridge, and read the facts the bridge hands to its emitter.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest import BacktestEngine  # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountType,
    Currency,
    Money,
    OmsType,
    OrderSide,
    Venue,
)
from nautilus_trader.testkit.providers import TestInstrumentProvider  # noqa: E402
from nautilus_trader.trading import Strategy, StrategyConfig  # noqa: E402

from custos.core.runner_fact import RunnerFactAuthority  # noqa: E402
from custos.core.runner_fact_producer import (  # noqa: E402
    RunnerFactDeployment,
    RunnerFactEventBridge,
)
from custos.engines.nautilus.strategy_event_forwarding import (  # noqa: E402
    StrategyEventForwarder,
)
from tests.fixtures import nt_data_stubs as stubs  # noqa: E402

USDT = Currency.from_str("USDT")
INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()
VENUE = Venue("BINANCE")

# One order per quote: open long, close it, reopen long, flip to short, close the short.
# Under NETTING that is three lifecycles of one position slot; the flip closes the
# second lifecycle and opens the third with the same order.
ROUND_TRIPS = [
    (OrderSide.BUY, 1.0, 100.0),
    (OrderSide.SELL, 1.0, 110.0),
    (OrderSide.BUY, 1.0, 105.0),
    (OrderSide.SELL, 2.0, 120.0),
    (OrderSide.BUY, 1.0, 100.0),
]


def _authority() -> RunnerFactAuthority:
    return RunnerFactAuthority(
        tenant_id="tenant-a",
        trading_mode="sandbox",
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


class _Emitter:
    def __init__(self) -> None:
        self.facts: list[dict[str, object]] = []

    # The bridge restores its order attribution when it is bootstrapped; these runs
    # submit no attributed strategy orders, so there is nothing to remember.
    def recall_orders(self, deployment_instance_id):
        return {}

    def remember_order(self, **_order) -> None: ...

    def forget_order(self, **_order) -> None: ...

    def emit_strategy_signal_sync(self, authority, **signal):
        assert authority == _authority()
        return signal["fact_id"]

    def emit_sync(self, authority, facts):
        assert authority == _authority()
        self.facts.extend(facts)
        return tuple(facts)


class _RoundTrips(Strategy):
    # 2.0's Strategy is a pyclass that initialises in __new__; absorb the extra argument.
    def __new__(cls, config: StrategyConfig, **kwargs: object):
        return super().__new__(cls, config)

    def __init__(self, config: StrategyConfig, *, hedging: bool) -> None:
        super().__init__(config)
        self._hedging = hedging
        self._step = 0
        self.closed_slots: list[str] = []

    def on_start(self) -> None:
        self.subscribe_quotes(INSTRUMENT.id)

    def on_quote(self, tick) -> None:
        if self._step >= len(ROUND_TRIPS):
            return
        side, quantity, _price = ROUND_TRIPS[self._step]
        self._step += 1
        if self._hedging and self._step == 4:
            # HEDGING has no flip: the fourth order only closes the second long.
            quantity = 1.0
        order = self.order_factory.market(
            instrument_id=INSTRUMENT.id,
            order_side=side,
            quantity=INSTRUMENT.make_qty(quantity),
        )
        if self._hedging and self._step in (2, 4):
            # HEDGING only reduces a position when told which one.
            (position,) = [
                position
                for position in self.cache.positions_open(instrument_id=INSTRUMENT.id)
                if position.entry != side
            ]
            self.submit_order(order, position_id=position.id)
        else:
            self.submit_order(order)

    def on_position_event(self, event) -> None:
        if type(event).__name__ == "PositionClosed":
            self.closed_slots.append(str(event.position_id))


def _run(oms: OmsType) -> tuple[list[dict[str, object]], list[str]]:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True)))
    engine.add_venue(
        venue=VENUE,
        oms_type=oms,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(INSTRUMENT)
    strategy = _RoundTrips(StrategyConfig(), hedging=oms == OmsType.HEDGING)
    emitter = _Emitter()
    failures: list[tuple[str, str]] = []
    forwarder = StrategyEventForwarder(
        deployment_instance_id=_deployment().deployment_instance_id,
        on_sink_failure=lambda sink, reason: failures.append((sink, reason)),
    )
    RunnerFactEventBridge(emitter=emitter, deployment=_deployment()).bootstrap(forwarder)
    forwarder.install(strategy)
    engine.add_strategy(strategy)
    ticks = []
    for index, (_side, _quantity, price) in enumerate(ROUND_TRIPS * 2):
        ts = (index + 1) * 1_000_000_000
        ticks.append(
            stubs.quote_tick(INSTRUMENT, bid_price=price, ask_price=price, ts_event=ts, ts_init=ts)
        )
    engine.add_data(ticks)
    engine.run()
    closed_slots = list(strategy.closed_slots)
    engine.dispose()
    assert failures == []
    return [fact for fact in emitter.facts if fact["kind"] == "position_closed"], closed_slots


def test_netting_round_trips_on_one_slot_close_distinct_positions() -> None:
    closes, slots = _run(OmsType.NETTING)

    # The engine really did reuse one slot for all three lifecycles.
    assert len(slots) == 3
    assert len(set(slots)) == 1
    assert len(closes) == 3
    assert len({fact["position_id"] for fact in closes}) == 3
    assert len({fact["event_id"] for fact in closes}) == 3


def test_netting_round_trips_each_carry_only_their_own_realized_pnl() -> None:
    closes, _slots = _run(OmsType.NETTING)

    # Gross per lifecycle: +10 long, +15 long, +20 short, each less a few cents of fee.
    # A cumulative figure would read about 25 and 45 for the later two.
    pnls = [Decimal(str(fact["realized_pnl"])) for fact in closes]
    assert Decimal("9") < pnls[0] < Decimal("10")
    assert Decimal("14") < pnls[1] < Decimal("15")
    assert Decimal("19") < pnls[2] < Decimal("20")
    opened = [fact["opened_at"] for fact in closes]
    assert opened == sorted(opened)
    assert len(set(opened)) == 3


def test_hedging_positions_keep_distinct_identities() -> None:
    closes, slots = _run(OmsType.HEDGING)

    assert len(closes) == len(slots) >= 2
    assert len(set(slots)) == len(slots)
    assert len({fact["position_id"] for fact in closes}) == len(closes)


def _native_close(
    *,
    position_id: str,
    opening_order: str,
    closing_order: str,
    opened_ns: int,
    closed_ns: int,
    event_id=None,
):
    """A native PositionClosed for one long round trip on ``position_id``."""
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import (
        AccountId,
        ClientOrderId,
        LiquiditySide,
        OrderFilled,
        OrderType,
        Position,
        PositionClosed,
        PositionId,
        Price,
        Quantity,
        StrategyId,
        TradeId,
        TraderId,
        VenueOrderId,
    )

    def fill(order: str, side, price: str, ts: int):
        return OrderFilled(
            TraderId("TRADER-001"),
            StrategyId("TEST-001"),
            INSTRUMENT.id,
            ClientOrderId(order),
            VenueOrderId(order),
            AccountId("BINANCE-001"),
            TradeId(f"{order}-{ts}"),
            side,
            OrderType.MARKET,
            Quantity.from_str("1.000"),
            Price.from_str(price),
            USDT,
            LiquiditySide.TAKER,
            UUID4(),
            ts,
            ts,
            False,
            position_id=PositionId(position_id),
            commission=Money.from_str("0.01 USDT"),
        )

    position = Position(INSTRUMENT, fill(opening_order, OrderSide.BUY, "100.00", opened_ns))
    closing = fill(closing_order, OrderSide.SELL, "101.00", closed_ns)
    position.apply(closing)
    return PositionClosed.create(position, closing, event_id or UUID4(), closed_ns)


def _bridge_facts(*events) -> list[dict[str, object]]:
    emitter = _Emitter()
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())
    for event in events:
        bridge._on_position_event(event)
    return emitter.facts


def test_lifecycles_opened_in_the_same_nanosecond_stay_distinct() -> None:
    # A simulated bar can open, close and reopen one slot at a single timestamp.
    first = _native_close(
        position_id="BTCUSDT-PERP.BINANCE-TEST-000",
        opening_order="order-1",
        closing_order="order-2",
        opened_ns=5_000_000_000,
        closed_ns=5_000_000_000,
    )
    second = _native_close(
        position_id="BTCUSDT-PERP.BINANCE-TEST-000",
        opening_order="order-3",
        closing_order="order-4",
        opened_ns=5_000_000_000,
        closed_ns=6_000_000_000,
    )

    facts = _bridge_facts(first, second)

    assert [fact["opened_at"] for fact in facts] == ["1970-01-01T00:00:05Z"] * 2
    assert facts[0]["position_id"] != facts[1]["position_id"]


def test_a_replayed_close_derives_the_same_fact() -> None:
    from nautilus_trader.core import UUID4

    event_id = UUID4()
    close = _native_close(
        position_id="BTCUSDT-PERP.BINANCE-TEST-000",
        opening_order="order-1",
        closing_order="order-2",
        opened_ns=1_000_000_000,
        closed_ns=2_000_000_000,
        event_id=event_id,
    )
    redelivered = _native_close(
        position_id="BTCUSDT-PERP.BINANCE-TEST-000",
        opening_order="order-1",
        closing_order="order-2",
        opened_ns=1_000_000_000,
        closed_ns=2_000_000_000,
        event_id=event_id,
    )

    first, second = _bridge_facts(close, redelivered)

    assert first == second


def test_a_close_without_its_opening_order_is_refused() -> None:
    from custos.core.runner_fact import RunnerFactContractError

    class PositionClosed:
        @staticmethod
        def to_dict(_event):
            return {
                "event_id": "80000000-0000-4000-8000-000000000001",
                "position_id": "BTCUSDT-PERP.BINANCE-TEST-000",
                "realized_pnl": "1 USDT",
                "ts_opened": 1_000_000_000,
                "ts_closed": 2_000_000_000,
            }

    emitter = _Emitter()
    bridge = RunnerFactEventBridge(emitter=emitter, deployment=_deployment())
    with pytest.raises(RunnerFactContractError, match="lifecycle identity"):
        bridge._on_position_event(PositionClosed())
    assert emitter.facts == []
