"""Offline reproductions of review findings; assertions confirm observed bugs."""

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from custos.core.runner_fact import RunnerFactAuthority, RunnerFactIdentity, RunnerFactOutbox
from custos.core.runner_fact_producer import (
    RunnerFactEventBridge,
    RunnerFactProductionLoop,
    VenueLedgerEvidence,
)
from custos.engines.nautilus.host import NtTradingNodeHost
from custos.engines.nautilus.okx_ledger import OkxVenueLedgerSource
from custos.engines.nautilus.sodex_ledger import SodexVenueLedgerSource
from custos.engines.nautilus.strategy_event_forwarding import StrategyEventForwarder
from custos.offline.reconciler import OfflineReconciler, _Applied, runtime_identity
from custos.offline.spec import OfflineDeploymentSpec

NOW = datetime.now(UTC).replace(microsecond=0)
START, END = NOW - timedelta(seconds=60), NOW
STAMP = int((END + timedelta(seconds=1)).timestamp() * 1000)
COMPLETE = dict.fromkeys(
    ("balances_complete", "positions_complete", "fills_complete", "fees_complete"), True
)


def authority():
    return RunnerFactAuthority(
        "review", "testnet", uuid4(), uuid4(), uuid4(), "a" * 64, 1, uuid4(), uuid4(), 1, "b" * 64
    )


def source(venue, perpetual):
    if venue == "OKX":
        result = OkxVenueLedgerSource(
            {
                "connector": "okx_perpetual" if perpetual else "okx",
                "trading_mode": "testnet",
                "pairs": ["BTC-USDT"],
                "leverage": 1,
            },
            {"api_key": "fixture", "api_secret": "fixture", "api_passphrase": "fixture"},
        )

        def get(path, params, **kwargs):
            if path.endswith("/time"):
                return [{"ts": str(STAMP)}]
            if path.endswith("/balance"):
                rows = [{"ccy": "USDT", "cashBal": "100", "availBal": "90"}]
                if not perpetual:
                    rows.append({"ccy": "BTC", "cashBal": "1", "availBal": "1"})
                return [{"details": rows}]
            if path.endswith("/instruments"):
                return [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "ctType": "linear",
                        "ctValCcy": "BTC",
                        "ctVal": "1",
                        "ctMult": "1",
                    }
                ]
            if path.endswith("/positions"):
                return [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "pos": "1",
                        "posId": "1",
                        "posSide": "net",
                        "avgPx": "90",
                    }
                ]
            return []

        result._get = get
        return result
    result = SodexVenueLedgerSource(
        {
            "connector": "sodex_perpetual" if perpetual else "sodex",
            "trading_mode": "testnet",
            "pairs": ["BTC-USD" if perpetual else "vBTC_vUSDC"],
            "leverage": 1,
            "nautilus_config": {
                "venue": {
                    "wallet_address": "0x" + "a" * 40,
                    "sodex_account_id": 42,
                    "settlement_currency": "VUSDC",
                }
            },
        },
        {},
    )
    result._state = lambda: {"B": [{"a": "vUSDC", "wb": "100", "aw": "90"}]}

    def get(path, params=None):
        if path == "balances":
            rows = [{"coin": "vUSDC", "total": "100", "locked": "0"}]
            if not perpetual:
                rows.append({"coin": "vBTC", "total": "1", "locked": "0"})
            return {"blockHeight": 42, "blockTime": STAMP, "balances": rows}
        if path == "positions":
            return {
                "blockHeight": 42,
                "positions": [
                    {
                        "symbol": "BTC-USD",
                        "active": True,
                        "size": "1",
                        "positionSide": "BOTH",
                        "id": 1,
                        "avgEntryPrice": "90",
                    }
                ],
            }
        return []

    result._get = get
    return result


class Capture:
    def __init__(self):
        self.facts = []

    async def emit(self, auth, facts):
        self.facts.extend(facts)


def audit_loss():
    class BrokenOutbox:
        calls = 0

        def emit_sync(self, *args):
            self.calls += 1
            raise OSError("fixture disk full")

    class Strategy:
        called = 0

        def on_order_event(self, event):
            self.called += 1

        def on_position_event(self, event):
            pass

    class OrderFilled:
        @staticmethod
        def to_dict(event):
            return {
                "event_id": str(uuid4()),
                "client_order_id": "owned",
                "trade_id": "1",
                "venue_order_id": "1",
                "commission": "1 USDT",
                "ts_event": 1_000_000_000,
                "instrument_id": "BTC-USDT.OKX",
                "last_qty": "1",
                "last_px": "100",
                "order_side": "BUY",
                "order_type": "MARKET",
            }

    auth = authority()
    emitter = BrokenOutbox()
    failed = []
    bridge = RunnerFactEventBridge(
        emitter=emitter,
        deployment=SimpleNamespace(
            authority=auth,
            venue="OKX",
            currency="USDT",
            deployment_instance_id=str(auth.deployment_instance_id),
        ),
    )
    bridge._owned_order_ids.add("owned")
    forwarding = StrategyEventForwarder(
        deployment_instance_id=str(auth.deployment_instance_id),
        on_sink_failure=lambda *v: failed.append(v),
    )
    bridge.bootstrap(forwarding)
    strategy = Strategy()
    forwarding.install(strategy)
    strategy.on_order_event(OrderFilled())
    assert emitter.calls == 1 and failed == [] and strategy.called == 1
    print("CR-1: outbox write failed; host failure callbacks=0; strategy handler still called")


async def missing_valuation_and_spot_positions():
    for venue in ("OKX", "SODEX"):
        evidence = source(venue, True)._collect(START, END)
        assert evidence.balances[0]["total"] == "100"
        assert (
            evidence.valuation_collection_started_at is None
            and evidence.valuation_positions is None
        )
        capture = Capture()

        class Host:
            def __init__(self, value):
                self.evidence = value

            async def runner_fact_venue_ledger(self, *args):
                return self.evidence

            async def runner_fact_valuation_snapshot(self, *args):
                raise AssertionError("unexpected valuation call")

        loop = RunnerFactProductionLoop(
            host=Host(evidence),
            emitter=capture,
            snapshot_interval_secs=1,
            period_secs=60,
            period_retry_secs=1,
        )
        auth = authority()
        deployment = SimpleNamespace(
            authority=auth,
            deployment_instance_id=str(auth.deployment_instance_id),
            currency=evidence.balances[0]["currency"],
            reconciliation_available=True,
            valuation_checkpoint_available=True,
        )
        assert await loop._close_reconciliation_period(deployment, START, END)
        assert "RunnerValuationCheckpointFact.v1" not in [f["kind"] for f in capture.facts]
        print(
            f"CR-2 {venue}: valuation capability enabled, but period closes without a valuation checkpoint; wallet=100 vs equity=110 gives a false 10-unit balance difference"
        )
        spot = source(venue, False)._collect(START, END)
        assert (
            spot.balances[1]["total"] == "1"
            and not spot.positions
            and spot.completeness["positions_complete"]
        )
        print(f"CR-3 {venue}: base holding=1, independent positions=[], positions_complete=true")


async def mixed_snapshot_retry():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = bytes(range(1, 33))
    public = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    auth = authority()
    identity = RunnerFactIdentity.from_private_bytes(
        seed, "ed25519-" + hashlib.sha256(public).hexdigest()[:32]
    )

    class Host:
        count = 0

        async def runner_fact_venue_ledger(self, *args):
            self.count += 1
            return VenueLedgerEvidence(
                venue="OKX",
                source="venue_api",
                watermark=f"capture-{self.count}",
                coverage_from=START,
                observed_through=END + timedelta(seconds=self.count),
                completeness=COMPLETE,
                balances=[
                    {
                        "asset": "USDT",
                        "currency": "USDT",
                        "total": str(100 + self.count),
                        "available": "100",
                    }
                ],
                positions=[],
                fills=[],
                fees=[],
                valuation_collection_started_at=END,
                venue_wallet_balances={"USDT": str(100 + self.count)},
                valuation_positions=[],
            )

        async def runner_fact_valuation_snapshot(self, *args):
            if self.count == 1:
                raise RuntimeError("transient mark unavailable")
            return Decimal("102"), []

    with tempfile.TemporaryDirectory(prefix="custos-review-") as temp:
        box = RunnerFactOutbox(Path(temp) / "facts.db")

        class Durable:
            async def emit(self, authority, facts):
                return await box.enqueue(authority, identity, facts)

        loop = RunnerFactProductionLoop(
            host=Host(),
            emitter=Durable(),
            snapshot_interval_secs=1,
            period_secs=60,
            period_retry_secs=1,
        )
        dep = SimpleNamespace(
            authority=auth,
            deployment_instance_id=str(auth.deployment_instance_id),
            currency="USDT",
            reconciliation_available=True,
            valuation_checkpoint_available=True,
        )
        assert not await loop._close_reconciliation_period(dep, START, END)
        assert await loop._close_reconciliation_period(dep, START, END)
        facts = [f for batch in await box.pending() for f in json.loads(batch.payload)["facts"]]
        manifest = next(f for f in facts if f["kind"] == "venue_ledger_snapshot_manifest")
        checkpoint = next(f for f in facts if f["kind"] == "RunnerValuationCheckpointFact.v1")
        assert manifest["snapshot_id"] == checkpoint["venue_snapshot_id"]
        assert manifest["watermark"] == "capture-1" and checkpoint["venue_watermark"] == "capture-2"
        print(
            "CR-4: real SQLite outbox retains capture-1 manifest, then commits capture-2 checkpoint and period close as success"
        )


async def offline_update():
    class AttachedHost(NtTradingNodeHost):
        def attached(self, instance):
            return True

    spec = OfflineDeploymentSpec.model_validate(
        {
            "spec_id": "review",
            "generation": 2,
            "trading_mode": "testnet",
            "lifecycle_state": "running",
            "strategy_path": "/tmp/no-import",
            "provenance_ref": {"credential_id": "review"},
            "connector": "binance_perpetual",
            "pairs": ["BTC-USDT"],
            "leverage": 2,
        }
    )

    async def publish(*args, **kwargs):
        pass

    reconciler = OfflineReconciler(
        tenant_id="review",
        runner_label="review",
        strategy_id="review",
        engine=AttachedHost(),
        publish=publish,
        artifact_for=lambda s: None,
        credential_for=lambda s: {},
    )
    try:
        await reconciler._engage(
            spec, _Applied(generation=1, container_id="existing"), runtime_identity(spec)
        )
    except NotImplementedError as error:
        print("CR-5: real NtTradingNodeHost refuses generation-2 update:", str(error))
    else:
        raise AssertionError("expected refused native reconfiguration")


def native_spot_equity():
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.model import AccountType, Money, OmsType, Venue

    from custos.core.fallback_breaker import FallbackBreaker, FallbackBreakerConfig
    from custos.engines.nautilus.portfolio_snapshot import NautilusPortfolioSnapshotProvider

    engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
    try:
        engine.add_venue(
            Venue("OKX"),
            OmsType.NETTING,
            AccountType.CASH,
            [Money.from_str("1000 USDT"), Money.from_str("1 BTC")],
        )
        engine.run()
        values = engine.portfolio.equity(Venue("OKX"))
        currency, observed = NautilusPortfolioSnapshotProvider._resolve_equity(values, "USDT")
        assert observed == Decimal("1000") and len(values) == 2
        breaker = FallbackBreaker(
            FallbackBreakerConfig(max_notional=Decimal("20000"), max_drawdown_pct=Decimal("10"))
        )
        breaker.evaluate(open_notional=Decimal("0"), current_equity=Decimal("10000"))
        verdict = breaker.evaluate(open_notional=Decimal("9000"), current_equity=observed)
        assert verdict.tripped
        print(
            "CR-6: native CASH balances=1000 USDT + 1 BTC; Custos equity=1000; at BTC mark 9000 unchanged NAV=10000, but drawdown breaker trips"
        )
    finally:
        engine.dispose()


async def main():
    audit_loss()
    await missing_valuation_and_spot_positions()
    await mixed_snapshot_retry()
    await offline_update()
    native_spot_equity()
    print("6 finding groups reproduced; no external network, user credentials or orders used")


if __name__ == "__main__":
    asyncio.run(main())
