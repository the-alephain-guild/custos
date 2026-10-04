"""Print one signed RunnerFact batch closing two lifecycles of one netting slot.

The deployment service's settlement tests launch this to consume exactly what the
runner signs: native NautilusTrader ``PositionClosed`` events for two round trips
that share one engine position id, turned into facts by the production RunnerFact
bridge and sealed by the production outbox with the published fixture key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from uuid import UUID

from nautilus_trader.core import UUID4
from nautilus_trader.model import (
    AccountId,
    ClientOrderId,
    Currency,
    LiquiditySide,
    Money,
    OrderFilled,
    OrderSide,
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
from nautilus_trader.testkit.providers import TestInstrumentProvider

from custos.core.runner_fact import RunnerFactAuthority, RunnerFactIdentity, RunnerFactOutbox
from custos.core.runner_fact_producer import RunnerFactDeployment, RunnerFactEventBridge

TENANT_ID = "acme"
RUNNER_ID = UUID("10000000-0000-4000-8000-000000000001")
DEPLOYMENT_SPEC_ID = UUID("30000000-0000-4000-8000-000000000003")
STRATEGY_ID = UUID("40000000-0000-4000-8000-000000000004")
CAPABILITY_VERSION_ID = UUID("50000000-0000-4000-8000-000000000005")
SPEC_DIGEST = "a" * 64
GENERATION = 7
KEY_ID = "ed25519-65b60673d6ed884bf01c2c222d82ada0"
USDT = Currency.from_str("USDT")
INSTRUMENT = TestInstrumentProvider.btcusdt_perp_binance()
# Every lifecycle below is a lifecycle of this one netting slot.
SLOT = "BTCUSDT-PERP.BINANCE-SuperTrendStrategy-000"
# (opening order, closing order, open price, close price, opened, closed), long 1.
ROUND_TRIPS = (
    ("order-1", "order-2", "100.00", "110.00", 1_786_000_000_000_000_000),
    ("order-3", "order-4", "110.00", "105.00", 1_786_000_120_000_000_000),
)


def _fill(order: str, side: OrderSide, price: str, ts: int) -> OrderFilled:
    return OrderFilled(
        TraderId("TRADER-001"),
        StrategyId("SuperTrendStrategy-000"),
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
        position_id=PositionId(SLOT),
        commission=Money.from_str("0.5 USDT"),
    )


def _closes() -> list[PositionClosed]:
    events = []
    for opening, closing, open_price, close_price, opened in ROUND_TRIPS:
        closed = opened + 60_000_000_000
        position = Position(INSTRUMENT, _fill(opening, OrderSide.BUY, open_price, opened))
        exit_fill = _fill(closing, OrderSide.SELL, close_price, closed)
        position.apply(exit_fill)
        events.append(PositionClosed.create(position, exit_fill, UUID4(), closed))
    return events


class _Collector:
    def __init__(self) -> None:
        self.facts: list[dict[str, object]] = []

    def emit_sync(self, _authority, facts):
        self.facts.extend(facts)
        return tuple(facts)


async def _batch(instance: UUID, manifest_digest: str) -> dict[str, object]:
    authority = RunnerFactAuthority(
        tenant_id=TENANT_ID,
        trading_mode="sandbox",
        runner_id=RUNNER_ID,
        deployment_instance_id=instance,
        deployment_spec_id=DEPLOYMENT_SPEC_ID,
        deployment_spec_digest=SPEC_DIGEST,
        generation=GENERATION,
        strategy_id=STRATEGY_ID,
        capability_version_id=CAPABILITY_VERSION_ID,
        capability_version=1,
        capability_manifest_digest=manifest_digest,
    )
    collector = _Collector()
    bridge = RunnerFactEventBridge(
        emitter=collector,
        deployment=RunnerFactDeployment(
            authority=authority,
            deployment_instance_id=str(instance),
            deployment_spec_id=str(DEPLOYMENT_SPEC_ID),
            deployment_spec_digest=SPEC_DIGEST,
            venue="BINANCE",
            currency="USDT",
            reconciliation_available=True,
            strategy_version="v2",
            timeframe="1-MINUTE",
        ),
    )
    for event in _closes():
        bridge._on_position_event(event)
    with tempfile.TemporaryDirectory() as directory:
        outbox = RunnerFactOutbox(Path(directory) / "outbox.sqlite3")
        identity = RunnerFactIdentity.from_private_bytes(bytes(range(1, 33)), KEY_ID)
        if await outbox.enqueue(authority, identity, collector.facts) is None:
            raise RuntimeError("the lifecycle batch was unexpectedly deduplicated")
        (pending,) = await outbox.pending()
        return json.loads(pending.payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment-instance-id", type=UUID, required=True)
    parser.add_argument("--capability-manifest-digest", required=True)
    args = parser.parse_args()
    envelope = asyncio.run(_batch(args.deployment_instance_id, args.capability_manifest_digest))
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
