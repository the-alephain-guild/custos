"""The runner's safety gate reads a plain cash-account close as reducing exposure.

On a cash account the toolkit closes with plain orders, because spot adapters deny
reduce-only. That is only safe to ship if the runner's gate -- which refuses a plain
``close_position`` outright -- recognises a plain *submitted* close for what it is: an
order that can only take away exposure that is still there. Otherwise a frozen breaker
would refuse the very close that contains the position.

Real NautilusTrader types throughout: a cash account on a ``BacktestEngine`` holding a
real long position, real market orders from the strategy's own order factory, and the
gate's semantics reading that engine's cache.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
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
    QuoteTick,
    TimeInForce,
    Venue,
)
from nautilus_trader.testkit.providers import TestInstrumentProvider  # noqa: E402
from nautilus_trader.trading import Strategy, StrategyConfig  # noqa: E402

from custos.core.execution_reservation_store import PlatformReservationStore
from custos.core.fallback_breaker import (  # noqa: E402
    FallbackBreaker,
    FallbackBreakerConfig,
)
from custos.core.order_reservation_boundary import RunnerReservationBoundary  # noqa: E402
from custos.engines.nautilus.runner_safety import (  # noqa: E402
    NautilusCachedOrderSemantics,
    RunnerSafetyOrderGate,
)

_SPOT = TestInstrumentProvider.btcusdt_binance()
_VENUE = Venue("BINANCE")


class _HoldsAndDrafts(Strategy):
    """Buy 0.0063 BTC, then draft -- but never send -- the closes under test."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self._quotes = 0
        self.drafts: dict[str, object] = {}

    def on_start(self) -> None:
        self.subscribe_quotes(_SPOT.id)

    def on_quote(self, tick) -> None:
        self._quotes += 1
        if self._quotes == 1:
            self.submit_order(self._market(OrderSide.BUY, "0.0063"))
        elif self._quotes == 3:
            self.drafts = {
                "whole": self._market(OrderSide.SELL, "0.0063"),
                "part": self._market(OrderSide.SELL, "0.0021"),
                "more_than_held": self._market(OrderSide.SELL, "0.0064"),
                "buy": self._market(OrderSide.BUY, "0.0063"),
            }

    def _market(self, side: OrderSide, quantity: str):
        return self.order_factory.market(
            instrument_id=_SPOT.id,
            order_side=side,
            quantity=_SPOT.make_qty(float(quantity)),
            time_in_force=TimeInForce.IOC,
            reduce_only=False,
        )


@pytest.fixture(scope="module")
def held():
    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True), run_analysis=False)
    )
    engine.add_venue(
        _VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money.from_str("100000 USDT"), Money(0, Currency.from_str("BTC"))],
    )
    engine.add_instrument(_SPOT)
    px = _SPOT.make_price(80_000)
    size = _SPOT.make_qty(10)
    engine.add_data(
        [QuoteTick(_SPOT.id, px, px, size, size, n * 10**9, n * 10**9) for n in (1, 2, 3, 4)]
    )
    strategy = _HoldsAndDrafts(StrategyConfig())
    engine.add_strategy(strategy)
    engine.run()
    positions = engine.cache.positions_open(instrument_id=_SPOT.id)
    assert len(positions) == 1 and positions[0].is_long, "precondition: one long position"
    assert engine.cache.account_for_venue(_VENUE).account_type == AccountType.CASH
    yield engine, strategy.drafts
    engine.dispose()


def test_the_runner_gate_reads_a_cash_exit_as_reducing(held) -> None:
    engine, drafts = held
    semantics = NautilusCachedOrderSemantics(engine.cache)

    assert drafts["whole"].is_reduce_only is False
    assert semantics.order_is_risk_reducing(drafts["whole"]) is True
    assert semantics.order_is_risk_reducing(drafts["part"]) is True
    assert semantics.order_is_risk_reducing(drafts["more_than_held"]) is False, (
        "a plain sell beyond the position would leave the account trying to sell what "
        "it does not hold"
    )
    assert semantics.order_is_risk_reducing(drafts["buy"]) is False


def test_a_frozen_gate_still_lets_the_cash_close_out(held) -> None:
    """The case that matters: the breaker is frozen and the close must still go."""
    engine, drafts = held
    breaker = FallbackBreaker(
        FallbackBreakerConfig(max_notional=Decimal("10000"), max_drawdown_pct=Decimal("10"))
    )
    breaker.fail_closed("review_freeze")
    gate = RunnerSafetyOrderGate(
        boundary=RunnerReservationBoundary(
            store=PlatformReservationStore(
                _Store(), deployment_instance_id=UUID("20000000-0000-4000-8000-000000000001")
            ),
            runtime_id=UUID("20000000-0000-4000-8000-000000000001"),
            policy_id="policy-1",
            fallback_breaker=breaker,
            trading_mode="testnet",
            semantics=NautilusCachedOrderSemantics(engine.cache),
        ),
        client_order_id_len_limit=None,
        on_refusal=None,
    )
    sent: list = []

    assert gate.submit_order(lambda order, *a, **k: sent.append(order), drafts["whole"]) is True
    assert gate.submit_order(lambda order, *a, **k: sent.append(order), drafts["buy"]) is False
    assert [order.client_order_id for order in sent] == [drafts["whole"].client_order_id]


class _Store:
    """The reservation store, recording calls; a reducing order reserves nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def reserve_order_notional_sync(self, **_kwargs):
        self.calls.append("reserve")

    def release_order_reservation_sync(self, **_kwargs):
        self.calls.append("release")

    def load_order_reservation_sync(self, *_args, **_kwargs):
        return SimpleNamespace(reserved_notional="0")

    def replace_order_reservation_sync(self, **_kwargs):
        self.calls.append("replace")

    def record_order_fill_sync(self, **_kwargs):
        self.calls.append("fill")

    def record_position_reduction_sync(self, **_kwargs):
        self.calls.append("reduce")

    def record_position_reduction_fifo_sync(self, **_kwargs):
        self.calls.append("reduce_fifo")
